from __future__ import annotations

import math

import pandas as pd
import pytest

from src.tick_detector.reference_selection import (
    attach_reference_metrics,
    infer_tick_size,
    select_reference_contracts,
)


def _frame(contract: str, commodity: str, rows: list[dict[str, object]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["contract"] = contract
    df["commodity"] = commodity
    df["parse_status"] = "ok"
    df["trade_date"] = "20260520"
    if "is_tradable_session" not in df.columns:
        df["is_tradable_session"] = True
    return df


def test_infer_tick_size_and_select_reference_contracts():
    a = _frame(
        "AU2606",
        "AU",
        [
            {"LastPrice": 100.00, "BidPrice1": 99.98, "AskPrice1": 100.00, "Volume": 100},
            {"LastPrice": 100.02, "BidPrice1": 100.00, "AskPrice1": 100.02, "Volume": 150},
        ],
    )
    b = _frame("AU2608", "AU", [{"LastPrice": 100.04, "BidPrice1": 100.02, "AskPrice1": 100.04, "Volume": 300}])
    c = _frame("AU2610", "AU", [{"LastPrice": 100.06, "BidPrice1": 100.04, "AskPrice1": 100.06, "Volume": 200}])
    ag = _frame("AG2606", "AG", [{"LastPrice": 10.0, "BidPrice1": 9.9, "AskPrice1": 10.0, "Volume": 1000}])

    assert infer_tick_size(a) == 0.02
    refs = select_reference_contracts(
        {"AU2606": a, "AU2608": b, "AU2610": c, "AG2606": ag},
        target_contract="AU2606",
    )
    assert refs == ["AU2608", "AU2610"]


def test_attach_reference_metrics_uses_latest_same_timestamp_snapshot_and_computes_full():
    target = _frame(
        "AU2606",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:03"), "snapshot_seq": 1, "mid_price": 99.90, "LastPrice": 99.90},
        ],
    )
    peer_a = _frame(
        "AU2608",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:03"), "snapshot_seq": 4, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:03"), "snapshot_seq": 5, "mid_price": 100.04, "LastPrice": 100.04},
        ],
    )
    peer_b = _frame(
        "AU2610",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:03"), "snapshot_seq": 1, "mid_price": 100.02, "LastPrice": 100.02},
        ],
    )

    out = attach_reference_metrics(target, [peer_a, peer_b], tick_size=0.02)
    row = out.iloc[1]

    assert row["reference_contract_count"] == 2
    assert row["full_blocked_reason"] == "not_blocked"
    assert row["peer_median_move_ticks"] == pytest.approx(1.5)
    assert row["peer_move_limit_ticks"] == pytest.approx(2.0)
    assert row["expected_price_simple"] > 100.0
    assert row["expected_price_full"] == row["expected_price_simple"]
    assert row["down_deviation_ticks"] > 0


def test_attach_reference_metrics_uses_target_mid_move_instead_of_last_trade_move():
    target = _frame(
        "AU2606",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 21:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 21:00:03"), "snapshot_seq": 1, "mid_price": 100.00, "LastPrice": 99.50},
        ],
    )
    peer_a = _frame(
        "AU2608",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 21:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 21:00:03"), "snapshot_seq": 1, "mid_price": 100.02, "LastPrice": 100.02},
        ],
    )
    peer_b = _frame(
        "AU2610",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 21:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 21:00:03"), "snapshot_seq": 1, "mid_price": 100.02, "LastPrice": 100.02},
        ],
    )

    out = attach_reference_metrics(target, [peer_a, peer_b], tick_size=0.02)
    row = out.iloc[1]

    assert row["target_move_ticks"] == pytest.approx(0.0)
    assert row["peer_median_move_ticks"] == pytest.approx(1.0)
    assert row["peer_excess_down_ticks"] == pytest.approx(1.0)


def test_attach_reference_metrics_stale_peer_blocks_simple_and_full_metrics():
    target = _frame(
        "AU2606",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:04"), "snapshot_seq": 1, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:07"), "snapshot_seq": 2, "mid_price": 99.90, "LastPrice": 99.90},
        ],
    )
    peer_a = _frame(
        "AU2608",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:07"), "snapshot_seq": 1, "mid_price": 100.04, "LastPrice": 100.04},
        ],
    )
    peer_b = _frame(
        "AU2610",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:07"), "snapshot_seq": 1, "mid_price": 100.02, "LastPrice": 100.02},
        ],
    )

    out = attach_reference_metrics(target, [peer_a, peer_b], tick_size=0.02)
    row = out.iloc[2]

    assert row["reference_contract_count"] == 0
    assert row["full_blocked_reason"] == "insufficient_fresh_peers"
    assert math.isnan(row["peer_median_move_ticks"])
    assert math.isnan(row["peer_excess_down_ticks"])
    assert math.isnan(row["expected_price_full"])
    assert math.isnan(row["expected_price_simple"])


def test_attach_reference_metrics_returns_nan_when_reference_contracts_are_insufficient():
    target = _frame(
        "AU2606",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:03"), "snapshot_seq": 1, "mid_price": 99.90, "LastPrice": 99.90},
        ],
    )
    peer_a = _frame(
        "AU2608",
        "AU",
        [
            {"timestamp": pd.Timestamp("2026-05-20 09:00:00"), "snapshot_seq": 0, "mid_price": 100.00, "LastPrice": 100.00},
            {"timestamp": pd.Timestamp("2026-05-20 09:00:03"), "snapshot_seq": 1, "mid_price": 100.02, "LastPrice": 100.02},
        ],
    )

    out = attach_reference_metrics(target, [peer_a], tick_size=0.02)
    row = out.iloc[1]

    assert row["reference_contract_count"] == 1
    assert math.isnan(row["expected_price_simple"])
    assert math.isnan(row["down_deviation_ticks"])
