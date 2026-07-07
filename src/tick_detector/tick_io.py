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
DAY_SESSION_RANGES = (
    ("09:00:00", "10:15:00"),
    ("10:30:00", "11:30:00"),
    ("13:30:00", "15:00:00"),
)
CONTRACT_MULTIPLIERS = {"AU": 1000}


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
    if raw_df.empty:
        return raw_df.copy()

    df = raw_df.copy()
    df["snapshot_seq"] = np.arange(len(df))
    df["trade_date"] = df["trade_date"].astype(str)
    df["timestamp"] = pd.to_datetime(
        df["TradingDay"].astype(str)
        + " "
        + df["UpdateTime"].astype(str)
        + "."
        + df["UpdateMillisec"].astype(int).astype(str).str.zfill(3),
        format="%Y%m%d %H:%M:%S.%f",
        errors="coerce",
    )
    df["session_state"] = df["UpdateTime"].map(_session_state)
    df["is_tradable_session"] = df["UpdateTime"].map(_is_tradable_session)
    df["night_session_coverage"] = bool((~df["is_tradable_session"]).any())
    df = df.loc[df["is_tradable_session"]].copy()
    df = df.sort_values(["timestamp", "snapshot_seq"], kind="stable").reset_index(drop=True)
    df["night_session_coverage"] = bool(raw_df["UpdateTime"].map(_is_tradable_session).eq(False).any())

    df["mid_price"] = np.where(
        (df["BidPrice1"] > 0) & (df["AskPrice1"] > 0) & (df["AskPrice1"] >= df["BidPrice1"]),
        (df["BidPrice1"] + df["AskPrice1"]) / 2.0,
        np.nan,
    )
    df["spread"] = np.where(df["mid_price"].notna(), df["AskPrice1"] - df["BidPrice1"], np.nan)

    df["delta_volume"] = df["Volume"].diff()
    df["delta_turnover"] = df["Turnover"].diff()
    time_gap = df["timestamp"].diff().dt.total_seconds()
    reset_mask = (time_gap > 60) | (df.index == 0)
    df.loc[reset_mask, ["delta_volume", "delta_turnover"]] = np.nan
    df.loc[df["delta_volume"] <= 0, ["delta_volume", "delta_turnover"]] = np.nan

    multiplier = CONTRACT_MULTIPLIERS.get(str(df["commodity"].iloc[0]))
    if multiplier is None:
        df["avg_trade_price_enabled"] = False
        df["snapshot_avg_trade_price"] = np.nan
        return df

    df["avg_trade_price_enabled"] = _average_price_matches_multiplier(df, multiplier)
    avg_trade_price = df["delta_turnover"] / df["delta_volume"] / multiplier
    df["snapshot_avg_trade_price"] = np.where(
        df["avg_trade_price_enabled"] & df["delta_turnover"].notna() & (df["delta_turnover"] > 0),
        avg_trade_price,
        np.nan,
    )
    return df


def _parse_trade_date(file_name: str) -> str | None:
    match = DATE_RE.search(file_name)
    return match.group(1) if match else None


def _session_state(update_time: str) -> str:
    for start, end in DAY_SESSION_RANGES:
        if start <= update_time < end:
            return "continuous_trading"
    if update_time < "09:00:00":
        return "pre_open_snapshot"
    if "10:15:00" <= update_time < "10:30:00":
        return "intermission"
    if "11:30:00" <= update_time < "13:30:00":
        return "lunch"
    return "off_session"


def _is_tradable_session(update_time: str) -> bool:
    return _session_state(update_time) == "continuous_trading"


def _average_price_matches_multiplier(df: pd.DataFrame, multiplier: int) -> pd.Series:
    ratio = df["Turnover"] / df["Volume"] / multiplier
    enabled = (
        df["AveragePrice"].notna()
        & (df["AveragePrice"] > 0)
        & df["Volume"].notna()
        & (df["Volume"] > 0)
        & ratio.notna()
        & ((ratio - df["AveragePrice"]).abs() <= 0.05)
    )
    return enabled.fillna(False)
