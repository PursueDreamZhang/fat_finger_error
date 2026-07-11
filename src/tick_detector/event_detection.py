from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

MIN_EVENT_DELTA_VOLUME = 10
MIN_REFERENCE_CONTRACT_COUNT = 2
MAX_SPREAD_TICKS = 20
STRONG_SIGNAL_MAX_SPREAD_TICKS = 25
MIN_LAST_VS_MID_DOWN_TICKS = 20
MIN_PEER_EXCESS_DOWN_TICKS = 20
STRONG_SIGNAL_MIN_DELTA_VOLUME = 50
STRONG_SIGNAL_MIN_LAST_VS_MID_DOWN_TICKS = 50
STRONG_SIGNAL_MIN_PEER_EXCESS_DOWN_TICKS = 10
MIN_AVG_TRADE_GAP_TICKS = 20
LIMIT_BUFFER_TICKS = 2
MERGE_WINDOW_SECONDS = 10
RECOVERY_WINDOWS = (10, 30)
OPEN_GUARD_SECONDS = 60
OPEN_GUARD_TIMES = ("09:00:00", "09:30:00", "21:00:00")


def detect_candidate_ticks(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    out = df.copy()
    base_mask = (
        (out["delta_volume"] > 0)
        & (out["delta_volume"] >= MIN_EVENT_DELTA_VOLUME)
        & (out["reference_contract_count"] >= MIN_REFERENCE_CONTRACT_COUNT)
        & (out["LastPrice"] > (out["LowerLimitPrice"] + LIMIT_BUFFER_TICKS * _infer_tick_size_from_row(out)))
        & (~out.apply(_is_open_guard_window, axis=1))
    )

    visible_regular_mask = (
        (out["last_vs_mid_down_ticks"] >= MIN_LAST_VS_MID_DOWN_TICKS)
        & (out["peer_excess_down_ticks"] >= MIN_PEER_EXCESS_DOWN_TICKS)
        & (out["spread_ticks"] <= MAX_SPREAD_TICKS)
    )
    visible_strong_signal_mask = (
        (out["delta_volume"] >= STRONG_SIGNAL_MIN_DELTA_VOLUME)
        & (out["last_vs_mid_down_ticks"] >= STRONG_SIGNAL_MIN_LAST_VS_MID_DOWN_TICKS)
        & (out["spread_ticks"] <= STRONG_SIGNAL_MAX_SPREAD_TICKS)
        & (out["peer_excess_down_ticks"] >= STRONG_SIGNAL_MIN_PEER_EXCESS_DOWN_TICKS)
    )
    hidden_mask = (
        out["snapshot_avg_trade_gap_ticks"].notna()
        & (out["snapshot_avg_trade_gap_ticks"] >= MIN_AVG_TRADE_GAP_TICKS)
    )
    out = out.loc[base_mask & (visible_regular_mask | visible_strong_signal_mask | hidden_mask)].copy()
    if out.empty:
        return out
    out["trigger_reasons"] = out.apply(_build_trigger_reasons, axis=1)
    return out


def _is_open_guard_window(row: pd.Series) -> bool:
    timestamp = row.get("timestamp")
    if pd.isna(timestamp):
        return False
    current = pd.Timestamp(timestamp)
    for open_time in OPEN_GUARD_TIMES:
        anchor = pd.Timestamp(f"{current.date()} {open_time}")
        delta_seconds = (current - anchor).total_seconds()
        if 0 <= delta_seconds < OPEN_GUARD_SECONDS:
            return True
    return False


def merge_candidates(candidates: pd.DataFrame, merge_window_seconds: int = MERGE_WINDOW_SECONDS) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    candidates = candidates.sort_values(["contract", "timestamp", "snapshot_seq"], kind="stable").reset_index(drop=True)
    events: list[dict[str, object]] = []
    for _, contract_rows in candidates.groupby("contract", sort=False):
        group_start = 0
        rows = contract_rows.reset_index(drop=True)
        for idx in range(1, len(rows) + 1):
            should_flush = idx == len(rows) or (rows.loc[idx, "timestamp"] - rows.loc[idx - 1, "timestamp"]).total_seconds() > merge_window_seconds
            if not should_flush:
                continue
            window = rows.iloc[group_start:idx].copy()
            anchor = window.sort_values(["event_depth_ticks", "timestamp", "snapshot_seq"], ascending=[False, True, True], kind="stable").iloc[0]
            trigger_reasons = ""
            if "trigger_reasons" in window.columns:
                trigger_reasons = ",".join(sorted(set(window["trigger_reasons"].dropna())))
            events.append(
                {
                    "trade_date": anchor["trade_date"],
                    "commodity": anchor["commodity"],
                    "contract": anchor["contract"],
                    "event_time": anchor["timestamp"],
                    "event_start_time": window["timestamp"].min(),
                    "event_end_time": window["timestamp"].max(),
                    "event_low_price": float(window["LastPrice"].min()),
                    "event_volume": float(window.loc[window["delta_volume"] > 0, "delta_volume"].sum()),
                    "event_depth_ticks": float(window["event_depth_ticks"].max()),
                    "recovery_denominator_ticks": max(
                        1.0,
                        (float(anchor["mid_price"]) - float(window["LastPrice"].min())) / _infer_tick_size_from_row(window),
                    ),
                    "reference_contract_count": int(anchor["reference_contract_count"]),
                    "trigger_reasons": trigger_reasons,
                }
            )
            group_start = idx
    return pd.DataFrame(events)


def attach_recovery_metrics(events_df: pd.DataFrame, contract_df: pd.DataFrame, tick_size: float) -> pd.DataFrame:
    if events_df.empty:
        return events_df.copy()
    out = events_df.copy()
    for seconds in RECOVERY_WINDOWS:
        out[f"quote_recovery_{seconds}s_ticks"] = np.nan
        out[f"trade_recovery_{seconds}s_ticks"] = np.nan
    out["recovery_label"] = pd.Series([None] * len(out), dtype="object")

    contract_rows = contract_df.sort_values(["contract", "timestamp"], kind="stable").reset_index(drop=True)
    for index, event in out.iterrows():
        rows = contract_rows.loc[contract_rows["contract"] == event["contract"]].reset_index(drop=True)
        anchor_idx = rows.index[rows["timestamp"] == event["event_time"]]
        if len(anchor_idx) == 0:
            out.at[index, "recovery_label"] = "no_recovery"
            continue
        anchor_idx = int(anchor_idx[0])
        truncated = False
        usable = [rows.iloc[anchor_idx]]
        window_end = pd.Timestamp(event["event_time"]) + pd.Timedelta(seconds=max(RECOVERY_WINDOWS))
        broke_on_gap = False
        for row_idx in range(anchor_idx + 1, len(rows)):
            prev_ts = pd.Timestamp(rows.iloc[row_idx - 1]["timestamp"])
            cur_ts = pd.Timestamp(rows.iloc[row_idx]["timestamp"])
            if (cur_ts - prev_ts).total_seconds() > 60:
                broke_on_gap = True
                break
            if cur_ts > window_end:
                break
            usable.append(rows.iloc[row_idx])
        usable_df = pd.DataFrame(usable)
        if broke_on_gap and pd.Timestamp(usable_df["timestamp"].max()) < window_end:
            truncated = True
        event_low = float(event["event_low_price"])
        for seconds in RECOVERY_WINDOWS:
            cutoff = pd.Timestamp(event["event_time"]) + pd.Timedelta(seconds=seconds)
            window = usable_df.loc[usable_df["timestamp"] <= cutoff]
            if window.empty:
                continue
            quote_ticks = (float(window["BidPrice1"].max()) - event_low) / tick_size
            trade_rows = window.loc[window["delta_volume"] > 0]
            trade_ticks = np.nan
            if not trade_rows.empty:
                trade_ticks = (float(trade_rows["LastPrice"].max()) - event_low) / tick_size
            out.at[index, f"quote_recovery_{seconds}s_ticks"] = quote_ticks
            out.at[index, f"trade_recovery_{seconds}s_ticks"] = trade_ticks

        if truncated:
            out.at[index, "recovery_label"] = "truncated"
            continue

        denominator = float(event["recovery_denominator_ticks"]) or 1.0
        quote_10 = float(out.at[index, "quote_recovery_10s_ticks"]) if pd.notna(out.at[index, "quote_recovery_10s_ticks"]) else 0.0
        trade_10 = float(out.at[index, "trade_recovery_10s_ticks"]) if pd.notna(out.at[index, "trade_recovery_10s_ticks"]) else 0.0
        quote_30 = float(out.at[index, "quote_recovery_30s_ticks"]) if pd.notna(out.at[index, "quote_recovery_30s_ticks"]) else 0.0
        trade_30 = float(out.at[index, "trade_recovery_30s_ticks"]) if pd.notna(out.at[index, "trade_recovery_30s_ticks"]) else 0.0
        if trade_10 / denominator >= 0.5:
            out.at[index, "recovery_label"] = "fast_trade_recovery"
        elif quote_10 / denominator >= 0.5:
            out.at[index, "recovery_label"] = "fast_quote_recovery"
        elif max(quote_30, trade_30) / denominator >= 0.5:
            out.at[index, "recovery_label"] = "slow_recovery"
        else:
            out.at[index, "recovery_label"] = "no_recovery"
    return out


def _build_trigger_reasons(row: pd.Series) -> str:
    reasons: list[str] = []
    if (
        pd.notna(row.get("last_vs_mid_down_ticks"))
        and pd.notna(row.get("peer_excess_down_ticks"))
        and float(row["last_vs_mid_down_ticks"]) >= MIN_LAST_VS_MID_DOWN_TICKS
        and float(row["peer_excess_down_ticks"]) >= MIN_PEER_EXCESS_DOWN_TICKS
        and pd.notna(row.get("spread_ticks"))
        and float(row["spread_ticks"]) <= MAX_SPREAD_TICKS
    ):
        reasons.append("visible_last_drop")
    if (
        pd.notna(row.get("delta_volume"))
        and pd.notna(row.get("last_vs_mid_down_ticks"))
        and pd.notna(row.get("peer_excess_down_ticks"))
        and pd.notna(row.get("spread_ticks"))
        and float(row["delta_volume"]) >= STRONG_SIGNAL_MIN_DELTA_VOLUME
        and float(row["last_vs_mid_down_ticks"]) >= STRONG_SIGNAL_MIN_LAST_VS_MID_DOWN_TICKS
        and float(row["spread_ticks"]) <= STRONG_SIGNAL_MAX_SPREAD_TICKS
        and float(row["peer_excess_down_ticks"]) >= STRONG_SIGNAL_MIN_PEER_EXCESS_DOWN_TICKS
    ):
        reasons.append("strong_visible_last_drop")
    if pd.notna(row.get("snapshot_avg_trade_gap_ticks")) and float(row["snapshot_avg_trade_gap_ticks"]) >= MIN_AVG_TRADE_GAP_TICKS:
        reasons.append("hidden_avg_trade_drop")
    return ",".join(reasons)


def _infer_tick_size_from_row(df_or_row: pd.DataFrame | pd.Series) -> float:
    spread_ticks = df_or_row["spread_ticks"]
    spread = df_or_row["spread"]
    if isinstance(df_or_row, pd.DataFrame):
        valid = df_or_row.loc[pd.notna(spread_ticks) & (spread_ticks > 0) & pd.notna(spread) & (spread > 0), ["spread", "spread_ticks"]]
        if valid.empty:
            return 1.0
        sample = valid.iloc[0]
        return float(sample["spread"]) / float(sample["spread_ticks"])
    if pd.notna(spread_ticks) and float(spread_ticks) > 0 and pd.notna(spread) and float(spread) > 0:
        return float(spread) / float(spread_ticks)
    return 1.0
