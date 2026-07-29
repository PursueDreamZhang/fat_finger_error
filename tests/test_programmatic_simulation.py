from __future__ import annotations

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from src.programmatic_simulation import (
    DEFAULT_CONFIG,
    _DayReplay,
    _fill_evidence,
    _simulate_programmatic_day_prepared,
    build_programmatic_summary,
    build_grid,
    simulate_programmatic_day,
)


def _frame(contract: str, rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["contract"] = contract
    frame["commodity"] = "NI"
    frame["trade_date"] = "20260302"
    frame["tick_size"] = 1.0
    frame["contract_multiplier"] = 1
    frame["is_tradable_session"] = True
    frame["is_open_protected"] = False
    return frame


def _row(
    key: int,
    *,
    last: float = 100.0,
    vwap: float = 100.0,
    bid: float = 100.0,
    ask: float = 101.0,
    bid_volume: float = 10.0,
    ask_volume: float = 10.0,
    fair: float = 100.0,
    reliable: bool = True,
    delta_volume: float = 1.0,
) -> dict[str, object]:
    return {
        "market_time_key": key,
        "display_time": f"21:00:{key // 1000:02d}.000",
        "LastPrice": last,
        "interval_vwap": vwap,
        "delta_volume": delta_volume,
        "BidPrice1": bid,
        "AskPrice1": ask,
        "BidVolume1": bid_volume,
        "AskVolume1": ask_volume,
        "mid_price": (bid + ask) / 2,
        "fair_price": fair,
        "fair_price_reliable": reliable,
    }


def _config(**overrides: object) -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "account_equity": 100000,
            "max_margin_ratio": 0.3,
            "default_margin_rate": 0.1,
            "default_commission_per_lot_per_side": 0,
            "target_lots": 1,
            "hedge_lots": 1,
            "band_half_width_ticks": 10,
            "outer_quote_offset_ticks": 10,
            "reanchor_step_ticks": 10,
            "reanchor_confirm_ms": 500,
            "resume_confirm_ms": 0,
            "min_reprice_interval_ms": 0,
            "max_order_actions_per_minute": 20,
            "cancel_ack_latency_ms": 0,
            "new_order_ack_latency_ms": 0,
            "hedge_submit_latency_ms": 0,
            "max_hedge_wait_ms": 1000,
            "max_quote_age_ms": 3000,
            "max_data_gap_ms": 3000,
            "fill_model": "observable_cross_assumed",
            "require_top_of_book_full_lot": True,
            "hedged_exit_delay_ms": 0,
            "cooldown_ms": 10000,
        }
    )
    config.update(overrides)
    return config


def _hedge(rows: list[dict[str, object]] | None = None) -> pd.DataFrame:
    return _frame(
        "NI2604",
        rows
        or [
            _row(0),
            _row(500),
            _row(1000),
            _row(1500),
            _row(2000),
            _row(2500),
            _row(3000),
        ],
    )


def test_grid_and_normal_fill_then_programmatic_hedge_and_exit():
    assert build_grid(100, 1, _config()) == {
        "anchor": 100,
        "band_lower": 90,
        "band_upper": 110,
        "buy_limit": 80,
        "sell_limit": 120,
    }
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, last=80, vwap=80, bid=79, ask=80),
            _row(1500, last=81, vwap=81, bid=80, ask=81),
            _row(2000, last=100, vwap=100, bid=100, ask=101),
        ],
    )

    result = simulate_programmatic_day(target, _hedge(), _config(), trade_date="20260302")

    assert len(result["trades"]) == 1
    trade = result["trades"].iloc[0]
    assert trade["status"] == "closed"
    assert trade["direction"] == "long"
    assert trade["target_entry_price"] == pytest.approx(80)
    assert trade["hedge_entry_price"] == pytest.approx(100)
    assert trade["target_exit_price"] == pytest.approx(100)
    assert trade["hedge_exit_price"] == pytest.approx(101)
    assert trade["gross_pnl"] == pytest.approx(19)
    assert "last_trade" in trade["fill_evidence"]
    assert "interval_vwap" in trade["fill_evidence"]
    assert "top_of_book" in trade["fill_evidence"]


def test_replay_state_machine_does_not_use_dataframe_iterrows(monkeypatch):
    def _unexpected_iterrows(*_args, **_kwargs):
        raise AssertionError("状态机不应逐行创建 pandas Series")

    monkeypatch.setattr(pd.DataFrame, "iterrows", _unexpected_iterrows)

    result = simulate_programmatic_day(
        _frame("NI2605", [_row(0), _row(500), _row(1000, last=80, vwap=80, bid=79, ask=80)]),
        _hedge(),
        _config(),
        trade_date="20260302",
    )

    assert len(result["transitions"]) > 0


def test_public_entry_sorts_unsorted_frames_but_prepared_entry_reuses_sorted_frames():
    target = _frame(
        "NI2605",
        [
            _row(1000, last=80, vwap=80, bid=79, ask=80),
            _row(0),
            _row(500),
            _row(1500, last=100, vwap=100, bid=100, ask=101),
        ],
    )
    hedge = _hedge([_row(1000), _row(0), _row(500), _row(1500)])
    config = _config()

    public_result = simulate_programmatic_day(target, hedge, config, trade_date="20260302")
    prepared_result = _simulate_programmatic_day_prepared(
        target.sort_values("market_time_key", kind="stable").reset_index(drop=True),
        hedge.sort_values("market_time_key", kind="stable").reset_index(drop=True),
        config,
        trade_date="20260302",
        assume_sorted=True,
    )

    for name in ("transitions", "orders", "trades"):
        assert_frame_equal(public_result[name], prepared_result[name])


def test_active_target_index_keeps_cancel_requested_order_until_cancel_ack():
    target = _frame("NI2605", [_row(0), _row(1000)])
    replay = _DayReplay(
        target,
        _hedge(),
        _config(new_order_ack_latency_ms=0, cancel_ack_latency_ms=1000),
        trade_date="20260302",
        detector_event_keys=set(),
    )
    first_row = replay.target.iloc[0]
    replay.anchor = 100
    replay.grid = build_grid(100, 1, _config())

    assert replay._submit_grid(first_row, "test") is True
    buy_id = replay.active_target_order_ids["target_buy"][0]
    sell_id = replay.active_target_order_ids["target_sell"][0]
    replay._request_cancel(first_row, replay.orders_by_id[buy_id], "test")

    assert buy_id in replay.active_target_order_ids["target_buy"]
    replay._process_due(replay.target.iloc[1])

    assert buy_id not in replay.active_target_order_ids["target_buy"]
    assert replay.active_target_order_ids["target_sell"] == [sell_id]




def test_last_vwap_and_top_of_book_are_or_fill_evidence_at_limit_price():
    order = {"side": "buy", "price": 80.0, "lots": 1}
    config = _config()

    assert _fill_evidence(_frame("NI2605", [_row(0, last=80, vwap=100, bid=79, ask=81)]).iloc[0], order, config)[0] == ["last_trade"]
    assert _fill_evidence(_frame("NI2605", [_row(0, last=100, vwap=80, bid=79, ask=81)]).iloc[0], order, config)[0] == ["interval_vwap"]
    assert _fill_evidence(_frame("NI2605", [_row(0, last=100, vwap=100, bid=79, ask=80, delta_volume=0)]).iloc[0], order, config)[0] == ["top_of_book"]

    sell_order = {"side": "sell", "price": 120.0, "lots": 1}
    evidence, _ = _fill_evidence(_frame("NI2605", [_row(0, last=120, vwap=100, bid=119, ask=121)]).iloc[0], sell_order, config)
    assert evidence == ["last_trade"]


def test_book_cross_with_insufficient_one_level_volume_is_not_silent_full_fill():
    order = {"side": "buy", "price": 80.0, "lots": 1}
    row = _frame("NI2605", [_row(0, last=100, vwap=100, bid=79, ask=80, ask_volume=0, delta_volume=0)]).iloc[0]

    evidence, partial_unknown = _fill_evidence(row, order, _config())

    assert evidence == []
    assert partial_unknown is True


def test_fair_only_reanchors_after_confirmation_and_keeps_order_when_target_last_moves():
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, last=89, vwap=89, bid=88, ask=90, fair=89),
            _row(1500, last=89, vwap=89, bid=88, ask=90, fair=89),
            _row(2000, last=89, vwap=89, bid=88, ask=90, fair=89),
        ],
    )

    result = simulate_programmatic_day(target, _hedge(), _config(), trade_date="20260302")

    assert result["trades"].empty
    completed = result["transitions"].loc[result["transitions"]["reason"] == "replace_complete"].iloc[-1]
    assert completed["grid_anchor"] == pytest.approx(90)
    assert completed["buy_limit"] == pytest.approx(70)
    assert completed["sell_limit"] == pytest.approx(110)


def test_old_order_can_fill_before_cancel_ack_and_is_labeled_replace_race():
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, last=89, vwap=89, bid=88, ask=90, fair=89),
            _row(1500, last=89, vwap=89, bid=88, ask=90, fair=89),
            _row(2000, last=80, vwap=80, bid=79, ask=80, fair=89),
            _row(2500, last=81, vwap=81, bid=80, ask=81, fair=100),
            _row(3000, last=100, vwap=100, bid=100, ask=101, fair=100),
        ],
    )

    result = simulate_programmatic_day(
        target,
        _hedge(),
        _config(cancel_ack_latency_ms=1000),
        trade_date="20260302",
    )

    assert len(result["trades"]) == 1
    assert result["trades"].iloc[0]["fill_context"] == "fill_during_replace"


def test_missing_hedge_quote_at_deadline_emergency_flattens_target():
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, last=80, vwap=80, bid=79, ask=80),
            _row(1500, last=100, vwap=100, bid=100, ask=101),
        ],
    )
    stale_hedge = _hedge([_row(0)])

    result = simulate_programmatic_day(
        target,
        stale_hedge,
        _config(max_quote_age_ms=100, max_hedge_wait_ms=500),
        trade_date="20260302",
    )

    assert len(result["trades"]) == 1
    trade = result["trades"].iloc[0]
    assert trade["status"] == "hedge_failure_exit"
    assert trade["exit_reason"] == "hedge_failure_exit"
    assert pd.isna(trade["hedge_entry_price"])


def test_single_bad_fair_snapshot_does_not_create_a_cancel_and_requote_cycle():
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, reliable=False),
            _row(1500),
        ],
    )

    result = simulate_programmatic_day(
        target,
        _hedge(),
        _config(),
        trade_date="20260302",
    )

    assert "fair_invalid_or_session_guard" not in result["transitions"]["reason"].tolist()
    assert "cancel_requested" not in result["orders"]["event"].tolist()


def test_actual_hedge_margin_guard_closes_target_if_reference_price_jumps():
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, last=80, vwap=80, bid=79, ask=80),
            _row(1500, last=100, vwap=100, bid=100, ask=101),
        ],
    )
    hedge = _hedge([_row(0), _row(1500, last=200, vwap=200, bid=200, ask=201)])

    result = simulate_programmatic_day(
        target,
        hedge,
        _config(account_equity=250, max_margin_ratio=0.1),
        trade_date="20260302",
    )

    assert len(result["trades"]) == 1
    trade = result["trades"].iloc[0]
    assert trade["status"] == "capital_limit_exit"
    assert trade["exit_reason"] == "capital_limit_at_hedge"
    assert pd.isna(trade["hedge_entry_price"])


def test_summary_counts_only_real_reanchors_and_not_the_initial_paused_state():
    transitions = pd.DataFrame(
        [
            {"trade_date": "20260302", "to_state": "PAUSED", "reason": "start"},
            {"trade_date": "20260302", "to_state": "FLAT_QUOTING", "reason": "fair_recovered"},
            {"trade_date": "20260302", "to_state": "REPLACE_PENDING", "reason": "fair_down_confirmed"},
            {"trade_date": "20260302", "to_state": "REPLACE_PENDING", "reason": "fair_up_confirmed"},
            {"trade_date": "20260302", "to_state": "PAUSED", "reason": "data_gap"},
        ]
    )
    orders = pd.DataFrame(
        [
            {"trade_date": "20260302", "contract": "NI2605", "role": "target_buy", "event": "submit", "market_time_key": 0},
            {"trade_date": "20260302", "contract": "NI2605", "role": "target_sell", "event": "cancel_requested", "market_time_key": 500},
        ]
    )

    summary = build_programmatic_summary(pd.DataFrame(), orders, transitions).iloc[0]

    assert summary["reprice_count"] == 2
    assert summary["pause_count"] == 1
    assert summary["peak_order_actions_per_minute"] == 2


def test_hedged_exit_defers_flatten_until_delay():
    """对冲成交后须等 hedged_exit_delay_ms 才平两腿。"""
    target = _frame(
        "NI2605",
        [
            _row(0),
            _row(500),
            _row(1000, last=80, vwap=80, bid=79, ask=80),
            _row(1500, last=100, vwap=100, bid=100, ask=101),
            _row(2000, last=100, vwap=100, bid=100, ask=101),
            _row(2500, last=100, vwap=100, bid=100, ask=101),
            _row(3000, last=100, vwap=100, bid=100, ask=101),
        ],
    )
    result = simulate_programmatic_day(
        target, _hedge(), _config(hedged_exit_delay_ms=1000), trade_date="20260302"
    )
    trades = result["trades"]
    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade["exit_reason"] == "hedged_exit"
    assert int(trade["exit_key"]) - int(trade["hedge_entry_key"]) >= 1000


def test_fair_invalid_falls_back_to_last_price_for_quoting():
    """fair 失效时用最新成交价兜底继续报价，不 pause。"""
    target = _frame(
        "NI2605",
        [
            _row(0, reliable=False, last=100),
            _row(500, reliable=False, last=100),
            _row(1000, reliable=False, last=100),
            _row(1500, reliable=False, last=100),
        ],
    )
    result = simulate_programmatic_day(
        target, _hedge(), _config(resume_confirm_ms=0), trade_date="20260302"
    )
    states = result["transitions"]["to_state"].tolist()
    assert "FLAT_QUOTING" in states
    assert "fair_invalid_or_session_guard" not in result["transitions"]["reason"].tolist()
