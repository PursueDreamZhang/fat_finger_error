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
# 日线 [Low,High] 边界容差（tick 数）：日线极值本身是取整/近似值，真实 VWAP 可能卡在
# 边界外一两跳，放 N 跳容差避免误伤 sub-tick 边界帧（仍远小于真污染的偏离）。
DAILY_VWAP_TOLERANCE_TICKS = 3
# 同日校验：tick 涨跌停中点 ≈ 日线 pre_settle 的最大允许相对差（防日线属于别的交易日）。
DAILY_SAMEDAY_TOLERANCE = 0.005

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

# 品种元数据：tick_size / contract_multiplier / 参数档 / 验证状态。
# 20260520 自动推导并经本次全品种实验审核的 81 个品种可参与检测；
# BB/JR/PM/RI/WH/ZC 因无有效成交样本暂不配置。
COMMODITY_PROFILES: dict[str, dict[str, object]] = {
    "AU": {
        "tick_size": 0.02,
        "contract_multiplier": 1000,
        "parameter_profile": "AU_V1",
        "validation_status": "validated",
    },
}

_EXPERIMENTAL_PROFILE_ROWS = """
A 1 10
AD 5 10
AG 1 15
AL 5 5
AO 1 20
AP 1 1
B 1 10
BC 10 5
BR 5 5
BU 1 10
BZ 1 30
C 1 10
CF 5 1
CJ 5 1
CS 1 10
CU 10 5
CY 5 1
EB 1 5
EC 0.5 50
EG 1 10
FB 0.5 10
FG 1 1
FU 1 10
HC 1 10
I 0.5 100
IC 0.2 200
IF 0.2 300
IH 0.2 300
IM 0.2 200
J 0.5 100
JD 1 10
JM 0.5 60
L 1 5
LC 20 1
LG 0.5 90
LH 5 16
LU 1 10
M 1 10
MA 1 1
NI 10 1
NR 5 10
OI 1 1
OP 2 40
P 1 10
PB 5 5
PD 0.05 1000
PF 2 1
PG 1 20
PK 2 1
PL 1 1
PP 1 5
PR 2 1
PS 5 3
PT 0.05 1000
PX 2 1
RB 1 10
RM 1 1
RR 1 10
RS 1 1
RU 5 10
SA 1 1
SC 0.1 1000
SF 2 1
SH 1 1
SI 5 5
SM 2 1
SN 10 1
SP 2 10
SR 1 1
SS 5 5
T 0.005 10000
TA 2 1
TF 0.005 10000
TL 0.01 10000
TS 0.002 20000
UR 1 1
V 1 5
WR 1 10
Y 1 10
ZN 5 5
"""

for _row in _EXPERIMENTAL_PROFILE_ROWS.splitlines():
    if not _row:
        continue
    _code, _tick_size, _multiplier = _row.split()
    COMMODITY_PROFILES[_code] = {
        "tick_size": float(_tick_size),
        "contract_multiplier": int(_multiplier),
        "parameter_profile": "AUTO_INFERRED_V1",
        "validation_status": "validated",
        "parameter_source": "AUTO_INFERRED_20260520",
        "review_status": "approved",
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


def iter_day_contract_files(tick_day_path: str | Path, trade_date: str | None = None) -> Iterable[ContractFile]:
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
        if trade_date:
            names = [name for name in names if Path(name).name.lower().endswith(f"_{trade_date}.csv")]
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


def normalize_contract_code(code: str) -> str:
    """归一化合约码用于 tick↔日线对齐：大写；郑商所 3 位月份补成 4 位（2020s）。

    tick 用 `AP610`（3 位）、日线用 `AP2610`（4 位）；大小写也有差异（`au2606` vs `AU2606`）。
    非郑商所品种天然 4 位，不受影响。连续合约（无月份）原样返回。
    """
    s = str(code).upper()
    m = CONTRACT_RE.match(s)
    if not m:
        return s
    letters, digits = m.group(1), m.group(2)
    if len(digits) == 3:
        digits = "2" + digits
    return letters + digits


def load_daily_bounds(
    trade_date: str, daily_root: str = "data/1d_futures"
) -> dict[str, tuple[float, float, float]] | None:
    """读当日日线 parquet → {归一化合约码: (low, high, pre_settle)}。文件缺失返回 None。

    供 prepare_contract_snapshots 的守卫A 做日线 [Low,High] 边界。合约码经归一化对齐 tick
    （大小写、郑商所 3↔4 位）。排除连续合约（码无月份）与 low/high=0 的空柱。
    """
    path = Path(daily_root) / trade_date[:4] / f"{trade_date}.parquet"
    if not path.is_file():
        return None
    daily = pd.read_parquet(path)
    code_col = daily["code"].astype(str)
    has_digits = code_col.str.contains(r"\d", regex=True)
    daily = daily.loc[has_digits].copy()
    if daily.empty:
        return None
    daily["inst"] = code_col.loc[has_digits].str.split(".").str[0].map(normalize_contract_code)
    daily[["low", "high", "pre_settle"]] = daily[["low", "high", "pre_settle"]].astype(float)
    daily = daily.loc[(daily["low"] > 0) & (daily["high"] > 0)]
    if daily.empty:
        return None
    agg = daily.groupby("inst").agg(
        low=("low", "min"), high=("high", "max"), pre_settle=("pre_settle", "first")
    )
    return {inst: (float(r.low), float(r.high), float(r.pre_settle)) for inst, r in agg.iterrows()}


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


def prepare_contract_snapshots(raw_df: pd.DataFrame, daily_bounds: dict | None = None) -> pd.DataFrame:
    """归一化交易时钟：生成内部 market_time_key、合并同时间键、计算差分与 interval_vwap。

    market_time_key 是 tick 链路唯一内部时间契约：排序、asof、窗口、间隔、断点、
    合并、恢复、replay 切片全部只使用它；自然日 timestamp 仅生成展示文本。

    daily_bounds: {归一化合约码: (low, high, pre_settle)}，来自当日日线 parquet。
    提供且同日校验通过时，守卫A 用日线 [Low,High]±容差（更紧）；否则退回涨跌停带。
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
    time_parts = df["UpdateTime"].astype(str).str.split(":")
    total_millis = (
        ((time_parts.str[0].astype(int).to_numpy() * 60
          + time_parts.str[1].astype(int).to_numpy()) * 60
         + time_parts.str[2].astype(int).to_numpy()) * 1000
        + df["UpdateMillisec"].astype(int).to_numpy()
    )
    night_offset = 21 * 3600 * 1000
    df["market_time_key"] = np.where(
        total_millis >= night_offset,
        total_millis - night_offset,
        total_millis + 3 * 3600 * 1000,
    )
    df = df.sort_values(["market_time_key", "snapshot_seq"], kind="stable").reset_index(drop=True)

    # 展示字段：保留原始交易日与显示时间，不输出 cycle 数值或伪造自然日
    df["display_trade_date"] = df["TradingDay"].astype(str)
    df["display_time"] = (
        df["UpdateTime"].astype(str)
        + "."
        + df["UpdateMillisec"].astype(int).astype(str).str.zfill(3)
    )

    # 累计 Turnover 回退 = 成交额计数器错位（feed 数据质量问题）。
    # 在合并前按排序后的原始帧打标，_collapse 再聚合成帧级标记。
    df["_raw_turnover_drop"] = df["Turnover"].astype(float).diff() < 0

    # 先合并同一 market_time_key 多行，最后一行提供盘口/累计值，保留序号范围
    df = _collapse_same_time_key(df)

    # 开盘保护标记（合并后帧）
    time_parts = df["UpdateTime"].astype(str).str.split(":")
    total_seconds = (
        (time_parts.str[0].astype(int).to_numpy() * 60
         + time_parts.str[1].astype(int).to_numpy()) * 60
        + time_parts.str[2].astype(int).to_numpy()
    )
    is_open_protected = np.zeros(len(df), dtype=bool)
    for session_open in SESSION_OPENS:
        open_parts = session_open.split(":")
        open_seconds = (int(open_parts[0]) * 60 + int(open_parts[1])) * 60 + int(open_parts[2])
        delta_seconds = total_seconds - open_seconds
        is_open_protected |= (delta_seconds >= 0) & (delta_seconds < OPEN_GUARD_SECONDS)
    df["is_open_protected"] = is_open_protected

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

    # 守卫B：累计成交额回退的帧，其 ΔTurnover 不可信，失效以免污染区间均价。
    # 只动 delta_turnover（delta_volume 保留，末笔通道不受影响）。
    if "frame_turnover_desync" in df.columns:
        df.loc[df["frame_turnover_desync"].to_numpy(dtype=bool), "delta_turnover"] = np.nan

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

    # 守卫A：区间均价越界失效。优先日线 [Low,High]±容差（更紧，需同日校验通过）；
    # 否则退回 tick 自带涨跌停带 [跌停,涨停]（交易所强制，零误杀，全天恒定免疫夜盘归日）。
    vwap = df["interval_vwap"]
    daily_band = _resolve_daily_band(df, daily_bounds, tick_size)
    if daily_band is not None:
        band_lo, band_hi = daily_band
        beyond = vwap.notna() & ((vwap < band_lo) | (vwap > band_hi))
        beyond_flag = "vwap_beyond_daily"
    else:
        up = pd.to_numeric(df["UpperLimitPrice"], errors="coerce")
        lo = pd.to_numeric(df["LowerLimitPrice"], errors="coerce")
        beyond = vwap.notna() & (up > 0) & (lo > 0) & ((vwap < lo) | (vwap > up))
        beyond_flag = "vwap_beyond_limit"
    df.loc[beyond.to_numpy(dtype=bool), "interval_vwap"] = np.nan

    # 数据质量标记
    flags = np.array([""] * len(df), dtype=object)
    if "frame_turnover_desync" in df.columns:
        flags[df["frame_turnover_desync"].to_numpy(dtype=bool)] = "turnover_counter_desync"
    flagged = beyond.to_numpy(dtype=bool) & (flags == "")
    flags[flagged] = beyond_flag
    df["data_quality_flags"] = flags
    return df


def _resolve_daily_band(
    df: pd.DataFrame, daily_bounds: dict | None, tick_size: float
) -> tuple[float, float] | None:
    """有日线且同日校验通过 -> (low - N·tick, high + N·tick)；否则 None（调用方退回涨跌停带）。

    同日校验：tick 涨跌停中点 (涨+跌)/2 ≈ 日线 pre_settle。pre_settle 是交易日唯一锚，
    对得上即证明 tick 文件与日线行属同一交易日，绕开夜盘归日口径问题。
    """
    if not daily_bounds or "contract" not in df.columns or len(df) == 0:
        return None
    ni = normalize_contract_code(str(df["contract"].iloc[0]))
    entry = daily_bounds.get(ni)
    if entry is None:
        return None
    d_low, d_high, d_pre_settle = (float(x) for x in entry)
    if not (d_low > 0 and d_high > 0 and d_pre_settle > 0):
        return None
    up = pd.to_numeric(df["UpperLimitPrice"], errors="coerce")
    lo = pd.to_numeric(df["LowerLimitPrice"], errors="coerce")
    ok = up.notna() & lo.notna() & (up > 0) & (lo > 0)
    if not ok.any():
        return None
    mid = (float(up[ok].median()) + float(lo[ok].median())) / 2.0
    if abs(mid - d_pre_settle) / d_pre_settle > DAILY_SAMEDAY_TOLERANCE:
        return None
    tol = DAILY_VWAP_TOLERANCE_TICKS * (tick_size if tick_size and not np.isnan(tick_size) else 0.0)
    return (d_low - tol, d_high + tol)


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
    keys = df["market_time_key"].to_numpy()
    if len(keys) == 1:
        first_positions = last_positions = np.array([0])
    else:
        new_group = np.empty(len(keys), dtype=bool)
        new_group[0] = True
        new_group[1:] = keys[1:] != keys[:-1]
        first_positions = np.flatnonzero(new_group)
        last_positions = np.concatenate((first_positions[1:], [len(keys)])) - 1

    out = df.iloc[last_positions].copy().reset_index(drop=True)
    sequence = df["snapshot_seq"].to_numpy()
    out["snapshot_seq_start"] = sequence[first_positions]
    out["snapshot_seq_end"] = sequence[last_positions]

    # 把原始 Turnover 回退标记聚合成帧级：组内任一原始行回退，整帧成交额不可信。
    if "_raw_turnover_drop" in df.columns:
        drop = df["_raw_turnover_drop"].to_numpy(dtype=bool)
        cum = np.concatenate(([0], np.cumsum(drop)))  # cum[i] = sum(drop[:i])
        group_drops = cum[last_positions + 1] - cum[first_positions]
        out["frame_turnover_desync"] = group_drops > 0
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
    """session 首条、非可交易行、开盘保护、数据断点(gap>3s)、回退或零增量 -> 差分失效。"""
    n = len(df)
    if n == 0:
        return
    invalidate = np.zeros(n, dtype=bool)
    invalidate[0] = True  # session 首条
    keys = df["market_time_key"].to_numpy(dtype=np.int64)
    tradable = df["is_tradable_session"].to_numpy(dtype=bool)
    if n > 1:
        cross_session = ~(tradable[:-1] & tradable[1:])
        gap = keys[1:] - keys[:-1]
        invalidate[1:] = cross_session | (gap > MAX_DATA_GAP_SECONDS * 1000)
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
