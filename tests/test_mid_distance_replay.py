from __future__ import annotations

import pandas as pd
import pytest

from src.programmatic_simulation import (
    DEFAULT_CONFIG,
    MID_DISTANCE_MODE,
    load_mid_distance_parameters,
    load_mid_distance_replay_config,
    run_mid_distance_replay,
    simulate_mid_distance_day,
    write_mid_distance_replay_outputs,
)


def _row(
    key: int,
    *,
    last: float = 100.0,
    bid: float = 99.0,
    ask: float = 101.0,
    delta_volume: float = 0.0,
    source_row: int | None = None,
) -> dict[str, object]:
    return {
        "market_time_key": key,
        "display_time": f"09:01:{key // 1000:02d}.{key % 1000:03d}",
        "LastPrice": last,
        "delta_volume": delta_volume,
        "BidPrice1": bid,
        "AskPrice1": ask,
        "BidVolume1": 10,
        "AskVolume1": 10,
        "mid_price": (bid + ask) / 2,
        "is_tradable_session": True,
        "is_open_protected": False,
        "source_row": source_row if source_row is not None else key,
    }


def _frame(rows: list[dict[str, object]], contract: str = "DEMO2601") -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["contract"] = contract
    frame["commodity"] = "DEMO"
    frame["trade_date"] = "20260302"
    frame["tick_size"] = 1.0
    frame["contract_multiplier"] = 10
    return frame


def _params(*, sell_status: str = "OK") -> dict[str, dict[str, object]]:
    return {
        "BUY": {
            "InstrumentID": "DEMO2601",
            "Direction": "BUY",
            "SafeDistance": 0.08,
            "TargetDistance": 0.10,
            "MinDistance": 0.08,
            "MaxDistance": 0.12,
            "Status": "OK",
        },
        "SELL": {
            "InstrumentID": "DEMO2601",
            "Direction": "SELL",
            "SafeDistance": 0.08,
            "TargetDistance": 0.10,
            "MinDistance": 0.08,
            "MaxDistance": 0.12,
            "Status": sell_status,
        },
    }


def _config(**overrides: object) -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "enable_hedge": False,
            "order_effective_latency_ms": 0,
            "new_order_ack_latency_ms": 0,
            "cancel_ack_latency_ms": 0,
            "quote_check_interval_ms": 1000,
            "quote_spread_multiple": 2,
            "require_top_of_book_full_lot": True,
            "default_commission_per_lot_per_side": 2,
            "max_data_gap_ms": 3000,
            "max_order_actions_per_minute": 20,
        }
    )
    config.update(overrides)
    return config


def test_parameter_loader_normalizes_direction_and_disables_unqualified_side(tmp_path):
    path = tmp_path / "auto_parameters.csv"
    pd.DataFrame(
        [
            {**_params()["BUY"], "Direction": " down ", "EventCount": 42},
            {**_params(sell_status="NO_QUALIFIED_DISTANCE")["SELL"], "Direction": "up"},
        ]
    ).to_csv(path, index=False)

    loaded = load_mid_distance_parameters(path, ["demo2601"])

    assert set(loaded) == {"DEMO2601"}
    assert loaded["DEMO2601"]["BUY"]["Status"] == "OK"
    assert loaded["DEMO2601"]["BUY"]["EventCount"] == 42
    assert loaded["DEMO2601"]["SELL"]["Status"] == "NO_QUALIFIED_DISTANCE"

    duplicate = pd.DataFrame([_params()["BUY"], _params()["BUY"]])
    duplicate.to_csv(path, index=False)
    with pytest.raises(ValueError, match="重复合约方向"):
        load_mid_distance_parameters(path)


def test_mid_distance_uses_mid_price_and_fixed_hold_without_hedge():
    target = _frame(
        [
            _row(0),
            _row(500),
            _row(1000, last=90, bid=89, ask=90, delta_volume=1),
            _row(3000, bid=100, ask=101),
        ]
    )

    result = simulate_mid_distance_day(target, _params(), _config(), hold_seconds=2)

    trade = result["trades"].iloc[0]
    assert trade["quote_mode"] == MID_DISTANCE_MODE
    assert trade["direction"] == "long"
    assert trade["event_label"] == "unclassified"
    assert trade["target_entry_price"] == pytest.approx(90)
    assert trade["entry_mid_price"] == pytest.approx(89.5)
    assert trade["fill_key"] == 1000
    assert trade["planned_exit_key"] == pytest.approx(3000)
    assert trade["actual_exit_delay_ms"] == 0
    assert trade["exit_reason"] == "no_hedge_exit"
    assert trade["target_exit_price"] == pytest.approx(100)
    assert trade["gross_pnl"] == pytest.approx(100)
    assert trade["commission"] == pytest.approx(4)
    assert trade["net_pnl"] == pytest.approx(96)
    assert result["orders"].loc[result["orders"]["role"].str.startswith("hedge_")].empty
    submit = result["orders"].query("event == 'submit' and role == 'target_buy'").iloc[0]
    assert submit["effective_key"] == 0
    assert submit["quote_mode"] == MID_DISTANCE_MODE


def test_mid_distance_checks_directions_independently_and_requotes_after_cancel():
    target = _frame(
        [
            _row(0),
            _row(1000, bid=94, ask=96),
            _row(1500, bid=94, ask=96),
            _row(2000, bid=94, ask=96),
        ]
    )
    # At Mid=95 the old BUY=90 is too close, while old SELL=110 remains in band.
    target["mid_price"] = target["mid_price"].where(target["market_time_key"] == 0, 95)
    params = _params()
    params["SELL"] = {**params["SELL"], "MaxDistance": 0.20}

    result = simulate_mid_distance_day(target, params, _config(), hold_seconds=2)

    checks = result["quote_checks"]
    assert ((checks["direction"] == "BUY") & (checks["action"] == "CANCEL")).any()
    assert ((checks["direction"] == "SELL") & (checks["action"] == "KEEP")).any()
    sell_cancels = checks[(checks["direction"] == "SELL") & (checks["action"] == "CANCEL")]
    assert sell_cancels.empty
    buy_submits = result["orders"].query("role == 'target_buy' and event == 'submit'")
    assert len(buy_submits) == 2
    assert buy_submits.iloc[-1]["price"] == pytest.approx(85)


def test_mid_distance_does_not_favor_buy_when_both_new_orders_exceed_action_budget():
    result = simulate_mid_distance_day(
        _frame([_row(0), _row(1000)]),
        _params(),
        _config(max_order_actions_per_minute=1),
        hold_seconds=2,
    )

    assert result["orders"].empty
    assert set(result["quote_checks"]["reason"]) == {"order_action_limit"}


def test_mid_distance_does_not_fill_same_timestamp_as_submission():
    target = _frame(
        [
            _row(0, source_row=0),
            _row(0, last=90, bid=89, ask=90, delta_volume=1, source_row=1),
            _row(500, last=90, bid=89, ask=90, delta_volume=1, source_row=2),
            _row(2500, bid=100, ask=101, source_row=3),
        ]
    )

    result = simulate_mid_distance_day(target, _params(), _config(), hold_seconds=2)

    assert result["trades"].iloc[0]["fill_key"] == 500
    assert len(result["quote_checks"]) == 2


def test_mid_distance_does_not_retrofit_volume_across_effective_interval():
    target = _frame(
        [
            _row(0),
            _row(500, last=90, bid=89, ask=90, delta_volume=1),
            _row(1000, last=90, bid=89, ask=90, delta_volume=1),
            _row(3000, bid=100, ask=101),
        ]
    )

    result = simulate_mid_distance_day(
        target,
        _params(),
        _config(order_effective_latency_ms=250),
        hold_seconds=2,
    )

    assert result["trades"].iloc[0]["fill_key"] == 1000
    assert (result["quote_checks"]["reason"] == "activation_interval_unknown").any()


def test_batch_mid_distance_replay_reads_mid_source_and_writes_outputs(tmp_path):
    input_path = tmp_path / "AP501_20260302.csv"
    pd.DataFrame(
        [
            {"TradingDay": 20260302, "InstrumentID": "AP501", "UpdateTime": "09:01:00", "UpdateMillisec": 0, "LastPrice": 100, "Volume": 0, "BidPrice1": 99, "AskPrice1": 101, "BidVolume1": 10, "AskVolume1": 10},
            {"TradingDay": 20260302, "InstrumentID": "AP501", "UpdateTime": "09:01:00", "UpdateMillisec": 500, "LastPrice": 90, "Volume": 1, "BidPrice1": 89, "AskPrice1": 90, "BidVolume1": 10, "AskVolume1": 10},
            {"TradingDay": 20260302, "InstrumentID": "AP501", "UpdateTime": "09:01:02", "UpdateMillisec": 500, "LastPrice": 100, "Volume": 1, "BidPrice1": 100, "AskPrice1": 101, "BidVolume1": 10, "AskVolume1": 10},
        ]
    ).to_csv(input_path, index=False)
    parameter_path = tmp_path / "auto_parameters.csv"
    values = _params()
    values["BUY"]["InstrumentID"] = "AP501"
    values["SELL"]["InstrumentID"] = "AP501"
    pd.DataFrame(values.values()).to_csv(parameter_path, index=False)
    output_dir = tmp_path / "replay"

    result = run_mid_distance_replay(
        parameter_path,
        input_path,
        start_date="20260302",
        end_date="20260302",
        output_dir=output_dir,
        hold_seconds=2,
        config=_config(),
    )
    paths = write_mid_distance_replay_outputs(result)

    assert len(result["trades"]) == 1
    assert result["trades"].iloc[0]["target_contract"] == "AP501"
    assert result["skipped_days"].empty
    assert (output_dir / "parameters_used.csv").is_file()
    assert (output_dir / "AP501" / "quote_checks.csv").is_file()
    assert paths["report"].endswith("replay_index.html")


def test_mid_distance_rejects_non_integral_hold_and_bad_parameter_relation(tmp_path):
    with pytest.raises(ValueError, match="整数毫秒"):
        simulate_mid_distance_day(_frame([_row(0)]), _params(), _config(), hold_seconds=0.0005)

    path = tmp_path / "bad.csv"
    bad = _params()["BUY"] | {"TargetDistance": 0.05}
    pd.DataFrame([bad]).to_csv(path, index=False)
    with pytest.raises(ValueError, match="距离关系无效"):
        load_mid_distance_parameters(path)


def test_mid_distance_execution_config_rejects_hedge_and_legacy_fields(tmp_path):
    path = tmp_path / "execution.json"
    path.write_text('{"enable_hedge": false, "default_commission_per_lot_per_side": 1}', encoding="utf-8")
    assert load_mid_distance_replay_config(path)["enable_hedge"] is False

    path.write_text('{"enable_hedge": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="固定不对冲"):
        load_mid_distance_replay_config(path)
    path.write_text('{"band_half_width_pct": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="禁止"):
        load_mid_distance_replay_config(path)
