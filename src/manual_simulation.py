"""事件条件的手工乌龙指回放。

这不是逐笔排队回测，也不估计全天静态挂单的真实成交率。它只回答一个更窄、
但对手工交易更诚实的问题：对于已经被检测器识别的候选事件，若限价单在事件前
已经挂出，快照证据是否显示它会先被正常波动触及、在事件时触及，以及人工完成
对冲前后的实际价格路径和两腿损益会如何。
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
from itertools import product
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

from src.tick_detector.reference_selection import (
    BASELINE_EXCLUDE_RECENT_SECONDS,
    BASELINE_MIN_PAIRS_PER_PEER,
    BASELINE_MIN_SPAN_PER_PEER_SECONDS,
    BASELINE_WINDOW_SECONDS,
    FAIR_UNCERTAINTY_LIMIT_TICKS,
    LIMIT_BUFFER_TICKS,
    MAX_REFERENCE_AGE_SECONDS,
    MIN_VALID_PEERS,
    select_reference_contracts,
)
from src.tick_detector.tick_io import (
    COMMODITY_PROFILES,
    iter_day_contract_files,
    load_contract_snapshots,
    load_daily_bounds,
    parse_contract_file,
    prepare_contract_snapshots,
)


FILL_MODELS = {
    "strict_visible",
    "visible_touch",
    "interval_upper_bound",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "events_csv": "",
    "tick_data_root": "data/tick2026",
    "daily_data_root": "data/1d_futures",
    "output_dir": "",
    "commodities": [],
    "trade_date_start": None,
    "trade_date_end": None,
    "quality_exclusions": [],
    "account_equity": 100000.0,
    "max_margin_ratio": 0.30,
    "max_single_trade_loss": 500.0,
    "default_margin_rate": 0.10,
    "margin_rate_by_commodity": {},
    "default_commission_per_lot_per_side": 0.0,
    "commission_by_commodity": {},
    "slippage_ticks": 1.0,
    "target_lots": 1,
    "hedge_lots": 1,
    "fill_model": "strict_visible",
    "order_ages_seconds": [300],
    "entry_distances_bps": [100],
    "hedge_delays_seconds": [5, 10, 30, 60],
    "exit_delays_seconds": [30, 60],
    "event_fill_window_seconds": 1,
    "max_quote_age_seconds": 3,
    "hedge_reference_index": 0,
    "max_events": None,
}

TRADE_COLUMNS = [
    "event_id",
    "trade_date",
    "event_time",
    "commodity",
    "contract",
    "event_anchor_seq",
    "event_anchor_key",
    "event_trigger_reasons",
    "event_fair_price",
    "reference_contract",
    "fill_model",
    "order_age_seconds",
    "entry_distance_bps",
    "hedge_delay_seconds",
    "exit_delay_seconds",
    "status",
    "status_detail",
    "model_settled",
    "capital_eligible",
    "risk_eligible",
    "strategy_eligible",
    "order_time_key",
    "order_fair_key",
    "order_fair_price",
    "entry_price",
    "entry_distance_effective_bps",
    "pre_event_touch_key",
    "pre_event_touch_time",
    "fill_key",
    "fill_time",
    "fill_evidence_price",
    "hedge_requested_key",
    "hedge_quote_key",
    "hedge_entry_price",
    "exit_requested_key",
    "target_exit_quote_key",
    "target_exit_price",
    "hedge_exit_quote_key",
    "hedge_exit_price",
    "target_pnl",
    "hedge_pnl",
    "gross_pnl",
    "commission",
    "net_pnl",
    "gross_margin",
    "capital_limit",
    "unhedged_mae_bps",
    "unhedged_worst_mark_pnl",
    "hedged_worst_mark_pnl",
    "observed_worst_mark_pnl",
    "observed_worst_net_pnl",
]

SUMMARY_COLUMNS = [
    "commodity",
    "contract",
    "fill_model",
    "order_age_seconds",
    "entry_distance_bps",
    "hedge_delay_seconds",
    "exit_delay_seconds",
    "candidate_event_count",
    "pre_event_touch_count",
    "no_fill_count",
    "quote_failure_count",
    "model_settled_count",
    "capital_rejected_count",
    "risk_rejected_count",
    "strategy_eligible_count",
    "pre_event_touch_rate",
    "model_fill_rate_among_clean_orders",
    "strategy_eligible_rate_among_clean_orders",
    "settled_net_pnl",
    "eligible_net_pnl",
    "eligible_win_rate",
    "median_gross_margin",
    "unhedged_mae_bps_p10",
    "unhedged_mae_bps_min",
    "observed_worst_net_pnl_p10",
    "observed_worst_net_pnl_min",
]


def load_manual_simulation_config(path: str | Path) -> dict[str, Any]:
    """读取并规范化 JSON 配置。"""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("手工模拟器配置必须是 JSON 对象")
    raw = dict(raw)
    raw["_config_path"] = str(config_path)
    return normalize_manual_simulation_config(raw)


def normalize_manual_simulation_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """给命令行覆盖后的配置补齐默认值并做安全校验。"""
    config = dict(DEFAULT_CONFIG)
    config.update(dict(raw))

    for key in ("events_csv", "tick_data_root", "daily_data_root", "output_dir"):
        value = str(config.get(key) or "").strip()
        if not value:
            raise ValueError(f"配置缺少 {key}")
        config[key] = value

    config["commodities"] = _normalized_commodities(config.get("commodities", []))
    config["trade_date_start"] = _optional_date(config.get("trade_date_start"), "trade_date_start")
    config["trade_date_end"] = _optional_date(config.get("trade_date_end"), "trade_date_end")
    if config["trade_date_start"] and config["trade_date_end"] and config["trade_date_start"] > config["trade_date_end"]:
        raise ValueError("trade_date_start 不能晚于 trade_date_end")

    config["quality_exclusions"] = _normalize_quality_exclusions(config.get("quality_exclusions", []))
    config["account_equity"] = _positive_number(config.get("account_equity"), "account_equity")
    config["max_margin_ratio"] = _fraction(config.get("max_margin_ratio"), "max_margin_ratio")
    config["max_single_trade_loss"] = _nonnegative_number(config.get("max_single_trade_loss"), "max_single_trade_loss")
    config["default_margin_rate"] = _positive_number(config.get("default_margin_rate"), "default_margin_rate")
    config["margin_rate_by_commodity"] = _normalize_number_map(
        config.get("margin_rate_by_commodity", {}),
        "margin_rate_by_commodity",
        allow_zero=False,
    )
    config["default_commission_per_lot_per_side"] = _nonnegative_number(
        config.get("default_commission_per_lot_per_side"),
        "default_commission_per_lot_per_side",
    )
    config["commission_by_commodity"] = _normalize_number_map(
        config.get("commission_by_commodity", {}),
        "commission_by_commodity",
        allow_zero=True,
    )
    config["slippage_ticks"] = _nonnegative_number(config.get("slippage_ticks"), "slippage_ticks")
    config["target_lots"] = _positive_int(config.get("target_lots"), "target_lots")
    config["hedge_lots"] = _positive_int(config.get("hedge_lots"), "hedge_lots")
    config["fill_model"] = str(config.get("fill_model") or "").strip()
    if config["fill_model"] not in FILL_MODELS:
        allowed = ", ".join(sorted(FILL_MODELS))
        raise ValueError(f"fill_model 必须是以下之一: {allowed}")

    config["order_ages_seconds"] = _number_list(config.get("order_ages_seconds"), "order_ages_seconds", allow_zero=True)
    config["entry_distances_bps"] = _number_list(config.get("entry_distances_bps"), "entry_distances_bps")
    config["hedge_delays_seconds"] = _number_list(config.get("hedge_delays_seconds"), "hedge_delays_seconds", allow_zero=True)
    config["exit_delays_seconds"] = _number_list(config.get("exit_delays_seconds"), "exit_delays_seconds")
    config["event_fill_window_seconds"] = _nonnegative_number(
        config.get("event_fill_window_seconds"), "event_fill_window_seconds"
    )
    config["max_quote_age_seconds"] = _positive_number(
        config.get("max_quote_age_seconds"), "max_quote_age_seconds"
    )
    config["hedge_reference_index"] = _nonnegative_int(
        config.get("hedge_reference_index"), "hedge_reference_index"
    )
    max_events = config.get("max_events")
    config["max_events"] = None if max_events in (None, "") else _positive_int(max_events, "max_events")
    return config


def build_scenarios(config: Mapping[str, Any]) -> tuple[list[dict[str, float]], int]:
    """笛卡尔组合参数；退出早于人工对冲的组合没有可执行含义，直接跳过。"""
    scenarios: list[dict[str, float]] = []
    skipped_invalid_timing = 0
    for order_age, entry_distance, hedge_delay, exit_delay in product(
        config["order_ages_seconds"],
        config["entry_distances_bps"],
        config["hedge_delays_seconds"],
        config["exit_delays_seconds"],
    ):
        if exit_delay < hedge_delay:
            skipped_invalid_timing += 1
            continue
        scenarios.append(
            {
                "order_age_seconds": float(order_age),
                "entry_distance_bps": float(entry_distance),
                "hedge_delay_seconds": float(hedge_delay),
                "exit_delay_seconds": float(exit_delay),
            }
        )
    if not scenarios:
        raise ValueError("没有可执行场景：每个 exit_delays_seconds 必须大于等于 hedge_delays_seconds")
    return scenarios, skipped_invalid_timing


def run_manual_simulation(config: Mapping[str, Any]) -> dict[str, Any]:
    """运行事件条件回放，返回明细、汇总、排除清单和提示。"""
    config = normalize_manual_simulation_config(config)
    scenarios, skipped_invalid_timing = build_scenarios(config)
    events, excluded_events = _load_selected_events(config)
    warnings = _build_warnings(config, skipped_invalid_timing, len(events), excluded_events)
    records: list[dict[str, Any]] = []

    for (trade_date, commodity), day_events in events.groupby(["__trade_date", "__commodity"], sort=True):
        try:
            frames = _load_commodity_frames(config, trade_date, commodity)
        except FileNotFoundError as exc:
            records.extend(_failure_records(day_events, scenarios, config, "tick_day_missing", str(exc)))
            continue
        except Exception as exc:  # 单日源文件异常不能让其它品种的研究停掉。
            records.extend(_failure_records(day_events, scenarios, config, "tick_day_load_failed", str(exc)))
            continue

        if not frames:
            records.extend(_failure_records(day_events, scenarios, config, "commodity_data_missing", "当日未找到该品种合约"))
            continue

        for contract, contract_events in day_events.groupby("__contract", sort=True):
            if contract not in frames:
                records.extend(_failure_records(contract_events, scenarios, config, "target_contract_missing", "目标合约不在原始 tick 中"))
                continue
            try:
                # 只计算每个“事件锚点 - 挂单年龄”所需的合理价，不重建整天每一帧的
                # noise/fair 链。这个回放不需要检测器的全帧评分，重建它会让改一个参数
                # 的成本高到无法日常使用。
                placement_fairs = _build_placement_fairs(frames, contract, contract_events, config)
            except Exception as exc:  # 数据异常应落入结果，而不是悄悄删掉样本。
                records.extend(
                    _failure_records(contract_events, scenarios, config, "fair_build_failed", str(exc))
                )
                continue
            for _, event in contract_events.iterrows():
                for scenario in scenarios:
                    records.append(simulate_event(event, frames[contract], frames, config, scenario, placement_fairs))

    trades = pd.DataFrame(records, columns=TRADE_COLUMNS)
    summary = build_summary(trades)
    return {
        "config": config,
        "trades": trades,
        "summary": summary,
        "excluded_events": excluded_events,
        "warnings": warnings,
    }


def simulate_event(
    event: Mapping[str, Any],
    target_frame: pd.DataFrame,
    reference_frames: Mapping[str, pd.DataFrame],
    config: Mapping[str, Any],
    scenario: Mapping[str, float],
    placement_fairs: Mapping[tuple[int, int], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """模拟一个事件和一个参数组合，供主流程与单元测试共同使用。"""
    record = _base_record(event, scenario, config)
    anchor_seq = _safe_int(_event_value(event, "事件锚点结束序号"))
    if anchor_seq is None or "snapshot_seq_end" not in target_frame.columns:
        return _set_status(record, "anchor_not_found", "候选 CSV 缺少可匹配的事件锚点序号")

    seq = pd.to_numeric(target_frame["snapshot_seq_end"], errors="coerce")
    anchor_rows = target_frame.loc[seq == anchor_seq]
    if anchor_rows.empty:
        return _set_status(record, "anchor_not_found", "原始 tick 中没有对应的 snapshot_seq_end")
    anchor_row = anchor_rows.iloc[-1]
    anchor_key = _safe_int(anchor_row.get("market_time_key"))
    if anchor_key is None:
        return _set_status(record, "anchor_not_found", "事件锚点没有 market_time_key")
    record["event_anchor_key"] = anchor_key

    order_key = anchor_key - int(round(float(scenario["order_age_seconds"]) * 1000))
    record["order_time_key"] = order_key
    fair_info = placement_fairs.get((anchor_seq, order_key)) if placement_fairs is not None else None
    if fair_info is not None:
        fair_price = _finite_number(fair_info.get("fair_price"))
        fair_reliable = _truthy(fair_info.get("fair_price_reliable"), default=False)
        fair_detail = str(fair_info.get("reason", ""))
        fair_key = _safe_int(fair_info.get("fair_key"))
    else:
        # 保留此分支，供轻量单元测试和独立调用直接传入已带 fair_price 的 frame。
        fair_row = _asof_row(target_frame, order_key, float(config["max_quote_age_seconds"]))
        fair_price = _finite_number(fair_row.get("fair_price")) if fair_row is not None else None
        fair_reliable = _truthy(fair_row.get("fair_price_reliable"), default=True) if fair_row is not None else False
        fair_detail = ""
        fair_key = _safe_int(fair_row.get("market_time_key")) if fair_row is not None else None
    record["order_fair_key"] = fair_key if fair_key is not None else np.nan
    if fair_price is None or fair_price <= 0 or not fair_reliable:
        suffix = f"（{fair_detail}）" if fair_detail else ""
        return _set_status(record, "order_fair_missing", f"挂单时刻没有可靠合理价{suffix}")

    target_tick = _frame_tick_size(target_frame, str(record["commodity"]))
    target_multiplier = _frame_multiplier(target_frame, str(record["commodity"]))
    if target_tick is None or target_multiplier is None:
        return _set_status(record, "target_metadata_missing", "目标合约缺少最小变动价位或乘数")
    entry_price = _floor_to_tick(fair_price * (1.0 - float(scenario["entry_distance_bps"]) / 10000.0), target_tick)
    if entry_price <= 0:
        return _set_status(record, "entry_price_invalid", "按挂单距离换算后的限价无效")
    record["order_fair_price"] = fair_price
    record["entry_price"] = entry_price
    record["entry_distance_effective_bps"] = (fair_price - entry_price) / fair_price * 10000.0

    pre_touch = _first_fill_row(
        target_frame,
        order_key,
        anchor_key,
        entry_price,
        str(config["fill_model"]),
        end_inclusive=False,
    )
    if pre_touch is not None:
        record["pre_event_touch_key"] = int(pre_touch["market_time_key"])
        record["pre_event_touch_time"] = str(pre_touch.get("display_time", ""))
        record["fill_evidence_price"] = _fill_evidence_price(pre_touch, str(config["fill_model"]))
        return _set_status(record, "pre_event_touch", "挂单在候选事件前已被同一成交模型触及")

    fill_window_end = anchor_key + int(round(float(config["event_fill_window_seconds"]) * 1000))
    fill_row = _first_fill_row(
        target_frame,
        anchor_key,
        fill_window_end,
        entry_price,
        str(config["fill_model"]),
        end_inclusive=True,
    )
    if fill_row is None:
        return _set_status(record, "no_fill", "候选事件窗口内没有满足成交模型的触及证据")

    fill_key = int(fill_row["market_time_key"])
    record["fill_key"] = fill_key
    record["fill_time"] = str(fill_row.get("display_time", ""))
    record["fill_evidence_price"] = _fill_evidence_price(fill_row, str(config["fill_model"]))

    reference_contract = _select_reference_contract(event, int(config["hedge_reference_index"]))
    record["reference_contract"] = reference_contract or ""
    if not reference_contract or reference_contract not in reference_frames:
        return _set_status(record, "reference_contract_missing", "事件记录的参考合约不在原始 tick 中")
    hedge_frame = reference_frames[reference_contract]
    hedge_tick = _frame_tick_size(hedge_frame, str(record["commodity"]))
    hedge_multiplier = _frame_multiplier(hedge_frame, str(record["commodity"]))
    if hedge_tick is None or hedge_multiplier is None:
        return _set_status(record, "hedge_metadata_missing", "参考合约缺少最小变动价位或乘数")

    hedge_requested_key = fill_key + int(round(float(scenario["hedge_delay_seconds"]) * 1000))
    exit_requested_key = fill_key + int(round(float(scenario["exit_delay_seconds"]) * 1000))
    record["hedge_requested_key"] = hedge_requested_key
    record["exit_requested_key"] = exit_requested_key
    hedge_quote = _executable_quote(hedge_frame, hedge_requested_key, "bid", float(config["max_quote_age_seconds"]))
    if hedge_quote is None:
        return _set_status(record, "hedge_quote_missing", "人工对冲时刻没有新鲜、可卖出的参考合约买一")
    hedge_entry_price = hedge_quote["price"] - float(config["slippage_ticks"]) * hedge_tick
    if hedge_entry_price <= 0:
        return _set_status(record, "hedge_quote_missing", "滑点后的参考卖出价无效")
    record["hedge_quote_key"] = hedge_quote["key"]
    record["hedge_entry_price"] = hedge_entry_price

    unhedged_bps, unhedged_pnl = _unhedged_mae(
        target_frame,
        entry_price,
        fill_key,
        hedge_requested_key,
        target_tick,
        target_multiplier,
        int(config["target_lots"]),
        float(config["max_quote_age_seconds"]),
        float(config["slippage_ticks"]),
    )
    record["unhedged_mae_bps"] = unhedged_bps
    record["unhedged_worst_mark_pnl"] = unhedged_pnl

    target_exit_quote = _executable_quote(target_frame, exit_requested_key, "bid", float(config["max_quote_age_seconds"]))
    if target_exit_quote is None:
        return _set_status(record, "target_exit_quote_missing", "退出时刻没有新鲜、可卖出的目标合约买一")
    target_exit_price = target_exit_quote["price"] - float(config["slippage_ticks"]) * target_tick
    if target_exit_price <= 0:
        return _set_status(record, "target_exit_quote_missing", "滑点后的目标退出价无效")
    record["target_exit_quote_key"] = target_exit_quote["key"]
    record["target_exit_price"] = target_exit_price

    hedge_exit_quote = _executable_quote(hedge_frame, exit_requested_key, "ask", float(config["max_quote_age_seconds"]))
    if hedge_exit_quote is None:
        return _set_status(record, "hedge_exit_quote_missing", "退出时刻没有新鲜、可买回的参考合约卖一")
    hedge_exit_price = hedge_exit_quote["price"] + float(config["slippage_ticks"]) * hedge_tick
    record["hedge_exit_quote_key"] = hedge_exit_quote["key"]
    record["hedge_exit_price"] = hedge_exit_price

    target_lots = int(config["target_lots"])
    hedge_lots = int(config["hedge_lots"])
    target_pnl = (target_exit_price - entry_price) * target_multiplier * target_lots
    hedge_pnl = (hedge_entry_price - hedge_exit_price) * hedge_multiplier * hedge_lots
    target_commission = _commodity_number(config, "commission_by_commodity", "default_commission_per_lot_per_side", str(record["commodity"]))
    hedge_commission = _commodity_number(config, "commission_by_commodity", "default_commission_per_lot_per_side", str(record["commodity"]))
    commission = 2.0 * (target_lots * target_commission + hedge_lots * hedge_commission)
    gross_pnl = target_pnl + hedge_pnl
    net_pnl = gross_pnl - commission
    record["target_pnl"] = target_pnl
    record["hedge_pnl"] = hedge_pnl
    record["gross_pnl"] = gross_pnl
    record["commission"] = commission
    record["net_pnl"] = net_pnl

    hedged_worst_pnl = _hedged_worst_mark_pnl(
        target_frame,
        hedge_frame,
        entry_price,
        hedge_entry_price,
        hedge_requested_key,
        exit_requested_key,
        target_tick,
        hedge_tick,
        target_multiplier,
        hedge_multiplier,
        target_lots,
        hedge_lots,
        float(config["max_quote_age_seconds"]),
        float(config["slippage_ticks"]),
    )
    record["hedged_worst_mark_pnl"] = hedged_worst_pnl
    worst_candidates = [value for value in (unhedged_pnl, hedged_worst_pnl) if _finite_number(value) is not None]
    if worst_candidates:
        observed_worst = min(float(value) for value in worst_candidates)
        record["observed_worst_mark_pnl"] = observed_worst
        record["observed_worst_net_pnl"] = observed_worst - commission

    margin_rate = _commodity_number(config, "margin_rate_by_commodity", "default_margin_rate", str(record["commodity"]))
    gross_margin = (
        entry_price * target_multiplier * target_lots * margin_rate
        + hedge_entry_price * hedge_multiplier * hedge_lots * margin_rate
    )
    capital_limit = float(config["account_equity"]) * float(config["max_margin_ratio"])
    record["gross_margin"] = gross_margin
    record["capital_limit"] = capital_limit
    record["model_settled"] = True
    record["capital_eligible"] = gross_margin <= capital_limit
    observed_worst_net = _finite_number(record["observed_worst_net_pnl"])
    record["risk_eligible"] = (
        observed_worst_net is not None
        and observed_worst_net >= -float(config["max_single_trade_loss"])
    )
    record["strategy_eligible"] = bool(record["capital_eligible"] and record["risk_eligible"])
    if not record["capital_eligible"]:
        return _set_status(record, "capital_rejected", "两腿全额保证金超过账户可用比例", keep_flags=True)
    if not record["risk_eligible"]:
        return _set_status(record, "risk_rejected", "本事件的观察期最坏净盯市损失超过单笔预算", keep_flags=True)
    return _set_status(record, "event_fill", "事件窗口内触及，且完成两腿退出", keep_flags=True)


def build_summary(trades: pd.DataFrame) -> pd.DataFrame:
    """按目标合约和参数组合汇总；不把事件条件命中率伪装成全天成交率。"""
    if trades.empty:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)
    group_columns = [
        "commodity",
        "contract",
        "fill_model",
        "order_age_seconds",
        "entry_distance_bps",
        "hedge_delay_seconds",
        "exit_delay_seconds",
    ]
    rows: list[dict[str, Any]] = []
    for key, group in trades.groupby(group_columns, dropna=False, sort=True):
        result = dict(zip(group_columns, key, strict=True))
        statuses = group["status"].fillna("")
        settled = group.loc[group["model_settled"].fillna(False).astype(bool)]
        eligible = group.loc[group["strategy_eligible"].fillna(False).astype(bool)]
        clean_orders = len(group) - int((statuses == "pre_event_touch").sum())
        result.update(
            {
                "candidate_event_count": int(group["event_id"].nunique()),
                "pre_event_touch_count": int((statuses == "pre_event_touch").sum()),
                "no_fill_count": int((statuses == "no_fill").sum()),
                "quote_failure_count": int(statuses.str.contains("quote_missing|exit_quote_missing", regex=True).sum()),
                "model_settled_count": int(len(settled)),
                "capital_rejected_count": int((statuses == "capital_rejected").sum()),
                "risk_rejected_count": int((statuses == "risk_rejected").sum()),
                "strategy_eligible_count": int(len(eligible)),
                "pre_event_touch_rate": _safe_divide(int((statuses == "pre_event_touch").sum()), len(group)),
                "model_fill_rate_among_clean_orders": _safe_divide(len(settled), clean_orders),
                "strategy_eligible_rate_among_clean_orders": _safe_divide(len(eligible), clean_orders),
                "settled_net_pnl": _sum_numeric(settled.get("net_pnl", pd.Series(dtype=float))),
                "eligible_net_pnl": _sum_numeric(eligible.get("net_pnl", pd.Series(dtype=float))),
                "eligible_win_rate": _safe_divide(
                    int((pd.to_numeric(eligible.get("net_pnl", pd.Series(dtype=float)), errors="coerce") > 0).sum()),
                    len(eligible),
                ),
                "median_gross_margin": _percentile(settled.get("gross_margin", pd.Series(dtype=float)), 50),
                "unhedged_mae_bps_p10": _percentile(settled.get("unhedged_mae_bps", pd.Series(dtype=float)), 10),
                "unhedged_mae_bps_min": _minimum(settled.get("unhedged_mae_bps", pd.Series(dtype=float))),
                "observed_worst_net_pnl_p10": _percentile(settled.get("observed_worst_net_pnl", pd.Series(dtype=float)), 10),
                "observed_worst_net_pnl_min": _minimum(settled.get("observed_worst_net_pnl", pd.Series(dtype=float))),
            }
        )
        rows.append(result)
    summary = pd.DataFrame(rows, columns=SUMMARY_COLUMNS)
    return summary.sort_values(
        ["strategy_eligible_count", "eligible_net_pnl", "candidate_event_count"],
        ascending=[False, False, False],
        kind="stable",
    ).reset_index(drop=True)


def write_manual_simulation_outputs(result: Mapping[str, Any]) -> dict[str, str]:
    """写出可直接查看的 CSV、HTML 和本次实际生效的参数。"""
    config = dict(result["config"])
    output_dir = Path(str(config["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_path = output_dir / "manual_simulated_trades.csv"
    summary_path = output_dir / "manual_simulation_summary.csv"
    excluded_path = output_dir / "manual_simulation_excluded_events.csv"
    config_path = output_dir / "run_config.json"
    report_path = output_dir / "manual_simulation_report.html"

    result["trades"].to_csv(trades_path, index=False, encoding="utf-8-sig")
    result["summary"].to_csv(summary_path, index=False, encoding="utf-8-sig")
    result["excluded_events"].to_csv(excluded_path, index=False, encoding="utf-8-sig")
    config_payload = {
        "config": {key: value for key, value in config.items() if not key.startswith("_")},
        "warnings": list(result["warnings"]),
        "event_count_after_filters": int(result["trades"]["event_id"].nunique()) if not result["trades"].empty else 0,
    }
    config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_report(result), encoding="utf-8")
    return {
        "trades": str(trades_path),
        "summary": str(summary_path),
        "excluded_events": str(excluded_path),
        "config": str(config_path),
        "report": str(report_path),
    }


def _load_selected_events(config: Mapping[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame]:
    events_path = Path(str(config["events_csv"]))
    if not events_path.is_file():
        raise FileNotFoundError(f"候选事件文件不存在: {events_path}")
    events = pd.read_csv(events_path)
    required = {"交易日", "品种", "合约", "事件锚点结束序号"}
    missing = sorted(required - set(events.columns))
    if missing:
        raise ValueError(f"候选事件 CSV 缺少字段: {', '.join(missing)}")
    events = events.copy()
    events["__trade_date"] = events["交易日"].map(lambda value: _optional_date(value, "交易日"))
    events["__commodity"] = events["品种"].astype(str).str.upper().str.strip()
    events["__contract"] = events["合约"].astype(str).str.upper().str.strip()
    events["__event_id"] = events.apply(_event_id, axis=1)
    selected = events.loc[events["__trade_date"].notna() & events["__commodity"].ne("") & events["__contract"].ne("")].copy()
    if config["commodities"]:
        selected = selected.loc[selected["__commodity"].isin(config["commodities"])]
    if config["trade_date_start"]:
        selected = selected.loc[selected["__trade_date"] >= config["trade_date_start"]]
    if config["trade_date_end"]:
        selected = selected.loc[selected["__trade_date"] <= config["trade_date_end"]]

    if "异常方向" not in selected:
        selected["__event_direction"] = "down"
    else:
        direction = selected["异常方向"].astype("string").str.strip().str.lower().replace("", "down").fillna("down")
        if (~direction.isin(("down", "up"))).any():
            raise ValueError("异常方向只允许 down 或 up")
        selected["__event_direction"] = direction
    excluded_direction = selected.loc[selected["__event_direction"].eq("up")].copy()
    if not excluded_direction.empty:
        excluded_direction["exclusion_reason"] = "unsupported_event_direction_up"
    selected = selected.loc[selected["__event_direction"].eq("down")].copy()

    exclusion_set = {(item["trade_date"], item["commodity"]) for item in config["quality_exclusions"]}
    exclusion_mask = selected.apply(
        lambda row: (str(row["__trade_date"]), str(row["__commodity"])) in exclusion_set,
        axis=1,
    )
    excluded = selected.loc[exclusion_mask].copy()
    if not excluded.empty:
        excluded["exclusion_reason"] = "quality_exclusion"
    selected = selected.loc[~exclusion_mask].copy()
    excluded = pd.concat([excluded_direction, excluded], ignore_index=True)
    sort_columns = ["__trade_date"] + (["事件时间"] if "事件时间" in selected.columns else []) + ["__event_id"]
    selected = selected.sort_values(sort_columns, kind="stable")
    if config["max_events"] is not None:
        selected = selected.head(int(config["max_events"]))
    return selected.reset_index(drop=True), excluded.reset_index(drop=True)


def _load_commodity_frames(config: Mapping[str, Any], trade_date: str, commodity: str) -> dict[str, pd.DataFrame]:
    day_path = _resolve_tick_day_path(Path(str(config["tick_data_root"])), trade_date)
    daily_bounds = load_daily_bounds(trade_date, daily_root=str(config["daily_data_root"]))
    frames: dict[str, pd.DataFrame] = {}
    for contract_file in iter_day_contract_files(day_path):
        info = parse_contract_file(contract_file.file_name, None)
        if info.commodity != commodity or info.contract is None:
            continue
        raw = load_contract_snapshots(contract_file)
        if raw.empty:
            continue
        prepared = prepare_contract_snapshots(raw, daily_bounds=daily_bounds)
        if not prepared.empty:
            frames[info.contract] = prepared
    return frames


def _resolve_tick_day_path(tick_root: Path, trade_date: str) -> Path:
    candidates = [
        tick_root / trade_date[:6] / f"{trade_date}.zip",
        tick_root / trade_date[:6] / trade_date,
        tick_root / f"{trade_date}.zip",
        tick_root / trade_date,
    ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"找不到 {trade_date} 的 tick 文件，尝试过: {', '.join(str(path) for path in candidates)}")


def _build_placement_fairs(
    day_frames: Mapping[str, pd.DataFrame],
    target_contract: str,
    events: pd.DataFrame,
    config: Mapping[str, Any],
) -> dict[tuple[int, int], dict[str, Any]]:
    """仅计算所有挂单时点的合理价，复用检测器 Pass 1 的基线/参考规则。

    ``attach_fair_price_metrics`` 同时会为目标合约当天的每一帧构造 noise history；
    手工回放只需要订单挂出时的价格，逐帧重建会把少量事件放大成整天 O(N × 窗口)
    工作量。这里保留 Pass 1 的参考合约、5 分钟 basis、3 秒 asof、spread 门槛和
    不确定性规则，但只对实际请求的时点计算。
    """
    target_frame = day_frames[target_contract]
    commodity = str(target_frame["commodity"].iloc[0])
    tick_size = _frame_tick_size(target_frame, commodity)
    if tick_size is None:
        raise ValueError(f"目标合约缺少 tick_size: {target_contract}")
    references = select_reference_contracts(dict(day_frames), target_contract)
    reference_frames = {code: day_frames[code] for code in references if code in day_frames}
    anchor_sequences = pd.to_numeric(target_frame["snapshot_seq_end"], errors="coerce")
    lookup: dict[tuple[int, int], dict[str, Any]] = {}
    for _, event in events.iterrows():
        anchor_seq = _safe_int(event.get("事件锚点结束序号"))
        if anchor_seq is None:
            continue
        anchor_rows = target_frame.loc[anchor_sequences == anchor_seq]
        if anchor_rows.empty:
            continue
        anchor_key = _safe_int(anchor_rows.iloc[-1].get("market_time_key"))
        if anchor_key is None:
            continue
        for order_age in config["order_ages_seconds"]:
            order_key = anchor_key - int(round(float(order_age) * 1000))
            lookup[(anchor_seq, order_key)] = _point_fair_price(
                target_frame,
                reference_frames,
                set(references[:3]),
                tick_size,
                order_key,
                float(config["max_quote_age_seconds"]),
            )
    return lookup


def _point_fair_price(
    target_frame: pd.DataFrame,
    reference_frames: Mapping[str, pd.DataFrame],
    top_volume_references: set[str],
    tick_size: float,
    requested_key: int,
    max_target_age_seconds: float,
) -> dict[str, Any]:
    """在一个订单时点计算检测器 Pass 1 同口径的 fair_price。"""
    target_row = _asof_row(target_frame, requested_key, max_target_age_seconds)
    if target_row is None:
        return _unreliable_fair(None, "target_snapshot_stale")
    target_key = _safe_int(target_row.get("market_time_key"))
    if target_key is None:
        return _unreliable_fair(None, "target_time_missing")
    keys = pd.to_numeric(target_frame["market_time_key"], errors="coerce").to_numpy(dtype=float)
    if not len(keys):
        return _unreliable_fair(target_key, "target_frame_empty")
    window_start = target_key - BASELINE_WINDOW_SECONDS * 1000
    window_end = target_key - BASELINE_EXCLUDE_RECENT_SECONDS * 1000
    lo = int(np.searchsorted(keys, window_start, side="left"))
    hi = int(np.searchsorted(keys, window_end, side="right"))
    if hi - lo < BASELINE_MIN_PAIRS_PER_PEER:
        return _unreliable_fair(target_key, "target_baseline_too_short")

    target_window = target_frame.iloc[lo:hi]
    target_window_keys = pd.to_numeric(target_window["market_time_key"], errors="coerce").to_numpy(dtype=np.int64)
    target_mids = _mid_prices(target_window)
    target_valid = _base_quote_valid_mask(target_window, tick_size)
    target_spreads = _spread_ticks(target_window, tick_size)
    valid_target_spreads = target_spreads[target_valid & np.isfinite(target_spreads)]
    if len(valid_target_spreads) < BASELINE_MIN_PAIRS_PER_PEER:
        return _unreliable_fair(target_key, "target_spread_baseline_missing")
    target_spread_limit = float(np.percentile(valid_target_spreads, 95)) + 1.0

    fair_candidates: list[float] = []
    valid_references: list[str] = []
    for code, peer_frame in reference_frames.items():
        window_peer = _peer_asof(peer_frame, target_window_keys, tick_size)
        valid_peer_spreads = window_peer["spread_ticks"][
            window_peer["valid"] & np.isfinite(window_peer["spread_ticks"])
        ]
        if len(valid_peer_spreads) < BASELINE_MIN_PAIRS_PER_PEER:
            continue
        peer_spread_limit = float(np.percentile(valid_peer_spreads, 95)) + 1.0
        current_peer = _peer_asof(peer_frame, np.array([target_key], dtype=np.int64), tick_size)
        if not bool(current_peer["valid"][0]):
            continue
        current_mid = float(current_peer["mid"][0])
        current_spread = float(current_peer["spread_ticks"][0])
        if not np.isfinite(current_mid) or not np.isfinite(current_spread) or current_spread > peer_spread_limit:
            continue
        diffs = target_mids - window_peer["mid"]
        valid_pairs = (
            target_valid
            & np.isfinite(target_spreads)
            & (target_spreads <= target_spread_limit)
            & window_peer["valid"]
            & np.isfinite(window_peer["spread_ticks"])
            & (window_peer["spread_ticks"] <= peer_spread_limit)
            & np.isfinite(diffs)
        )
        if int(valid_pairs.sum()) < BASELINE_MIN_PAIRS_PER_PEER:
            continue
        paired_keys = window_peer["key"][valid_pairs & np.isfinite(window_peer["key"])]
        if len(paired_keys) < 2:
            continue
        if int(paired_keys.max()) - int(paired_keys.min()) < BASELINE_MIN_SPAN_PER_PEER_SECONDS * 1000:
            continue
        basis = float(np.median(diffs[valid_pairs]))
        fair_candidates.append(current_mid + basis)
        valid_references.append(code)

    if len(valid_references) < MIN_VALID_PEERS or not any(code in top_volume_references for code in valid_references):
        return _unreliable_fair(target_key, "insufficient_peers")
    fair_values = np.array(fair_candidates, dtype=float)
    fair_price = float(np.median(fair_values))
    uncertainty_ticks = _mad_sigma(fair_values) / tick_size
    if uncertainty_ticks > FAIR_UNCERTAINTY_LIMIT_TICKS:
        return _unreliable_fair(target_key, "fair_uncertainty_exceeded")
    return {
        "fair_key": target_key,
        "fair_price": fair_price,
        "fair_price_reliable": True,
        "fair_uncertainty_ticks": uncertainty_ticks,
        "reason": "",
    }


def _peer_asof(peer_frame: pd.DataFrame, target_keys: np.ndarray, tick_size: float) -> dict[str, np.ndarray]:
    """把参考合约以 detector 的 3 秒 asof 规则对齐到给定的目标时点。"""
    frame = peer_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
    if frame.empty:
        length = len(target_keys)
        return {
            "mid": np.full(length, np.nan),
            "spread_ticks": np.full(length, np.nan),
            "key": np.full(length, np.nan),
            "valid": np.zeros(length, dtype=bool),
        }
    peer_keys = pd.to_numeric(frame["market_time_key"], errors="coerce").to_numpy(dtype=np.int64)
    positions = np.searchsorted(peer_keys, target_keys, side="right") - 1
    has_position = positions >= 0
    safe_positions = np.clip(positions, 0, len(peer_keys) - 1)
    matched = frame.iloc[safe_positions].reset_index(drop=True)
    mids = _mid_prices(matched)
    spreads = _spread_ticks(matched, tick_size)
    base_valid = _base_quote_valid_mask(matched, tick_size)
    ages = target_keys - peer_keys[safe_positions]
    valid = has_position & (ages <= MAX_REFERENCE_AGE_SECONDS * 1000) & base_valid
    return {
        "mid": np.where(valid, mids, np.nan),
        "spread_ticks": np.where(valid, spreads, np.nan),
        "key": np.where(valid, peer_keys[safe_positions].astype(float), np.nan),
        "valid": valid,
    }


def _base_quote_valid_mask(frame: pd.DataFrame, tick_size: float) -> np.ndarray:
    bids = pd.to_numeric(frame.get("BidPrice1", pd.Series(index=frame.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
    asks = pd.to_numeric(frame.get("AskPrice1", pd.Series(index=frame.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
    mids = _mid_prices(frame)
    tradable = frame.get("is_tradable_session", pd.Series(True, index=frame.index)).fillna(False).to_numpy(dtype=bool)
    upper = pd.to_numeric(frame.get("UpperLimitPrice", pd.Series(np.inf, index=frame.index)), errors="coerce").to_numpy(dtype=float)
    lower = pd.to_numeric(frame.get("LowerLimitPrice", pd.Series(-np.inf, index=frame.index)), errors="coerce").to_numpy(dtype=float)
    return (
        tradable
        & (bids > 0)
        & (asks > 0)
        & (asks >= bids)
        & np.isfinite(mids)
        & (mids > 0)
        & ~((upper > 0) & (mids >= upper - LIMIT_BUFFER_TICKS * tick_size))
        & ~((lower > 0) & (mids <= lower + LIMIT_BUFFER_TICKS * tick_size))
    )


def _mid_prices(frame: pd.DataFrame) -> np.ndarray:
    if "mid_price" in frame.columns:
        return pd.to_numeric(frame["mid_price"], errors="coerce").to_numpy(dtype=float)
    bids = pd.to_numeric(frame.get("BidPrice1", pd.Series(index=frame.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
    asks = pd.to_numeric(frame.get("AskPrice1", pd.Series(index=frame.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
    return np.where((bids > 0) & (asks > 0) & (asks >= bids), (bids + asks) / 2.0, np.nan)


def _spread_ticks(frame: pd.DataFrame, tick_size: float) -> np.ndarray:
    bids = pd.to_numeric(frame.get("BidPrice1", pd.Series(index=frame.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
    asks = pd.to_numeric(frame.get("AskPrice1", pd.Series(index=frame.index, dtype=float)), errors="coerce").to_numpy(dtype=float)
    return (asks - bids) / tick_size


def _unreliable_fair(fair_key: int | None, reason: str) -> dict[str, Any]:
    return {
        "fair_key": fair_key,
        "fair_price": np.nan,
        "fair_price_reliable": False,
        "fair_uncertainty_ticks": np.nan,
        "reason": reason,
    }


def _mad_sigma(values: np.ndarray) -> float:
    median = float(np.median(values))
    return 1.4826 * float(np.median(np.abs(values - median)))


def _failure_records(
    events: pd.DataFrame,
    scenarios: list[dict[str, float]],
    config: Mapping[str, Any],
    status: str,
    detail: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for _, event in events.iterrows():
        for scenario in scenarios:
            records.append(_set_status(_base_record(event, scenario, config), status, detail))
    return records


def _base_record(event: Mapping[str, Any], scenario: Mapping[str, float], config: Mapping[str, Any]) -> dict[str, Any]:
    record: dict[str, Any] = {column: np.nan for column in TRADE_COLUMNS}
    commodity = str(_event_value(event, "__commodity", _event_value(event, "品种", ""))).upper().strip()
    contract = str(_event_value(event, "__contract", _event_value(event, "合约", ""))).upper().strip()
    anchor_seq = _safe_int(_event_value(event, "事件锚点结束序号"))
    record.update(
        {
            "event_id": str(_event_value(event, "__event_id", _event_id(event))),
            "trade_date": _optional_date(_event_value(event, "__trade_date", _event_value(event, "交易日")), "交易日") or "",
            "event_time": str(_event_value(event, "事件时间", "")),
            "commodity": commodity,
            "contract": contract,
            "event_anchor_seq": anchor_seq if anchor_seq is not None else np.nan,
            "event_trigger_reasons": str(_event_value(event, "触发原因", "")),
            "event_fair_price": _finite_number(_event_value(event, "合理价")),
            "fill_model": str(config["fill_model"]),
            "order_age_seconds": float(scenario["order_age_seconds"]),
            "entry_distance_bps": float(scenario["entry_distance_bps"]),
            "hedge_delay_seconds": float(scenario["hedge_delay_seconds"]),
            "exit_delay_seconds": float(scenario["exit_delay_seconds"]),
            "status": "",
            "status_detail": "",
            "model_settled": False,
            "capital_eligible": False,
            "risk_eligible": False,
            "strategy_eligible": False,
        }
    )
    return record


def _set_status(record: dict[str, Any], status: str, detail: str, *, keep_flags: bool = False) -> dict[str, Any]:
    record["status"] = status
    record["status_detail"] = detail
    if not keep_flags:
        record["model_settled"] = bool(record.get("model_settled", False))
    return record


def _asof_row(frame: pd.DataFrame, requested_key: int, max_age_seconds: float) -> pd.Series | None:
    if frame.empty or "market_time_key" not in frame.columns:
        return None
    keys = pd.to_numeric(frame["market_time_key"], errors="coerce").to_numpy(dtype=float)
    idx = int(np.searchsorted(keys, requested_key, side="right")) - 1
    if idx < 0 or not np.isfinite(keys[idx]):
        return None
    if requested_key - int(keys[idx]) > int(round(max_age_seconds * 1000)):
        return None
    return frame.iloc[idx]


def _executable_quote(
    frame: pd.DataFrame,
    requested_key: int,
    side: str,
    max_age_seconds: float,
) -> dict[str, float] | None:
    row = _asof_row(frame, requested_key, max_age_seconds)
    if row is None:
        return None
    if not _truthy(row.get("is_tradable_session"), default=True):
        return None
    field = "BidPrice1" if side == "bid" else "AskPrice1"
    price = _finite_number(row.get(field))
    key = _safe_int(row.get("market_time_key"))
    if price is None or price <= 0 or key is None:
        return None
    return {"price": price, "key": float(key)}


def _first_fill_row(
    frame: pd.DataFrame,
    start_key: int,
    end_key: int,
    entry_price: float,
    fill_model: str,
    *,
    end_inclusive: bool,
) -> pd.Series | None:
    keys = pd.to_numeric(frame["market_time_key"], errors="coerce")
    mask = (keys >= start_key) & (keys <= end_key if end_inclusive else keys < end_key)
    if "is_tradable_session" in frame.columns:
        mask &= frame["is_tradable_session"].fillna(False).astype(bool)
    delta_volume = pd.to_numeric(frame.get("delta_volume", pd.Series(index=frame.index, dtype=float)), errors="coerce")
    mask &= delta_volume > 0
    if fill_model == "strict_visible":
        evidence = pd.to_numeric(frame.get("LastPrice", pd.Series(index=frame.index, dtype=float)), errors="coerce")
        mask &= evidence < entry_price
    elif fill_model == "visible_touch":
        evidence = pd.to_numeric(frame.get("LastPrice", pd.Series(index=frame.index, dtype=float)), errors="coerce")
        mask &= evidence <= entry_price
    elif fill_model == "interval_upper_bound":
        evidence = pd.to_numeric(frame.get("interval_vwap", pd.Series(index=frame.index, dtype=float)), errors="coerce")
        mask &= evidence <= entry_price
    else:  # normalize_manual_simulation_config 已验证；保留防御以便独立单测调用。
        raise ValueError(f"未知成交模型: {fill_model}")
    matched = frame.loc[mask]
    return None if matched.empty else matched.iloc[0]


def _fill_evidence_price(row: pd.Series, fill_model: str) -> float | None:
    field = "interval_vwap" if fill_model == "interval_upper_bound" else "LastPrice"
    return _finite_number(row.get(field))


def _unhedged_mae(
    target_frame: pd.DataFrame,
    entry_price: float,
    fill_key: int,
    hedge_key: int,
    target_tick: float,
    target_multiplier: int,
    target_lots: int,
    max_quote_age_seconds: float,
    slippage_ticks: float,
) -> tuple[float, float]:
    keys = pd.to_numeric(target_frame["market_time_key"], errors="coerce")
    rows = target_frame.loc[(keys >= fill_key) & (keys <= hedge_key)]
    bids = pd.to_numeric(rows.get("BidPrice1", pd.Series(dtype=float)), errors="coerce")
    bids = bids.loc[bids > 0]
    end_quote = _executable_quote(target_frame, hedge_key, "bid", max_quote_age_seconds)
    if end_quote is not None:
        bids = pd.concat([bids, pd.Series([end_quote["price"]])], ignore_index=True)
    if bids.empty:
        return np.nan, np.nan
    worst_exit = float(bids.min()) - slippage_ticks * target_tick
    worst_pnl = (worst_exit - entry_price) * target_multiplier * target_lots
    worst_bps = (worst_exit - entry_price) / entry_price * 10000.0
    return worst_bps, worst_pnl


def _hedged_worst_mark_pnl(
    target_frame: pd.DataFrame,
    hedge_frame: pd.DataFrame,
    entry_price: float,
    hedge_entry_price: float,
    hedge_key: int,
    exit_key: int,
    target_tick: float,
    hedge_tick: float,
    target_multiplier: int,
    hedge_multiplier: int,
    target_lots: int,
    hedge_lots: int,
    max_quote_age_seconds: float,
    slippage_ticks: float,
) -> float:
    keys = pd.to_numeric(target_frame["market_time_key"], errors="coerce")
    target_keys = keys.loc[(keys >= hedge_key) & (keys <= exit_key)].dropna().astype(int).tolist()
    target_keys.extend([hedge_key, exit_key])
    worst: float | None = None
    for mark_key in sorted(set(target_keys)):
        target_quote = _executable_quote(target_frame, mark_key, "bid", max_quote_age_seconds)
        hedge_quote = _executable_quote(hedge_frame, mark_key, "ask", max_quote_age_seconds)
        if target_quote is None or hedge_quote is None:
            continue
        target_mark = target_quote["price"] - slippage_ticks * target_tick
        hedge_mark = hedge_quote["price"] + slippage_ticks * hedge_tick
        pnl = (
            (target_mark - entry_price) * target_multiplier * target_lots
            + (hedge_entry_price - hedge_mark) * hedge_multiplier * hedge_lots
        )
        worst = pnl if worst is None else min(worst, pnl)
    return np.nan if worst is None else worst


def _select_reference_contract(event: Mapping[str, Any], reference_index: int) -> str | None:
    raw = _event_value(event, "参考合约列表", "")
    if raw is None or pd.isna(raw):
        return None
    contracts = [part.strip().upper() for part in str(raw).split(",") if part.strip()]
    return contracts[reference_index] if reference_index < len(contracts) else None


def _frame_tick_size(frame: pd.DataFrame, commodity: str) -> float | None:
    value = _finite_number(frame["tick_size"].iloc[0]) if "tick_size" in frame.columns and not frame.empty else None
    if value is None:
        profile = COMMODITY_PROFILES.get(commodity)
        value = _finite_number(profile.get("tick_size")) if profile else None
    return value if value is not None and value > 0 else None


def _frame_multiplier(frame: pd.DataFrame, commodity: str) -> int | None:
    value = _safe_int(frame["contract_multiplier"].iloc[0]) if "contract_multiplier" in frame.columns and not frame.empty else None
    if value is None:
        profile = COMMODITY_PROFILES.get(commodity)
        value = _safe_int(profile.get("contract_multiplier")) if profile else None
    return value if value is not None and value > 0 else None


def _commodity_number(config: Mapping[str, Any], map_key: str, default_key: str, commodity: str) -> float:
    mapping = config.get(map_key, {})
    value = mapping.get(commodity.upper(), config[default_key])
    return float(value)


def _floor_to_tick(price: float, tick_size: float) -> float:
    return round(math.floor((price / tick_size) + 1e-9) * tick_size, 10)


def _build_warnings(
    config: Mapping[str, Any], skipped_invalid_timing: int, event_count: int, excluded_events: pd.DataFrame | None = None
) -> list[str]:
    warnings = [
        "这是事件条件回放：每条候选事件都假定订单在事件前已挂出；它不等于全天静态挂单的真实成交率回测。",
        "快照数据不能还原排队位置、可成交数量和撤单竞争；event_fill 仅表示该成交模型下存在触及证据。",
        "两腿按全额保证金计算，未假设组合保证金优惠；固定一手比也尚未按 beta 或相关性优化。",
    ]
    if config["fill_model"] == "interval_upper_bound":
        warnings.append("interval_upper_bound 只是区间均价给出的潜在触及上界，不能当作真实成交确认。")
    if (
        float(config["default_commission_per_lot_per_side"]) == 0
        and not any(float(value) > 0 for value in config["commission_by_commodity"].values())
    ):
        warnings.append("当前手续费全部为 0，仅适合结构验证；投入真实费率后再看净收益。")
    if skipped_invalid_timing:
        warnings.append(f"已跳过 {skipped_invalid_timing} 个 exit_delay < hedge_delay 的不可执行参数组合。")
    excluded_up_count = 0 if excluded_events is None else int(
        excluded_events.get("exclusion_reason", pd.Series(dtype=str)).eq("unsupported_event_direction_up").sum()
    )
    if excluded_up_count:
        warnings.append(f"当前手工回放仅支持向下事件，已排除 {excluded_up_count} 条向上事件。")
    if event_count == 0:
        warnings.append("筛选后没有候选事件，请检查品种、日期和质量排除参数。")
    return warnings


def _render_report(result: Mapping[str, Any]) -> str:
    config = result["config"]
    trades = result["trades"]
    summary = result["summary"]
    warnings = "".join(f"<li>{escape(str(item))}</li>" for item in result["warnings"])
    config_json = escape(json.dumps({key: value for key, value in config.items() if not key.startswith("_")}, ensure_ascii=False, indent=2))
    table = summary.to_html(index=False, border=0, classes="summary", justify="left", escape=True)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<title>手工延迟回放报告</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif; margin: 28px; color: #202124; }}
h1 {{ margin-bottom: 6px; }}
.muted {{ color: #5f6368; }}
.warning {{ background: #fff7e6; border-left: 4px solid #e37400; padding: 12px 16px; margin: 20px 0; }}
pre {{ background: #f6f8fa; padding: 14px; overflow: auto; }}
table {{ border-collapse: collapse; font-size: 12px; max-width: 100%; }}
th, td {{ border: 1px solid #dfe3e8; padding: 6px 8px; white-space: nowrap; }}
th {{ background: #f6f8fa; position: sticky; top: 0; }}
.scroll {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>手工延迟回放报告</h1>
<p class=\"muted\">候选事件 {int(trades['event_id'].nunique()) if not trades.empty else 0} 条；参数场景 {int(len(trades) / max(trades['event_id'].nunique(), 1)) if not trades.empty else 0} 个/事件。</p>
<div class=\"warning\"><strong>结果边界</strong><ul>{warnings}</ul></div>
<h2>参数组合汇总</h2>
<div class=\"scroll\">{table}</div>
<h2>本次生效参数</h2>
<pre>{config_json}</pre>
</body>
</html>
"""


def _normalized_commodities(value: Any) -> list[str]:
    if isinstance(value, str):
        source = value.split(",")
    elif isinstance(value, list):
        source = value
    else:
        raise ValueError("commodities 必须是品种数组或逗号分隔字符串")
    return [str(item).strip().upper() for item in source if str(item).strip()]


def _normalize_quality_exclusions(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("quality_exclusions 必须是数组")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("quality_exclusions 的每项必须包含 trade_date 和 commodity")
        trade_date = _optional_date(item.get("trade_date"), "quality_exclusions.trade_date")
        commodity = str(item.get("commodity") or "").strip().upper()
        if not trade_date or not commodity:
            raise ValueError("quality_exclusions 的每项必须包含 trade_date 和 commodity")
        result.append({"trade_date": trade_date, "commodity": commodity})
    return result


def _normalize_number_map(value: Any, name: str, *, allow_zero: bool) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} 必须是对象")
    result: dict[str, float] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().upper()
        if not key:
            raise ValueError(f"{name} 不能包含空品种")
        result[key] = _nonnegative_number(raw_value, f"{name}.{key}") if allow_zero else _positive_number(raw_value, f"{name}.{key}")
    return result


def _number_list(value: Any, name: str, *, allow_zero: bool = False) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} 必须是非空数组")
    converted = [
        _nonnegative_number(item, name) if allow_zero else _positive_number(item, name)
        for item in value
    ]
    return list(dict.fromkeys(converted))


def _optional_date(value: Any, name: str) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)) or str(value).strip() == "":
        return None
    text = str(value).strip()
    text = re.sub(r"\.0+$", "", text)
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{name} 必须是 YYYYMMDD")
    return text


def _positive_number(value: Any, name: str) -> float:
    result = _finite_number(value)
    if result is None or result <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return result


def _nonnegative_number(value: Any, name: str) -> float:
    result = _finite_number(value)
    if result is None or result < 0:
        raise ValueError(f"{name} 必须大于等于 0")
    return result


def _fraction(value: Any, name: str) -> float:
    result = _positive_number(value, name)
    if result > 1:
        raise ValueError(f"{name} 必须小于等于 1")
    return result


def _positive_int(value: Any, name: str) -> int:
    result = _safe_int(value)
    if result is None or result <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return result


def _nonnegative_int(value: Any, name: str) -> int:
    result = _safe_int(value)
    if result is None or result < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return result


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe_int(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or not float(number).is_integer():
        return None
    return int(number)


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    try:
        return bool(value)
    except (TypeError, ValueError):
        return default


def _event_value(event: Mapping[str, Any], key: str, default: Any = None) -> Any:
    getter = getattr(event, "get", None)
    return getter(key, default) if callable(getter) else default


def _event_id(event: Mapping[str, Any]) -> str:
    supplied = _event_value(event, "事件编号", "")
    if supplied is not None and str(supplied).strip() and str(supplied).lower() != "nan":
        return str(supplied)
    return "|".join(
        [
            str(_event_value(event, "合约", "")),
            str(_event_value(event, "交易日", "")),
            str(_event_value(event, "事件锚点结束序号", "")),
        ]
    )


def _safe_divide(numerator: int | float, denominator: int | float) -> float:
    return np.nan if denominator == 0 else float(numerator) / float(denominator)


def _sum_numeric(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.sum()) if not numeric.empty else 0.0


def _percentile(values: pd.Series, percentile: float) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(np.percentile(numeric, percentile)) if not numeric.empty else np.nan


def _minimum(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.min()) if not numeric.empty else np.nan
