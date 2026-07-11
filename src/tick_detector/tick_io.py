from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import BinaryIO, Callable, Iterable
import zipfile

import numpy as np
import pandas as pd

CONTINUOUS_KEYWORDS = ("主力连续", "当月连续", "下月连续", "当季连续", "下季连续", "隔季连续")
CONTRACT_RE = re.compile(r"^([A-Za-z]+)(\d{3,4})$")
DATE_RE = re.compile(r"_(\d{8})\.csv$", re.IGNORECASE)
TICK_COLUMNS = [
    "TradingDay",
    "InstrumentID",
    "UpdateTime",
    "UpdateMillisec",
    "LastPrice",
    "Volume",
    "BidPrice1",
    "BidVolume1",
    "AskPrice1",
    "AskVolume1",
    "AveragePrice",
    "Turnover",
    "OpenInterest",
    "UpperLimitPrice",
    "LowerLimitPrice",
]

# 数据断点：相邻时间键 gap > 3s 视为数据中断；==3s 仍连续。
MAX_DATA_GAP_SECONDS = 3
# 1 秒均价确认窗的采样完整性条件：比数据断点更严格。
MAX_CONFIRMATION_GAP_SECONDS = 1

# 日盘交易时段（开始时间, 结束时间）
DAY_SESSION_RANGES = (
    ("09:00:00", "10:15:00"),
    ("10:30:00", "11:30:00"),
    ("13:30:00", "15:00:00"),
)
NIGHT_SESSION_START = "21:00:00"
NIGHT_SESSION_END = "02:30:00"
# 每段开盘后 0-59s 保护，60s 后允许
OPEN_GUARD_SECONDS = 60
SESSION_OPENS = ("09:00:00", "10:30:00", "13:30:00", "21:00:00")

# 品种元数据：tick_size / contract_multiplier / 参数档 / 验证状态
# 第一版只有 AU validated；其他品种即使元数据齐全也只记 unvalidated_commodity。
COMMODITY_PROFILES: dict[str, dict[str, object]] = {
    "AU": {
        "tick_size": 0.02,
        "contract_multiplier": 1000,
        "parameter_profile": "AU_V1",
        "validation_status": "validated",
    },
}


@dataclass(frozen=True)
class ContractFile:
    file_name: str
    open_handle: Callable[[], BinaryIO]
    source_type: str


@dataclass(frozen=True)
class ContractInfo:
    commodity: str | None
    contract: str | None
    parse_status: str
    trade_date: str | None


def iter_day_contract_files(tick_day_path: str | Path) -> Iterable[ContractFile]:
    path = Path(tick_day_path)
    if path.is_dir():
        for file_path in sorted(path.glob("*.csv")):
            yield ContractFile(
                file_name=file_path.name,
                open_handle=lambda p=file_path: p.open("rb"),
                source_type="directory",
            )
        return
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = sorted(name for name in zf.namelist() if name.lower().endswith(".csv"))
        for name in names:
            yield ContractFile(
                file_name=Path(name).name,
                open_handle=lambda p=path, n=name: zipfile.ZipFile(p).open(n),
                source_type="zip",
            )
        return
    raise FileNotFoundError(f"tick_day_path 不存在或不是目录/zip: {tick_day_path}")


def parse_contract_file(file_name: str, instrument_id: str | None) -> ContractInfo:
    base_name = Path(file_name).name
    trade_date = _parse_trade_date(base_name)
    stem = Path(base_name).stem
    name_part = stem.rsplit("_", 1)[0]
    if any(keyword in name_part for keyword in CONTINUOUS_KEYWORDS):
        return ContractInfo(None, None, "continuous_alias", trade_date)
    candidate = (instrument_id or name_part).strip()
    match = CONTRACT_RE.fullmatch(candidate)
    if not match:
        return ContractInfo(None, None, "unknown_format", trade_date)
    commodity = match.group(1).upper()
    contract = f"{commodity}{match.group(2)}"
    return ContractInfo(commodity, contract, "ok", trade_date)


def load_contract_snapshots(contract_file: ContractFile) -> pd.DataFrame:
    with contract_file.open_handle() as handle:
        df = pd.read_csv(handle, usecols=TICK_COLUMNS)
    instrument_id = str(df["InstrumentID"].iloc[0]) if not df.empty else None
    info = parse_contract_file(contract_file.file_name, instrument_id)
    df["commodity"] = info.commodity
    df["contract"] = info.contract
    df["parse_status"] = info.parse_status
    df["trade_date"] = info.trade_date
    df["source_file"] = contract_file.file_name
    return df


def prepare_contract_snapshots(raw_df: pd.DataFrame) -> pd.DataFrame:
    """归一化交易时钟：生成内部 market_time_key、合并同时间键、计算差分与 interval_vwap。

    market_time_key 是 tick 链路唯一内部时间契约：排序、asof、窗口、间隔、断点、
    合并、恢复、replay 切片全部只使用它；自然日 timestamp 仅生成展示文本。
    """
    if raw_df.empty:
        return raw_df.copy()

    df = raw_df.copy()
    df["snapshot_seq"] = np.arange(len(df))
    df["trade_date"] = df["trade_date"].astype(str)

    commodity = str(df["commodity"].iloc[0])
    profile = COMMODITY_PROFILES.get(commodity)
    tick_size = float(profile["tick_size"]) if profile else np.nan
    multiplier = int(profile["contract_multiplier"]) if profile else None
    parameter_profile = profile["parameter_profile"] if profile else None
    validation_status = profile["validation_status"] if profile else "unvalidated_commodity"
    df["tick_size"] = tick_size
    df["contract_multiplier"] = multiplier
    df["parameter_profile"] = parameter_profile
    df["validation_status"] = validation_status

    # session_state / is_tradable_session 基于原始 UpdateTime 字符串
    df["session_state"] = df["UpdateTime"].astype(str).map(_session_state)
    df["is_tradable_session"] = df["session_state"].isin(["continuous_trading", "night_trading"])

    # 内部 market_time_key：交易日周期内单调递增，正确处理 21:xx -> 00:xx 跨午夜
    df["market_time_key"] = df.apply(_compute_market_time_key, axis=1)
    df = df.sort_values(["market_time_key", "snapshot_seq"], kind="stable").reset_index(drop=True)

    # 展示字段：保留原始交易日与显示时间，不输出 cycle 数值或伪造自然日
    df["display_trade_date"] = df["TradingDay"].astype(str)
    df["display_time"] = (
        df["UpdateTime"].astype(str)
        + "."
        + df["UpdateMillisec"].astype(int).astype(str).str.zfill(3)
    )

    # 先合并同一 market_time_key 多行，最后一行提供盘口/累计值，保留序号范围
    df = _collapse_same_time_key(df)

    # 开盘保护标记（合并后帧）
    df["is_open_protected"] = df.apply(_is_open_protected_row, axis=1)

    # 盘口派生
    df["mid_price"] = np.where(
        (df["BidPrice1"] > 0) & (df["AskPrice1"] > 0) & (df["AskPrice1"] >= df["BidPrice1"]),
        (df["BidPrice1"] + df["AskPrice1"]) / 2.0,
        np.nan,
    )
    df["spread"] = np.where(df["mid_price"].notna(), df["AskPrice1"] - df["BidPrice1"], np.nan)
    df["spread_ticks"] = df["spread"] / tick_size if not np.isnan(tick_size) else np.nan

    # 差分只发生在不同时间键之间；session 首条、回退、无成交行不可触发
    df["delta_volume"] = df["Volume"].diff()
    df["delta_turnover"] = df["Turnover"].diff()
    _invalidate_blocked_diffs(df)

    # AveragePrice 单位校验（只校验单位，不能当作计数器同步证据）
    if multiplier is not None:
        df["avg_trade_price_enabled"] = _average_price_matches_multiplier(df, multiplier)
    else:
        df["avg_trade_price_enabled"] = False

    # interval_vwap = delta_turnover / delta_volume / multiplier
    df["interval_vwap"] = pd.Series([np.nan] * len(df), dtype="float64")
    valid_vwap = (
        df["avg_trade_price_enabled"]
        & df["delta_turnover"].notna()
        & (df["delta_turnover"] > 0)
        & df["delta_volume"].notna()
        & (df["delta_volume"] > 0)
    )
    if valid_vwap.any():
        df.loc[valid_vwap, "interval_vwap"] = (
            df.loc[valid_vwap, "delta_turnover"] / df.loc[valid_vwap, "delta_volume"] / multiplier
        )

    # 数据质量标记（后续 Task 会扩充）
    df["data_quality_flags"] = pd.Series([""] * len(df), dtype="object")
    return df


# ---------------------------------------------------------------------------
# 时间轴
# ---------------------------------------------------------------------------


def _compute_market_time_key(row: pd.Series) -> int:
    """交易日周期内单调递增的毫秒键，正确排序 21:xx -> 00:xx 跨午夜。"""
    update_time = str(row["UpdateTime"])
    millisec = int(row["UpdateMillisec"])
    parts = update_time.split(":")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2])
    total_millis = ((hours * 60 + minutes) * 60 + seconds) * 1000 + millisec
    night_offset = 21 * 3600 * 1000  # 21:00:00.000 的毫秒数
    if total_millis >= night_offset:
        # 21:xx~23:59 夜盘开盘段
        return total_millis - night_offset
    # 午夜后（00:00~02:30）排在 21:xx 之后
    return total_millis + 3 * 3600 * 1000  # +03:00:00 的毫秒数


def _collapse_same_time_key(df: pd.DataFrame) -> pd.DataFrame:
    """同一 market_time_key 多行先合并：最后一行提供盘口/累计值，保留序号范围。"""
    if df.empty:
        return df
    collapsed: list[pd.DataFrame] = []
    for _, group in df.groupby("market_time_key", sort=False):
        if len(group) == 1:
            collapsed.append(group.copy())
            continue
        merged = group.iloc[[-1]].copy()
        merged["snapshot_seq_start"] = int(group["snapshot_seq"].iloc[0])
        merged["snapshot_seq_end"] = int(group["snapshot_seq"].iloc[-1])
        collapsed.append(merged)
    out = pd.concat(collapsed, ignore_index=True)
    if "snapshot_seq_start" not in out.columns:
        out["snapshot_seq_start"] = out["snapshot_seq"]
    if "snapshot_seq_end" not in out.columns:
        out["snapshot_seq_end"] = out["snapshot_seq"]
    return out


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def _session_state(update_time: str) -> str:
    for start, end in DAY_SESSION_RANGES:
        if start <= update_time < end:
            return "continuous_trading"
    if _is_night_trading(update_time):
        return "night_trading"
    if ("08:55:00" <= update_time < "09:00:00") or ("20:55:00" <= update_time < "21:00:00"):
        return "pre_open_snapshot"
    if update_time < "09:00:00":
        return "closed_break"
    if "10:15:00" <= update_time < "10:30:00":
        return "intermission"
    if "11:30:00" <= update_time < "13:30:00":
        return "lunch"
    if "15:00:00" <= update_time < NIGHT_SESSION_START:
        return "closed_break"
    return "off_session"


def _is_night_trading(update_time: str) -> bool:
    return update_time >= NIGHT_SESSION_START or update_time < NIGHT_SESSION_END


# ---------------------------------------------------------------------------
# 开盘保护
# ---------------------------------------------------------------------------


def _is_open_protected_row(row: pd.Series) -> bool:
    """每段开盘后 0-59s 保护；session 首条不差分（也受保护）。"""
    update_time = str(row["UpdateTime"])
    parts = update_time.split(":")
    total_sec = (int(parts[0]) * 60 + int(parts[1])) * 60 + int(parts[2])
    for session_open in SESSION_OPENS:
        op = session_open.split(":")
        open_sec = (int(op[0]) * 60 + int(op[1])) * 60 + int(op[2])
        delta = total_sec - open_sec
        if 0 <= delta < OPEN_GUARD_SECONDS:
            return True
    # 夜盘开盘跨午夜时，00:00~00:00:59 也应视为 21:00 段保护？
    # 不：21:00 段保护只覆盖 21:00:00~21:00:59；午夜后是夜盘延续，不重置保护。
    return False


# ---------------------------------------------------------------------------
# 差分失效规则
# ---------------------------------------------------------------------------


def _invalidate_blocked_diffs(df: pd.DataFrame) -> None:
    """session 首条、非可交易行、开盘保护、回退或零增量 -> 差分失效。"""
    n = len(df)
    if n == 0:
        return
    invalidate = np.zeros(n, dtype=bool)
    invalidate[0] = True  # session 首条
    for i in range(1, n):
        prev_tradable = bool(df["is_tradable_session"].iloc[i - 1])
        cur_tradable = bool(df["is_tradable_session"].iloc[i])
        if not (prev_tradable and cur_tradable):
            # 跨 session 或非可交易行
            invalidate[i] = True
    # 开盘保护行不差分
    invalidate = invalidate | df["is_open_protected"].to_numpy(dtype=bool)
    # 回退或零增量
    bad = (df["delta_volume"] <= 0) | (df["delta_turnover"] <= 0) | df["delta_volume"].isna()
    invalidate = invalidate | bad.to_numpy(dtype=bool)
    df.loc[invalidate, ["delta_volume", "delta_turnover"]] = np.nan


# ---------------------------------------------------------------------------
# AveragePrice 单位校验
# ---------------------------------------------------------------------------


def _average_price_matches_multiplier(df: pd.DataFrame, multiplier: int) -> pd.Series:
    """校验 AveragePrice 单位。

    设计文档 §4.4：Turnover/Volume ≈ AveragePrice（AveragePrice 本身就是元/手单位，
    不再除以 multiplier）。只有 (Turnover/Volume)/multiplier 才是价格单位。
    """
    ratio = df["Turnover"] / df["Volume"]
    enabled = (
        df["AveragePrice"].notna()
        & (df["AveragePrice"] > 0)
        & df["Volume"].notna()
        & (df["Volume"] > 0)
        & ratio.notna()
        & ((ratio - df["AveragePrice"]).abs() <= 0.5)
    )
    return enabled.fillna(False)


def _parse_trade_date(file_name: str) -> str | None:
    match = DATE_RE.search(file_name)
    return match.group(1) if match else None
