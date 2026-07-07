from __future__ import annotations

import math

import numpy as np
import pandas as pd


def infer_tick_size(df: pd.DataFrame) -> float | None:
    prices = []
    for column in ("LastPrice", "BidPrice1", "AskPrice1"):
        if column in df.columns:
            prices.extend(df.loc[df[column] > 0, column].astype(float).tolist())
    if not prices:
        return None
    unique_prices = np.array(sorted(set(round(price, 6) for price in prices)))
    if len(unique_prices) < 2:
        return None
    diffs = np.diff(unique_prices)
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        return None
    rounded = pd.Series(np.round(diffs, 6))
    mode = rounded.mode()
    return float(mode.iloc[0]) if not mode.empty else float(rounded.iloc[0])


def select_reference_contracts(day_frames: dict[str, pd.DataFrame], target_contract: str, limit: int = 5) -> list[str]:
    target_df = day_frames[target_contract]
    target_commodity = str(target_df["commodity"].iloc[0])
    candidates: list[tuple[str, float]] = []
    for contract, frame in day_frames.items():
        if contract == target_contract:
            continue
        if frame.empty or str(frame["commodity"].iloc[0]) != target_commodity:
            continue
        volume = float(frame["Volume"].max()) if "Volume" in frame.columns else 0.0
        candidates.append((contract, volume))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return [contract for contract, _ in candidates[:limit]]


def attach_reference_metrics(
    target_df: pd.DataFrame,
    reference_dfs: list[pd.DataFrame],
    tick_size: float,
    lookback_seconds: int = 3,
    max_reference_age_seconds: int = 3,
) -> pd.DataFrame:
    out = target_df.sort_values(["timestamp", "snapshot_seq"], kind="stable").reset_index(drop=True).copy()
    out["reference_contract_count"] = 0
    out["peer_median_move_ticks"] = np.nan
    out["peer_move_limit_ticks"] = np.nan
    out["expected_price_simple"] = np.nan
    out["expected_price_full"] = np.nan
    out["down_deviation_ticks"] = np.nan
    out["down_deviation_bps"] = np.nan
    out["full_blocked_reason"] = pd.Series([None] * len(out), dtype="object")
    if "spread_ticks" not in out.columns and "spread" in out.columns:
        out["spread_ticks"] = out["spread"] / tick_size

    target_index = _build_asof_index(out)
    sorted_refs = [
        _build_asof_index(frame.sort_values(["timestamp", "snapshot_seq"], kind="stable").reset_index(drop=True))
        for frame in reference_dfs
    ]

    for index, row in out.iterrows():
        query_ts = pd.Timestamp(row["timestamp"])
        lookback_ts = query_ts - pd.Timedelta(seconds=lookback_seconds)
        baseline = _take_asof_value(target_index, lookback_ts)
        peer_moves: list[float] = []
        peer_log_returns: list[float] = []
        peer_age_flags: list[tuple[bool, bool]] = []

        for peer in sorted_refs:
            current = _take_asof_value(peer, query_ts)
            lookback = _take_asof_value(peer, lookback_ts)
            if current is None or lookback is None:
                continue
            current_mid = float(current["mid_price"])
            lookback_mid = float(lookback["mid_price"])
            if current_mid <= 0 or lookback_mid <= 0:
                continue
            peer_moves.append((current_mid - lookback_mid) / tick_size)
            peer_log_returns.append(math.log(current_mid / lookback_mid))
            peer_age_flags.append(
                (
                    current["age_seconds"] <= max_reference_age_seconds,
                    lookback["age_seconds"] <= max_reference_age_seconds,
                )
            )

        out.at[index, "reference_contract_count"] = len(peer_moves)
        if len(peer_moves) < 2 or baseline is None:
            continue

        baseline_mid = float(baseline["mid_price"])
        if baseline_mid <= 0:
            continue

        out.at[index, "peer_median_move_ticks"] = float(np.median(peer_moves))
        out.at[index, "peer_move_limit_ticks"] = float(np.max(np.abs(peer_moves)))

        median_return = float(np.median(peer_log_returns))
        expected_simple = baseline_mid * math.exp(median_return)
        out.at[index, "expected_price_simple"] = expected_simple
        out.at[index, "down_deviation_ticks"] = (expected_simple - float(row["mid_price"])) / tick_size
        out.at[index, "down_deviation_bps"] = ((expected_simple - float(row["mid_price"])) / expected_simple) * 10000

        baseline_age_ok = baseline["age_seconds"] <= max_reference_age_seconds
        if not baseline_age_ok:
            out.at[index, "full_blocked_reason"] = "age_target_baseline"
            continue
        if not any(flag[0] for flag in peer_age_flags):
            out.at[index, "full_blocked_reason"] = "age_peer_current"
            continue
        if not any(flag[1] for flag in peer_age_flags):
            out.at[index, "full_blocked_reason"] = "age_peer_lookback"
            continue

        valid_returns = [ret for ret, flags in zip(peer_log_returns, peer_age_flags, strict=False) if flags[0] and flags[1]]
        if len(valid_returns) < 2:
            out.at[index, "full_blocked_reason"] = "age_peer_lookback"
            continue

        expected_full = baseline_mid * math.exp(float(np.median(valid_returns)))
        out.at[index, "expected_price_full"] = expected_full
        out.at[index, "full_blocked_reason"] = "not_blocked"

    return out


def _build_asof_index(frame: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "timestamps": frame["timestamp"].to_numpy(dtype="datetime64[ns]"),
        "mid_prices": frame["mid_price"].to_numpy(dtype=float),
    }


def _take_asof_value(index_data: dict[str, np.ndarray], query_ts: pd.Timestamp) -> dict[str, object] | None:
    timestamps = index_data["timestamps"]
    position = np.searchsorted(timestamps, query_ts.to_datetime64(), side="right") - 1
    if position < 0:
        return None
    ts = pd.Timestamp(timestamps[position])
    return {
        "timestamp": ts,
        "mid_price": float(index_data["mid_prices"][position]),
        "age_seconds": (query_ts - ts).total_seconds(),
    }
