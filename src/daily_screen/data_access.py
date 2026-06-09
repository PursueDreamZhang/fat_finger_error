from __future__ import annotations

import json
import os
import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.daily_screen.schemas import REQUIRED_DAILY_COLUMNS, REQUIRED_META_COLUMNS


CACHE_FILE_PATTERN = re.compile(r"^future_(?P<contract>.+)_(?P<start>\d{8})_(?P<end>\d{8})\.csv$")
EMPTY_FILE_PATTERN = re.compile(r"^future_(?P<contract>.+)_(?P<start>\d{8})_(?P<end>\d{8})\.empty$")
META_FILE_PATTERN = "contract_meta_{symbol}.csv"
TRADING_LOOKBACK_BUFFER_DAYS = 45
TUSHARE_RETRY_TIMES = 2
TUSHARE_HOME_DIR = Path(".runtime/tushare_home")


@dataclass
class LocalConfig:
    tushare_token: str | None = None


@dataclass
class CacheFileInfo:
    contract: str
    start_date: str
    end_date: str
    path: Path


def load_local_config(config_path: str | Path = "config/local_config.json") -> LocalConfig:
    path = Path(config_path)
    if not path.exists():
        return LocalConfig()

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    return LocalConfig(tushare_token=payload.get("tushare_token"))


def load_commodity_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    cache_dir: str | Path = "data/csv_data/data",
    config_path: str | Path = "config/local_config.json",
) -> dict[str, pd.DataFrame]:
    normalized_symbols = [symbol.strip().upper() for symbol in symbols]
    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    config = load_local_config(config_path)
    expanded_start_date = _expand_start_date(start_date)
    all_daily_frames: list[pd.DataFrame] = []
    all_meta_frames: list[pd.DataFrame] = []

    for symbol in normalized_symbols:
        symbol_meta = _load_symbol_meta(
            symbol=symbol,
            start_date=expanded_start_date,
            end_date=end_date,
            cache_dir=cache_path,
            config=config,
        )
        contracts = _discover_contracts_for_symbol(symbol, symbol_meta, cache_path)
        if not contracts:
            contracts = _discover_contracts_from_fallback_source(symbol, expanded_start_date, end_date)

        if symbol_meta.empty and not contracts:
            continue

        if symbol_meta.empty and contracts:
            symbol_meta = pd.DataFrame([_build_contract_meta_from_code(contract) for contract in contracts])
        symbol_meta = symbol_meta.loc[symbol_meta["contract"].isin(contracts)].copy()

        symbol_daily_frames: list[pd.DataFrame] = []
        loaded_contracts: set[str] = set()
        for contract in contracts:
            meta_row = _lookup_meta_row(symbol_meta, contract)
            contract_df = _load_contract_daily_with_cache(
                contract_code=contract,
                start_date=expanded_start_date,
                end_date=end_date,
                cache_dir=cache_path,
                config=config,
                meta_row=meta_row,
            )
            if contract_df.empty:
                continue
            symbol_daily_frames.append(contract_df)
            loaded_contracts.add(contract)
            all_daily_frames.append(contract_df)

        if not symbol_meta.empty:
            symbol_meta = _fill_meta_dates_from_daily(symbol_meta, symbol_daily_frames)
            symbol_meta = _drop_meta_without_dates_or_daily_data(symbol_meta, loaded_contracts)
            all_meta_frames.append(symbol_meta)

    if all_daily_frames:
        daily_bar = pd.concat(all_daily_frames, ignore_index=True)
        daily_bar = daily_bar.sort_values(["commodity", "contract", "trade_date"]).reset_index(drop=True)
        daily_bar = daily_bar.loc[
            (daily_bar["trade_date"] >= pd.to_datetime(expanded_start_date, format="%Y%m%d"))
            & (daily_bar["trade_date"] <= pd.to_datetime(end_date, format="%Y%m%d"))
        ].reset_index(drop=True)
    else:
        daily_bar = pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    if all_meta_frames:
        contract_meta = pd.concat(all_meta_frames, ignore_index=True)
        contract_meta = contract_meta.drop_duplicates(subset=["commodity", "contract"]).reset_index(drop=True)
    else:
        contract_meta = pd.DataFrame(columns=REQUIRED_META_COLUMNS)

    return {
        "daily_bar": daily_bar,
        "contract_meta": contract_meta,
    }


def _expand_start_date(start_date: str) -> str:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    expanded = start_ts - timedelta(days=TRADING_LOOKBACK_BUFFER_DAYS)
    return expanded.strftime("%Y%m%d")


def _load_symbol_meta(
    *,
    symbol: str,
    start_date: str,
    end_date: str,
    cache_dir: Path,
    config: LocalConfig,
) -> pd.DataFrame:
    cached_meta = _load_cached_meta(symbol, cache_dir)
    if not cached_meta.empty:
        filtered_cached_meta = _filter_meta_by_date(cached_meta, start_date, end_date)
        if not filtered_cached_meta.empty:
            return filtered_cached_meta

    remote_meta = _fetch_symbol_meta_from_tushare(symbol, config)
    if remote_meta.empty:
        remote_meta = _fetch_symbol_meta_from_akshare(symbol, cache_dir)

    if remote_meta.empty:
        return filtered_cached_meta if "filtered_cached_meta" in locals() else pd.DataFrame(columns=REQUIRED_META_COLUMNS)

    _write_meta_cache(symbol, remote_meta, cache_dir)
    return _filter_meta_by_date(remote_meta, start_date, end_date)


def _load_cached_meta(symbol: str, cache_dir: Path) -> pd.DataFrame:
    meta_path = cache_dir / META_FILE_PATTERN.format(symbol=symbol)
    if not meta_path.exists():
        return pd.DataFrame(columns=REQUIRED_META_COLUMNS)

    try:
        meta_df = pd.read_csv(meta_path, dtype=str)
    except Exception:
        return pd.DataFrame(columns=REQUIRED_META_COLUMNS)

    return _standardize_meta(meta_df)


def _write_meta_cache(symbol: str, meta_df: pd.DataFrame, cache_dir: Path) -> None:
    meta_path = cache_dir / META_FILE_PATTERN.format(symbol=symbol)
    meta_df.to_csv(meta_path, index=False, encoding="utf-8-sig")


def _filter_meta_by_date(meta_df: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if meta_df.empty:
        return meta_df

    out = meta_df.copy()
    start_ts = pd.to_datetime(start_date, format="%Y%m%d")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d")
    out["listed_date"] = pd.to_datetime(out["listed_date"], errors="coerce")
    out["last_trade_date"] = pd.to_datetime(out["last_trade_date"], errors="coerce")

    overlap_mask = (
        out["listed_date"].fillna(start_ts) <= end_ts
    ) & (
        out["last_trade_date"].fillna(end_ts) >= start_ts
    )
    filtered = out.loc[overlap_mask].copy()
    return filtered.reset_index(drop=True)


def _discover_contracts_for_symbol(symbol: str, meta_df: pd.DataFrame, cache_dir: Path) -> list[str]:
    contract_codes: set[str] = set()

    if not meta_df.empty:
        contract_codes.update(meta_df["contract"].dropna().astype(str).str.upper().tolist())

    for file_path in cache_dir.glob(f"future_{symbol}*.csv"):
        parsed = _parse_cache_file_name(file_path.name)
        if parsed:
            contract_codes.add(parsed["contract"].upper())

    for file_path in cache_dir.glob(f"future_{symbol}*.empty"):
        parsed = _parse_empty_file_name(file_path.name)
        if parsed:
            contract_codes.add(parsed["contract"].upper())

    return sorted(contract_codes)


def _discover_contracts_from_fallback_source(symbol: str, start_date: str, end_date: str) -> list[str]:
    contract_codes: list[str] = []
    for contract_code in _build_fallback_contract_candidates(symbol, start_date, end_date):
        daily_df = _fetch_contract_daily_from_akshare(contract_code, start_date, end_date)
        if daily_df.empty:
            continue
        contract_codes.append(contract_code)
    return contract_codes


def _build_fallback_contract_candidates(symbol: str, start_date: str, end_date: str) -> list[str]:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    end_ts = datetime.strptime(end_date, "%Y%m%d")
    cursor = datetime(start_ts.year - 1, 1, 1)
    last_month = datetime(end_ts.year, end_ts.month, 1)

    candidates: list[str] = []
    while cursor <= last_month:
        candidates.append(f"{symbol.upper()}{cursor.strftime('%y%m')}")
        if cursor.month == 12:
            cursor = datetime(cursor.year + 1, 1, 1)
        else:
            cursor = datetime(cursor.year, cursor.month + 1, 1)
    return candidates


def _lookup_meta_row(meta_df: pd.DataFrame, contract_code: str) -> dict[str, str | None] | None:
    if meta_df.empty:
        return None
    matched = meta_df.loc[meta_df["contract"] == contract_code]
    if matched.empty:
        return None
    return matched.iloc[0].to_dict()


def _load_contract_daily_with_cache(
    *,
    contract_code: str,
    start_date: str,
    end_date: str,
    cache_dir: Path,
    config: LocalConfig,
    meta_row: dict[str, str | None] | None,
) -> pd.DataFrame:
    cache_files = _list_contract_cache_files(contract_code, cache_dir)
    cached_frames: list[pd.DataFrame] = []

    for cache_file in cache_files:
        loaded = _load_cached_contract_file(cache_file)
        if not loaded.empty:
            cached_frames.append(loaded)

    cached_df = _merge_daily_frames(cached_frames, contract_code)
    covered_ranges = [(file_info.start_date, file_info.end_date) for file_info in cache_files]
    missing_ranges = _calculate_missing_ranges(start_date, end_date, covered_ranges)
    empty_ranges = _list_contract_empty_ranges(contract_code, cache_dir)

    fetched_frames: list[pd.DataFrame] = []
    fetched_ranges: list[tuple[str, str]] = []
    for missing_start, missing_end in missing_ranges:
        if _is_range_covered_by_empty_marker(missing_start, missing_end, empty_ranges):
            continue

        fetched = _fetch_contract_daily_remote(
            contract_code=contract_code,
            start_date=missing_start,
            end_date=missing_end,
            config=config,
            meta_row=meta_row,
        )
        if fetched.empty:
            _write_empty_marker(contract_code, missing_start, missing_end, cache_dir)
            continue

        fetched_frames.append(fetched)
        fetched_ranges.append((missing_start, missing_end))

    merged_df = _merge_daily_frames([cached_df, *fetched_frames], contract_code)
    if merged_df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    if cache_files or fetched_ranges:
        _rewrite_contract_cache(
            contract_code=contract_code,
            merged_df=merged_df,
            cache_files=cache_files,
            fetched_ranges=fetched_ranges,
            cache_dir=cache_dir,
        )

    start_ts = pd.to_datetime(start_date, format="%Y%m%d")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d")
    filtered = merged_df.loc[(merged_df["trade_date"] >= start_ts) & (merged_df["trade_date"] <= end_ts)].copy()
    return filtered.reset_index(drop=True)


def _list_contract_cache_files(contract_code: str, cache_dir: Path) -> list[CacheFileInfo]:
    matches: list[CacheFileInfo] = []
    for file_path in cache_dir.glob(f"future_{contract_code}_*.csv"):
        parsed = _parse_cache_file_name(file_path.name)
        if not parsed:
            continue
        start_date, end_date = _get_cache_file_date_bounds(file_path, parsed["contract"].upper())
        matches.append(
            CacheFileInfo(
                contract=parsed["contract"].upper(),
                start_date=start_date,
                end_date=end_date,
                path=file_path,
            )
        )
    matches.sort(key=lambda item: (item.start_date, item.end_date))
    return matches


def _list_contract_empty_ranges(contract_code: str, cache_dir: Path) -> list[tuple[str, str]]:
    ranges: list[tuple[str, str]] = []
    for file_path in cache_dir.glob(f"future_{contract_code}_*.empty"):
        parsed = _parse_empty_file_name(file_path.name)
        if not parsed:
            continue
        ranges.append((parsed["start"], parsed["end"]))
    return sorted(ranges)


def _parse_cache_file_name(file_name: str) -> dict[str, str] | None:
    matched = CACHE_FILE_PATTERN.match(file_name)
    if not matched:
        return None
    return matched.groupdict()


def _parse_empty_file_name(file_name: str) -> dict[str, str] | None:
    matched = EMPTY_FILE_PATTERN.match(file_name)
    if not matched:
        return None
    return matched.groupdict()


def _load_cached_contract_file(file_info: CacheFileInfo) -> pd.DataFrame:
    try:
        raw_df = pd.read_csv(file_info.path)
    except Exception:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    return _standardize_daily(raw_df, file_info.contract)


def _get_cache_file_date_bounds(file_path: Path, contract_code: str) -> tuple[str, str]:
    parsed = _parse_cache_file_name(file_path.name)
    fallback_start = parsed["start"] if parsed else "00010101"
    fallback_end = parsed["end"] if parsed else "99991231"

    try:
        raw_df = pd.read_csv(file_path)
    except Exception:
        return fallback_start, fallback_end

    standardized = _standardize_daily(raw_df, contract_code)
    if standardized.empty:
        return fallback_start, fallback_end

    start_date = standardized["trade_date"].min().strftime("%Y%m%d")
    end_date = standardized["trade_date"].max().strftime("%Y%m%d")
    return start_date, end_date


def _calculate_missing_ranges(
    required_start: str,
    required_end: str,
    covered_ranges: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if not covered_ranges:
        return [(required_start, required_end)]

    req_start = datetime.strptime(required_start, "%Y%m%d").date()
    req_end = datetime.strptime(required_end, "%Y%m%d").date()
    normalized = sorted(
        [
            (
                max(datetime.strptime(start, "%Y%m%d").date(), req_start),
                min(datetime.strptime(end, "%Y%m%d").date(), req_end),
            )
            for start, end in covered_ranges
            if datetime.strptime(end, "%Y%m%d").date() >= req_start
            and datetime.strptime(start, "%Y%m%d").date() <= req_end
        ],
        key=lambda item: item[0],
    )

    if not normalized:
        return [(required_start, required_end)]

    merged: list[tuple[datetime.date, datetime.date]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + timedelta(days=1):
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    missing: list[tuple[str, str]] = []
    cursor = req_start
    for start, end in merged:
        if cursor < start:
            missing.append((cursor.strftime("%Y%m%d"), (start - timedelta(days=1)).strftime("%Y%m%d")))
        cursor = max(cursor, end + timedelta(days=1))

    if cursor <= req_end:
        missing.append((cursor.strftime("%Y%m%d"), req_end.strftime("%Y%m%d")))
    return missing


def _is_range_covered_by_empty_marker(
    missing_start: str,
    missing_end: str,
    empty_ranges: list[tuple[str, str]],
) -> bool:
    for empty_start, empty_end in empty_ranges:
        if empty_start <= missing_start and empty_end >= missing_end:
            return True
    return False


def _write_empty_marker(contract_code: str, start_date: str, end_date: str, cache_dir: Path) -> None:
    marker_path = cache_dir / f"future_{contract_code}_{start_date}_{end_date}.empty"
    marker_path.write_text("", encoding="utf-8")


def _fetch_contract_daily_remote(
    *,
    contract_code: str,
    start_date: str,
    end_date: str,
    config: LocalConfig,
    meta_row: dict[str, str | None] | None,
) -> pd.DataFrame:
    ts_code = meta_row.get("ts_code") if meta_row else None

    tushare_errors: list[str] = []
    for _ in range(TUSHARE_RETRY_TIMES):
        fetched = _fetch_contract_daily_from_tushare(ts_code, contract_code, start_date, end_date, config)
        if not fetched.empty:
            return fetched
        tushare_errors.append("tushare_empty")
        time.sleep(0.3)

    return _fetch_contract_daily_from_akshare(contract_code, start_date, end_date)


def _fetch_contract_daily_from_tushare(
    ts_code: str | None,
    contract_code: str,
    start_date: str,
    end_date: str,
    config: LocalConfig,
) -> pd.DataFrame:
    if not config.tushare_token or not ts_code:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    try:
        import tushare as ts
    except ImportError:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    try:
        _prepare_tushare_home()
        ts.set_token(config.tushare_token)
        pro = ts.pro_api()
        raw_df = pro.fut_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    return _standardize_daily(raw_df, contract_code)


def _fetch_contract_daily_from_akshare(contract_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    try:
        import akshare as ak
    except ImportError:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    candidates: list[pd.DataFrame] = []
    for symbol in [contract_code, contract_code.lower()]:
        try:
            raw_df = ak.futures_zh_daily_sina(symbol=symbol)
        except Exception:
            raw_df = None
        if raw_df is None or raw_df.empty:
            continue
        standardized = _standardize_daily(raw_df, contract_code)
        if standardized.empty:
            continue
        candidates.append(standardized)
        break

    if not candidates:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    df = candidates[0]
    start_ts = pd.to_datetime(start_date, format="%Y%m%d")
    end_ts = pd.to_datetime(end_date, format="%Y%m%d")
    filtered = df.loc[(df["trade_date"] >= start_ts) & (df["trade_date"] <= end_ts)].copy()
    return filtered.reset_index(drop=True)


def _fetch_symbol_meta_from_tushare(symbol: str, config: LocalConfig) -> pd.DataFrame:
    if not config.tushare_token:
        return pd.DataFrame(columns=[*REQUIRED_META_COLUMNS, "ts_code"])

    try:
        import tushare as ts
    except ImportError:
        return pd.DataFrame(columns=[*REQUIRED_META_COLUMNS, "ts_code"])

    try:
        _prepare_tushare_home()
        ts.set_token(config.tushare_token)
        pro = ts.pro_api()
        raw_df = pro.fut_basic(
            fut_code=symbol,
            fields="ts_code,symbol,fut_code,name,exchange,list_date,delist_date,d_month,last_ddate",
        )
    except Exception as exc:
        warnings.warn(f"Tushare fut_basic failed for {symbol}: {exc}", RuntimeWarning)
        return pd.DataFrame(columns=[*REQUIRED_META_COLUMNS, "ts_code"])

    return _standardize_meta(raw_df)


def _fetch_symbol_meta_from_akshare(symbol: str, cache_dir: Path) -> pd.DataFrame:
    contracts = sorted(
        {
            _parse_cache_file_name(path.name)["contract"].upper()
            for path in cache_dir.glob(f"future_{symbol}*.csv")
            if _parse_cache_file_name(path.name)
        }
    )
    if not contracts:
        return pd.DataFrame(columns=[*REQUIRED_META_COLUMNS, "ts_code"])
    rows = [_build_contract_meta_from_code(contract) for contract in contracts]
    return pd.DataFrame(rows)


def _standardize_meta(raw_df: pd.DataFrame) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=[*REQUIRED_META_COLUMNS, "ts_code"])

    df = raw_df.copy()
    rename_map = {
        "symbol": "contract",
        "fut_code": "commodity",
        "list_date": "listed_date",
        "delist_date": "last_trade_date",
        "d_month": "delivery_month",
    }
    df = df.rename(columns={key: value for key, value in rename_map.items() if key in df.columns})
    df = df.loc[:, ~df.columns.duplicated(keep="last")].copy()

    if "contract" not in df.columns and "ts_code" in df.columns:
        df["contract"] = df["ts_code"].astype(str).str.split(".").str[0]
    if "commodity" not in df.columns:
        df["commodity"] = df["contract"].astype(str).map(_extract_commodity)
    if "delivery_month" not in df.columns:
        df["delivery_month"] = df["contract"].astype(str).str.extract(r"(\d{4})$")
    if "listed_date" not in df.columns:
        df["listed_date"] = pd.NA
    if "last_trade_date" not in df.columns:
        df["last_trade_date"] = pd.NA
    if "ts_code" not in df.columns:
        df["ts_code"] = pd.NA

    keep_columns = [*REQUIRED_META_COLUMNS, "ts_code"]
    out = df[keep_columns].copy()
    out["commodity"] = out["commodity"].astype(str).str.upper()
    out["contract"] = out["contract"].astype(str).str.upper()
    return out.drop_duplicates(subset=["commodity", "contract"]).reset_index(drop=True)


def _build_contract_meta_from_code(contract_code: str) -> dict[str, str | None]:
    commodity = _extract_commodity(contract_code)
    delivery_match = re.search(r"(\d{4})$", contract_code)
    delivery_month = delivery_match.group(1) if delivery_match else None
    return {
        "commodity": commodity,
        "contract": contract_code,
        "listed_date": None,
        "last_trade_date": None,
        "delivery_month": delivery_month,
        "ts_code": None,
    }


def _fill_meta_dates_from_daily(meta_df: pd.DataFrame, daily_frames: list[pd.DataFrame]) -> pd.DataFrame:
    if meta_df.empty or not daily_frames:
        return meta_df

    daily_df = pd.concat(daily_frames, ignore_index=True)
    if daily_df.empty:
        return meta_df

    bounds = (
        daily_df.groupby("contract", as_index=False)["trade_date"]
        .agg(listed_date="min", last_trade_date="max")
    )
    out = meta_df.merge(bounds, on="contract", how="left", suffixes=("", "_from_daily"))
    for column in ["listed_date", "last_trade_date"]:
        fallback_column = f"{column}_from_daily"
        out[column] = out[column].where(out[column].notna(), out[fallback_column])
        out[column] = out[column].map(_format_optional_date)
        out = out.drop(columns=[fallback_column])
    return out


def _drop_meta_without_dates_or_daily_data(meta_df: pd.DataFrame, loaded_contracts: set[str]) -> pd.DataFrame:
    if meta_df.empty:
        return meta_df

    out = meta_df.copy()
    has_dates = out["listed_date"].notna() & out["last_trade_date"].notna()
    has_daily_data = out["contract"].astype(str).isin(loaded_contracts)
    return out.loc[has_dates | has_daily_data].reset_index(drop=True)


def _format_optional_date(value: object) -> str | object:
    if pd.isna(value):
        return pd.NA
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return value


def _standardize_daily(raw_df: pd.DataFrame, contract_code: str) -> pd.DataFrame:
    if raw_df is None or raw_df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    df = raw_df.copy()
    rename_map = {
        "date": "trade_date",
        "日期": "trade_date",
        "trade_date": "trade_date",
        "open": "open",
        "开盘": "open",
        "开盘价": "open",
        "high": "high",
        "最高": "high",
        "最高价": "high",
        "low": "low",
        "最低": "low",
        "最低价": "low",
        "close": "close",
        "收盘": "close",
        "收盘价": "close",
        "volume": "volume",
        "成交量": "volume",
        "vol": "volume",
        "pre_close": "pre_close",
        "昨收盘": "pre_close",
        "昨收": "pre_close",
        "settle": "pre_close",
    }
    df = df.rename(columns={key: value for key, value in rename_map.items() if key in df.columns})

    if "trade_date" not in df.columns:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    for column in ["open", "high", "low", "close", "volume", "pre_close"]:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        else:
            df[column] = pd.NA

    df["commodity"] = _extract_commodity(contract_code)
    df["contract"] = contract_code.upper()
    df = df.sort_values("trade_date").reset_index(drop=True)
    df["pre_close"] = pd.to_numeric(df["pre_close"], errors="coerce")
    df["pre_close"] = df["pre_close"].where(df["pre_close"].notna(), df["close"].shift(1))

    out = df[REQUIRED_DAILY_COLUMNS].copy()
    out = out.dropna(subset=["trade_date", "open", "high", "low", "close"])
    out = out.drop_duplicates(subset=["trade_date"], keep="last").reset_index(drop=True)
    return out


def _merge_daily_frames(frames: list[pd.DataFrame], contract_code: str) -> pd.DataFrame:
    valid_frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not valid_frames:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)

    merged = pd.concat(valid_frames, ignore_index=True)
    merged = _standardize_daily(merged, contract_code)
    return merged.sort_values("trade_date").reset_index(drop=True)


def _rewrite_contract_cache(
    *,
    contract_code: str,
    merged_df: pd.DataFrame,
    cache_files: list[CacheFileInfo],
    fetched_ranges: list[tuple[str, str]],
    cache_dir: Path,
) -> None:
    if merged_df.empty:
        return

    range_starts = [item.start_date for item in cache_files] + [start for start, _ in fetched_ranges]
    range_ends = [item.end_date for item in cache_files] + [end for _, end in fetched_ranges]
    if not range_starts or not range_ends:
        return

    new_start = min(range_starts)
    new_end = max(range_ends)
    new_path = cache_dir / f"future_{contract_code}_{new_start}_{new_end}.csv"
    merged_df.to_csv(new_path, index=False, encoding="utf-8-sig")

    try:
        validation_df = pd.read_csv(new_path)
    except Exception:
        return

    if validation_df.empty or "trade_date" not in validation_df.columns:
        return

    old_paths = [item.path for item in cache_files if item.path != new_path]
    for old_path in old_paths:
        if old_path.exists():
            old_path.unlink()


def _extract_commodity(contract_code: str) -> str:
    matched = re.match(r"^[A-Z]+", str(contract_code).upper())
    if not matched:
        raise ValueError(f"无法从合约代码提取品种: {contract_code}")
    return matched.group(0)


def _prepare_tushare_home() -> None:
    TUSHARE_HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(TUSHARE_HOME_DIR.resolve())
