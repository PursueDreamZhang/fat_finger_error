"""从单日 tick 数据推导待人工审核的品种参数。"""
from __future__ import annotations

import argparse
import math
import re
from functools import reduce
from pathlib import Path

import numpy as np
import pandas as pd

from src.tick_detector.tick_io import (
    iter_day_contract_files,
    load_contract_snapshots,
)


EXPECTED_COMMODITIES = frozenset(
    "A AD AG AL AO AP AU B BB BC BR BU BZ C CF CJ CS CU CY EB EC EG FB FG FU HC I IC IF IH IM J JD JM JR L LC LG LH LU M MA NI NR OI OP P PB PD PF PG PK PL PM PP PR PS PT PX RB RI RM RR RS RU SA SC SF SH SI SM SN SP SR SS T TA TF TL TS UR V WH WR Y ZC ZN".split()
)
SCALE = 10**8


def derive_profiles(tick_day_path: str) -> dict[str, dict[str, object]]:
    """返回可从该交易日自动推导的待审核 profile。"""
    grouped: dict[str, list] = {}
    for contract_file in iter_day_contract_files(tick_day_path):
        commodity = _commodity_from_file_name(contract_file.file_name)
        if commodity is not None:
            grouped.setdefault(commodity, []).append(contract_file)

    source_date = _source_date(tick_day_path)
    profiles: dict[str, dict[str, object]] = {}
    for commodity, files in sorted(grouped.items()):
        print(f"处理 {commodity}（{len(files)} 个合约文件）", flush=True)
        raw = _most_active_contract(files)
        if raw is None:
            continue
        df = _add_raw_deltas(raw)
        tick_size = _derive_tick_size(df)
        multiplier = _derive_multiplier(df)
        if tick_size is None or multiplier is None:
            continue
        profiles[commodity] = {
            "tick_size": tick_size,
            "contract_multiplier": multiplier,
            "parameter_profile": "AUTO_INFERRED_V1",
            "validation_status": "pending_manual_review",
            "parameter_source": f"AUTO_INFERRED_{source_date}",
            "review_status": "pending",
            "reviewed_by": None,
            "reviewed_at": None,
            "review_note": None,
        }
        print(f"  -> tick_size={tick_size}, multiplier={multiplier}", flush=True)
    extra = sorted(set(profiles) - EXPECTED_COMMODITIES)
    if extra:
        raise RuntimeError(f"出现预期集合之外的品种：{extra}")
    return profiles


def _most_active_contract(files: list) -> pd.DataFrame | None:
    best_volume = -1.0
    best_file = None
    for contract_file in files:
        with contract_file.open_handle() as handle:
            volume = float(pd.read_csv(handle, usecols=["Volume"])["Volume"].max())
        if volume > best_volume:
            best_volume = volume
            best_file = contract_file
    if best_file is None:
        return None
    raw = load_contract_snapshots(best_file)
    return None if raw.empty or raw["parse_status"].iloc[0] != "ok" else raw


def _add_raw_deltas(raw: pd.DataFrame) -> pd.DataFrame:
    """参数推导只需累计量/额差分，避免复用检测链的完整逐行归一化。"""
    df = raw.copy()
    df["delta_volume"] = df["Volume"].diff()
    df["delta_turnover"] = df["Turnover"].diff()
    return df


def _derive_multiplier(df: pd.DataFrame) -> int | None:
    delta_volume = df["delta_volume"]
    delta_turnover = df["delta_turnover"]
    last_price = df["LastPrice"]
    valid = (
        delta_volume.notna()
        & (delta_volume > 0)
        & delta_turnover.notna()
        & (delta_turnover > 0)
        & last_price.notna()
        & (last_price > 0)
    )
    if valid.sum() < 10:
        return None
    ratio = delta_turnover[valid] / delta_volume[valid] / last_price[valid]
    return max(1, round(float(np.median(ratio.to_numpy()))))


def _derive_tick_size(df: pd.DataFrame) -> float | None:
    prices: list[float] = []
    for column in ("BidPrice1", "AskPrice1", "LastPrice"):
        prices.extend(df.loc[df[column] > 0, column].to_numpy(dtype=float))
    if len(prices) < 10:
        return None
    differences = np.diff(np.array(sorted(set(prices))))
    integers = [int(round(value * SCALE)) for value in differences if round(value * SCALE) > 0]
    if not integers:
        return None
    tick_size = reduce(math.gcd, integers) / SCALE
    if tick_size < 1e-4:
        raise ValueError(f"推导出的 tick_size={tick_size} 过小，疑似浮点噪声或非网格价格")
    for precision in range(1, 6):
        rounded = round(tick_size, precision)
        if abs(tick_size - rounded) < 1e-9:
            return rounded
    return tick_size


def _commodity_from_file_name(file_name: str) -> str | None:
    stem = Path(file_name).stem.rsplit("_", 1)[0]
    if any(keyword in stem for keyword in ("主力连续", "当月连续", "下月连续", "当季连续", "下季连续", "隔季连续")):
        return None
    match = re.match(r"^([A-Za-z]+)", stem)
    return match.group(1).upper() if match else None


def _source_date(tick_day_path: str) -> str:
    match = re.search(r"(?<!\d)(20\d{6})(?!\d)", tick_day_path)
    return match.group(1) if match else "UNKNOWN_DATE"


def _missing_commodities(profiles: dict[str, dict[str, object]]) -> list[str]:
    return sorted(EXPECTED_COMMODITIES - set(profiles))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推导待人工审核的品种参数")
    parser.add_argument("--tick-day-path", required=True)
    args = parser.parse_args(argv)
    profiles = derive_profiles(args.tick_day_path)
    missing = _missing_commodities(profiles)
    print(f"# 自动推导 {len(profiles)} 个品种")
    if missing:
        print(f"# 待人工补齐 {len(missing)} 个品种：{missing}")
    print("COMMODITY_PROFILES = {")
    for code, profile in sorted(profiles.items()):
        print(f"    {code!r}: {profile!r},")
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
