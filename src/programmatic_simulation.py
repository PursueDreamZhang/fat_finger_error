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
    "max_single_trade_loss": 500.0,
    "default_margin_rate": 0.10,
    "margin_rate_by_commodity": {},
    "default_commission_per_lot_per_side": 0.0,
    "commission_by_commodity": {},
    "target_lots": 1,
    "hedge_lots": 1,
    "band_half_width_ticks": 10,
    "outer_quote_offset_ticks": 10,
    "reanchor_step_ticks": 10,
    "reanchor_confirm_ms": 1000,
    "resume_confirm_ms": 2000,
    "fair_invalid_confirm_ms": 1000,
    "min_reprice_interval_ms": 1000,
    "max_order_actions_per_minute": 20,
    "cancel_ack_latency_ms": 500,
    "new_order_ack_latency_ms": 500,
    "hedge_submit_latency_ms": 500,
    "max_hedge_wait_ms": 2000,
    "max_quote_age_ms": 3000,
    "max_fair_age_ms": 3000,
    "max_data_gap_ms": 3000,
    "fill_model": "observable_cross_assumed",
    "require_top_of_book_full_lot": True,
    "slippage_ticks": 1.0,
    "reversion_exit_ratio": 0.20,
    "take_profit_amount": 0.0,
    "max_hold_ms": 60000,
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
    config["max_single_trade_loss"] = _positive_number(config.get("max_single_trade_loss"), "max_single_trade_loss")
    config["default_margin_rate"] = _positive_number(config.get("default_margin_rate"), "default_margin_rate")
    config["margin_rate_by_commodity"] = _number_map(config.get("margin_rate_by_commodity"), "margin_rate_by_commodity", allow_zero=False)
    config["default_commission_per_lot_per_side"] = _nonnegative_number(
        config.get("default_commission_per_lot_per_side"), "default_commission_per_lot_per_side"
    )
    config["commission_by_commodity"] = _number_map(config.get("commission_by_commodity"), "commission_by_commodity", allow_zero=True)
    config["target_lots"] = _positive_int(config.get("target_lots"), "target_lots")
    config["hedge_lots"] = _positive_int(config.get("hedge_lots"), "hedge_lots")

    for key in ("band_half_width_ticks", "outer_quote_offset_ticks", "reanchor_step_ticks"):
        config[key] = _positive_number(config.get(key), key)
    for key in (
        "reanchor_confirm_ms",
        "resume_confirm_ms",
        "fair_invalid_confirm_ms",
        "min_reprice_interval_ms",
        "cancel_ack_latency_ms",
        "new_order_ack_latency_ms",
        "hedge_submit_latency_ms",
        "max_hedge_wait_ms",
        "cooldown_ms",
    ):
        config[key] = _nonnegative_int(config.get(key), key)
    config["max_order_actions_per_minute"] = _positive_int(
        config.get("max_order_actions_per_minute"), "max_order_actions_per_minute"
    )
    for key in ("max_quote_age_ms", "max_fair_age_ms", "max_data_gap_ms", "max_hold_ms"):
        config[key] = _positive_int(config.get(key), key)
    config["fill_model"] = str(config.get("fill_model") or "").strip()
    if config["fill_model"] not in FILL_MODELS:
        raise ValueError(f"fill_model 必须是以下之一: {', '.join(sorted(FILL_MODELS))}")
    config["require_top_of_book_full_lot"] = _as_bool(
        config.get("require_top_of_book_full_lot"), "require_top_of_book_full_lot"
    )
    config["slippage_ticks"] = _nonnegative_number(config.get("slippage_ticks"), "slippage_ticks")
    config["reversion_exit_ratio"] = _fraction_or_zero(config.get("reversion_exit_ratio"), "reversion_exit_ratio")
    config["take_profit_amount"] = _nonnegative_number(config.get("take_profit_amount"), "take_profit_amount")
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

        required = {config["target_contract"], config["hedge_contract"], *config["fair_reference_contracts"]}
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
            frames[config["hedge_contract"]],
            config,
            trade_date=trade_date,
            detector_event_keys=event_keys,
        )
        transitions.append(day["transitions"])
        orders.append(day["orders"])
        trades.append(day["trades"])

    transition_df = _concat_frames(transitions, STATE_COLUMNS)
    order_df = _concat_frames(orders, ORDER_COLUMNS)
    trade_df = _concat_frames(trades, TRADE_COLUMNS)
    skipped_df = pd.DataFrame(skipped, columns=["trade_date", "reason"])
    summary = build_programmatic_summary(trade_df, order_df, transition_df)
    warnings = [
        "这是基于约 500ms 快照的全天状态机回放；observable_cross_assumed 不是交易所成交回报。",
        "fair_reference_contracts 由配置显式冻结，未按当天最终成交量选参考合约。",
        "成交后参考腿、退出腿均按当时可见买卖一加滑点估算，未还原盘口队列位置。",
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
        if hedge_frame.empty:
            raise ValueError("对冲合约快照为空")
        self.target = target_frame if assume_sorted else target_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        self.hedge = hedge_frame if assume_sorted else hedge_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        self.hedge_keys = pd.to_numeric(self.hedge["market_time_key"], errors="coerce").to_numpy(dtype=np.int64)
        self.config = config
        self.trade_date = trade_date
        self.detector_event_keys = detector_event_keys
        self.commodity = str(self.target["commodity"].iloc[0]).upper()
        self.target_contract = str(self.target["contract"].iloc[0]).upper()
        self.hedge_contract = str(self.hedge["contract"].iloc[0]).upper()
        self.target_tick = _required_frame_tick_size(self.target, self.commodity)
        self.hedge_tick = _required_frame_tick_size(self.hedge, self.commodity)
        self.target_multiplier = _required_frame_multiplier(self.target, self.commodity)
        self.hedge_multiplier = _required_frame_multiplier(self.hedge, self.commodity)

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
        self.fair_recovered_since_key: int | None = None
        self.fair_invalid_since_key: int | None = None
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
                else:
                    fill = self._first_target_fill(row)
                    if fill is not None:
                        self._open_trade(row, *fill)
                    else:
                        self._manage_flat_state(row, data_gap=False)
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
        if key is None or not self._can_submit(key, 2):
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
    # 平仓状态：fair 驱动锚点与撤改单
    # ------------------------------------------------------------------

    def _manage_flat_state(self, row: pd.Series, *, data_gap: bool) -> None:
        key = _row_key(row)
        if key is None:
            return
        fair = _valid_fair(row)
        if data_gap:
            self._pause(row, "data_gap")
            return
        if fair is None:
            self.fair_recovered_since_key = None
            if self.state in {"PAUSED", "COOLDOWN"}:
                self._pause(row, "fair_invalid_or_session_guard")
                return
            if self.fair_invalid_since_key is None:
                self.fair_invalid_since_key = key
                return
            if key - self.fair_invalid_since_key >= int(self.config["fair_invalid_confirm_ms"]):
                self._pause(row, "fair_invalid_or_session_guard")
            return
        self.fair_invalid_since_key = None
        if self.state == "PAUSED":
            if not self._has_live_target_orders() and key >= self.cooldown_until_key:
                self._resume_if_stable(row, fair)
            return
        if self.state == "COOLDOWN":
            if key >= self.cooldown_until_key and not self._has_live_target_orders():
                self._resume_if_stable(row, fair)
            return
        if self.state == "FLAT_QUOTING":
            self._check_reanchor(row, fair)

    def _pause(self, row: pd.Series, reason: str) -> None:
        if self.state not in {"PAUSED", "COOLDOWN"}:
            self._record_transition(row, "PAUSED", reason)
        self._cancel_target_orders(row, reason)
        self.reanchor_direction = ""
        self.reanchor_started_key = None
        self.fair_recovered_since_key = None
        self.fair_invalid_since_key = None
        if self.state == "COOLDOWN":
            self._record_transition(row, "PAUSED", reason)

    def _resume_if_stable(self, row: pd.Series, fair: float) -> None:
        key = _row_key(row)
        if key is None:
            return
        if self.fair_recovered_since_key is None:
            self.fair_recovered_since_key = key
            if int(self.config["resume_confirm_ms"]) > 0:
                return
        if key - self.fair_recovered_since_key < int(self.config["resume_confirm_ms"]):
            return
        self._start_quoting(row, fair)

    def _start_quoting(self, row: pd.Series, fair: float) -> None:
        key = _row_key(row)
        if key is None:
            return
        candidate_anchor = _round_to_tick(fair, self.target_tick)
        candidate_grid = build_grid(candidate_anchor, self.target_tick, self.config)
        if not self._quote_margin_ok(key, candidate_grid):
            self._record_transition(row, "PAUSED", "capital_or_hedge_quote_guard")
            return
        if not self._can_submit(key, 2):
            self._record_transition(row, "PAUSED", "order_action_limit")
            return
        self.anchor = candidate_anchor
        self.grid = candidate_grid
        self.version += 1
        self.fair_recovered_since_key = None
        self._record_transition(row, "FLAT_QUOTING", "fair_recovered")
        if not self._submit_grid(row, "initial_quote"):
            self._record_transition(row, "PAUSED", "order_action_limit")

    def _check_reanchor(self, row: pd.Series, fair: float) -> None:
        if not self.grid:
            return
        key = _row_key(row)
        if key is None:
            return
        direction = ""
        if fair < float(self.grid["band_lower"]):
            direction = "down"
        elif fair > float(self.grid["band_upper"]):
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
            steps = math.ceil((float(self.grid["band_lower"]) - fair) / step_price - 1e-12)
            new_anchor = float(self.anchor) - max(1, steps) * step_price
        else:
            steps = math.ceil((fair - float(self.grid["band_upper"])) / step_price - 1e-12)
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
        self._record_transition(row, "REPLACE_PENDING", f"fair_{direction}_confirmed")
        self._cancel_target_orders(row, "reanchor")
        self.reanchor_direction = ""
        self.reanchor_started_key = None
        self._maybe_submit_replacement(row)

    def _quote_margin_ok(self, key: int, grid: Mapping[str, float]) -> bool:
        hedge_bid = self._asof_quote(key, "sell", int(self.config["hedge_lots"]))
        hedge_ask = self._asof_quote(key, "buy", int(self.config["hedge_lots"]))
        if hedge_bid is None or hedge_ask is None:
            return False
        rate = _commodity_number(self.config, "margin_rate_by_commodity", "default_margin_rate", self.commodity)
        target_lots = int(self.config["target_lots"])
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
        self._record_transition(row, "LONG_PENDING_HEDGE" if direction == "long" else "SHORT_PENDING_HEDGE", "target_fill")

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
        if self.state in {"LONG_PENDING_HEDGE", "SHORT_PENDING_HEDGE"}:
            self._update_worst_mark(row)
            unhedged_mark = self._mark_pnl(row)
            if unhedged_mark is not None and unhedged_mark - self._estimated_unhedged_exit_commission() <= -float(self.config["max_single_trade_loss"]):
                self.trade["exit_reason"] = "unhedged_stop_loss"
                self._record_transition(row, "EMERGENCY_FLATTEN", "unhedged_stop_loss")
                self._try_flatten(row)
                return
            if key >= int(self.trade["hedge_due_key"]):
                self._try_hedge(row)
            if self.trade is not None and not self.trade["hedge_open"] and key >= int(self.trade["hedge_deadline_key"]):
                self.trade["exit_reason"] = "hedge_failure_exit"
                self._record_transition(row, "EMERGENCY_FLATTEN", "hedge_quote_timeout")
                self._try_flatten(row)
            return
        if self.state == "HEDGED_POSITION":
            self._update_worst_mark(row)
            reason = self._position_exit_reason(row)
            if reason:
                self.trade["exit_reason"] = reason
                self._record_transition(row, "FLATTENING", reason)
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
        price = _aggressive_price(float(quote["price"]), direction, self.hedge_tick, float(self.config["slippage_ticks"]))
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
        self.trade["hedge_entry_price"] = price
        self.trade["hedge_open"] = True
        self.trade["gross_margin"] = self._actual_margin()
        self._record_transition(row, "HEDGED_POSITION", "hedge_fill")

    # ------------------------------------------------------------------
    # 盯市、退出与记账
    # ------------------------------------------------------------------

    def _position_exit_reason(self, row: pd.Series) -> str | None:
        if self.trade is None:
            return None
        key = _row_key(row)
        if key is None:
            return None
        fair = _valid_fair(row)
        if fair is None:
            return "reference_invalid_exit"
        mark = self._mark_pnl(row)
        estimated_net = mark - self._commission() if mark is not None else None
        if estimated_net is not None and estimated_net <= -float(self.config["max_single_trade_loss"]):
            return "stop_loss"
        if (
            estimated_net is not None
            and float(self.config["take_profit_amount"]) > 0
            and estimated_net >= float(self.config["take_profit_amount"])
        ):
            return "take_profit"
        mid = _mid_price(row)
        if math.isfinite(mid):
            residual = fair - mid if self.trade["direction"] == "long" else mid - fair
            if residual <= float(self.trade["entry_residual_ticks"]) * self.target_tick * float(self.config["reversion_exit_ratio"]):
                return "reversion_exit"
        if key - int(self.trade["fill_key"]) >= int(self.config["max_hold_ms"]):
            return "time_exit"
        return None

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
                price = _aggressive_price(float(quote["price"]), direction, self.target_tick, float(self.config["slippage_ticks"]))
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
                price = _aggressive_price(float(quote["price"]), direction, self.hedge_tick, float(self.config["slippage_ticks"]))
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
            target_price = _aggressive_price(float(quote["price"]), side, self.target_tick, float(self.config["slippage_ticks"]))
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
            hedge_price = _aggressive_price(float(quote["price"]), side, self.hedge_tick, float(self.config["slippage_ticks"]))
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

    def _estimated_unhedged_exit_commission(self) -> float:
        per_side = _commodity_number(
            self.config,
            "commission_by_commodity",
            "default_commission_per_lot_per_side",
            self.commodity,
        )
        return 2.0 * per_side * int(self.config["target_lots"])

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


def _fair_price(row: pd.Series) -> float:
    value = _finite_number(row.get("fair_price"))
    return value if value is not None else np.nan


def _mid_price(row: pd.Series) -> float:
    mid = _finite_number(row.get("mid_price"))
    if mid is not None:
        return mid
    bid = _finite_number(row.get("BidPrice1"))
    ask = _finite_number(row.get("AskPrice1"))
    return (bid + ask) / 2.0 if bid is not None and ask is not None and bid > 0 and ask >= bid else np.nan


def _aggressive_price(price: float, side: str, tick_size: float, slippage_ticks: float) -> float:
    adjusted = price + slippage_ticks * tick_size if side == "buy" else price - slippage_ticks * tick_size
    return _round_to_tick(adjusted, tick_size)


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
    required = {config["target_contract"], config["hedge_contract"], *config["fair_reference_contracts"]}
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
        "reprice_count": int(reasons.isin(["fair_down_confirmed", "fair_up_confirmed"]).sum()),
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


def _render_programmatic_report(result: Mapping[str, Any]) -> str:
    summary = result["summary"]
    warnings = "".join(f"<li>{escape(str(item))}</li>" for item in result["warnings"])
    config_json = escape(
        json.dumps({key: value for key, value in result["config"].items() if not key.startswith("_")}, ensure_ascii=False, indent=2)
    )
    table = summary.to_html(index=False, border=0, classes="summary", justify="left", escape=True)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<title>乌龙指程序化状态机回放</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, \"PingFang SC\", sans-serif; margin: 28px; color: #202124; }}
.muted {{ color: #5f6368; }} .warning {{ background: #fff7e6; border-left: 4px solid #e37400; padding: 12px 16px; margin: 20px 0; }}
pre {{ background: #f6f8fa; padding: 14px; overflow: auto; }} table {{ border-collapse: collapse; font-size: 12px; }}
th, td {{ border: 1px solid #dfe3e8; padding: 6px 8px; white-space: nowrap; }} th {{ background: #f6f8fa; }} .scroll {{ overflow-x: auto; }}
</style>
</head>
<body>
<h1>乌龙指程序化状态机回放</h1>
<p class=\"muted\">全天快照分母；候选事件只作事后标签。详细状态、订单和交易见同目录 CSV。</p>
<div class=\"warning\"><strong>结果边界</strong><ul>{warnings}</ul></div>
<h2>汇总</h2><div class=\"scroll\">{table}</div>
<h2>本次生效参数</h2><pre>{config_json}</pre>
</body></html>"""


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


def _fraction_or_zero(value: Any, name: str) -> float:
    result = _nonnegative_number(value, name)
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
