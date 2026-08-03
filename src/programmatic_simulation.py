"""乌龙指程序化双向被动报价的全天快照回放。

该模块刻意独立于 ``manual_simulation.py``：后者从已知候选事件出发，
本模块从全天快照出发，候选事件只会在成交后用于标签，绝不参与下单、撤单或重定锚。
它仍是 500ms 快照上的可观察成交假设，不是逐笔队列回测，也不产生真实下单指令。
"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
import json
import math
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

from src.tick_detector.reference_selection import attach_fair_price_metrics
from src.tick_detector.tick_io import (
    COMMODITY_PROFILES,
    iter_day_contract_files,
    load_contract_snapshots,
    load_daily_bounds,
    parse_contract_file,
    prepare_contract_snapshots,
)


FILL_MODELS = {"strict_cross", "observable_cross_assumed"}
TARGET_ORDER_ROLES = ("target_buy", "target_sell")

DEFAULT_CONFIG: dict[str, Any] = {
    "tick_data_root": "data/tick2026",
    "daily_data_root": "data/1d_futures",
    "output_dir": "",
    "trade_date_start": "",
    "trade_date_end": "",
    "commodity": "",
    "target_contract": "",
    "fair_reference_contracts": [],
    "hedge_contract": "",
    "events_csv": "",
    "quality_exclusions": [],
    "detector_event_window_ms": 1000,
    "account_equity": 100000.0,
    "max_margin_ratio": 0.30,
    "default_margin_rate": 0.10,
    "margin_rate_by_commodity": {},
    "default_commission_per_lot_per_side": 0.0,
    "commission_by_commodity": {},
    "target_lots": 1,
    "hedge_lots": 1,
    "enable_hedge": True,
    "band_half_width_ticks": 10,
    "outer_quote_offset_ticks": 10,
    "reanchor_step_ticks": 10,
    "quote_spread_multiple": 2.0,
    "reanchor_confirm_ms": 1000,
    "resume_confirm_ms": 2000,
    "min_reprice_interval_ms": 1000,
    "max_order_actions_per_minute": 20,
    "cancel_ack_latency_ms": 500,
    "new_order_ack_latency_ms": 500,
    "hedge_submit_latency_ms": 500,
    "max_hedge_wait_ms": 2000,
    "hedged_exit_delay_ms": 2000,
    "max_quote_age_ms": 3000,
    "max_fair_age_ms": 3000,
    "max_data_gap_ms": 3000,
    "fill_model": "observable_cross_assumed",
    "require_top_of_book_full_lot": True,
    "cooldown_ms": 1000,
}

STATE_COLUMNS = [
    "trade_date",
    "market_time_key",
    "display_time",
    "from_state",
    "to_state",
    "reason",
    "grid_anchor",
    "buy_limit",
    "sell_limit",
    "fair_price",
    "order_actions_last_minute",
]

ORDER_COLUMNS = [
    "trade_date",
    "market_time_key",
    "display_time",
    "order_id",
    "parent_order_id",
    "contract",
    "role",
    "side",
    "price",
    "lots",
    "version",
    "event",
    "detail",
    "state",
]

TRADE_COLUMNS = [
    "trade_id",
    "trade_date",
    "target_contract",
    "hedge_contract",
    "direction",
    "fill_key",
    "fill_time",
    "fill_context",
    "event_label",
    "fill_evidence",
    "target_entry_price",
    "hedge_entry_key",
    "hedge_entry_price",
    "exit_key",
    "exit_time",
    "exit_reason",
    "status",
    "target_exit_price",
    "hedge_exit_price",
    "target_pnl",
    "hedge_pnl",
    "gross_pnl",
    "commission",
    "net_pnl",
    "gross_margin",
    "unhedged_worst_mark_pnl",
    "hedged_worst_mark_pnl",
    "observed_worst_mark_pnl",
]

SUMMARY_COLUMNS = [
    "scope",
    "trade_date",
    "target_contract",
    "fill_count",
    "detector_event_fill_count",
    "normal_move_fill_count",
    "fill_during_replace_count",
    "closed_count",
    "hedge_failure_count",
    "capital_limit_exit_count",
    "net_pnl",
    "win_rate",
    "worst_net_pnl",
    "max_gross_margin",
    "order_action_count",
    "peak_order_actions_per_minute",
    "reprice_count",
    "pause_count",
    "hedge_failure_rate",
]


def load_programmatic_simulation_config(path: str | Path) -> dict[str, Any]:
    """读取 V2 JSON 配置并完成校验。"""
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise ValueError("程序化回放配置必须是 JSON 对象")
    raw = dict(raw)
    raw["_config_path"] = str(config_path)
    return normalize_programmatic_simulation_config(raw)


def normalize_programmatic_simulation_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    """补齐默认值并拦截会改变回放语义的无效配置。"""
    config = dict(DEFAULT_CONFIG)
    config.update(dict(raw))
    for key in ("tick_data_root", "daily_data_root", "output_dir", "commodity", "target_contract", "hedge_contract"):
        value = str(config.get(key) or "").strip()
        if not value:
            raise ValueError(f"配置缺少 {key}")
        config[key] = value.upper() if key in {"commodity", "target_contract", "hedge_contract"} else value

    config["trade_date_start"] = _required_date(config.get("trade_date_start"), "trade_date_start")
    config["trade_date_end"] = _required_date(config.get("trade_date_end"), "trade_date_end")
    if config["trade_date_start"] > config["trade_date_end"]:
        raise ValueError("trade_date_start 不能晚于 trade_date_end")

    config["fair_reference_contracts"] = _contract_list(config.get("fair_reference_contracts"), "fair_reference_contracts")
    if len(config["fair_reference_contracts"]) < 2:
        raise ValueError("V2 的 fair_reference_contracts 至少需要 2 个冻结参考合约")
    if config["target_contract"] in config["fair_reference_contracts"]:
        raise ValueError("target_contract 不能同时作为 fair_reference_contracts")
    if config["hedge_contract"] not in config["fair_reference_contracts"]:
        raise ValueError("hedge_contract 必须属于 fair_reference_contracts，避免使用未冻结的对冲参考")
    config["events_csv"] = str(config.get("events_csv") or "").strip()
    config["quality_exclusions"] = _quality_exclusions(config.get("quality_exclusions"))

    config["detector_event_window_ms"] = _nonnegative_int(config.get("detector_event_window_ms"), "detector_event_window_ms")
    config["account_equity"] = _positive_number(config.get("account_equity"), "account_equity")
    config["max_margin_ratio"] = _fraction(config.get("max_margin_ratio"), "max_margin_ratio")
    config["default_margin_rate"] = _positive_number(config.get("default_margin_rate"), "default_margin_rate")
    config["margin_rate_by_commodity"] = _number_map(config.get("margin_rate_by_commodity"), "margin_rate_by_commodity", allow_zero=False)
    config["default_commission_per_lot_per_side"] = _nonnegative_number(
        config.get("default_commission_per_lot_per_side"), "default_commission_per_lot_per_side"
    )
    config["commission_by_commodity"] = _number_map(config.get("commission_by_commodity"), "commission_by_commodity", allow_zero=True)
    config["target_lots"] = _positive_int(config.get("target_lots"), "target_lots")
    config["hedge_lots"] = _positive_int(config.get("hedge_lots"), "hedge_lots")
    config["enable_hedge"] = _as_bool(config.get("enable_hedge"), "enable_hedge")

    for key in ("band_half_width_ticks", "outer_quote_offset_ticks", "reanchor_step_ticks"):
        config[key] = _positive_number(config.get(key), key)
    config["quote_spread_multiple"] = _positive_number(config.get("quote_spread_multiple"), "quote_spread_multiple")
    for key in (
        "reanchor_confirm_ms",
        "resume_confirm_ms",
        "min_reprice_interval_ms",
        "cancel_ack_latency_ms",
        "new_order_ack_latency_ms",
        "hedge_submit_latency_ms",
        "max_hedge_wait_ms",
        "hedged_exit_delay_ms",
        "cooldown_ms",
    ):
        config[key] = _nonnegative_int(config.get(key), key)
    config["max_order_actions_per_minute"] = _positive_int(
        config.get("max_order_actions_per_minute"), "max_order_actions_per_minute"
    )
    for key in ("max_quote_age_ms", "max_fair_age_ms", "max_data_gap_ms"):
        config[key] = _positive_int(config.get(key), key)
    config["fill_model"] = str(config.get("fill_model") or "").strip()
    if config["fill_model"] not in FILL_MODELS:
        raise ValueError(f"fill_model 必须是以下之一: {', '.join(sorted(FILL_MODELS))}")
    config["require_top_of_book_full_lot"] = _as_bool(
        config.get("require_top_of_book_full_lot"), "require_top_of_book_full_lot"
    )
    return config


def build_grid(anchor: float, tick_size: float, config: Mapping[str, Any]) -> dict[str, float]:
    """由合法 tick 锚点生成一层双向被动报价。"""
    anchor = _round_to_tick(anchor, tick_size)
    width = float(config["band_half_width_ticks"]) * tick_size
    outer = float(config["outer_quote_offset_ticks"]) * tick_size
    return {
        "anchor": anchor,
        "band_lower": _round_to_tick(anchor - width, tick_size),
        "band_upper": _round_to_tick(anchor + width, tick_size),
        "buy_limit": _round_to_tick(anchor - width - outer, tick_size),
        "sell_limit": _round_to_tick(anchor + width + outer, tick_size),
    }


def simulate_programmatic_day(
    target_frame: pd.DataFrame,
    hedge_frame: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    trade_date: str | None = None,
    detector_event_keys: set[int] | None = None,
) -> dict[str, pd.DataFrame]:
    """回放一日已经附带 ``fair_price`` 的目标快照，供真实入口和合成测试共用。"""
    return _simulate_programmatic_day_prepared(
        target_frame,
        hedge_frame,
        config,
        trade_date=trade_date,
        detector_event_keys=detector_event_keys,
        assume_sorted=False,
    )


def _simulate_programmatic_day_prepared(
    target_frame: pd.DataFrame,
    hedge_frame: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    trade_date: str | None = None,
    detector_event_keys: set[int] | None = None,
    assume_sorted: bool,
) -> dict[str, pd.DataFrame]:
    """网格内部入口；仅在每日准备已稳定排序时传入 ``assume_sorted=True``。"""
    runtime = dict(DEFAULT_CONFIG)
    runtime.update(dict(config))
    replay = _DayReplay(
        target_frame,
        hedge_frame,
        runtime,
        trade_date=trade_date or _frame_trade_date(target_frame),
        detector_event_keys=detector_event_keys or set(),
        assume_sorted=assume_sorted,
    )
    return replay.run()


def run_programmatic_simulation(config: Mapping[str, Any]) -> dict[str, Any]:
    """加载本地全天快照、构造无未来 fair，再逐日执行 V2 状态机。"""
    config = normalize_programmatic_simulation_config(config)
    event_rows = _load_event_rows(config["events_csv"])
    transitions: list[pd.DataFrame] = []
    orders: list[pd.DataFrame] = []
    trades: list[pd.DataFrame] = []
    trade_contexts: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []

    exclusions = {(item["trade_date"], item["commodity"]) for item in config["quality_exclusions"]}
    for stamp in pd.date_range(config["trade_date_start"], config["trade_date_end"], freq="D"):
        trade_date = stamp.strftime("%Y%m%d")
        if (trade_date, config["commodity"]) in exclusions:
            skipped.append({"trade_date": trade_date, "reason": "quality_exclusion"})
            continue
        try:
            frames = _load_required_frames(config, trade_date)
        except FileNotFoundError:
            skipped.append({"trade_date": trade_date, "reason": "tick_day_missing"})
            continue
        except Exception as exc:
            skipped.append({"trade_date": trade_date, "reason": f"day_load_failed: {exc}"})
            continue

        fair_reference_contracts = set(config["fair_reference_contracts"])
        if not config["enable_hedge"]:
            fair_reference_contracts.discard(config["hedge_contract"])
        required = {config["target_contract"], *fair_reference_contracts}
        if config["enable_hedge"]:
            required.add(config["hedge_contract"])
        missing = sorted(contract for contract in required if contract not in frames)
        if missing:
            skipped.append({"trade_date": trade_date, "reason": f"contract_missing: {','.join(missing)}"})
            continue
        target = frames[config["target_contract"]]
        tick_size = _frame_tick_size(target, config["commodity"])
        if tick_size is None:
            skipped.append({"trade_date": trade_date, "reason": "target_metadata_missing"})
            continue

        fair_references = {
            contract: frames[contract]
            for contract in config["fair_reference_contracts"]
            if contract in frames
        }
        enriched = attach_fair_price_metrics(
            target,
            fair_references,
            tick_size=tick_size,
            top_volume_peer_contracts=set(config["fair_reference_contracts"]),
            max_reference_age_seconds=float(config["max_fair_age_ms"]) / 1000.0,
        )
        event_keys = _event_keys_for_day(event_rows, trade_date, config["target_contract"], enriched)
        day = simulate_programmatic_day(
            enriched,
            frames.get(config["hedge_contract"], pd.DataFrame()),
            config,
            trade_date=trade_date,
            detector_event_keys=event_keys,
        )
        transitions.append(day["transitions"])
        orders.append(day["orders"])
        trades.append(day["trades"])
        trade_contexts.update(
            build_trade_contexts(
                day["trades"],
                enriched,
                frames,
                config,
                orders=day["orders"],
                transitions=day["transitions"],
            )
        )

    transition_df = _concat_frames(transitions, STATE_COLUMNS)
    order_df = _concat_frames(orders, ORDER_COLUMNS)
    trade_df = _concat_frames(trades, TRADE_COLUMNS)
    skipped_df = pd.DataFrame(skipped, columns=["trade_date", "reason"])
    summary = build_programmatic_summary(trade_df, order_df, transition_df)
    warnings = [
        "这是基于约 500ms 快照的全天状态机回放；observable_cross_assumed 不是交易所成交回报。",
        "fair_reference_contracts 由配置显式冻结，未按当天最终成交量选参考合约。",
        "目标腿挂单锚点和重定锚均由目标合约 LastPrice 驱动；fair_price 仅保留为诊断和详情对比字段。",
        "所有成交腿（对冲参考腿、退出腿）与盯市浮亏均按可见买卖一档价估算（不加滑点），未还原盘口队列位置。",
    ]
    if config["events_csv"]:
        warnings.append("候选事件 CSV 只用于成交后的 detector_event_fill 标签，不参与任何状态转移。")
    if not trade_df.empty and (trade_df["status"] == "unclosed_end_of_day").any():
        warnings.append("存在收盘前无法按可见盘口平完的仓位，相关记录不应作为可实现收益。")
    return {
        "config": config,
        "transitions": transition_df,
        "orders": order_df,
        "trades": trade_df,
        "summary": summary,
        "skipped_days": skipped_df,
        "warnings": warnings,
        "trade_contexts": trade_contexts,
    }


class _DayReplay:
    """单交易日的最小状态机；用一个对象保存订单、仓位和时间轴状态。"""

    def __init__(
        self,
        target_frame: pd.DataFrame,
        hedge_frame: pd.DataFrame,
        config: Mapping[str, Any],
        *,
        trade_date: str,
        detector_event_keys: set[int],
        assume_sorted: bool = False,
    ) -> None:
        if target_frame.empty:
            raise ValueError("目标合约快照为空")
        if hedge_frame.empty and bool(config.get("enable_hedge", True)):
            raise ValueError("对冲合约快照为空")
        self.target = target_frame if assume_sorted else target_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        self.hedge = (
            hedge_frame
            if assume_sorted or hedge_frame.empty
            else hedge_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        )
        self.hedge_keys = (
            pd.to_numeric(self.hedge["market_time_key"], errors="coerce").to_numpy(dtype=np.int64)
            if not self.hedge.empty
            else np.array([], dtype=np.int64)
        )
        self.config = config
        self.trade_date = trade_date
        self.detector_event_keys = detector_event_keys
        self.commodity = str(self.target["commodity"].iloc[0]).upper()
        self.target_contract = str(self.target["contract"].iloc[0]).upper()
        self.hedge_contract = str(
            self.hedge["contract"].iloc[0] if not self.hedge.empty else config["hedge_contract"]
        ).upper()
        self.target_tick = _required_frame_tick_size(self.target, self.commodity)
        self.hedge_tick = (
            _required_frame_tick_size(self.hedge, self.commodity)
            if not self.hedge.empty
            else self.target_tick
        )
        self.target_multiplier = _required_frame_multiplier(self.target, self.commodity)
        self.hedge_multiplier = (
            _required_frame_multiplier(self.hedge, self.commodity)
            if not self.hedge.empty
            else self.target_multiplier
        )

        self.state = "PAUSED"
        self.anchor: float | None = None
        self.grid: dict[str, float] = {}
        self.version = 0
        self.replace: dict[str, Any] | None = None
        self.trade: dict[str, Any] | None = None
        self.orders_by_id: dict[str, dict[str, Any]] = {}
        self.active_target_order_ids: dict[str, list[str]] = {role: [] for role in TARGET_ORDER_ROLES}
        self.pending: list[dict[str, Any]] = []
        self.transitions: list[dict[str, Any]] = []
        self.order_log: list[dict[str, Any]] = []
        self.trade_log: list[dict[str, Any]] = []
        self.action_keys: list[int] = []
        self.order_counter = 0
        self.trade_counter = 0
        self.previous_key: int | None = None
        self.last_reprice_key: int | None = None
        self.cooldown_until_key = 0
        self.reanchor_direction = ""
        self.reanchor_started_key: int | None = None
        self.last_price_recovered_since_key: int | None = None
        self.last_row: Mapping[str, Any] | None = None

    def run(self) -> dict[str, pd.DataFrame]:
        columns = tuple(self.target.columns)
        for values in self.target.itertuples(index=False, name=None):
            row = dict(zip(columns, values, strict=True))
            key = _row_key(row)
            if key is None:
                continue
            self.last_row = row
            if not self.transitions:
                self._record_transition(row, self.state, "start")
            self._process_due(row)

            data_gap = self.previous_key is not None and key - self.previous_key > int(self.config["max_data_gap_ms"])
            if self.trade is None:
                # 断点期间旧单是否曾被打到不可观察；不能在恢复后的第一帧补造一笔成交。
                if data_gap:
                    self._pause(row, "data_gap")
                elif _valid_last_price(row) is None:
                    if self.state not in {"PAUSED", "COOLDOWN"} or self._has_live_target_orders():
                        self._pause(row, "last_price_invalid_or_session_guard")
                elif not _quote_spread_ok(row, self.target_tick, self.config):
                    if self.state not in {"PAUSED", "COOLDOWN"} or self._has_live_target_orders():
                        self._pause(row, "quote_spread_guard")
                else:
                    fill = self._first_target_fill(row)
                    if fill is not None:
                        self._open_trade(row, *fill)
                    else:
                        self._manage_flat_state(row)
            else:
                opposite_fill = self._first_target_fill(row)
                if opposite_fill is not None:
                    self._handle_opposite_fill(row, *opposite_fill)
                if self.trade is not None:
                    if data_gap and self.state not in {"FLATTENING", "EMERGENCY_FLATTEN"}:
                        self.trade["exit_reason"] = "data_gap_exit"
                        self._record_transition(row, "EMERGENCY_FLATTEN", "data_gap")
                        self._try_flatten(row)
                    else:
                        self._manage_position(row)
            self.previous_key = key

        self._finish_day()
        return {
            "transitions": pd.DataFrame(self.transitions, columns=STATE_COLUMNS),
            "orders": pd.DataFrame(self.order_log, columns=ORDER_COLUMNS),
            "trades": pd.DataFrame(self.trade_log, columns=TRADE_COLUMNS),
        }

    # ------------------------------------------------------------------
    # 订单生命周期与状态转换
    # ------------------------------------------------------------------

    def _record_transition(self, row: pd.Series, new_state: str, reason: str) -> None:
        key = _row_key(row)
        if key is None:
            return
        old_state = self.state
        self.state = new_state
        self.transitions.append(
            {
                "trade_date": self.trade_date,
                "market_time_key": key,
                "display_time": _display_time(row),
                "from_state": old_state,
                "to_state": new_state,
                "reason": reason,
                "grid_anchor": self.anchor if self.anchor is not None else np.nan,
                "buy_limit": self.grid.get("buy_limit", np.nan),
                "sell_limit": self.grid.get("sell_limit", np.nan),
                "fair_price": _fair_price(row),
                "order_actions_last_minute": self._action_count(key),
            }
        )

    def _new_order_id(self) -> str:
        self.order_counter += 1
        return f"{self.trade_date}-{self.target_contract}-{self.order_counter:05d}"

    def _log_order(self, row: pd.Series, order: Mapping[str, Any], event: str, detail: str = "") -> None:
        key = _row_key(row)
        if key is None:
            return
        self.order_log.append(
            {
                "trade_date": self.trade_date,
                "market_time_key": key,
                "display_time": _display_time(row),
                "order_id": order["order_id"],
                "parent_order_id": order.get("parent_order_id", ""),
                "contract": order["contract"],
                "role": order["role"],
                "side": order["side"],
                "price": order.get("price", np.nan),
                "lots": order["lots"],
                "version": order.get("version", self.version),
                "event": event,
                "detail": detail,
                "state": self.state,
            }
        )

    def _consume_action(self, key: int) -> None:
        self.action_keys.append(key)
        self._action_count(key)

    def _action_count(self, key: int) -> int:
        floor = key - 60000
        self.action_keys = [action_key for action_key in self.action_keys if action_key > floor]
        return len(self.action_keys)

    def _can_submit(self, key: int, count: int) -> bool:
        return self._action_count(key) + count <= int(self.config["max_order_actions_per_minute"])

    def _submit_passive(
        self,
        row: pd.Series,
        *,
        role: str,
        side: str,
        price: float,
        lots: int,
        reason: str,
    ) -> str | None:
        key = _row_key(row)
        if key is None or not self._can_submit(key, 1):
            return None
        order = {
            "order_id": self._new_order_id(),
            "contract": self.target_contract,
            "role": role,
            "side": side,
            "price": price,
            "lots": lots,
            "version": self.version,
            "status": "submitted",
            "cancel_reason": "",
        }
        self.orders_by_id[order["order_id"]] = order
        if role in TARGET_ORDER_ROLES:
            self.active_target_order_ids[role].append(str(order["order_id"]))
        self._consume_action(key)
        self._log_order(row, order, "submit", reason)
        self.pending.append(
            {
                "due_key": key + int(self.config["new_order_ack_latency_ms"]),
                "kind": "ack",
                "order_id": order["order_id"],
            }
        )
        return str(order["order_id"])

    def _request_cancel(self, row: pd.Series, order: dict[str, Any], reason: str) -> None:
        if order["status"] not in {"submitted", "active"}:
            return
        key = _row_key(row)
        if key is None:
            return
        order["status"] = "cancel_requested"
        order["cancel_reason"] = reason
        self._consume_action(key)  # 风险撤单即使压线也必须发送，不能静默保留旧单。
        self._log_order(row, order, "cancel_requested", reason)
        self.pending.append(
            {
                "due_key": key + int(self.config["cancel_ack_latency_ms"]),
                "kind": "cancel_ack",
                "order_id": order["order_id"],
            }
        )

    def _cancel_target_orders(self, row: pd.Series, reason: str) -> None:
        for order in self._active_target_orders():
            self._request_cancel(row, order, reason)

    def _active_target_orders(self):
        for role in TARGET_ORDER_ROLES:
            for order_id in self.active_target_order_ids[role]:
                order = self.orders_by_id.get(order_id)
                if order is not None:
                    yield order

    def _remove_active_target_order(self, order: Mapping[str, Any]) -> None:
        role = str(order.get("role") or "")
        if role not in TARGET_ORDER_ROLES:
            return
        order_id = str(order.get("order_id") or "")
        ids = self.active_target_order_ids[role]
        if order_id in ids:
            ids.remove(order_id)

    def _is_live(self, order: Mapping[str, Any]) -> bool:
        return str(order.get("status")) in {"submitted", "active", "cancel_requested"}

    def _has_live_target_orders(self) -> bool:
        return any(self._is_live(order) for order in self._active_target_orders())

    def _process_due(self, row: pd.Series) -> None:
        key = _row_key(row)
        if key is None:
            return
        while True:
            due = [item for item in self.pending if int(item["due_key"]) <= key]
            if not due:
                break
            self.pending = [item for item in self.pending if int(item["due_key"]) > key]
            for item in sorted(due, key=lambda value: (int(value["due_key"]), str(value["kind"]))):
                order = self.orders_by_id.get(str(item["order_id"]))
                if order is None:
                    continue
                if item["kind"] == "ack" and order["status"] == "submitted":
                    order["status"] = "active"
                    self._log_order(row, order, "ack")
                elif item["kind"] == "cancel_ack" and order["status"] == "cancel_requested":
                    order["status"] = "cancelled"
                    self._remove_active_target_order(order)
                    self._log_order(row, order, "cancel_ack", str(order.get("cancel_reason", "")))
            self._maybe_submit_replacement(row)
            self._maybe_complete_replacement(row)

    def _submit_grid(self, row: pd.Series, reason: str) -> bool:
        key = _row_key(row)
        if key is None or not _quote_spread_ok(row, self.target_tick, self.config) or not self._can_submit(key, 2):
            return False
        buy_id = self._submit_passive(
            row,
            role="target_buy",
            side="buy",
            price=float(self.grid["buy_limit"]),
            lots=int(self.config["target_lots"]),
            reason=reason,
        )
        sell_id = self._submit_passive(
            row,
            role="target_sell",
            side="sell",
            price=float(self.grid["sell_limit"]),
            lots=int(self.config["target_lots"]),
            reason=reason,
        )
        return buy_id is not None and sell_id is not None

    def _maybe_submit_replacement(self, row: pd.Series) -> None:
        if self.replace is None or self.replace.get("new_order_ids") is not None:
            return
        old_ids = self.replace["old_order_ids"]
        if any(self._is_live(self.orders_by_id[order_id]) for order_id in old_ids if order_id in self.orders_by_id):
            return
        self.anchor = float(self.replace["new_anchor"])
        self.grid = build_grid(self.anchor, self.target_tick, self.config)
        self.version += 1
        if not _quote_spread_ok(row, self.target_tick, self.config):
            self.replace = None
            self._pause(row, "quote_spread_guard")
            return
        if not self._submit_grid(row, "reanchor_replace"):
            self.replace = None
            self._record_transition(row, "PAUSED", "order_action_limit")
            return
        self.replace["new_order_ids"] = [
            str(order["order_id"])
            for order in self._active_target_orders()
            if order["version"] == self.version
        ]

    def _maybe_complete_replacement(self, row: pd.Series) -> None:
        if self.replace is None or not self.replace.get("new_order_ids"):
            return
        ids = self.replace["new_order_ids"]
        if all(self.orders_by_id[order_id]["status"] == "active" for order_id in ids if order_id in self.orders_by_id):
            self.replace = None
            self._record_transition(row, "FLAT_QUOTING", "replace_complete")

    # ------------------------------------------------------------------
    # 平仓状态：LastPrice 驱动锚点与撤改单
    # ------------------------------------------------------------------

    def _manage_flat_state(self, row: pd.Series) -> None:
        key = _row_key(row)
        if key is None:
            return
        # 非交易时段或开盘保护：暂停，不挂单。
        if not _row_bool(row, "is_tradable_session", True) or _row_bool(row, "is_open_protected", False):
            self._pause(row, "last_price_invalid_or_session_guard")
            return
        last_price = _valid_last_price(row)
        if last_price is None:
            self._pause(row, "last_price_invalid_or_session_guard")
            return
        last_price = _round_to_tick(last_price, self.target_tick)
        if self.state == "PAUSED":
            if not self._has_live_target_orders() and key >= self.cooldown_until_key:
                self._resume_if_stable(row, last_price)
            return
        if self.state == "COOLDOWN":
            if key >= self.cooldown_until_key and not self._has_live_target_orders():
                self._resume_if_stable(row, last_price)
            return
        if self.state == "FLAT_QUOTING":
            self._check_reanchor(row, last_price)

    def _pause(self, row: pd.Series, reason: str) -> None:
        if self.state not in {"PAUSED", "COOLDOWN"}:
            self._record_transition(row, "PAUSED", reason)
        self._cancel_target_orders(row, reason)
        self.reanchor_direction = ""
        self.reanchor_started_key = None
        self.last_price_recovered_since_key = None
        if self.state == "COOLDOWN":
            self._record_transition(row, "PAUSED", reason)

    def _resume_if_stable(self, row: pd.Series, last_price: float) -> None:
        key = _row_key(row)
        if key is None:
            return
        if self.last_price_recovered_since_key is None:
            self.last_price_recovered_since_key = key
            if int(self.config["resume_confirm_ms"]) > 0:
                return
        if key - self.last_price_recovered_since_key < int(self.config["resume_confirm_ms"]):
            return
        self._start_quoting(row, last_price)

    def _start_quoting(self, row: pd.Series, last_price: float) -> None:
        key = _row_key(row)
        if key is None:
            return
        candidate_anchor = _round_to_tick(last_price, self.target_tick)
        candidate_grid = build_grid(candidate_anchor, self.target_tick, self.config)
        if not _quote_spread_ok(row, self.target_tick, self.config):
            self._pause(row, "quote_spread_guard")
            return
        if not self._quote_margin_ok(key, candidate_grid):
            self._record_transition(row, "PAUSED", "capital_or_hedge_quote_guard")
            return
        if not self._can_submit(key, 2):
            self._record_transition(row, "PAUSED", "order_action_limit")
            return
        self.anchor = candidate_anchor
        self.grid = candidate_grid
        self.version += 1
        self.last_price_recovered_since_key = None
        self._record_transition(row, "FLAT_QUOTING", "last_price_recovered")
        if not self._submit_grid(row, "initial_quote"):
            self._record_transition(row, "PAUSED", "order_action_limit")

    def _check_reanchor(self, row: pd.Series, last_price: float) -> None:
        if not self.grid:
            return
        key = _row_key(row)
        if key is None:
            return
        direction = ""
        if last_price < float(self.grid["band_lower"]):
            direction = "down"
        elif last_price > float(self.grid["band_upper"]):
            direction = "up"
        if not direction:
            self.reanchor_direction = ""
            self.reanchor_started_key = None
            return
        if direction != self.reanchor_direction:
            self.reanchor_direction = direction
            self.reanchor_started_key = key
            return
        if self.reanchor_started_key is None or key - self.reanchor_started_key < int(self.config["reanchor_confirm_ms"]):
            return
        if self.last_reprice_key is not None and key - self.last_reprice_key < int(self.config["min_reprice_interval_ms"]):
            return
        if not self._can_submit(key, 4):
            self._pause(row, "order_action_limit")
            return

        step_price = float(self.config["reanchor_step_ticks"]) * self.target_tick
        if direction == "down":
            steps = math.ceil((float(self.grid["band_lower"]) - last_price) / step_price - 1e-12)
            new_anchor = float(self.anchor) - max(1, steps) * step_price
        else:
            steps = math.ceil((last_price - float(self.grid["band_upper"])) / step_price - 1e-12)
            new_anchor = float(self.anchor) + max(1, steps) * step_price
        self.replace = {
            "old_order_ids": [
                str(order["order_id"])
                for order in self._active_target_orders()
                if self._is_live(order)
            ],
            "new_anchor": _round_to_tick(new_anchor, self.target_tick),
            "new_order_ids": None,
        }
        self.last_reprice_key = key
        self._record_transition(row, "REPLACE_PENDING", f"last_price_{direction}_confirmed")
        self._cancel_target_orders(row, "reanchor")
        self.reanchor_direction = ""
        self.reanchor_started_key = None
        self._maybe_submit_replacement(row)

    def _quote_margin_ok(self, key: int, grid: Mapping[str, float]) -> bool:
        rate = _commodity_number(self.config, "margin_rate_by_commodity", "default_margin_rate", self.commodity)
        target_lots = int(self.config["target_lots"])
        target_margin = max(
            float(grid["buy_limit"]) * self.target_multiplier * target_lots * rate,
            float(grid["sell_limit"]) * self.target_multiplier * target_lots * rate,
        )
        if not self.config["enable_hedge"]:
            return target_margin <= float(self.config["account_equity"]) * float(self.config["max_margin_ratio"])

        hedge_bid = self._asof_quote(key, "sell", int(self.config["hedge_lots"]))
        hedge_ask = self._asof_quote(key, "buy", int(self.config["hedge_lots"]))
        if hedge_bid is None or hedge_ask is None:
            return False
        hedge_lots = int(self.config["hedge_lots"])
        long_margin = (
            float(grid["buy_limit"]) * self.target_multiplier * target_lots * rate
            + float(hedge_bid["price"]) * self.hedge_multiplier * hedge_lots * rate
        )
        short_margin = (
            float(grid["sell_limit"]) * self.target_multiplier * target_lots * rate
            + float(hedge_ask["price"]) * self.hedge_multiplier * hedge_lots * rate
        )
        return max(long_margin, short_margin) <= float(self.config["account_equity"]) * float(self.config["max_margin_ratio"])

    # ------------------------------------------------------------------
    # 被动成交与程序化对冲
    # ------------------------------------------------------------------

    def _first_target_fill(self, row: pd.Series) -> tuple[dict[str, Any], list[str]] | None:
        for order in self._active_target_orders():
            if not self._is_live(order):
                continue
            evidence, partial_unknown = _fill_evidence(row, order, self.config)
            if evidence:
                return order, evidence
            if partial_unknown and not order.get("partial_logged"):
                order["partial_logged"] = True
                self._log_order(row, order, "quote_cross_partial_unknown")
        return None

    def _open_trade(self, row: pd.Series, order: dict[str, Any], evidence: list[str]) -> None:
        key = _row_key(row)
        if key is None:
            return
        order["status"] = "filled"
        self._remove_active_target_order(order)
        order["fill_evidence"] = "+".join(evidence)
        self._log_order(row, order, "fill", order["fill_evidence"])
        fill_context = "normal_quote"
        if self.state == "REPLACE_PENDING" or order.get("cancel_reason") == "reanchor":
            fill_context = "fill_during_replace"
        elif order.get("cancel_reason"):
            fill_context = "fill_during_cancel"
        self._cancel_target_orders(row, "opposite_target_fill")
        self.replace = None
        direction = "long" if order["side"] == "buy" else "short"
        self.trade_counter += 1
        fair = _fair_price(row)
        residual = abs((fair if math.isfinite(fair) else float(order["price"])) - float(order["price"])) / self.target_tick
        self.trade = {
            "trade_id": f"{self.trade_date}-{self.target_contract}-{self.trade_counter:04d}",
            "direction": direction,
            "fill_key": key,
            "fill_time": _display_time(row),
            "fill_context": fill_context,
            "event_label": self._event_label(key),
            "fill_evidence": "+".join(evidence),
            "target_entry_price": float(order["price"]),
            "target_open": True,
            "hedge_open": False,
            "hedge_due_key": key + int(self.config["hedge_submit_latency_ms"]),
            "hedge_deadline_key": key + int(self.config["max_hedge_wait_ms"]),
            "hedge_exit_due_key": key + int(self.config["hedged_exit_delay_ms"]),
            "hedge_entry_key": np.nan,
            "hedge_entry_price": np.nan,
            "target_exit_price": np.nan,
            "hedge_exit_price": np.nan,
            "entry_residual_ticks": max(residual, 1e-9),
            "unhedged_worst_mark_pnl": 0.0,
            "hedged_worst_mark_pnl": np.nan,
            "observed_worst_mark_pnl": 0.0,
            "gross_margin": np.nan,
            "exit_reason": "",
            "hedge_order": None,
            "target_exit_order": None,
            "hedge_exit_order": None,
        }
        if self.config["enable_hedge"]:
            self._record_transition(row, "LONG_PENDING_HEDGE" if direction == "long" else "SHORT_PENDING_HEDGE", "target_fill")
        else:
            rate = _commodity_number(self.config, "margin_rate_by_commodity", "default_margin_rate", self.commodity)
            self.trade["gross_margin"] = (
                float(order["price"]) * self.target_multiplier * int(self.config["target_lots"]) * rate
            )
            self.trade["target_only_exit_due_key"] = key + int(self.config["hedged_exit_delay_ms"])
            self._record_transition(row, "UNHEDGED_POSITION", "hedge_disabled")

    def _handle_opposite_fill(self, row: pd.Series, order: dict[str, Any], evidence: list[str]) -> None:
        if self.trade is None:
            return
        key = _row_key(row)
        if key is None:
            return
        order["status"] = "filled"
        self._remove_active_target_order(order)
        self._log_order(row, order, "fill", "+".join(evidence))
        self.trade["target_exit_price"] = float(order["price"])
        self.trade["target_exit_key"] = key
        self.trade["target_exit_time"] = _display_time(row)
        self.trade["target_open"] = False
        self.trade["exit_reason"] = "opposite_fill_before_cancel"
        if not math.isfinite(_finite_number(self.trade.get("hedge_entry_price")) or np.nan):
            self._finalize_trade(row, "opposite_fill_before_cancel")
            return
        self._record_transition(row, "EMERGENCY_FLATTEN", "opposite_fill_before_cancel")
        self._try_flatten(row)

    def _manage_position(self, row: pd.Series) -> None:
        if self.trade is None:
            return
        key = _row_key(row)
        if key is None:
            return
        if self.state == "UNHEDGED_POSITION":
            self._update_worst_mark(row)
            if key >= int(self.trade["target_only_exit_due_key"]):
                self.trade["exit_reason"] = "no_hedge_exit"
                self._record_transition(row, "FLATTENING", "no_hedge_exit")
                self._try_flatten(row)
            return
        if self.state in {"LONG_PENDING_HEDGE", "SHORT_PENDING_HEDGE"}:
            # 对冲未完成：记录最差浮亏（不触发止损）；尝试对冲；超时则直接平目标腿。
            self._update_worst_mark(row)
            if key >= int(self.trade["hedge_due_key"]):
                self._try_hedge(row)
            if self.trade is not None and not self.trade["hedge_open"] and key >= int(self.trade["hedge_deadline_key"]):
                self.trade["exit_reason"] = "hedge_failure_exit"
                self._record_transition(row, "EMERGENCY_FLATTEN", "hedge_quote_timeout")
                self._try_flatten(row)
            return
        if self.state == "HEDGED_POSITION":
            # 对冲已成交：到延迟时点直接平两腿，不看 fair。
            self._update_worst_mark(row)
            if key >= int(self.trade["hedge_exit_due_key"]):
                self.trade["exit_reason"] = "hedged_exit"
                self._record_transition(row, "FLATTENING", "hedged_exit")
                self._try_flatten(row)
            return
        if self.state in {"FLATTENING", "EMERGENCY_FLATTEN"}:
            self._update_worst_mark(row)
            self._try_flatten(row)

    def _try_hedge(self, row: pd.Series) -> None:
        if self.trade is None or self.trade["hedge_open"] or not self.trade["target_open"]:
            return
        key = _row_key(row)
        if key is None:
            return
        direction = "sell" if self.trade["direction"] == "long" else "buy"
        order = self.trade.get("hedge_order")
        if order is None:
            order = self._new_aggressive_order(row, self.hedge_contract, "hedge_entry", direction, int(self.config["hedge_lots"]))
            self.trade["hedge_order"] = order
        quote = self._asof_quote(key, direction, int(self.config["hedge_lots"]))
        if quote is None:
            if not order.get("quote_missing_logged"):
                order["quote_missing_logged"] = True
                self._log_order(row, order, "quote_unavailable")
            return
        price = float(quote["price"])
        if price <= 0:
            return
        if self._margin_for_hedge_entry(price) > float(self.config["account_equity"]) * float(self.config["max_margin_ratio"]):
            order["status"] = "rejected"
            self._log_order(row, order, "capital_rejected")
            self.trade["exit_reason"] = "capital_limit_at_hedge"
            self._record_transition(row, "EMERGENCY_FLATTEN", "capital_limit_at_hedge")
            self._try_flatten(row)
            return
        order["status"] = "filled"
        order["price"] = price
        self._log_order(row, order, "fill", f"quote_key={quote['key']}")
        self.trade["hedge_entry_key"] = key
        self.trade["hedge_exit_due_key"] = key + int(self.config["hedged_exit_delay_ms"])
        self.trade["hedge_entry_price"] = price
        self.trade["hedge_open"] = True
        self.trade["gross_margin"] = self._actual_margin()
        self._record_transition(row, "HEDGED_POSITION", "hedge_fill")

    # ------------------------------------------------------------------
    # 盯市、退出与记账
    # ------------------------------------------------------------------

    def _new_aggressive_order(
        self,
        row: pd.Series,
        contract: str,
        role: str,
        side: str,
        lots: int,
    ) -> dict[str, Any]:
        key = _row_key(row)
        if key is not None:
            self._consume_action(key)
        order = {
            "order_id": self._new_order_id(),
            "contract": contract,
            "role": role,
            "side": side,
            "price": np.nan,
            "lots": lots,
            "version": self.version,
            "status": "submitted",
            "parent_order_id": self.trade["trade_id"] if self.trade else "",
        }
        self.orders_by_id[order["order_id"]] = order
        self._log_order(row, order, "submit")
        return order

    def _try_flatten(self, row: pd.Series) -> None:
        if self.trade is None:
            return
        key = _row_key(row)
        if key is None:
            return
        if self.trade["target_open"]:
            direction = "sell" if self.trade["direction"] == "long" else "buy"
            order = self.trade.get("target_exit_order")
            if order is None:
                order = self._new_aggressive_order(row, self.target_contract, "target_exit", direction, int(self.config["target_lots"]))
                self.trade["target_exit_order"] = order
            quote = _row_quote(row, direction, int(self.config["target_lots"]), bool(self.config["require_top_of_book_full_lot"]))
            if quote is not None:
                price = float(quote["price"])
                if price > 0:
                    order["status"] = "filled"
                    order["price"] = price
                    self._log_order(row, order, "fill")
                    self.trade["target_exit_price"] = price
                    self.trade["target_exit_key"] = key
                    self.trade["target_exit_time"] = _display_time(row)
                    self.trade["target_open"] = False
            elif not order.get("quote_missing_logged"):
                order["quote_missing_logged"] = True
                self._log_order(row, order, "quote_unavailable")

        if self.trade is not None and self.trade["hedge_open"]:
            direction = "buy" if self.trade["direction"] == "long" else "sell"
            order = self.trade.get("hedge_exit_order")
            if order is None:
                order = self._new_aggressive_order(row, self.hedge_contract, "hedge_exit", direction, int(self.config["hedge_lots"]))
                self.trade["hedge_exit_order"] = order
            quote = self._asof_quote(key, direction, int(self.config["hedge_lots"]))
            if quote is not None:
                price = float(quote["price"])
                if price > 0:
                    order["status"] = "filled"
                    order["price"] = price
                    self._log_order(row, order, "fill", f"quote_key={quote['key']}")
                    self.trade["hedge_exit_price"] = price
                    self.trade["hedge_exit_key"] = key
                    self.trade["hedge_open"] = False
            elif not order.get("quote_missing_logged"):
                order["quote_missing_logged"] = True
                self._log_order(row, order, "quote_unavailable")

        if self.trade is not None and not self.trade["target_open"] and not self.trade["hedge_open"]:
            self._finalize_trade(row, str(self.trade.get("exit_reason") or "flatten"))

    def _mark_pnl(self, row: pd.Series) -> float | None:
        if self.trade is None:
            return None
        target_price = _finite_number(self.trade.get("target_exit_price"))
        if self.trade["target_open"]:
            side = "sell" if self.trade["direction"] == "long" else "buy"
            quote = _row_quote(row, side, int(self.config["target_lots"]), bool(self.config["require_top_of_book_full_lot"]))
            if quote is None:
                return None
            target_price = float(quote["price"])
        if target_price is None:
            return None
        hedge_price = _finite_number(self.trade.get("hedge_exit_price"))
        if self.trade["hedge_open"]:
            side = "buy" if self.trade["direction"] == "long" else "sell"
            key = _row_key(row)
            if key is None:
                return None
            quote = self._asof_quote(key, side, int(self.config["hedge_lots"]))
            if quote is None:
                return None
            hedge_price = float(quote["price"])
        return self._pnl(float(target_price), hedge_price)

    def _pnl(self, target_exit_price: float, hedge_exit_price: float | None) -> float:
        if self.trade is None:
            return np.nan
        target_entry = float(self.trade["target_entry_price"])
        target_lots = int(self.config["target_lots"])
        if self.trade["direction"] == "long":
            target_pnl = (target_exit_price - target_entry) * self.target_multiplier * target_lots
        else:
            target_pnl = (target_entry - target_exit_price) * self.target_multiplier * target_lots
        hedge_entry = _finite_number(self.trade.get("hedge_entry_price"))
        if hedge_entry is None or hedge_exit_price is None:
            return target_pnl
        hedge_lots = int(self.config["hedge_lots"])
        if self.trade["direction"] == "long":
            hedge_pnl = (hedge_entry - hedge_exit_price) * self.hedge_multiplier * hedge_lots
        else:
            hedge_pnl = (hedge_exit_price - hedge_entry) * self.hedge_multiplier * hedge_lots
        return target_pnl + hedge_pnl

    def _update_worst_mark(self, row: pd.Series) -> None:
        if self.trade is None:
            return
        mark = self._mark_pnl(row)
        if mark is None:
            return
        if self.trade["hedge_open"]:
            current = _finite_number(self.trade.get("hedged_worst_mark_pnl"))
            self.trade["hedged_worst_mark_pnl"] = mark if current is None else min(current, mark)
        else:
            self.trade["unhedged_worst_mark_pnl"] = min(float(self.trade["unhedged_worst_mark_pnl"]), mark)
        self.trade["observed_worst_mark_pnl"] = min(float(self.trade["observed_worst_mark_pnl"]), mark)

    def _actual_margin(self) -> float:
        if self.trade is None:
            return np.nan
        hedge_entry = _finite_number(self.trade.get("hedge_entry_price"))
        if hedge_entry is None:
            return np.nan
        return self._margin_for_hedge_entry(hedge_entry)

    def _margin_for_hedge_entry(self, hedge_entry: float) -> float:
        if self.trade is None:
            return np.nan
        rate = _commodity_number(self.config, "margin_rate_by_commodity", "default_margin_rate", self.commodity)
        return (
            float(self.trade["target_entry_price"]) * self.target_multiplier * int(self.config["target_lots"]) * rate
            + hedge_entry * self.hedge_multiplier * int(self.config["hedge_lots"]) * rate
        )

    def _commission(self) -> float:
        if self.trade is None:
            return 0.0
        per_side = _commodity_number(
            self.config,
            "commission_by_commodity",
            "default_commission_per_lot_per_side",
            self.commodity,
        )
        target_sides = 1 + int(_finite_number(self.trade.get("target_exit_price")) is not None)
        hedge_sides = int(_finite_number(self.trade.get("hedge_entry_price")) is not None) + int(
            _finite_number(self.trade.get("hedge_exit_price")) is not None
        )
        return per_side * (target_sides * int(self.config["target_lots"]) + hedge_sides * int(self.config["hedge_lots"]))

    def _finalize_trade(self, row: pd.Series, reason: str, *, unclosed: bool = False) -> None:
        if self.trade is None:
            return
        trade = self.trade
        target_exit = _finite_number(trade.get("target_exit_price"))
        hedge_entry = _finite_number(trade.get("hedge_entry_price"))
        hedge_exit = _finite_number(trade.get("hedge_exit_price"))
        target_pnl = np.nan
        hedge_pnl = np.nan
        gross_pnl = np.nan
        if target_exit is not None:
            target_pnl = self._pnl(target_exit, None)
            if hedge_entry is not None and hedge_exit is not None:
                gross_pnl = self._pnl(target_exit, hedge_exit)
                hedge_pnl = gross_pnl - target_pnl
            elif hedge_entry is None:
                gross_pnl = target_pnl
        commission = self._commission()
        net_pnl = gross_pnl - commission if math.isfinite(gross_pnl) else np.nan
        status = "unclosed_end_of_day" if unclosed else (
            "hedge_failure_exit" if reason == "hedge_failure_exit" else (
                "capital_limit_exit" if reason == "capital_limit_at_hedge" else "closed"
            )
        )
        record = {
            "trade_id": trade["trade_id"],
            "trade_date": self.trade_date,
            "target_contract": self.target_contract,
            "hedge_contract": self.hedge_contract,
            "direction": trade["direction"],
            "fill_key": trade["fill_key"],
            "fill_time": trade["fill_time"],
            "fill_context": trade["fill_context"],
            "event_label": trade["event_label"],
            "fill_evidence": trade["fill_evidence"],
            "target_entry_price": trade["target_entry_price"],
            "hedge_entry_key": trade["hedge_entry_key"],
            "hedge_entry_price": hedge_entry if hedge_entry is not None else np.nan,
            "exit_key": _row_key(row),
            "exit_time": _display_time(row),
            "exit_reason": reason,
            "status": status,
            "target_exit_price": target_exit if target_exit is not None else np.nan,
            "hedge_exit_price": hedge_exit if hedge_exit is not None else np.nan,
            "target_pnl": target_pnl,
            "hedge_pnl": hedge_pnl,
            "gross_pnl": gross_pnl,
            "commission": commission,
            "net_pnl": net_pnl,
            "gross_margin": trade["gross_margin"],
            "unhedged_worst_mark_pnl": trade["unhedged_worst_mark_pnl"],
            "hedged_worst_mark_pnl": trade["hedged_worst_mark_pnl"],
            "observed_worst_mark_pnl": trade["observed_worst_mark_pnl"],
        }
        self.trade_log.append(record)
        self.trade = None
        if not unclosed:
            key = _row_key(row)
            self.cooldown_until_key = (key or 0) + int(self.config["cooldown_ms"])
            self._record_transition(row, "COOLDOWN", reason)

    # ------------------------------------------------------------------
    # 行情访问与日终
    # ------------------------------------------------------------------

    def _asof_quote(self, requested_key: int, side: str, lots: int) -> dict[str, float | int] | None:
        position = int(np.searchsorted(self.hedge_keys, requested_key, side="right") - 1)
        if position < 0:
            return None
        row = self.hedge.iloc[position]
        source_key = _row_key(row)
        if source_key is None or requested_key - source_key > int(self.config["max_quote_age_ms"]):
            return None
        quote = _row_quote(row, side, lots, bool(self.config["require_top_of_book_full_lot"]))
        if quote is None:
            return None
        return {"price": float(quote["price"]), "key": source_key}

    def _event_label(self, fill_key: int) -> str:
        window = int(self.config["detector_event_window_ms"])
        return "detector_event_fill" if any(abs(fill_key - key) <= window for key in self.detector_event_keys) else "normal_move_fill"

    def _finish_day(self) -> None:
        if self.last_row is None:
            return
        if self.trade is not None:
            self.trade["exit_reason"] = "end_of_day_exit"
            self._record_transition(self.last_row, "EMERGENCY_FLATTEN", "end_of_day_exit")
            self._try_flatten(self.last_row)
            if self.trade is not None:
                self._finalize_trade(self.last_row, "end_of_day_exit", unclosed=True)
        for order in self.orders_by_id.values():
            if order["role"] in {"target_buy", "target_sell"} and self._is_live(order):
                order["status"] = "expired"
                self._log_order(self.last_row, order, "expired", "end_of_day")


def _fill_evidence(row: pd.Series, order: Mapping[str, Any], config: Mapping[str, Any]) -> tuple[list[str], bool]:
    """返回被动限价的可观察成交证据；盘口量不足单独保留为未知状态。"""
    limit = float(order["price"])
    is_buy = order["side"] == "buy"
    delta_volume = _finite_number(row.get("delta_volume"))
    last = _finite_number(row.get("LastPrice"))
    interval_vwap = _finite_number(row.get("interval_vwap"))
    if config["fill_model"] == "strict_cross":
        if delta_volume is not None and delta_volume > 0 and last is not None:
            crossed = last < limit if is_buy else last > limit
            return (["last_trade"] if crossed else []), False
        return [], False

    evidence: list[str] = []
    if delta_volume is not None and delta_volume > 0:
        if last is not None and (last <= limit if is_buy else last >= limit):
            evidence.append("last_trade")
        if interval_vwap is not None and (interval_vwap <= limit if is_buy else interval_vwap >= limit):
            evidence.append("interval_vwap")
    book_side = "buy" if is_buy else "sell"
    book = _row_quote(
        row,
        book_side,
        int(order["lots"]),
        bool(config["require_top_of_book_full_lot"]),
    )
    partial_unknown = False
    if book is not None:
        crossed = float(book["price"]) <= limit if is_buy else float(book["price"]) >= limit
        if crossed:
            evidence.append("top_of_book")
    elif _book_price_crosses(row, book_side, limit):
        partial_unknown = True
    return evidence, partial_unknown


def _quote_spread_ok(row: pd.Series, tick_size: float, config: Mapping[str, Any]) -> bool:
    """目标腿只有在总触达深度严格覆盖 N 倍盘口价差时才允许报价。"""
    if tick_size <= 0 or not _quote_row_valid(row):
        return False
    bid = _finite_number(row.get("BidPrice1"))
    ask = _finite_number(row.get("AskPrice1"))
    if bid is None or ask is None:
        return False
    spread_ticks = (ask - bid) / tick_size
    total_quote_ticks = float(config["band_half_width_ticks"]) + float(config["outer_quote_offset_ticks"])
    return total_quote_ticks > spread_ticks * float(config["quote_spread_multiple"])


def _book_price_crosses(row: pd.Series, side: str, limit: float) -> bool:
    """只判断价格相交；调用方据此把一档量不足与无成交证据区分开。"""
    price_col = "AskPrice1" if side == "buy" else "BidPrice1"
    price = _finite_number(row.get(price_col))
    if price is None or price <= 0 or not _quote_row_valid(row):
        return False
    return price <= limit if side == "buy" else price >= limit


def _row_quote(row: pd.Series, side: str, lots: int, require_full_lot: bool) -> dict[str, float] | None:
    """当前快照的可执行一档报价；buy 使用卖一，sell 使用买一。"""
    if side not in {"buy", "sell"} or not _quote_row_valid(row):
        return None
    price_col = "AskPrice1" if side == "buy" else "BidPrice1"
    volume_col = "AskVolume1" if side == "buy" else "BidVolume1"
    price = _finite_number(row.get(price_col))
    volume = _finite_number(row.get(volume_col))
    if price is None or price <= 0:
        return None
    if require_full_lot and (volume is None or volume < lots):
        return None
    return {"price": price, "volume": volume if volume is not None else np.nan}


def _quote_row_valid(row: pd.Series) -> bool:
    bid = _finite_number(row.get("BidPrice1"))
    ask = _finite_number(row.get("AskPrice1"))
    tradable = _row_bool(row, "is_tradable_session", True)
    return bool(tradable and bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid)


def _valid_fair(row: pd.Series) -> float | None:
    fair = _fair_price(row)
    if not math.isfinite(fair) or fair <= 0:
        return None
    if not _row_bool(row, "fair_price_reliable", False):
        return None
    if not _row_bool(row, "is_tradable_session", True) or _row_bool(row, "is_open_protected", False):
        return None
    return fair


def _valid_last_price(row: pd.Series) -> float | None:
    last_price = _finite_number(row.get("LastPrice"))
    if last_price is None or not math.isfinite(last_price) or last_price <= 0:
        return None
    return last_price


def _fair_price(row: pd.Series) -> float:
    value = _finite_number(row.get("fair_price"))
    return value if value is not None else np.nan


def _row_key(row: pd.Series) -> int | None:
    value = _finite_number(row.get("market_time_key"))
    return int(value) if value is not None and value.is_integer() else None


def _display_time(row: pd.Series) -> str:
    value = row.get("display_time", "")
    return "" if value is None or pd.isna(value) else str(value)


def _row_bool(row: pd.Series, key: str, default: bool) -> bool:
    value = row.get(key, default)
    if value is None or pd.isna(value):
        return default
    return bool(value)


def _round_to_tick(price: float, tick_size: float) -> float:
    return round(round(price / tick_size) * tick_size, 10)


def _frame_trade_date(frame: pd.DataFrame) -> str:
    if "trade_date" in frame.columns and not frame.empty:
        return str(frame["trade_date"].iloc[0])
    return ""


def _required_frame_tick_size(frame: pd.DataFrame, commodity: str) -> float:
    value = _frame_tick_size(frame, commodity)
    if value is None:
        raise ValueError("目标或参考合约缺少 tick_size")
    return value


def _required_frame_multiplier(frame: pd.DataFrame, commodity: str) -> int:
    value = _frame_multiplier(frame, commodity)
    if value is None:
        raise ValueError("目标或参考合约缺少 contract_multiplier")
    return value


def _frame_tick_size(frame: pd.DataFrame, commodity: str) -> float | None:
    if not frame.empty and "tick_size" in frame.columns:
        value = _finite_number(frame["tick_size"].iloc[0])
        if value is not None and value > 0:
            return value
    profile = COMMODITY_PROFILES.get(commodity)
    value = _finite_number(profile.get("tick_size")) if profile else None
    return value if value is not None and value > 0 else None


def _frame_multiplier(frame: pd.DataFrame, commodity: str) -> int | None:
    if not frame.empty and "contract_multiplier" in frame.columns:
        value = _finite_number(frame["contract_multiplier"].iloc[0])
        if value is not None and value > 0 and value.is_integer():
            return int(value)
    profile = COMMODITY_PROFILES.get(commodity)
    value = _finite_number(profile.get("contract_multiplier")) if profile else None
    return int(value) if value is not None and value > 0 and value.is_integer() else None


def _commodity_number(config: Mapping[str, Any], map_key: str, default_key: str, commodity: str) -> float:
    values = config.get(map_key, {})
    if isinstance(values, Mapping):
        value = values.get(commodity.upper(), config[default_key])
    else:
        value = config[default_key]
    return float(value)


# ---------------------------------------------------------------------------
# 本地数据入口、候选事件标签与输出
# ---------------------------------------------------------------------------


def _load_required_frames(config: Mapping[str, Any], trade_date: str) -> dict[str, pd.DataFrame]:
    fair_reference_contracts = set(config["fair_reference_contracts"])
    if not config["enable_hedge"]:
        fair_reference_contracts.discard(config["hedge_contract"])
    required = {config["target_contract"], *fair_reference_contracts}
    if config["enable_hedge"]:
        required.add(config["hedge_contract"])
    day_path = _resolve_tick_day_path(Path(str(config["tick_data_root"])), trade_date)
    daily_bounds = load_daily_bounds(trade_date, daily_root=str(config["daily_data_root"]))
    frames: dict[str, pd.DataFrame] = {}
    for contract_file in iter_day_contract_files(day_path):
        info = parse_contract_file(contract_file.file_name, None)
        if info.commodity != config["commodity"] or info.contract not in required:
            continue
        raw = load_contract_snapshots(contract_file)
        if raw.empty:
            continue
        prepared = prepare_contract_snapshots(raw, daily_bounds=daily_bounds)
        if not prepared.empty:
            frames[info.contract] = prepared
    return frames


def _resolve_tick_day_path(tick_root: Path, trade_date: str) -> Path:
    candidates = (
        tick_root / trade_date[:6] / f"{trade_date}.zip",
        tick_root / trade_date[:6] / trade_date,
        tick_root / f"{trade_date}.zip",
        tick_root / trade_date,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"找不到 {trade_date} 的 tick 文件")


def _load_event_rows(path_text: str) -> pd.DataFrame:
    columns = ["trade_date", "contract", "anchor_seq"]
    if not path_text:
        return pd.DataFrame(columns=columns)
    path = Path(path_text)
    if not path.is_file():
        raise FileNotFoundError(f"候选事件文件不存在: {path}")
    raw = pd.read_csv(path)
    required = {"交易日", "合约", "事件锚点结束序号"}
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"候选事件 CSV 缺少字段: {', '.join(missing)}")
    out = pd.DataFrame(
        {
            "trade_date": raw["交易日"].map(lambda value: _required_date(value, "候选事件.交易日")),
            "contract": raw["合约"].astype(str).str.upper().str.strip(),
            "anchor_seq": pd.to_numeric(raw["事件锚点结束序号"], errors="coerce"),
        }
    )
    out = out.loc[out["anchor_seq"].notna() & (out["anchor_seq"] >= 0)].copy()
    out["anchor_seq"] = out["anchor_seq"].astype(int)
    return out.reset_index(drop=True)


def _event_keys_for_day(events: pd.DataFrame, trade_date: str, contract: str, target: pd.DataFrame) -> set[int]:
    if events.empty or "snapshot_seq_end" not in target.columns:
        return set()
    selected = events.loc[(events["trade_date"] == trade_date) & (events["contract"] == contract)]
    if selected.empty:
        return set()
    sequences = pd.to_numeric(target["snapshot_seq_end"], errors="coerce")
    keys = pd.to_numeric(target["market_time_key"], errors="coerce")
    lookup = {
        int(sequence): int(key)
        for sequence, key in zip(sequences, keys, strict=True)
        if pd.notna(sequence) and pd.notna(key)
    }
    return {lookup[seq] for seq in selected["anchor_seq"].tolist() if seq in lookup}


def build_programmatic_summary(
    trades: pd.DataFrame,
    orders: pd.DataFrame,
    transitions: pd.DataFrame,
) -> pd.DataFrame:
    """输出整体和按日的最小统计，不把快照触价假设夸大成真实成交率。"""
    dates = sorted(
        set(trades.get("trade_date", pd.Series(dtype=str)).dropna().astype(str))
        | set(orders.get("trade_date", pd.Series(dtype=str)).dropna().astype(str))
        | set(transitions.get("trade_date", pd.Series(dtype=str)).dropna().astype(str))
    )
    rows = [_summary_row("overall", "ALL", trades, orders, transitions)]
    for trade_date in dates:
        rows.append(
            _summary_row(
                "day",
                trade_date,
                trades.loc[trades["trade_date"] == trade_date] if not trades.empty else trades,
                orders.loc[orders["trade_date"] == trade_date] if not orders.empty else orders,
                transitions.loc[transitions["trade_date"] == trade_date] if not transitions.empty else transitions,
            )
        )
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


def _summary_row(
    scope: str,
    trade_date: str,
    trades: pd.DataFrame,
    orders: pd.DataFrame,
    transitions: pd.DataFrame,
) -> dict[str, Any]:
    filled = len(trades)
    closed = trades.loc[trades["status"] != "unclosed_end_of_day"] if not trades.empty else trades
    net = pd.to_numeric(trades.get("net_pnl", pd.Series(dtype=float)), errors="coerce").dropna()
    margins = pd.to_numeric(trades.get("gross_margin", pd.Series(dtype=float)), errors="coerce").dropna()
    order_events = orders.get("event", pd.Series(dtype=str)).fillna("")
    reasons = transitions.get("reason", pd.Series(dtype=str)).fillna("")
    states = transitions.get("to_state", pd.Series(dtype=str)).fillna("")
    contract = ""
    if not trades.empty:
        contract = str(trades["target_contract"].iloc[0])
    elif not orders.empty:
        contract = str(orders.loc[orders["role"].isin(["target_buy", "target_sell"]), "contract"].iloc[0]) if (orders["role"].isin(["target_buy", "target_sell"])).any() else ""
    return {
        "scope": scope,
        "trade_date": trade_date,
        "target_contract": contract,
        "fill_count": filled,
        "detector_event_fill_count": int((trades.get("event_label", pd.Series(dtype=str)) == "detector_event_fill").sum()),
        "normal_move_fill_count": int((trades.get("event_label", pd.Series(dtype=str)) == "normal_move_fill").sum()),
        "fill_during_replace_count": int((trades.get("fill_context", pd.Series(dtype=str)) == "fill_during_replace").sum()),
        "closed_count": int(len(closed)),
        "hedge_failure_count": int((trades.get("status", pd.Series(dtype=str)) == "hedge_failure_exit").sum()),
        "capital_limit_exit_count": int((trades.get("status", pd.Series(dtype=str)) == "capital_limit_exit").sum()),
        "net_pnl": float(net.sum()) if not net.empty else 0.0,
        "win_rate": float((net > 0).mean()) if not net.empty else np.nan,
        "worst_net_pnl": float(net.min()) if not net.empty else np.nan,
        "max_gross_margin": float(margins.max()) if not margins.empty else np.nan,
        "order_action_count": int(order_events.isin(["submit", "cancel_requested"]).sum()),
        "peak_order_actions_per_minute": _peak_actions_per_minute(orders),
        "reprice_count": int(reasons.isin(["last_price_down_confirmed", "last_price_up_confirmed"]).sum()),
        "pause_count": int(((states == "PAUSED") & (reasons != "start")).sum()),
        "hedge_failure_rate": _safe_divide(
            int((trades.get("status", pd.Series(dtype=str)) == "hedge_failure_exit").sum()),
            filled,
        ),
    }


def write_programmatic_simulation_outputs(result: Mapping[str, Any]) -> dict[str, str]:
    """写出 V2 规定的四类结果及本次实际参数。"""
    output_dir = Path(str(result["config"]["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "transitions": output_dir / "quote_state_transitions.csv",
        "orders": output_dir / "order_lifecycle.csv",
        "trades": output_dir / "programmatic_trades.csv",
        "summary": output_dir / "programmatic_summary.csv",
        "skipped_days": output_dir / "programmatic_skipped_days.csv",
        "config": output_dir / "run_config.json",
        "report": output_dir / "programmatic_report.html",
    }
    result["transitions"].to_csv(paths["transitions"], index=False, encoding="utf-8-sig")
    result["orders"].to_csv(paths["orders"], index=False, encoding="utf-8-sig")
    result["trades"].to_csv(paths["trades"], index=False, encoding="utf-8-sig")
    result["summary"].to_csv(paths["summary"], index=False, encoding="utf-8-sig")
    result["skipped_days"].to_csv(paths["skipped_days"], index=False, encoding="utf-8-sig")
    payload = {
        "config": {key: value for key, value in result["config"].items() if not key.startswith("_")},
        "warnings": list(result["warnings"]),
    }
    paths["config"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["report"].write_text(_render_programmatic_report(result), encoding="utf-8")
    return {name: str(path) for name, path in paths.items()}


TRADE_CONTEXT_WINDOW_MS = 10_000

TARGET_CONTEXT_COLUMNS = [
    "display_time", "LastPrice", "Volume", "Turnover", "BidPrice1", "BidVolume1",
    "AskPrice1", "AskVolume1", "delta_volume", "delta_turnover", "interval_vwap", "fair_price",
    "last_down_ticks", "fair_price_reliable", "valid_peer_count", "peer_contracts",
]

REFERENCE_CONTEXT_COLUMNS = [
    "display_time", "LastPrice", "Volume", "Turnover", "BidPrice1", "BidVolume1",
    "AskPrice1", "AskVolume1", "AveragePrice", "OpenInterest", "UpperLimitPrice",
    "LowerLimitPrice", "delta_volume", "delta_turnover", "interval_vwap",
]


def build_trade_contexts(
    trades: pd.DataFrame,
    target: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    config: Mapping[str, Any],
    *,
    orders: pd.DataFrame | None = None,
    transitions: pd.DataFrame | None = None,
) -> dict[str, dict[str, Any]]:
    """为每笔模拟成交截取入场前 10 秒到退出后 10 秒的行情复盘窗口。"""
    contexts: dict[str, dict[str, Any]] = {}
    if trades.empty:
        return contexts
    if "last_down_ticks" not in target.columns:
        target = target.copy()
        _attach_context_metrics(target, _frame_tick_size(target, str(config.get("commodity") or "")))
    for trade in trades.to_dict("records"):
        fill_key = _context_key_number(trade.get("fill_key"))
        if fill_key is None:
            continue
        exit_key = _context_key_number(trade.get("exit_key"))
        if exit_key is None:
            exit_key = _last_frame_key(target)
        if exit_key is None:
            continue
        start_key = fill_key - TRADE_CONTEXT_WINDOW_MS
        end_key = exit_key + TRADE_CONTEXT_WINDOW_MS
        phases = {
            "目标成交": fill_key,
            "参考腿成交": _context_key_number(trade.get("hedge_entry_key")),
            "退出": _context_key_number(trade.get("exit_key")),
        }
        target_rows = _context_rows(target, start_key, end_key, phases, TARGET_CONTEXT_COLUMNS)
        tick_size = _frame_tick_size(target, str(config.get("commodity") or ""))
        pre_fill_quote_rows = _build_pre_fill_quote_rows(
            target,
            fill_key,
            {"目标成交": phases["目标成交"]},
            config,
            tick_size,
            transitions,
        )
        flow_events = _build_trade_flow_events(
            trade,
            orders,
            transitions,
            start_key,
            end_key,
        )
        contracts: dict[str, dict[str, Any]] = {}
        reference_contracts = [str(code).upper() for code in config["fair_reference_contracts"]]
        hedge_contract = str(config["hedge_contract"]).upper()
        for code in dict.fromkeys([*reference_contracts, hedge_contract]):
            frame = frames.get(code)
            if frame is None:
                continue
            roles = []
            if code in reference_contracts:
                roles.append("合理价参考")
            if code == hedge_contract:
                roles.append("对冲合约")
            contracts[code] = {
                "roles": roles,
                "rows": _context_rows(frame, start_key, end_key, phases, REFERENCE_CONTEXT_COLUMNS),
            }
        context_key = _trade_context_id(trade)
        contexts[context_key] = {
            "window": {
                "start_key": start_key,
                "end_key": end_key,
                "start_time": target_rows[0].get("display_time") if target_rows else None,
                "end_time": target_rows[-1].get("display_time") if target_rows else None,
                "before_after_ms": TRADE_CONTEXT_WINDOW_MS,
            },
            "phases": {label: key for label, key in phases.items() if key is not None},
            "phase_times": {
                "目标成交": trade.get("fill_time"),
                "参考腿成交": _asof_display_time(frames.get(hedge_contract), phases["参考腿成交"]),
                "退出": trade.get("exit_time"),
            },
            "pre_fill_quote_rows": pre_fill_quote_rows,
            "flow_events": flow_events,
            "target_rows": target_rows,
            "contracts": contracts,
        }
    return contexts


def _build_pre_fill_quote_rows(
    target: pd.DataFrame,
    fill_key: int,
    phases: Mapping[str, int | None],
    config: Mapping[str, Any],
    tick_size: float | None,
    transitions: pd.DataFrame | None,
) -> list[dict[str, Any]]:
    columns = [*TARGET_CONTEXT_COLUMNS, "market_time_key"]
    rows = _context_rows(target, fill_key - TRADE_CONTEXT_WINDOW_MS, fill_key, phases, columns)
    if not rows:
        return []
    width = _finite_number(config.get("band_half_width_ticks"))
    outer = _finite_number(config.get("outer_quote_offset_ticks"))
    step = _finite_number(config.get("reanchor_step_ticks"))
    transition_frame = transitions if transitions is not None else pd.DataFrame()
    if not transition_frame.empty and "market_time_key" in transition_frame:
        transition_frame = transition_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        transition_keys = pd.to_numeric(transition_frame["market_time_key"], errors="coerce").to_numpy(dtype=float)
    else:
        transition_keys = np.array([], dtype=float)
    for row in rows:
        key = _context_key_number(row.get("market_time_key"))
        transition = None
        if key is not None and transition_keys.size:
            position = int(np.searchsorted(transition_keys, key, side="right") - 1)
            if position >= 0:
                transition = transition_frame.iloc[position]
        anchor = _finite_number(transition.get("grid_anchor") if transition is not None else None)
        state = str(transition.get("to_state") or "") if transition is not None else ""
        row.update(
            {
                "W_ticks": width,
                "D_ticks": outer,
                "S_ticks": step,
                "grid_anchor": anchor,
                "band_lower": None,
                "band_upper": None,
                "buy_limit": _finite_number(transition.get("buy_limit") if transition is not None else None),
                "sell_limit": _finite_number(transition.get("sell_limit") if transition is not None else None),
                "quote_state": state or None,
                "quote_reason": str(transition.get("reason") or "") if transition is not None else None,
                "quote_active": state in {"FLAT_QUOTING", "REPLACE_PENDING"},
            }
        )
        if anchor is not None and tick_size is not None:
            row["band_lower"] = _round_to_tick(anchor - float(width or 0) * tick_size, tick_size)
            row["band_upper"] = _round_to_tick(anchor + float(width or 0) * tick_size, tick_size)
        row.pop("market_time_key", None)
    return rows


def _build_trade_flow_events(
    trade: Mapping[str, Any],
    orders: pd.DataFrame | None,
    transitions: pd.DataFrame | None,
    start_key: int,
    end_key: int,
) -> list[dict[str, Any]]:
    trade_id = str(trade.get("trade_id") or "")
    fill_key = _context_key_number(trade.get("fill_key"))
    exit_key = _context_key_number(trade.get("exit_key"))
    events: list[dict[str, Any]] = []
    if transitions is not None and not transitions.empty:
        transition_window = transitions.loc[
            pd.to_numeric(transitions["market_time_key"], errors="coerce").between(start_key, end_key)
        ]
        for row in transition_window.to_dict("records"):
            to_state = str(row.get("to_state") or "")
            reason = str(row.get("reason") or "")
            phase = "目标成交" if reason == "target_fill" else (
                "未对冲" if reason == "hedge_disabled" else (
                    "对冲" if reason in {"hedge_fill", "hedge_quote_timeout", "capital_limit_at_hedge"} else (
                        "平仓" if to_state in {"FLATTENING", "EMERGENCY_FLATTEN"} else "报价状态"
                    )
                )
            )
            events.append(
                {
                    "market_time_key": _context_key_number(row.get("market_time_key")),
                    "display_time": row.get("display_time"),
                    "phase": phase,
                    "event_type": "状态切换",
                    "action": f"{row.get('from_state') or '—'} → {to_state or '—'}",
                    "contract": trade.get("target_contract"),
                    "role": "state",
                    "side": None,
                    "price": None,
                    "lots": None,
                    "from_state": row.get("from_state"),
                    "to_state": row.get("to_state"),
                    "reason": row.get("reason"),
                    "detail": None,
                    "_sort_priority": 60 if reason == "target_fill" else (
                        55 if reason == "hedge_fill" else (90 if to_state == "COOLDOWN" else 10 if phase == "平仓" else 5)
                    ),
                }
            )
    relevant_order_rows: list[dict[str, Any]] = []
    if orders is not None and not orders.empty:
        order_frame = orders.copy()
        keys = pd.to_numeric(order_frame["market_time_key"], errors="coerce")
        parent_match = order_frame["parent_order_id"].fillna("").astype(str).eq(trade_id)
        target_fill = (
            order_frame["contract"].fillna("").astype(str).str.upper().eq(str(trade.get("target_contract") or "").upper())
            & order_frame["event"].eq("fill")
            & keys.eq(fill_key)
        )
        target_exit_fill = (
            order_frame["contract"].fillna("").astype(str).str.upper().eq(str(trade.get("target_contract") or "").upper())
            & order_frame["event"].eq("fill")
            & keys.eq(exit_key)
        )
        selected = order_frame.loc[
            keys.between(start_key, end_key)
            & (parent_match | target_fill | target_exit_fill)
            & order_frame["event"].isin({"submit", "fill", "quote_unavailable", "capital_rejected"})
        ]
        relevant_order_rows = selected.to_dict("records")
    for row in relevant_order_rows:
        role = str(row.get("role") or "")
        phase = "目标成交" if role in TARGET_ORDER_ROLES else ("对冲" if role == "hedge_entry" else "平仓")
        events.append(
            {
                "market_time_key": _context_key_number(row.get("market_time_key")),
                "display_time": row.get("display_time"),
                "phase": phase,
                "event_type": "订单事件",
                "action": row.get("event"),
                "contract": row.get("contract"),
                "role": role,
                "side": row.get("side"),
                "price": _finite_number(row.get("price")),
                "lots": _finite_number(row.get("lots")),
                "from_state": None,
                "to_state": row.get("state"),
                "reason": None,
                "detail": row.get("detail"),
                "_sort_priority": {"submit": 20, "quote_unavailable": 25, "capital_rejected": 25, "fill": 50}.get(
                    str(row.get("event") or ""), 30
                ),
            }
        )
    has_target_fill = any(
        event["phase"] == "目标成交" and event["action"] == "fill" for event in events
    )
    if fill_key is not None and not has_target_fill:
        direction = "buy" if trade.get("direction") == "long" else "sell"
        events.append(
            {
                "market_time_key": fill_key,
                "display_time": trade.get("fill_time"),
                "phase": "目标成交",
                "event_type": "订单事件",
                "action": "fill",
                "contract": trade.get("target_contract"),
                "role": "target_buy" if direction == "buy" else "target_sell",
                "side": direction,
                "price": _finite_number(trade.get("target_entry_price")),
                "lots": None,
                "from_state": None,
                "to_state": None,
                "reason": trade.get("fill_evidence"),
                "detail": "成交证据",
                "_sort_priority": 40,
            }
        )
    events.sort(key=lambda event: (event["market_time_key"] is None, event["market_time_key"] or 0, event["_sort_priority"]))
    for event in events:
        event.pop("_sort_priority", None)
    return events


def _attach_context_metrics(target: pd.DataFrame, tick_size: float | None) -> None:
    if tick_size is None or "last_down_ticks" in target.columns:
        return
    last = pd.to_numeric(target["LastPrice"], errors="coerce")
    fair = pd.to_numeric(target["fair_price"], errors="coerce")
    target["last_down_ticks"] = np.where((last > 0) & (fair > 0), ((fair - last) / tick_size).round(2), np.nan)


def _trade_context_id(trade: Mapping[str, Any]) -> str:
    return "::".join(str(trade.get(key) or "") for key in ("instrument", "scenario_id", "trade_id"))


def _context_key_number(value: Any) -> int | None:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return int(number) if pd.notna(number) else None


def _last_frame_key(frame: pd.DataFrame) -> int | None:
    keys = pd.to_numeric(frame.get("market_time_key", pd.Series(dtype=float)), errors="coerce").dropna()
    return int(keys.iloc[-1]) if not keys.empty else None


def _asof_display_time(frame: pd.DataFrame | None, key: int | None) -> str | None:
    if frame is None or key is None or frame.empty:
        return None
    keys = pd.to_numeric(frame.get("market_time_key", pd.Series(dtype=float)), errors="coerce").to_numpy(dtype=float)
    position = int(np.searchsorted(keys, key, side="right") - 1)
    if position < 0 or "display_time" not in frame.columns:
        return None
    value = frame.iloc[position]["display_time"]
    return str(value) if pd.notna(value) else None


def _context_rows(
    frame: pd.DataFrame,
    start_key: int,
    end_key: int,
    phases: Mapping[str, int | None],
    columns: list[str],
) -> list[dict[str, Any]]:
    keys = frame["market_time_key"].to_numpy(copy=False)
    start = int(np.searchsorted(keys, start_key, side="left"))
    end = int(np.searchsorted(keys, end_key, side="right"))
    window = frame.iloc[start:end].copy()
    if window.empty:
        return []
    window_keys = pd.to_numeric(window["market_time_key"], errors="coerce").to_numpy(dtype=float)
    marker_map: dict[int, list[str]] = {}
    for label, key in phases.items():
        if key is None:
            continue
        position = int(np.searchsorted(window_keys, key, side="right") - 1)
        if position >= 0:
            marker_map.setdefault(position, []).append(label)
    available = [column for column in columns if column in window.columns]
    rows = json.loads(window[available].to_json(orient="records", force_ascii=False))
    for index, row in enumerate(rows):
        row["关键时点"] = "、".join(marker_map.get(index, []))
    return rows


def _render_programmatic_report(result: Mapping[str, Any]) -> str:
    summary_table = result["summary"].to_html(index=False, border=0, classes="summary", justify="left", escape=True)
    warnings_html = "".join(f"<li>{escape(str(item))}</li>" for item in result["warnings"])
    config_view = {key: value for key, value in result["config"].items() if not key.startswith("_")}
    config_json = escape(json.dumps(config_view, ensure_ascii=False, indent=2))
    trades = result.get("trades")
    payload = {
        "trades": json.loads(trades.to_json(orient="records", force_ascii=False)) if trades is not None and not trades.empty else [],
        "trade_contexts": result.get("trade_contexts", {}),
    }
    payload_json = json.dumps(payload, ensure_ascii=False, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return (
        _SIMULATION_REPORT_TEMPLATE.replace("__SUMMARY__", summary_table)
        .replace("__WARNINGS__", warnings_html)
        .replace("__CONFIG__", config_json)
        .replace("__PAYLOAD__", payload_json)
    )


_SIMULATION_REPORT_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>乌龙指程序化状态机回放</title>
<style>
body{max-width:1500px;margin:0 auto;padding:26px;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
h1{margin:0 0 6px}h2{margin:30px 0 12px;font-size:19px}.muted{color:#667085}
.note,.warning{padding:12px 14px;background:#fff6df;border-left:4px solid #e28b00;border-radius:7px;margin:18px 0}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#f7f9fc;padding:12px;border-radius:7px;max-height:340px;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #edf0f4;text-align:left;white-space:nowrap}
thead th{background:#f7f9fc}.scroll,.table-wrap{max-height:560px;overflow:auto;background:#fff;border:1px solid #e4e8ef;border-radius:10px}
.summary th{background:#f6f8fa}.target-table{overflow-x:hidden}.target-table table{table-layout:fixed}.target-table th,.target-table td{white-space:normal;word-break:break-word;padding:7px 5px;font-size:11px}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}button,select{font:inherit;padding:7px 10px;border:1px solid #cbd5e1;border-radius:7px;background:#fff}button{cursor:pointer}
.tag{border-radius:999px;padding:3px 8px;font-size:12px;background:#eef2f6}.good{background:#e7f6ec;color:#146c35}.bad{background:#ffebe9;color:#b42318}.warn{background:#fff4d6;color:#8a5700}
.negative{color:#b42318;font-weight:650}.positive{color:#146c35;font-weight:650}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:18px 0}.card{background:#fff;border:1px solid #e4e8ef;border-radius:10px;box-shadow:0 1px 3px #dfe5ee;padding:13px}.card span{display:block;color:#667085;font-size:12px}.card strong{display:block;font-size:16px;margin-top:4px;word-break:break-all}
.modal{position:fixed;inset:0;z-index:20;padding:26px;overflow:auto;background:rgba(15,23,42,.48)}.modal-panel{max-width:1440px;margin:auto;background:#fff;border-radius:12px;padding:20px;box-shadow:0 20px 80px rgba(15,23,42,.3)}.modal-topbar{display:flex;justify-content:space-between;gap:12px;align-items:center}.modal-close{background:#172033;color:#fff;border-color:#172033}
.marker{background:#fee2e2;font-weight:700}.contract-title{margin:24px 0 8px;font-size:16px}.detail-button{color:#fff;background:#1769aa;border-color:#1769aa}.hidden{display:none}
</style>
</head>
<body>
<h1>乌龙指程序化状态机回放</h1>
<p class="muted">全天 500ms 快照状态机回放；候选事件仅作事后标签。点“查看详情”复盘单笔成交。</p>
<div class="warning"><strong>结果边界</strong><ul>__WARNINGS__</ul></div>
<h2>汇总</h2><div class="scroll">__SUMMARY__</div>
<h2>单笔模拟成交</h2>
<div class="toolbar">
<label>成交分类 <select id="event-filter"><option value="">全部</option><option value="detector_event_fill">候选事件成交</option><option value="normal_move_fill">正常行情成交</option></select></label>
<label>方向 <select id="direction-filter"><option value="">全部</option><option value="long">买入目标</option><option value="short">卖出目标</option></select></label>
<label>成交证据 <select id="evidence-filter"><option value="">全部</option></select></label>
<label>退出原因 <select id="exit-filter"><option value="">全部</option></select></label>
<span id="trade-count" class="muted"></span>
</div>
<div class="table-wrap"><table><thead><tr><th>交易日</th><th>成交时间</th><th>方向</th><th>成交分类</th><th>成交证据</th><th>目标入场</th><th>参考腿入场</th><th>退出原因</th><th>目标盈亏</th><th>净收益</th><th>未对冲最差</th><th>复盘</th></tr></thead><tbody id="trade-body"></tbody></table></div>
<details><summary>本次生效参数</summary><pre>__CONFIG__</pre></details>
<section id="trade-modal" class="modal hidden" role="dialog" aria-modal="true">
<div class="modal-panel"><div class="modal-topbar"><strong id="modal-title">单笔模拟成交复盘</strong><button id="modal-close" class="modal-close">关闭</button></div>
<p class="note">复盘窗口为目标成交前 10 秒至退出后 10 秒；参考合约关键时点均使用当时或之前最近快照。</p>
<div id="modal-cards" class="cards"></div><div id="modal-content"></div>
<details><summary>该笔交易原始字段</summary><pre id="modal-raw"></pre></details></div>
</section>
<script id="sim-payload" type="application/json">__PAYLOAD__</script>
<script>
const data=JSON.parse(document.getElementById('sim-payload').textContent);
const eventLabels={detector_event_fill:'候选事件成交',normal_move_fill:'正常行情成交'};
const statusLabels={closed:'已正常退出',hedge_failure_exit:'对冲失败退出',capital_limit_exit:'保证金限制退出',unclosed_end_of_day:'收盘未平'};
const exitLabels={hedged_exit:'对冲后平仓',no_hedge_exit:'无对冲延迟平仓',emergency_flatten:'紧急平仓',hedge_failure_exit:'对冲失败退出',end_of_day_exit:'收盘平仓'};
const evidenceLabels={last_trade:'LastPrice',interval_vwap:'区间均价',top_of_book:'买卖一'};
const trades=data.trades||[],tradeContexts=data.trade_contexts||{};
const targetColumns=[['关键时点','关键时点'],['display_time','更新时间'],['LastPrice','最新成交价'],['Volume','累计成交量'],['Turnover','累计成交额'],['BidPrice1','买一价'],['BidVolume1','买一量'],['AskPrice1','卖一价'],['AskVolume1','卖一量'],['delta_volume','区间增量成交量'],['delta_turnover','区间增量成交额'],['interval_vwap','区间成交均价'],['fair_price','合理价'],['last_down_ticks','末笔向下偏离_跳'],['fair_price_reliable','合理价可靠'],['valid_peer_count','有效参考数'],['peer_contracts','有效参考合约']];
const referenceColumns=[['关键时点','关键时点'],['display_time','更新时间'],['LastPrice','最新成交价'],['Volume','累计成交量'],['Turnover','累计成交额'],['BidPrice1','买一价'],['BidVolume1','买一量'],['AskPrice1','卖一价'],['AskVolume1','卖一量'],['AveragePrice','原始平均价'],['OpenInterest','持仓量'],['UpperLimitPrice','涨停价'],['LowerLimitPrice','跌停价'],['delta_volume','区间增量成交量'],['delta_turnover','区间增量成交额'],['interval_vwap','区间成交均价']];
const number=v=>v===null||v===undefined||Number.isNaN(Number(v))?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2});
const display=v=>v===null||v===undefined||v===''?'—':typeof v==='number'?number(v):String(v);
const money=v=>v===null||v===undefined?'—':(Number(v)>=0?'+':'')+number(v);
const labelEvidence=v=>(v||'—').split('+').map(x=>evidenceLabels[x]||x).join(' + ');
function tradeContextKey(x){return (x.instrument||'')+'::'+(x.scenario_id||'')+'::'+x.trade_id}
function contextTable(rows,columns,target=false){const head=columns.map(([,label])=>'<th>'+label+'</th>').join('');const body=rows.map(row=>'<tr class="'+(row['关键时点']?'marker':'')+'">'+columns.map(([key])=>'<td>'+display(row[key])+'</td>').join('')+'</tr>').join('')||'<tr><td colspan="'+columns.length+'">该窗口无可用快照。</td></tr>';return '<div class="table-wrap'+(target?' target-table':'')+'"><table><thead><tr>'+head+'</tr></thead><tbody>'+body+'</tbody></table></div>'}
function openTradeDetail(trade){const context=tradeContexts[tradeContextKey(trade)];if(!context){alert('该笔交易未生成复盘上下文。');return}document.getElementById('modal-title').textContent=trade.trade_id;const phaseTimes=context.phase_times||{};const hold=trade.exit_key===null||trade.exit_key===undefined?'—':((Number(trade.exit_key)-Number(trade.fill_key))/1000).toFixed(1)+' 秒';const cards=[['交易日',display(trade.trade_date)],['目标/对冲合约',trade.target_contract+' / '+(trade.hedge_contract||'—')],['方向',trade.direction==='long'?'买入目标':'卖出目标'],['成交分类',eventLabels[trade.event_label]||trade.event_label],['成交证据',labelEvidence(trade.fill_evidence)],['目标成交价',number(trade.target_entry_price)],['对冲成交价',(trade.hedge_contract||'—')+'：'+number(trade.hedge_entry_price)],['目标成交',phaseTimes['目标成交']||trade.fill_time],['参考腿成交',phaseTimes['参考腿成交']||'未成交'],['退出',phaseTimes['退出']||'未退出'],['持仓时长',hold],['退出原因',exitLabels[trade.exit_reason]||trade.exit_reason||'—'],['状态',statusLabels[trade.status]||trade.status],['目标/对冲盈亏',money(trade.target_pnl)+' / '+money(trade.hedge_pnl)],['净收益',money(trade.net_pnl)],['未对冲最差',money(trade.unhedged_worst_mark_pnl)],['对冲后最差',money(trade.hedged_worst_mark_pnl)]];document.getElementById('modal-cards').innerHTML=cards.map(([k,v])=>'<div class="card"><span>'+k+'</span><strong>'+v+'</strong></div>').join('');let content='<h2>目标合约 '+trade.target_contract+'</h2><p class="muted">完整持仓窗口：'+display(context.window.start_time)+' 至 '+display(context.window.end_time)+'（目标成交前 '+number(context.window.before_after_ms/1000)+' 秒至退出后 '+number(context.window.before_after_ms/1000)+' 秒）</p>'+contextTable(context.target_rows,targetColumns,true);Object.entries(context.contracts||{}).forEach(([code,part])=>{content+='<h3 class="contract-title">'+code+' <span class="muted">'+(part.roles||[]).join(' / ')+'</span></h3>'+contextTable(part.rows||[],referenceColumns)});document.getElementById('modal-content').innerHTML=content;document.getElementById('modal-raw').textContent=JSON.stringify(trade,null,2);document.getElementById('trade-modal').classList.remove('hidden')}
function fillOptions(id,values,labels={}){const el=document.getElementById(id),old=el.value;el.innerHTML='<option value="">全部</option>'+[...new Set(values.filter(Boolean))].sort().map(x=>'<option value="'+x+'">'+(labels[x]||labelEvidence(x))+'</option>').join('');el.value=old;}
function renderTrades(){const event=document.getElementById('event-filter').value,direction=document.getElementById('direction-filter').value,evidence=document.getElementById('evidence-filter').value,exit=document.getElementById('exit-filter').value;const rows=trades.filter(x=>(!event||x.event_label===event)&&(!direction||x.direction===direction)&&(!evidence||x.fill_evidence===evidence)&&(!exit||x.exit_reason===exit));document.getElementById('trade-count').textContent=rows.length+' 笔';document.getElementById('trade-body').innerHTML=rows.sort((a,b)=>Number(a.fill_key)-Number(b.fill_key)).map(x=>'<tr><td>'+display(x.trade_date)+'</td><td>'+x.fill_time+'</td><td>'+(x.direction==='long'?'买入目标':'卖出目标')+'</td><td><span class="tag '+(x.event_label==='normal_move_fill'?'warn':'good')+'">'+(eventLabels[x.event_label]||x.event_label)+'</span></td><td>'+labelEvidence(x.fill_evidence)+'</td><td>'+number(x.target_entry_price)+'</td><td>'+number(x.hedge_entry_price)+'</td><td>'+(exitLabels[x.exit_reason]||x.exit_reason||'—')+'</td><td>'+money(x.target_pnl)+'</td><td class="'+(Number(x.net_pnl)<0?'negative':'positive')+'">'+money(x.net_pnl)+'</td><td>'+money(x.unhedged_worst_mark_pnl)+'</td><td><button class="detail-button" data-context="'+tradeContextKey(x)+'">查看详情</button></td></tr>').join('')||'<tr><td colspan="12">当前筛选下无成交。</td></tr>';document.querySelectorAll('[data-context]').forEach(button=>button.onclick=()=>openTradeDetail(trades.find(x=>tradeContextKey(x)===button.dataset.context)));}
function init(){fillOptions('evidence-filter',trades.map(x=>x.fill_evidence));fillOptions('exit-filter',trades.map(x=>x.exit_reason),exitLabels);['event-filter','direction-filter','evidence-filter','exit-filter'].forEach(id=>document.getElementById(id).onchange=renderTrades);document.getElementById('modal-close').onclick=()=>document.getElementById('trade-modal').classList.add('hidden');document.getElementById('trade-modal').onclick=e=>{if(e.target.id==='trade-modal')e.currentTarget.classList.add('hidden')};document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('trade-modal').classList.add('hidden')});renderTrades();}
function flowTableEnhanced(events){
const roleLabels={state:'状态',target_buy:'目标买单',target_sell:'目标卖单',hedge_entry:'对冲开仓',target_exit:'目标平仓',hedge_exit:'对冲平仓'};
const actionLabels={state_change:'状态切换',submit:'提交',fill:'成交',quote_unavailable:'盘口不可用',capital_rejected:'资金限制'};
const stateLabels={PAUSED:'暂停',FLAT_QUOTING:'双向挂单',REPLACE_PENDING:'重定锚换单',COOLDOWN:'冷却',LONG_PENDING_HEDGE:'多头待对冲',SHORT_PENDING_HEDGE:'空头待对冲',UNHEDGED_POSITION:'未对冲持仓',HEDGED_POSITION:'已对冲持仓',FLATTENING:'平仓中',EMERGENCY_FLATTEN:'紧急平仓'};
const value=(row,key)=>{if(key==='role')return roleLabels[row[key]]||row[key]||'—';if(key==='action')return actionLabels[row[key]]||row[key]||'—';if(key==='side')return row[key]==='buy'?'买入':row[key]==='sell'?'卖出':'—';if(key==='from_state'||key==='to_state')return stateLabels[row[key]]||row[key]||'—';return display(row[key]);};
const columns=[['display_time','时间'],['phase','阶段'],['event_type','类型'],['contract','合约'],['role','角色'],['side','方向'],['action','动作'],['price','价格'],['lots','手数'],['from_state','原状态'],['to_state','新状态'],['reason','原因'],['detail','说明']];
const head=columns.map(([,label])=>`<th>${label}</th>`).join('');
const body=(events||[]).map(row=>`<tr>${columns.map(([key])=>`<td>${value(row,key)}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${columns.length}">没有可用的关键流程记录。</td></tr>`;
return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function openTradeDetail(trade){
const context=tradeContexts[tradeContextKey(trade)];
if(!context){alert('该笔交易未生成复盘上下文。');return}
document.getElementById('modal-title').textContent=trade.trade_id;
const phaseTimes=context.phase_times||{};
const cfg=data.config||{};
const hold=trade.exit_key===null||trade.exit_key===undefined?'—':((Number(trade.exit_key)-Number(trade.fill_key))/1000).toFixed(1)+' 秒';
const cards=[['交易日',display(trade.trade_date)],['目标/对冲合约',`${trade.target_contract} / ${trade.hedge_contract||'—'}`],['W / D / S',`${number(cfg.band_half_width_ticks)} / ${number(cfg.outer_quote_offset_ticks)} / ${number(cfg.reanchor_step_ticks)} tick`],['对冲',cfg.enable_hedge?'开启':'关闭'],['价差倍数 N',number(cfg.quote_spread_multiple)],['方向',trade.direction==='long'?'买入目标':'卖出目标'],['成交分类',eventLabels[trade.event_label]||trade.event_label],['成交证据',labelEvidence(trade.fill_evidence)],['目标成交价',number(trade.target_entry_price)],['对冲成交价',`${trade.hedge_contract||'—'}：${number(trade.hedge_entry_price)}`],['目标成交',phaseTimes['目标成交']||trade.fill_time],['参考腿成交',phaseTimes['参考腿成交']||'未成交'],['退出',phaseTimes['退出']||'未退出'],['持仓时长',hold],['退出原因',exitLabels[trade.exit_reason]||trade.exit_reason||'—'],['状态',statusLabels[trade.status]||trade.status],['目标/对冲盈亏',money(trade.target_pnl)+' / '+money(trade.hedge_pnl)],['净收益',money(trade.net_pnl)],['未对冲最差',money(trade.unhedged_worst_mark_pnl)],['对冲后最差',money(trade.hedged_worst_mark_pnl)]];
document.getElementById('modal-cards').innerHTML=cards.map(([k,v])=>'<div class="card"><span>'+k+'</span><strong>'+v+'</strong></div>').join('');
const quoteColumns=[['关键时点','关键时点'],['display_time','更新时间'],['W_ticks','W（tick）'],['D_ticks','D（tick）'],['S_ticks','S（tick）'],['fair_price','合理价'],['fair_price_reliable','合理价可靠'],['grid_anchor','报价锚点'],['band_lower','合理价带下限'],['band_upper','合理价带上限'],['buy_limit','买入挂单价'],['sell_limit','卖出挂单价'],['quote_state','报价状态'],['quote_reason','状态原因'],['quote_active','报价有效'],['LastPrice','最新成交价'],['interval_vwap','区间成交均价'],['BidPrice1','买一价'],['AskPrice1','卖一价']];
let content='<h2>成交前 10 秒报价计算</h2>'+contextTable(context.pre_fill_quote_rows||[],quoteColumns)+'<h2>交易全流程</h2>'+flowTableEnhanced(context.flow_events||[])+'<h2>目标合约快照</h2><p class="muted">完整持仓窗口：'+display(context.window.start_time)+' 至 '+display(context.window.end_time)+'（目标成交前 '+number(context.window.before_after_ms/1000)+' 秒至退出后 '+number(context.window.before_after_ms/1000)+' 秒）</p>'+contextTable(context.target_rows,targetColumns,true);
Object.entries(context.contracts||{}).forEach(([code,part])=>{content+='<h3 class="contract-title">'+code+' <span class="muted">'+(part.roles||[]).join(' / ')+'</span></h3>'+contextTable(part.rows||[],referenceColumns)});
document.getElementById('modal-content').innerHTML=content;
document.getElementById('modal-raw').textContent=JSON.stringify(trade,null,2);
document.getElementById('trade-modal').classList.remove('hidden');
}
init();
</script>
</body>
</html>"""


def _concat_frames(frames: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True).reindex(columns=columns)


def _peak_actions_per_minute(orders: pd.DataFrame) -> int:
    if orders.empty:
        return 0
    keys = pd.to_numeric(
        orders.loc[orders["event"].isin(["submit", "cancel_requested"]), "market_time_key"],
        errors="coerce",
    ).dropna().astype(int).sort_values().to_numpy()
    peak = 0
    left = 0
    for right, key in enumerate(keys):
        while key - keys[left] > 60000:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


# ---------------------------------------------------------------------------
# 配置与数值小工具
# ---------------------------------------------------------------------------


def _required_date(value: Any, name: str) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise ValueError(f"{name} 必须是 YYYYMMDD")
    text = re.sub(r"\.0+$", "", str(value).strip())
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{name} 必须是 YYYYMMDD")
    return text


def _contract_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} 必须是非空合约数组")
    values = [str(item).strip().upper() for item in value if str(item).strip()]
    if len(values) != len(set(values)):
        raise ValueError(f"{name} 不能包含重复合约")
    return values


def _quality_exclusions(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("quality_exclusions 必须是数组")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("quality_exclusions 的每项必须包含 trade_date 和 commodity")
        result.append(
            {
                "trade_date": _required_date(item.get("trade_date"), "quality_exclusions.trade_date"),
                "commodity": str(item.get("commodity") or "").strip().upper(),
            }
        )
    if any(not item["commodity"] for item in result):
        raise ValueError("quality_exclusions 的每项必须包含 commodity")
    return result


def _number_map(value: Any, name: str, *, allow_zero: bool) -> dict[str, float]:
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


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise ValueError(f"{name} 必须是布尔值")


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


def _positive_int(value: Any, name: str) -> int:
    result = _finite_number(value)
    if result is None or result <= 0 or not result.is_integer():
        raise ValueError(f"{name} 必须是正整数")
    return int(result)


def _nonnegative_int(value: Any, name: str) -> int:
    result = _finite_number(value)
    if result is None or result < 0 or not result.is_integer():
        raise ValueError(f"{name} 必须是非负整数")
    return int(result)


def _fraction(value: Any, name: str) -> float:
    result = _positive_number(value, name)
    if result > 1:
        raise ValueError(f"{name} 必须小于等于 1")
    return result


def _finite_number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe_divide(numerator: int | float, denominator: int | float) -> float:
    return np.nan if denominator == 0 else float(numerator) / float(denominator)
