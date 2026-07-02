from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.daily_screen.schemas import REQUIRED_DAILY_COLUMNS, REQUIRED_META_COLUMNS

TRADING_LOOKBACK_BUFFER_DAYS = 45
CONTRACT_CODE_PATTERN = re.compile(r"^[A-Z]+\d{3,4}$")
FILE_DATE_PATTERN = re.compile(r"^(?P<date>\d{8})\.parquet$")


def load_commodity_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    data_dir: str | Path = "data/1d_futures",
) -> dict[str, pd.DataFrame]:
    normalized_symbols = _normalize_symbols(symbols)
    _validate_dates(start_date, end_date)

    data_path = Path(data_dir)
    if not data_path.exists():
        raise FileNotFoundError(f"data_dir 不存在: {data_path}")
    if not data_path.is_dir():
        raise NotADirectoryError(f"data_dir 不是目录(传入了文件): {data_path}")

    available_years = _list_available_years(data_path)
    if not available_years:
        raise FileNotFoundError(f"data_dir 下无年份目录: {data_path}")

    expanded_start_date = _expand_start_date(start_date)
    bounds = _scan_contract_date_bounds(data_path, available_years, normalized_symbols)
    daily_bar = _build_daily_bar(data_path, available_years, expanded_start_date, end_date, normalized_symbols)
    contract_meta = _build_contract_meta(daily_bar, bounds)

    return {"daily_bar": daily_bar, "contract_meta": contract_meta}


def _normalize_symbols(symbols: list[str]) -> list[str]:
    if symbols is None:
        raise ValueError("symbols 不能为空")
    normalized: list[str] = []
    for symbol in symbols:
        upper = str(symbol).strip().upper()
        if upper and upper not in normalized:
            normalized.append(upper)
    if not normalized:
        raise ValueError("symbols 不能为空")
    return normalized


def _validate_dates(start_date: str, end_date: str) -> None:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    end_ts = datetime.strptime(end_date, "%Y%m%d")
    if start_ts > end_ts:
        raise ValueError("start_date 不能晚于 end_date")


def _expand_start_date(start_date: str) -> str:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    return (start_ts - timedelta(days=TRADING_LOOKBACK_BUFFER_DAYS)).strftime("%Y%m%d")


def _list_available_years(data_path: Path) -> list[int]:
    years: list[int] = []
    for entry in sorted(data_path.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry.name.isdigit() and len(entry.name) == 4):
            raise ValueError(f"年份目录名非法(需为4位数字): {entry.name}")
        years.append(int(entry.name))
    return years


def _iter_parquet_files(data_path: Path, years: list[int]) -> list[tuple[int, Path, str]]:
    result: list[tuple[int, Path, str]] = []
    for year in years:
        year_dir = data_path / str(year)
        if not year_dir.exists():
            continue
        for path in sorted(year_dir.glob("*.parquet")):
            matched = FILE_DATE_PATTERN.match(path.name)
            if not matched:
                raise ValueError(f"文件名非 YYYYMMDD.parquet: {path} (年份目录 {year})")
            result.append((year, path, matched.group("date")))
    return result


def _read_parquet_safe(
    path: Path,
    columns: list[str],
    pass_name: str,
    file_date: str,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    try:
        return pd.read_parquet(path, columns=columns)
    except Exception as exc:
        message = f"parquet 读取失败: file_path={path}, pass={pass_name}, file_date={file_date}"
        if symbols is not None:
            message += f", symbols={symbols}"
        raise RuntimeError(message) from exc


def _split_code_body(code: object) -> str:
    return str(code).split(".")[0].upper()


def _scan_contract_date_bounds(
    data_path: Path,
    years: list[int],
    symbols: list[str],
) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    # Pass A: 全历史 [code, date],按目标品种过滤,拿每合约真实首/末日。
    # 流式聚合:逐文件读、逐合约更新 min/max,bounds dict 只持有"目标品种的合约"条目(全市场约两千个),
    # 不同时持有全历史行,避免把 2010-2026 全量行 concat 进内存。
    # ponytail: 全历史扫描换正确性;若遍历 4000 文件仍慢,可落 contract_date_bounds.json 索引,本函数改为读索引。
    files = _iter_parquet_files(data_path, years)
    if not files:
        return {}
    symbol_set = set(symbols)
    bounds: dict[str, list[pd.Timestamp]] = {}
    for _, path, file_date in files:
        df = _read_parquet_safe(path, ["code", "date"], "A", file_date)
        df = df.dropna(subset=["code", "date"])
        if df.empty:
            continue
        body = df["code"].map(_split_code_body)
        commodity = body.str.extract(r"^([A-Z]+)")[0]
        df = df.assign(contract=body, commodity=commodity)
        df = df.loc[
            df["commodity"].isin(symbol_set) & df["contract"].str.match(CONTRACT_CODE_PATTERN)
        ]
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["trade_date"])
        if df.empty:
            continue
        for contract, group in df.groupby("contract"):
            day_min = group["trade_date"].min()
            day_max = group["trade_date"].max()
            if contract in bounds:
                if day_min < bounds[contract][0]:
                    bounds[contract][0] = day_min
                if day_max > bounds[contract][1]:
                    bounds[contract][1] = day_max
            else:
                bounds[contract] = [day_min, day_max]
    return {contract: (lo, hi) for contract, (lo, hi) in bounds.items()}


def _build_daily_bar(
    data_path: Path,
    years: list[int],
    expanded_start_date: str,
    end_date: str,
    symbols: list[str],
) -> pd.DataFrame:
    start_ts = datetime.strptime(expanded_start_date, "%Y%m%d")
    end_ts = datetime.strptime(end_date, "%Y%m%d")
    window_years = [y for y in years if start_ts.year <= y <= end_ts.year]
    files = _iter_parquet_files(data_path, window_years)
    selected = [
        (path, file_date)
        for _, path, file_date in files
        if start_ts <= datetime.strptime(file_date, "%Y%m%d") <= end_ts
    ]
    if not selected:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    columns = ["code", "date", "pre_close", "pre_settle", "open", "high", "low", "close", "vol"]
    frames = [_read_parquet_safe(path, columns, "B", file_date, symbols) for path, file_date in selected]
    df = pd.concat(frames, ignore_index=True)
    daily = _standardize_daily(df, symbols)
    if daily.empty:
        return daily
    # 按解析后的 trade_date 再裁一次:防脏数据(文件内行日期与文件名不一致)越界
    daily = daily.loc[
        (daily["trade_date"] >= start_ts) & (daily["trade_date"] <= end_ts)
    ].reset_index(drop=True)
    return daily


def _standardize_daily(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    symbol_set = set(symbols)
    body = df["code"].map(_split_code_body)
    commodity = body.str.extract(r"^([A-Z]+)")[0]
    df = df.assign(contract=body, commodity=commodity)
    df = df.loc[
        df["commodity"].isin(symbol_set) & df["contract"].str.match(CONTRACT_CODE_PATTERN)
    ].copy()
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    df["trade_date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close", "pre_close", "pre_settle", "vol"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["trade_date", "open", "high", "low", "close"]).copy()
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    df = df.sort_values(["commodity", "contract", "trade_date"]).reset_index(drop=True)
    df["pre_close"] = df["pre_close"].where(df["pre_close"].notna(), df["pre_settle"])
    # ponytail: prev-close 兜底只在窗口内有前一日时生效;合约在缓冲区(expanded_start)首日无窗口内前驱 → pre_close 留 NaN。
    # 真实 parquet 的 pre_close 列基本都有值,此分支很少触发;缓冲区首行属历史样本,即便判 invalid 也不影响 [start_date,end_date] 的评分输出。
    previous_close = df.groupby(["commodity", "contract"])["close"].shift(1)
    df["pre_close"] = df["pre_close"].where(df["pre_close"].notna(), previous_close)
    df["volume"] = df["vol"]
    # ponytail: 同 (commodity,contract,trade_date) 重复行保留最后一条;规整数据里一日一合约一行不会触发,
    # 重复只可能来自脏数据/重复文件,此时"最后一条"语义=按 concat 顺序(文件名字典序),不按成交量等质量字段。
    df = df.drop_duplicates(subset=["commodity", "contract", "trade_date"], keep="last")
    return df[REQUIRED_DAILY_COLUMNS].reset_index(drop=True)


def _build_contract_meta(
    daily_bar: pd.DataFrame,
    bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
) -> pd.DataFrame:
    if daily_bar.empty:
        return pd.DataFrame(columns=REQUIRED_META_COLUMNS)
    contracts = daily_bar[["commodity", "contract"]].drop_duplicates().reset_index(drop=True)
    bounds_rows = [
        {"contract": contract, "listed_date": low, "last_trade_date": high}
        for contract, (low, high) in bounds.items()
    ]
    bounds_df = pd.DataFrame(bounds_rows)
    # left merge:bounds 由 Pass A 扫全历史得来,按构造覆盖 daily_bar 中的全部合约;若个别缺失则留 NaT,
    # 下游 sample_filter 会把 listed_date/last_trade_date 为 NaT 的样本判为 invalid_insufficient_history,优雅降级。
    out = contracts.merge(bounds_df, on="contract", how="left")
    out["delivery_month"] = out["contract"].astype(str).str.extract(r"(\d{3,4})$")
    return out[REQUIRED_META_COLUMNS].reset_index(drop=True)