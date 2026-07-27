from __future__ import annotations

import pandas as pd
import pytest

from src.manual_simulation import _point_fair_price, build_scenarios, simulate_event


def _frame(contract: str, rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["contract"] = contract
    frame["commodity"] = "NI"
    frame["tick_size"] = 1.0
    frame["contract_multiplier"] = 1
    frame["is_tradable_session"] = True
    return frame


def _target_frame(*, pre_event_touch: bool = False) -> pd.DataFrame:
    rows = [
        {
            "market_time_key": 0,
            "snapshot_seq_end": 1,
            "display_time": "21:00:00.000",
            "LastPrice": 100.0,
            "interval_vwap": 100.0,
            "delta_volume": 1.0,
            "BidPrice1": 100.0,
            "AskPrice1": 101.0,
            "fair_price": 100.0,
            "fair_price_reliable": True,
        },
    ]
    if pre_event_touch:
        rows.append(
            {
                "market_time_key": 5000,
                "snapshot_seq_end": 2,
                "display_time": "21:00:05.000",
                "LastPrice": 98.0,
                "interval_vwap": 98.0,
                "delta_volume": 1.0,
                "BidPrice1": 98.0,
                "AskPrice1": 99.0,
                "fair_price": 100.0,
                "fair_price_reliable": True,
            }
        )
    rows.extend(
        [
            {
                "market_time_key": 10000,
                "snapshot_seq_end": 100,
                "display_time": "21:00:10.000",
                "LastPrice": 98.0,
                "interval_vwap": 98.0,
                "delta_volume": 1.0,
                "BidPrice1": 98.0,
                "AskPrice1": 99.0,
                "fair_price": 100.0,
                "fair_price_reliable": True,
            },
            {
                "market_time_key": 15000,
                "snapshot_seq_end": 101,
                "display_time": "21:00:15.000",
                "LastPrice": 97.0,
                "interval_vwap": 97.0,
                "delta_volume": 1.0,
                "BidPrice1": 97.0,
                "AskPrice1": 98.0,
                "fair_price": 100.0,
                "fair_price_reliable": True,
            },
            {
                "market_time_key": 20000,
                "snapshot_seq_end": 102,
                "display_time": "21:00:20.000",
                "LastPrice": 101.0,
                "interval_vwap": 101.0,
                "delta_volume": 1.0,
                "BidPrice1": 101.0,
                "AskPrice1": 102.0,
                "fair_price": 100.0,
                "fair_price_reliable": True,
            },
        ]
    )
    return _frame("NI2605", rows)


def _hedge_frame() -> pd.DataFrame:
    return _frame(
        "NI2604",
        [
            {
                "market_time_key": 15000,
                "snapshot_seq_end": 1,
                "display_time": "21:00:15.000",
                "LastPrice": 100.0,
                "interval_vwap": 100.0,
                "delta_volume": 1.0,
                "BidPrice1": 100.0,
                "AskPrice1": 101.0,
            },
            {
                "market_time_key": 20000,
                "snapshot_seq_end": 2,
                "display_time": "21:00:20.000",
                "LastPrice": 99.0,
                "interval_vwap": 99.0,
                "delta_volume": 1.0,
                "BidPrice1": 98.0,
                "AskPrice1": 99.0,
            },
        ],
    )


def _config() -> dict[str, object]:
    return {
        "fill_model": "strict_visible",
        "max_quote_age_seconds": 3,
        "slippage_ticks": 0,
        "target_lots": 1,
        "hedge_lots": 1,
        "commission_by_commodity": {},
        "default_commission_per_lot_per_side": 0,
        "margin_rate_by_commodity": {},
        "default_margin_rate": 0.1,
        "account_equity": 100000,
        "max_margin_ratio": 0.3,
        "max_single_trade_loss": 1000,
        "hedge_reference_index": 0,
        "event_fill_window_seconds": 1,
    }


def _event() -> dict[str, object]:
    return {
        "事件编号": "NI2605|20260302|100",
        "交易日": "20260302",
        "事件时间": "21:00:10.000",
        "品种": "NI",
        "合约": "NI2605",
        "事件锚点结束序号": 100,
        "参考合约列表": "NI2604",
        "合理价": 100.0,
        "触发原因": "visible_execution_drop",
    }


def _scenario() -> dict[str, float]:
    return {
        "order_age_seconds": 10.0,
        "entry_distance_bps": 100.0,
        "hedge_delay_seconds": 5.0,
        "exit_delay_seconds": 10.0,
    }


def test_event_fill_models_manual_hedge_and_full_margin():
    target = _target_frame()
    hedge = _hedge_frame()

    result = simulate_event(_event(), target, {"NI2604": hedge}, _config(), _scenario())

    assert result["status"] == "event_fill"
    assert result["model_settled"] is True
    assert result["strategy_eligible"] is True
    assert result["entry_price"] == pytest.approx(99.0)
    assert result["hedge_entry_price"] == pytest.approx(100.0)
    assert result["target_pnl"] == pytest.approx(2.0)
    assert result["hedge_pnl"] == pytest.approx(1.0)
    assert result["net_pnl"] == pytest.approx(3.0)
    assert result["gross_margin"] == pytest.approx(19.9)
    assert result["unhedged_mae_bps"] == pytest.approx(-202.0202, rel=1e-4)


def test_touch_before_candidate_is_not_counted_as_clean_event_fill():
    target = _target_frame(pre_event_touch=True)

    result = simulate_event(_event(), target, {"NI2604": _hedge_frame()}, _config(), _scenario())

    assert result["status"] == "pre_event_touch"
    assert result["model_settled"] is False
    assert result["pre_event_touch_key"] == 5000


def test_scenarios_skip_exit_before_hedge_delay():
    scenarios, skipped = build_scenarios(
        {
            "order_ages_seconds": [300],
            "entry_distances_bps": [100],
            "hedge_delays_seconds": [5, 10],
            "exit_delays_seconds": [5],
        }
    )

    assert skipped == 1
    assert scenarios == [
        {
            "order_age_seconds": 300.0,
            "entry_distance_bps": 100.0,
            "hedge_delay_seconds": 5.0,
            "exit_delay_seconds": 5.0,
        }
    ]


def test_point_fair_price_only_calculates_requested_order_time():
    rows = []
    for second in range(121):
        rows.append(
            {
                "market_time_key": second * 1000,
                "snapshot_seq_end": second,
                "display_time": f"21:00:{second % 60:02d}.000",
                "LastPrice": 100.0,
                "interval_vwap": 100.0,
                "delta_volume": 1.0,
                "BidPrice1": 99.0,
                "AskPrice1": 101.0,
                "mid_price": 100.0,
                "Volume": float(second + 1),
            }
        )
    target = _frame("NI2605", rows)
    peer_a = _frame("NI2604", rows)
    peer_b = _frame("NI2606", rows)

    result = _point_fair_price(
        target,
        {"NI2604": peer_a, "NI2606": peer_b},
        {"NI2604", "NI2606"},
        1.0,
        120000,
        3.0,
    )

    assert result["fair_price_reliable"] is True
    assert result["fair_key"] == 120000
    assert result["fair_price"] == pytest.approx(100.0)
