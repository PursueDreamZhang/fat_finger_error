from __future__ import annotations

import pandas as pd

from src.tick_detector.event_detection import (
    attach_recovery_metrics,
    detect_candidate_ticks,
    merge_candidates,
)
from src.tick_detector.report_html import render_event_replay_html


def _candidate_row(
    timestamp: str,
    *,
    contract: str = "AU2606",
    snapshot_seq: int = 0,
    last_price: float = 99.90,
    mid_price: float = 99.90,
    delta_volume: float = 2.0,
    spread_ticks: float = 1.0,
    reference_contract_count: int = 2,
    peer_move_limit_ticks: float = 2.0,
    expected_price_simple: float = 100.04,
    down_deviation_ticks: float = 7.0,
    down_deviation_bps: float = 14.0,
) -> dict[str, object]:
    return {
        "trade_date": "20260520",
        "commodity": "AU",
        "contract": contract,
        "timestamp": pd.Timestamp(timestamp),
        "snapshot_seq": snapshot_seq,
        "LastPrice": last_price,
        "mid_price": mid_price,
        "delta_volume": delta_volume,
        "spread_ticks": spread_ticks,
        "reference_contract_count": reference_contract_count,
        "peer_move_limit_ticks": peer_move_limit_ticks,
        "expected_price_simple": expected_price_simple,
        "down_deviation_ticks": down_deviation_ticks,
        "down_deviation_bps": down_deviation_bps,
        "last_vs_mid_down_ticks": 3.0,
        "snapshot_avg_trade_gap_ticks": 4.0,
    }


def test_detect_candidate_ticks_enforces_core_filters():
    df = pd.DataFrame(
        [
            _candidate_row("2026-05-20 11:08:09", down_deviation_bps=35.0, down_deviation_ticks=7.0),
            _candidate_row("2026-05-20 11:08:10", mid_price=100.0, last_price=99.90, down_deviation_bps=35.0, down_deviation_ticks=7.0),
            _candidate_row("2026-05-20 11:08:11", peer_move_limit_ticks=4.0, down_deviation_bps=35.0, down_deviation_ticks=7.0),
        ]
    )

    out = detect_candidate_ticks(df)

    assert len(out) == 1
    assert out["timestamp"].iloc[0] == pd.Timestamp("2026-05-20 11:08:09")


def test_merge_candidates_merges_same_contract_events_and_uses_deepest_tick():
    candidates = pd.DataFrame(
        [
            _candidate_row("2026-05-20 11:08:09", snapshot_seq=0, down_deviation_ticks=6.0, down_deviation_bps=35.0, last_price=99.94),
            _candidate_row("2026-05-20 11:08:12", snapshot_seq=1, down_deviation_ticks=9.0, down_deviation_bps=42.0, last_price=99.90),
            _candidate_row("2026-05-20 11:08:12", snapshot_seq=2, down_deviation_ticks=9.0, down_deviation_bps=42.0, last_price=99.91),
        ]
    )

    events = merge_candidates(candidates, merge_window_seconds=10)

    assert len(events) == 1
    event = events.iloc[0]
    assert event["event_time"] == pd.Timestamp("2026-05-20 11:08:12")
    assert event["event_start_time"] == pd.Timestamp("2026-05-20 11:08:09")
    assert event["event_end_time"] == pd.Timestamp("2026-05-20 11:08:12")
    assert event["event_depth_ticks"] == 9.0
    assert event["event_low_price"] == 99.90
    assert event["event_volume"] == 6.0
    assert event["recovery_denominator_ticks"] == 9.0


def test_attach_recovery_metrics_marks_truncated_on_session_break_and_renders_html():
    events = pd.DataFrame(
        [
            {
                "trade_date": "20260520",
                "commodity": "AU",
                "contract": "AU2606",
                "event_time": pd.Timestamp("2026-05-20 11:29:50"),
                "event_start_time": pd.Timestamp("2026-05-20 11:29:49"),
                "event_end_time": pd.Timestamp("2026-05-20 11:29:50"),
                "event_depth_ticks": 10.0,
                "event_low_price": 99.80,
                "recovery_denominator_ticks": 10.0,
            }
        ]
    )
    contract_df = pd.DataFrame(
        [
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 11:29:49"), "BidPrice1": 99.82, "LastPrice": 99.82, "delta_volume": 2.0},
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 11:29:50"), "BidPrice1": 99.84, "LastPrice": 99.84, "delta_volume": 2.0},
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 11:29:55"), "BidPrice1": 99.90, "LastPrice": 99.92, "delta_volume": 2.0},
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 13:30:00"), "BidPrice1": 100.20, "LastPrice": 100.20, "delta_volume": 2.0},
        ]
    )

    recovered = attach_recovery_metrics(events, contract_df, tick_size=0.02)

    assert recovered["recovery_label"].iloc[0] == "truncated"
    html = render_event_replay_html(
        recovered,
        {
            "AU2606|2026-05-20 11:29:50": contract_df.to_dict("records"),
        },
    )
    assert "event_time" in html
    assert "11:29:50" in html
