import pandas as pd
import pytest

import src.programmatic_grid as programmatic_grid
from src.programmatic_grid import (
    GRID_SUMMARY_COLUMNS,
    _context_rows,
    _render_grid_report,
    build_grid_trade_contexts,
    build_grid_scenarios,
    normalize_programmatic_grid_config,
    _summarize_scenario,
    _stable_sorted_frame,
    run_programmatic_grid,
)


def test_build_grid_scenarios_cartesian_product():
    scenarios = build_grid_scenarios({
        "quote_shapes": [{"W": 5, "D": 6, "S": 7}, {"W": 10, "D": 11, "S": 12}],
        "latency_profiles": [{"cancel_ack_latency_ms": 100}, {"cancel_ack_latency_ms": 200}],
    })
    assert len(scenarios) == 4
    assert scenarios[0]["band_half_width_ticks"] == 5
    assert scenarios[-1]["cancel_ack_latency_ms"] == 200


def test_normalize_grid_requires_instruments_and_shapes():
    raw = {
        "output_dir": "tmp/grid",
        "base": {"trade_date_start": "20260301", "trade_date_end": "20260302"},
        "instruments": [{"name": "NI", "commodity": "NI", "target_contract": "NI2605", "fair_reference_contracts": ["NI2604", "NI2609"], "hedge_contract": "NI2604"}],
        "quote_shapes": [{"W": 5, "D": 5, "S": 5}],
        "latency_profiles": [{}],
    }
    config = normalize_programmatic_grid_config(raw)
    assert config["thresholds"]["min_fills"] == 3
    assert config["context_mode"] == "all"

    raw["context_mode"] = "selected"
    raw["context_scenarios"] = ["NI::Q01-L01"]
    assert normalize_programmatic_grid_config(raw)["context_scenarios"] == ["NI::Q01-L01"]

    raw["context_scenarios"] = ["missing::Q01-L01"]
    with pytest.raises(ValueError, match="不存在组合"):
        normalize_programmatic_grid_config(raw)


def test_grid_normalizes_each_scenario_once_before_date_loop(monkeypatch):
    raw = {
        "output_dir": "tmp/grid",
        "base": {"trade_date_start": "20260301", "trade_date_end": "20260302"},
        "instruments": [{"name": "NI", "commodity": "NI", "target_contract": "NI2605", "fair_reference_contracts": ["NI2604", "NI2609"], "hedge_contract": "NI2604"}],
        "quote_shapes": [{"W": 5, "D": 5, "S": 5}, {"W": 10, "D": 10, "S": 10}],
        "latency_profiles": [{}],
    }
    original = programmatic_grid.normalize_programmatic_simulation_config
    calls = 0

    def counted(config):
        nonlocal calls
        calls += 1
        return original(config)

    monkeypatch.setattr(programmatic_grid, "normalize_programmatic_simulation_config", counted)
    monkeypatch.setattr(programmatic_grid, "_load_event_rows", lambda _path: pd.DataFrame())
    monkeypatch.setattr(programmatic_grid, "_prepare_grid_day", lambda *_args, **_kwargs: "tick_day_missing")

    result = run_programmatic_grid(raw)

    assert calls == 3  # 1 个品种基础配置 + 2 个场景，不随 2 个日期重复。
    assert len(result["daily"]) == 4


def test_stable_sorted_frame_keeps_duplicate_time_order():
    frame = pd.DataFrame({"market_time_key": [2, 1, 1], "LastPrice": [20, 10, 11]})

    result = _stable_sorted_frame(frame)

    assert result["market_time_key"].tolist() == [1, 1, 2]
    assert result["LastPrice"].tolist() == [10, 11, 20]


def test_fair_cache_requires_complete_enriched_frame(tmp_path):
    config = {"fair_cache_dir": str(tmp_path)}
    frame = pd.DataFrame({
        "market_time_key": [1], "fair_price": [100.0], "fair_price_reliable": [True], "last_down_ticks": [1.0],
    })

    programmatic_grid._write_fair_cache(config, "valid", frame)
    assert programmatic_grid._read_fair_cache(config, "valid").equals(frame)

    pd.DataFrame({"market_time_key": [1]}).to_pickle(tmp_path / "fair_missing.pkl")
    (tmp_path / "fair_missing.json").write_text('{"schema": 1, "key": "missing"}', encoding="utf-8")
    assert programmatic_grid._read_fair_cache(config, "missing") is None


def test_summarize_marks_insufficient_and_reports_win_rate():
    scenario = build_grid_scenarios({"quote_shapes": [{"W": 5, "D": 5, "S": 5}], "latency_profiles": [{}]})[0]
    daily = pd.DataFrame([
        {"scenario_id": scenario["scenario_id"], "instrument": "NI", "trade_date": "20260301", "fill_count": 2,
         "detector_event_fill_count": 2, "normal_move_fill_count": 0, "fill_during_replace_count": 0,
         "closed_count": 2, "win_count": 1, "hedge_failure_count": 0, "capital_limit_exit_count": 0,
         "net_pnl": 10, "worst_net_pnl": 10, "order_action_count": 2, "peak_order_actions_per_minute": 2,
         "reprice_count": 0, "pause_count": 0, "skipped": False, "skip_reason": ""}
    ])
    result = _summarize_scenario("NI", scenario, daily, {
        "min_fills": 3, "min_event_fills": 1, "max_normal_move_fill_rate": 0.25,
        "max_hedge_failure_rate": 0.1, "max_daily_loss": 500, "max_peak_order_actions_per_minute": 20,
    })
    assert set(result) == set(GRID_SUMMARY_COLUMNS)
    assert result["win_rate"] == 0.5
    assert result["eligible"] is False
    assert "insufficient_fills" in result["selection_reason"]


def test_grid_report_embeds_layered_trade_view_and_translated_risks():
    summary = pd.DataFrame([
        {"scenario_id": "Q01-L01", "instrument": "NI2605", "band_half_width_ticks": 5, "outer_quote_offset_ticks": 5,
         "reanchor_step_ticks": 5, "reanchor_confirm_ms": 1000, "resume_confirm_ms": 2000, "fair_invalid_confirm_ms": 1000,
         "cancel_ack_latency_ms": 500, "new_order_ack_latency_ms": 500, "hedge_submit_latency_ms": 500, "max_hedge_wait_ms": 2000,
         "active_days": 1, "fill_count": 1, "detector_event_fill_count": 0, "normal_move_fill_count": 1,
         "normal_move_fill_rate": 1.0, "fill_during_replace_count": 0, "closed_count": 1, "hedge_failure_count": 0,
         "hedge_failure_rate": 0.0, "capital_limit_exit_count": 0, "net_pnl": -20, "win_rate": 0.0,
         "worst_day_net_pnl": -20, "daily_net_std": 0.0, "max_gross_margin": None,
         "peak_order_actions_per_minute": 21, "total_order_action_count": 30, "total_reprice_count": 2,
         "skipped_day_count": 0, "eligible": False, "selection_reason": "normal_move_fill_rate_too_high;order_action_limit_exceeded"},
        {"scenario_id": "Q02-L01", "instrument": "NI2605", "band_half_width_ticks": 50, "outer_quote_offset_ticks": 50,
         "reanchor_step_ticks": 50, "reanchor_confirm_ms": 1000, "resume_confirm_ms": 2000, "fair_invalid_confirm_ms": 1000,
         "cancel_ack_latency_ms": 500, "new_order_ack_latency_ms": 500, "hedge_submit_latency_ms": 500, "max_hedge_wait_ms": 2000,
         "active_days": 1, "fill_count": 0, "detector_event_fill_count": 0, "normal_move_fill_count": 0,
         "normal_move_fill_rate": None, "fill_during_replace_count": 0, "closed_count": 0, "hedge_failure_count": 0,
         "hedge_failure_rate": None, "capital_limit_exit_count": 0, "net_pnl": 0, "win_rate": None,
         "worst_day_net_pnl": 0, "daily_net_std": 0, "max_gross_margin": None,
         "peak_order_actions_per_minute": 2, "total_order_action_count": 8, "total_reprice_count": 0,
         "skipped_day_count": 0, "eligible": False, "selection_reason": "insufficient_fills"},
    ])
    daily = pd.DataFrame([{"scenario_id": "Q01-L01", "instrument": "NI2605", "trade_date": "20260302", "fill_count": 1,
                           "detector_event_fill_count": 0, "normal_move_fill_count": 1, "net_pnl": -20,
                           "hedge_failure_count": 0, "peak_order_actions_per_minute": 21, "skipped": False}])
    trades = pd.DataFrame([{"trade_id": "t1", "scenario_id": "Q01-L01", "instrument": "NI2605", "fill_time": "21:00:01",
                            "fill_key": 1, "direction": "long", "event_label": "normal_move_fill", "fill_evidence": "last_trade",
                            "target_entry_price": 100, "hedge_entry_price": 99, "exit_reason": "reversion_exit", "status": "closed",
                            "target_pnl": -10, "hedge_pnl": -10, "net_pnl": -20, "unhedged_worst_mark_pnl": -15,
                            "hedged_worst_mark_pnl": -20, "gross_margin": 2000}])
    contexts = {"NI2605::Q01-L01::t1": {"window": {}, "target_rows": [], "contracts": {}}}
    html = _render_grid_report({"summary": summary, "daily": daily, "trades": trades, "trade_contexts": contexts,
                                "config": {"thresholds": {"max_peak_order_actions_per_minute": 20}}})
    assert 'id="grid-payload"' in html
    assert "零成交组合（默认收起）" in html
    assert "单笔模拟成交" in html
    assert "正常行情误成交过高" in html
    assert "候选事件成交" in html
    assert "查看详情" in html and "单笔模拟成交复盘" in html
    assert "W / D / S" in html
    assert "目标成交价" in html
    assert "参考合约对冲成交价" in html
    assert "Q01-L01" in html and "Q02-L01" in html


def test_trade_context_keeps_full_holding_window_and_asof_references():
    keys = [0, 5000, 10000, 15000, 20000, 25000, 30000]
    target = pd.DataFrame({
        "market_time_key": keys, "display_time": [f"09:00:{index:02d}" for index in range(len(keys))],
        "LastPrice": range(100, 107), "fair_price": range(110, 117), "interval_vwap": range(105, 112), "tick_size": 2,
    })
    reference = pd.DataFrame({
        "market_time_key": [0, 7000, 16000, 27000], "display_time": ["09:00:00", "09:00:07", "09:00:16", "09:00:27"],
        "LastPrice": [200, 201, 202, 203], "interval_vwap": [200, 201, 202, 203],
    })
    trades = pd.DataFrame([{
        "instrument": "NI2605", "scenario_id": "Q01-L01", "trade_id": "t1",
        "fill_key": 10000, "hedge_entry_key": 11000, "exit_key": 20000,
    }])
    contexts = build_grid_trade_contexts(
        trades, target, {"NI2604": reference, "NI2609": reference},
        {"fair_reference_contracts": ["NI2604", "NI2609"], "hedge_contract": "NI2604"},
    )
    context = contexts["NI2605::Q01-L01::t1"]
    assert context["window"]["start_key"] == 0
    assert context["window"]["end_key"] == 30000
    assert context["window"]["before_after_ms"] == 10000
    assert context["window"]["start_time"] == "09:00:00"
    assert context["phase_times"]["参考腿成交"] == "09:00:07"
    assert [row["LastPrice"] for row in context["target_rows"]] == list(range(100, 107))
    assert [row["last_down_ticks"] for row in context["target_rows"]] == [5.0] * 7
    assert context["contracts"]["NI2604"]["roles"] == ["合理价参考", "对冲合约"]
    assert context["contracts"]["NI2604"]["rows"][1]["关键时点"] == "目标成交、参考腿成交"
    assert context["contracts"]["NI2604"]["rows"][2]["关键时点"] == "退出"


def test_context_rows_keeps_duplicate_boundary_keys_with_sorted_slice():
    frame = pd.DataFrame({"market_time_key": [0, 10, 10, 20, 20, 30], "LastPrice": range(6)})

    rows = _context_rows(frame, 10, 20, {}, ["market_time_key", "LastPrice"])

    assert [row["market_time_key"] for row in rows] == [10, 10, 20, 20]
