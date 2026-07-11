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
    delta_volume: float = 12.0,
    spread: float = 0.02,
    spread_ticks: float = 1.0,
    reference_contract_count: int = 2,
    expected_price_simple: float = 100.04,
    event_depth_ticks: float = 7.0,
    event_depth_bps: float = 14.0,
    last_vs_mid_down_ticks: float = 25.0,
    peer_excess_down_ticks: float = 25.0,
    snapshot_avg_trade_gap_ticks: float | None = None,
    lower_limit_price: float = 50.0,
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
        "spread": spread,
        "spread_ticks": spread_ticks,
        "LowerLimitPrice": lower_limit_price,
        "reference_contract_count": reference_contract_count,
        "expected_price_simple": expected_price_simple,
        "event_depth_ticks": event_depth_ticks,
        "event_depth_bps": event_depth_bps,
        "last_vs_mid_down_ticks": last_vs_mid_down_ticks,
        "peer_excess_down_ticks": peer_excess_down_ticks,
        "snapshot_avg_trade_gap_ticks": snapshot_avg_trade_gap_ticks,
    }


def test_detect_candidate_ticks_enforces_core_filters():
    df = pd.DataFrame(
        [
            _candidate_row("2026-05-20 21:04:35.500", event_depth_bps=35.0, event_depth_ticks=57.0),
            _candidate_row(
                "2026-05-20 21:04:36.000",
                event_depth_bps=35.0,
                event_depth_ticks=57.0,
                last_vs_mid_down_ticks=10.0,
            ),
            _candidate_row(
                "2026-05-20 21:04:37.000",
                event_depth_bps=35.0,
                event_depth_ticks=57.0,
                peer_excess_down_ticks=10.0,
            ),
        ]
    )

    out = detect_candidate_ticks(df)

    assert len(out) == 1
    assert out["timestamp"].iloc[0] == pd.Timestamp("2026-05-20 21:04:35.500")


def test_detect_candidate_ticks_blocks_first_minute_after_open_windows():
    df = pd.DataFrame(
        [
            _candidate_row("2026-05-20 09:00:10.000", event_depth_ticks=57.0, event_depth_bps=35.0, delta_volume=12.0),
            _candidate_row("2026-05-20 09:30:10.000", event_depth_ticks=57.0, event_depth_bps=35.0, delta_volume=12.0),
            _candidate_row("2026-05-20 21:00:10.000", event_depth_ticks=57.0, event_depth_bps=35.0, delta_volume=12.0),
        ]
    )

    out = detect_candidate_ticks(df)

    assert out.empty


def test_detect_candidate_ticks_does_not_trigger_when_peer_metrics_are_not_fresh():
    df = pd.DataFrame(
        [
            _candidate_row(
                "2026-05-20 21:04:35.500",
                event_depth_ticks=57.0,
                event_depth_bps=35.0,
                delta_volume=12.0,
                reference_contract_count=0,
                peer_excess_down_ticks=float("nan"),
                snapshot_avg_trade_gap_ticks=25.0,
            ),
        ]
    )

    out = detect_candidate_ticks(df)

    assert out.empty


def test_detect_candidate_ticks_allows_strong_signal_recovery_path():
    df = pd.DataFrame(
        [
            _candidate_row(
                "2026-05-20 21:04:35.500",
                delta_volume=60.0,
                spread_ticks=25.0,
                last_vs_mid_down_ticks=55.0,
                peer_excess_down_ticks=10.0,
                event_depth_ticks=55.0,
                event_depth_bps=35.0,
            ),
        ]
    )

    out = detect_candidate_ticks(df)

    assert len(out) == 1
    assert out["timestamp"].iloc[0] == pd.Timestamp("2026-05-20 21:04:35.500")
    assert "strong_visible_last_drop" in out["trigger_reasons"].iloc[0]


def test_detect_candidate_ticks_keeps_thin_volume_false_positive_blocked():
    df = pd.DataFrame(
        [
            _candidate_row(
                "2026-05-20 09:43:42.000",
                delta_volume=12.0,
                spread_ticks=25.0,
                last_vs_mid_down_ticks=55.0,
                peer_excess_down_ticks=10.0,
                event_depth_ticks=55.0,
                event_depth_bps=35.0,
            ),
        ]
    )

    out = detect_candidate_ticks(df)

    assert out.empty


def test_merge_candidates_merges_same_contract_events_and_uses_deepest_tick():
    candidates = pd.DataFrame(
        [
            _candidate_row("2026-05-20 21:04:35.500", snapshot_seq=0, event_depth_ticks=6.0, event_depth_bps=35.0, last_price=99.94, delta_volume=6.0),
            _candidate_row("2026-05-20 21:04:38.000", snapshot_seq=1, event_depth_ticks=9.0, event_depth_bps=42.0, last_price=99.90, delta_volume=6.0),
            _candidate_row("2026-05-20 21:04:38.000", snapshot_seq=2, event_depth_ticks=9.0, event_depth_bps=42.0, last_price=99.91, delta_volume=6.0),
        ]
    )

    events = merge_candidates(candidates, merge_window_seconds=10)

    assert len(events) == 1
    event = events.iloc[0]
    assert event["event_time"] == pd.Timestamp("2026-05-20 21:04:38")
    assert event["event_start_time"] == pd.Timestamp("2026-05-20 21:04:35.500")
    assert event["event_end_time"] == pd.Timestamp("2026-05-20 21:04:38")
    assert event["event_depth_ticks"] == 9.0
    assert event["event_low_price"] == 99.90
    assert event["event_volume"] == 18.0
    assert event["recovery_denominator_ticks"] == 1.0


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
                "reference_contract_count": 2,
                "trigger_reasons": "visible_last_drop",
                "recovery_label": "truncated",
            }
        ]
    )
    contract_df = pd.DataFrame(
        [
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 11:29:49"), "snapshot_seq": 0, "BidPrice1": 99.82, "LastPrice": 99.82, "mid_price": 99.82, "spread_ticks": 1.0, "delta_volume": 2.0, "last_vs_mid_down_ticks": 0.0, "peer_excess_down_ticks": 0.0, "snapshot_avg_trade_gap_ticks": None, "event_depth_ticks": 0.0, "__is_candidate": False, "__is_event_anchor": False},
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 11:29:50"), "snapshot_seq": 1, "BidPrice1": 99.84, "LastPrice": 99.84, "mid_price": 100.40, "spread_ticks": 2.0, "delta_volume": 2.0, "last_vs_mid_down_ticks": 28.0, "peer_excess_down_ticks": 26.0, "snapshot_avg_trade_gap_ticks": None, "event_depth_ticks": 28.0, "__is_candidate": True, "__is_event_anchor": True},
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 11:29:55"), "snapshot_seq": 2, "BidPrice1": 99.90, "LastPrice": 99.92, "mid_price": 99.92, "spread_ticks": 1.0, "delta_volume": 2.0, "last_vs_mid_down_ticks": 0.0, "peer_excess_down_ticks": 0.0, "snapshot_avg_trade_gap_ticks": None, "event_depth_ticks": 0.0, "__is_candidate": False, "__is_event_anchor": False},
            {"contract": "AU2606", "timestamp": pd.Timestamp("2026-05-20 13:30:00"), "snapshot_seq": 3, "BidPrice1": 100.20, "LastPrice": 100.20, "mid_price": 100.20, "spread_ticks": 1.0, "delta_volume": 2.0, "last_vs_mid_down_ticks": 0.0, "peer_excess_down_ticks": 0.0, "snapshot_avg_trade_gap_ticks": None, "event_depth_ticks": 0.0, "__is_candidate": False, "__is_event_anchor": False},
        ]
    )

    recovered = attach_recovery_metrics(events, contract_df, tick_size=0.02)

    assert recovered["recovery_label"].iloc[0] == "truncated"
    html = render_event_replay_html(
        recovered,
        {
            "AU2606|2026-05-20 11:29:50": {
                "rows": contract_df.to_dict("records"),
                "reference_rows": [
                    {
                        "reference_contract": "AU2606（目标合约）",
                        "current_time": pd.Timestamp("2026-05-20 11:29:50"),
                        "current_mid_price": 100.40,
                        "lookback_time": pd.Timestamp("2026-05-20 11:29:47"),
                        "lookback_mid_price": 100.00,
                        "move_ticks": 20.0,
                        "current_age_seconds": 0.0,
                        "lookback_age_seconds": 0.0,
                        "used_in_peer_median": True,
                        "missing_reason": "用于和参考合约横向对比，不参与参考中位数",
                    },
                    {
                        "reference_contract": "AU2608",
                        "current_time": pd.Timestamp("2026-05-20 11:29:50"),
                        "current_mid_price": 100.12,
                        "lookback_time": pd.Timestamp("2026-05-20 11:29:47"),
                        "lookback_mid_price": 100.00,
                        "move_ticks": 6.0,
                        "current_age_seconds": 0.0,
                        "lookback_age_seconds": 0.0,
                        "used_in_peer_median": True,
                        "missing_reason": "",
                    }
                ],
            },
        },
    )
    assert "乌龙指事件复盘" in html
    assert "点击上面事件总览最右侧的“查看详情”，会弹出这笔事件的详情" in html
    assert "参考合约对照" in html
    assert "AU2608" in html
    assert "目标合约" in html
    assert "是否纳入参考中位数" in html
    assert "本行就是被判为乌龙指的快照" in html
    assert "可见成交价砸穿盘口" in html
    assert "11:29:50" in html
    assert "event-summary-row" in html
    assert "detail-link" in html
    assert "查看详情" in html
    assert "modal-shell:target" in html
    assert "href='#modal-" in html
    assert "关闭" in html
