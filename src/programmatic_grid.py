"""V2 参数网格回放：缓存每日 fair，只重复执行状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from html import escape
import hashlib
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np
import pandas as pd

from src.programmatic_simulation import (
    DEFAULT_CONFIG,
    _concat_frames,
    _event_keys_for_day,
    _frame_tick_size,
    _load_event_rows,
    _load_required_frames,
    _resolve_tick_day_path,
    _simulate_programmatic_day_prepared,
    attach_fair_price_metrics,
    build_programmatic_summary,
    normalize_programmatic_simulation_config,
)

FAIR_CACHE_SCHEMA_VERSION = 1
_PATH_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


GRID_SCENARIO_COLUMNS = [
    "scenario_id",
    "instrument",
    "band_half_width_ticks",
    "outer_quote_offset_ticks",
    "reanchor_step_ticks",
    "reanchor_confirm_ms",
    "resume_confirm_ms",
    "fair_invalid_confirm_ms",
    "cancel_ack_latency_ms",
    "new_order_ack_latency_ms",
    "hedge_submit_latency_ms",
    "max_hedge_wait_ms",
]

GRID_DAILY_COLUMNS = [
    "scenario_id",
    "instrument",
    "trade_date",
    "fill_count",
    "detector_event_fill_count",
    "normal_move_fill_count",
    "fill_during_replace_count",
    "closed_count",
    "win_count",
    "hedge_failure_count",
    "capital_limit_exit_count",
    "net_pnl",
    "worst_net_pnl",
    "order_action_count",
    "peak_order_actions_per_minute",
    "reprice_count",
    "pause_count",
    "skipped",
    "skip_reason",
]

GRID_SUMMARY_COLUMNS = [
    *GRID_SCENARIO_COLUMNS,
    "active_days",
    "fill_count",
    "detector_event_fill_count",
    "normal_move_fill_count",
    "normal_move_fill_rate",
    "fill_during_replace_count",
    "closed_count",
    "hedge_failure_count",
    "hedge_failure_rate",
    "capital_limit_exit_count",
    "net_pnl",
    "win_rate",
    "worst_day_net_pnl",
    "daily_net_std",
    "max_gross_margin",
    "peak_order_actions_per_minute",
    "total_order_action_count",
    "total_reprice_count",
    "skipped_day_count",
    "eligible",
    "selection_reason",
]

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


def load_programmatic_grid_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise ValueError("网格配置必须是 JSON 对象")
    return normalize_programmatic_grid_config(dict(raw))


def normalize_programmatic_grid_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(raw)
    output_dir = str(config.get("output_dir") or "").strip()
    if not output_dir:
        raise ValueError("网格配置缺少 output_dir")
    base = dict(DEFAULT_CONFIG)
    base.update(dict(config.get("base") or {}))
    base["output_dir"] = output_dir
    instruments = config.get("instruments")
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("网格配置必须包含非空 instruments")
    for item in instruments:
        if not isinstance(item, Mapping):
            raise ValueError("instruments 的每项必须是对象")
        for key in ("name", "commodity", "target_contract", "fair_reference_contracts", "hedge_contract"):
            if not item.get(key):
                raise ValueError(f"instrument 缺少 {key}")

    shapes = config.get("quote_shapes")
    latencies = config.get("latency_profiles")
    if not isinstance(shapes, list) or not shapes:
        raise ValueError("quote_shapes 必须是非空数组")
    if not isinstance(latencies, list) or not latencies:
        raise ValueError("latency_profiles 必须是非空数组")
    for shape in shapes:
        if not isinstance(shape, Mapping) or not all(shape.get(key) is not None for key in ("W", "D", "S")):
            raise ValueError("quote_shapes 每项必须包含 W、D、S")
    for profile in latencies:
        if not isinstance(profile, Mapping):
            raise ValueError("latency_profiles 每项必须是对象")

    thresholds = {
        "min_fills": int(config.get("min_fills", 3)),
        "min_event_fills": int(config.get("min_event_fills", 1)),
        "max_normal_move_fill_rate": float(config.get("max_normal_move_fill_rate", 0.25)),
        "max_hedge_failure_rate": float(config.get("max_hedge_failure_rate", 0.10)),
        "max_daily_loss": float(config.get("max_daily_loss", base["max_single_trade_loss"])),
        "max_peak_order_actions_per_minute": int(
            config.get("max_peak_order_actions_per_minute", base["max_order_actions_per_minute"])
        ),
    }
    if thresholds["min_fills"] < 1 or thresholds["min_event_fills"] < 0:
        raise ValueError("min_fills 必须大于 0，min_event_fills 不能为负数")
    if not 0 <= thresholds["max_normal_move_fill_rate"] <= 1:
        raise ValueError("max_normal_move_fill_rate 必须在 0 到 1 之间")
    if not 0 <= thresholds["max_hedge_failure_rate"] <= 1:
        raise ValueError("max_hedge_failure_rate 必须在 0 到 1 之间")
    config["base"] = base
    config["instruments"] = [dict(item) for item in instruments]
    config["quote_shapes"] = [dict(item) for item in shapes]
    config["latency_profiles"] = [dict(item) for item in latencies]
    config["thresholds"] = thresholds
    context_mode = str(config.get("context_mode", "all"))
    if context_mode not in {"all", "none", "selected"}:
        raise ValueError("context_mode 必须是 all、none 或 selected")
    selected = config.get("context_scenarios", [])
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise ValueError("context_scenarios 必须是字符串数组")
    valid_context_scenarios = {
        f"{instrument['name']}::{scenario['scenario_id']}"
        for instrument in config["instruments"]
        for scenario in build_grid_scenarios(config)
    }
    unknown = sorted(set(selected) - valid_context_scenarios)
    if unknown:
        raise ValueError(f"context_scenarios 包含不存在组合: {','.join(unknown)}")
    config["context_mode"] = context_mode
    config["context_scenarios"] = selected
    cache_dir = str(config.get("fair_cache_dir") or "").strip()
    config["fair_cache_dir"] = cache_dir
    return config


def build_grid_scenarios(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for shape_index, shape in enumerate(config["quote_shapes"], start=1):
        for latency_index, latency in enumerate(config["latency_profiles"], start=1):
            scenario = {
                "scenario_id": f"Q{shape_index:02d}-L{latency_index:02d}",
                "band_half_width_ticks": float(shape["W"]),
                "outer_quote_offset_ticks": float(shape["D"]),
                "reanchor_step_ticks": float(shape["S"]),
                "reanchor_confirm_ms": int(latency.get("reanchor_confirm_ms", 1000)),
                "resume_confirm_ms": int(latency.get("resume_confirm_ms", 2000)),
                "fair_invalid_confirm_ms": int(latency.get("fair_invalid_confirm_ms", 1000)),
                "cancel_ack_latency_ms": int(latency.get("cancel_ack_latency_ms", 500)),
                "new_order_ack_latency_ms": int(latency.get("new_order_ack_latency_ms", 500)),
                "hedge_submit_latency_ms": int(latency.get("hedge_submit_latency_ms", 500)),
                "max_hedge_wait_ms": int(latency.get("max_hedge_wait_ms", 2000)),
            }
            scenarios.append(scenario)
    return scenarios


def run_programmatic_grid(
    config: Mapping[str, Any], *, timings: dict[str, float] | None = None, include_trade_contexts: bool = True
) -> dict[str, Any]:
    """运行网格回放；阶段 0 可关闭详情构造以测量核心路径。"""
    config = normalize_programmatic_grid_config(config)
    scenarios = build_grid_scenarios(config)
    started = perf_counter() if timings is not None else None
    events = _load_event_rows(str(config["base"].get("events_csv") or ""))
    _add_timing(timings, "event_load", started)
    daily_rows: list[dict[str, Any]] = []
    trade_frames: list[pd.DataFrame] = []
    trade_contexts: dict[str, dict[str, Any]] = {}
    skipped_global: list[dict[str, str]] = []

    for instrument in config["instruments"]:
        instrument_base = dict(config["base"])
        instrument_base["fair_cache_dir"] = config["fair_cache_dir"]
        instrument_base.update(
            {
                "commodity": str(instrument["commodity"]).upper(),
                "target_contract": str(instrument["target_contract"]).upper(),
                "fair_reference_contracts": [str(item).upper() for item in instrument["fair_reference_contracts"]],
                "hedge_contract": str(instrument["hedge_contract"]).upper(),
            }
        )
        instrument_base.update(scenarios[0])
        instrument_base = normalize_programmatic_simulation_config(instrument_base)
        instrument_name = str(instrument["name"])
        scenario_configs = []
        for scenario in scenarios:
            scenario_config = dict(instrument_base)
            scenario_config.update(scenario)
            scenario_configs.append((scenario, normalize_programmatic_simulation_config(scenario_config)))

        for stamp in pd.date_range(instrument_base["trade_date_start"], instrument_base["trade_date_end"], freq="D"):
            trade_date = stamp.strftime("%Y%m%d")
            prepared = _prepare_grid_day(instrument_base, trade_date, events, timings=timings)
            if isinstance(prepared, str):
                skipped_global.append({"instrument": instrument_name, "trade_date": trade_date, "reason": prepared})
                for scenario in scenarios:
                    daily_rows.append(_empty_daily(scenario, instrument_name, trade_date, prepared))
                continue
            enriched, frames, event_keys = prepared
            for scenario, scenario_config in scenario_configs:
                started = perf_counter() if timings is not None else None
                day = _simulate_programmatic_day_prepared(
                    enriched,
                    frames[scenario_config["hedge_contract"]],
                    scenario_config,
                    trade_date=trade_date,
                    detector_event_keys=event_keys,
                    assume_sorted=True,
                )
                _add_timing(timings, "state_machine", started)
                started = perf_counter() if timings is not None else None
                stats = build_programmatic_summary(day["trades"], day["orders"], day["transitions"]).iloc[0].to_dict()
                _add_timing(timings, "daily_stats", started)
                daily_rows.append(
                    {
                        "scenario_id": scenario["scenario_id"],
                        "instrument": instrument_name,
                        "trade_date": trade_date,
                        "fill_count": int(stats["fill_count"]),
                        "detector_event_fill_count": int(stats["detector_event_fill_count"]),
                        "normal_move_fill_count": int(stats["normal_move_fill_count"]),
                        "fill_during_replace_count": int(stats["fill_during_replace_count"]),
                        "closed_count": int(stats["closed_count"]),
                        "win_count": int(round(float(stats["win_rate"]) * int(stats["fill_count"]))) if pd.notna(stats["win_rate"]) else 0,
                        "hedge_failure_count": int(stats["hedge_failure_count"]),
                        "capital_limit_exit_count": int(stats["capital_limit_exit_count"]),
                        "net_pnl": float(stats["net_pnl"]),
                        "worst_net_pnl": stats["worst_net_pnl"],
                        "order_action_count": int(stats["order_action_count"]),
                        "peak_order_actions_per_minute": int(stats["peak_order_actions_per_minute"]),
                        "reprice_count": int(stats["reprice_count"]),
                        "pause_count": int(stats["pause_count"]),
                        "skipped": False,
                        "skip_reason": "",
                    }
                )
                if not day["trades"].empty:
                    trades = day["trades"].copy()
                    trades["scenario_id"] = scenario["scenario_id"]
                    trades["instrument"] = instrument_name
                    trade_frames.append(trades)
                    context_key = f"{instrument_name}::{scenario['scenario_id']}"
                    should_build_context = include_trade_contexts and (
                        config["context_mode"] == "all"
                        or (config["context_mode"] == "selected" and context_key in config["context_scenarios"])
                    )
                    if should_build_context:
                        started = perf_counter() if timings is not None else None
                        trade_contexts.update(
                            build_grid_trade_contexts(
                                trades,
                                enriched,
                                frames,
                                scenario_config,
                            )
                        )
                        _add_timing(timings, "trade_context", started)

    started = perf_counter() if timings is not None else None
    daily = pd.DataFrame(daily_rows, columns=GRID_DAILY_COLUMNS)
    summary_rows = []
    for (instrument, scenario_id), group in daily.groupby(["instrument", "scenario_id"], sort=True):
        scenario = next(item for item in scenarios if item["scenario_id"] == scenario_id)
        summary_rows.append(_summarize_scenario(instrument, scenario, group, config["thresholds"]))
    summary = pd.DataFrame(summary_rows, columns=GRID_SUMMARY_COLUMNS)
    summary = summary.sort_values(
        ["eligible", "net_pnl", "normal_move_fill_rate", "worst_day_net_pnl"],
        ascending=[False, False, True, False],
        kind="stable",
    ).reset_index(drop=True)
    trades = pd.concat(trade_frames, ignore_index=True) if trade_frames else pd.DataFrame()
    _add_timing(timings, "result_assembly", started)
    return {
        "config": config,
        "scenarios": pd.DataFrame(scenarios, columns=GRID_SCENARIO_COLUMNS),
        "daily": daily,
        "summary": summary,
        "trades": trades,
        "trade_contexts": trade_contexts,
        "context_mode": config["context_mode"],
        "skipped_days": pd.DataFrame(skipped_global, columns=["instrument", "trade_date", "reason"]),
    }


def _prepare_grid_day(
    config: Mapping[str, Any],
    trade_date: str,
    events: pd.DataFrame | None = None,
    *,
    timings: dict[str, float] | None = None,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], set[int]] | str:
    started = perf_counter() if timings is not None else None
    try:
        frames = _load_required_frames(config, trade_date)
    except FileNotFoundError:
        _add_timing(timings, "data_load", started)
        return "tick_day_missing"
    except Exception as exc:
        _add_timing(timings, "data_load", started)
        return f"day_load_failed: {exc}"
    _add_timing(timings, "data_load", started)
    frames = {contract: _stable_sorted_frame(frame) for contract, frame in frames.items()}
    required = {config["target_contract"], config["hedge_contract"], *config["fair_reference_contracts"]}
    missing = sorted(contract for contract in required if contract not in frames)
    if missing:
        return f"contract_missing: {','.join(missing)}"
    target = frames[config["target_contract"]]
    tick_size = _frame_tick_size(target, config["commodity"])
    if tick_size is None:
        return "target_metadata_missing"
    started = perf_counter() if timings is not None else None
    cache_key = _fair_cache_key(config, trade_date, tick_size)
    cached = _read_fair_cache(config, cache_key)
    if cached is not None:
        if events is None:
            events = _load_event_rows(str(config.get("events_csv") or ""))
        event_keys = _event_keys_for_day(events, trade_date, config["target_contract"], cached)
        _add_timing(timings, "fair_prepare", started)
        return cached, frames, event_keys
    started = perf_counter() if timings is not None else None
    refs = {contract: frames[contract] for contract in config["fair_reference_contracts"]}
    enriched = attach_fair_price_metrics(
        target,
        refs,
        tick_size=tick_size,
        top_volume_peer_contracts=set(config["fair_reference_contracts"]),
        max_reference_age_seconds=float(config["max_fair_age_ms"]) / 1000.0,
    )
    enriched = _stable_sorted_frame(enriched)
    _attach_context_metrics(enriched, tick_size)
    _write_fair_cache(config, cache_key, enriched)
    if events is None:
        events = _load_event_rows(str(config.get("events_csv") or ""))
    event_keys = _event_keys_for_day(events, trade_date, config["target_contract"], enriched)
    _add_timing(timings, "fair_prepare", started)
    return enriched, frames, event_keys


def _add_timing(timings: dict[str, float] | None, name: str, started: float | None) -> None:
    if timings is not None and started is not None:
        timings[name] = timings.get(name, 0.0) + (perf_counter() - started)


def _stable_sorted_frame(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)


def _fair_cache_key(config: Mapping[str, Any], trade_date: str, tick_size: float) -> str:
    tick_path = _resolve_tick_day_path(Path(str(config["tick_data_root"])), trade_date)
    bounds_path = Path(str(config["daily_data_root"])) / trade_date[:4] / f"{trade_date}.parquet"
    payload = {
        "schema": FAIR_CACHE_SCHEMA_VERSION,
        "trade_date": trade_date,
        "commodity": config["commodity"],
        "target": config["target_contract"],
        "references": list(config["fair_reference_contracts"]),
        "tick_size": tick_size,
        "max_fair_age_ms": config["max_fair_age_ms"],
        "tick_source": _path_digest(tick_path),
        "bounds_source": _path_digest(bounds_path),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _path_digest(path: Path) -> str:
    stat = path.stat()
    cache_key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if path.is_file() and cache_key in _PATH_DIGEST_CACHE:
        return _PATH_DIGEST_CACHE[cache_key]
    digest = hashlib.sha256()
    paths = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
    for item in paths:
        digest.update(str(item.relative_to(path.parent)).encode())
        with item.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    value = digest.hexdigest()
    if path.is_file():
        _PATH_DIGEST_CACHE[cache_key] = value
    return value


def _read_fair_cache(config: Mapping[str, Any], cache_key: str) -> pd.DataFrame | None:
    cache_dir_text = str(config.get("fair_cache_dir") or "")
    if not cache_dir_text:
        return None
    cache_dir = Path(cache_dir_text)
    path = cache_dir / f"fair_{cache_key}.pkl"
    manifest = cache_dir / f"fair_{cache_key}.json"
    try:
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata != {"schema": FAIR_CACHE_SCHEMA_VERSION, "key": cache_key}:
            return None
        cached = pd.read_pickle(path)
    except Exception:
        return None
    required = {"market_time_key", "fair_price", "fair_price_reliable", "last_down_ticks"}
    return cached if required <= set(cached.columns) else None


def _write_fair_cache(config: Mapping[str, Any], cache_key: str, enriched: pd.DataFrame) -> None:
    cache_dir_text = str(config.get("fair_cache_dir") or "")
    if not cache_dir_text:
        return
    cache_dir = Path(cache_dir_text)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"fair_{cache_key}.pkl"
    manifest = cache_dir / f"fair_{cache_key}.json"
    temporary = path.with_suffix(".tmp.pkl")
    temporary_manifest = manifest.with_suffix(".tmp.json")
    try:
        enriched.to_pickle(temporary)
        temporary_manifest.write_text(json.dumps({"schema": FAIR_CACHE_SCHEMA_VERSION, "key": cache_key}), encoding="utf-8")
        temporary.replace(path)
        temporary_manifest.replace(manifest)
    except Exception:
        temporary.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)


def build_grid_trade_contexts(
    trades: pd.DataFrame,
    target: pd.DataFrame,
    frames: Mapping[str, pd.DataFrame],
    config: Mapping[str, Any],
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
            "target_rows": target_rows,
            "contracts": contracts,
        }
    return contexts


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


def _empty_daily(scenario: Mapping[str, Any], instrument: str, trade_date: str, reason: str) -> dict[str, Any]:
    return {
        "scenario_id": scenario["scenario_id"],
        "instrument": instrument,
        "trade_date": trade_date,
        **{key: 0 for key in GRID_DAILY_COLUMNS if key not in {"scenario_id", "instrument", "trade_date", "skipped", "skip_reason"}},
        "skipped": True,
        "skip_reason": reason,
    }


def _summarize_scenario(
    instrument: str,
    scenario: Mapping[str, Any],
    daily: pd.DataFrame,
    thresholds: Mapping[str, Any],
) -> dict[str, Any]:
    active = daily.loc[~daily["skipped"]]
    fills = int(active["fill_count"].sum())
    event_fills = int(active["detector_event_fill_count"].sum())
    normal_fills = int(active["normal_move_fill_count"].sum())
    closed = int(active["closed_count"].sum())
    wins = int(active["win_count"].sum())
    hedge_failures = int(active["hedge_failure_count"].sum())
    net_pnl = float(active["net_pnl"].sum())
    daily_net = pd.to_numeric(active["net_pnl"], errors="coerce")
    normal_rate = normal_fills / fills if fills else np.nan
    hedge_rate = hedge_failures / fills if fills else np.nan
    reasons: list[str] = []
    if fills < int(thresholds["min_fills"]):
        reasons.append("insufficient_fills")
    if event_fills < int(thresholds["min_event_fills"]):
        reasons.append("insufficient_detector_event_fills")
    if fills and normal_rate > float(thresholds["max_normal_move_fill_rate"]):
        reasons.append("normal_move_fill_rate_too_high")
    if fills and hedge_rate > float(thresholds["max_hedge_failure_rate"]):
        reasons.append("hedge_failure_rate_too_high")
    if not daily_net.empty and float(daily_net.min()) < -float(thresholds["max_daily_loss"]):
        reasons.append("daily_loss_too_high")
    peak_actions = int(active["peak_order_actions_per_minute"].max()) if not active.empty else 0
    if peak_actions > int(thresholds["max_peak_order_actions_per_minute"]):
        reasons.append("order_action_limit_exceeded")
    if net_pnl <= 0 and fills:
        reasons.append("net_pnl_not_positive")
    return {
        **{key: scenario[key] for key in GRID_SCENARIO_COLUMNS if key != "instrument"},
        "instrument": instrument,
        "active_days": int(len(active)),
        "fill_count": fills,
        "detector_event_fill_count": event_fills,
        "normal_move_fill_count": normal_fills,
        "normal_move_fill_rate": normal_rate,
        "fill_during_replace_count": int(active["fill_during_replace_count"].sum()),
        "closed_count": closed,
        "hedge_failure_count": hedge_failures,
        "hedge_failure_rate": hedge_rate,
        "capital_limit_exit_count": int(active["capital_limit_exit_count"].sum()),
        "net_pnl": net_pnl,
        "win_rate": wins / fills if fills else np.nan,
        "worst_day_net_pnl": float(daily_net.min()) if not daily_net.empty else np.nan,
        "daily_net_std": float(daily_net.std(ddof=0)) if not daily_net.empty else np.nan,
        "max_gross_margin": np.nan,
        "peak_order_actions_per_minute": peak_actions,
        "total_order_action_count": int(active["order_action_count"].sum()),
        "total_reprice_count": int(active["reprice_count"].sum()),
        "skipped_day_count": int(daily["skipped"].sum()),
        "eligible": not reasons,
        "selection_reason": "eligible" if not reasons else ";".join(reasons),
    }


def write_programmatic_grid_outputs(result: Mapping[str, Any]) -> dict[str, str]:
    output_dir = Path(str(result["config"]["output_dir"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output_dir / "programmatic_grid_summary.csv",
        "daily": output_dir / "programmatic_grid_daily.csv",
        "trades": output_dir / "programmatic_grid_trades.csv",
        "trade_contexts": output_dir / "programmatic_grid_trade_contexts.json",
        "scenarios": output_dir / "programmatic_grid_scenarios.csv",
        "skipped_days": output_dir / "programmatic_grid_skipped_days.csv",
        "config": output_dir / "programmatic_grid_run_config.json",
        "report": output_dir / "programmatic_grid_report.html",
    }
    result["summary"].to_csv(paths["summary"], index=False, encoding="utf-8-sig")
    result["daily"].to_csv(paths["daily"], index=False, encoding="utf-8-sig")
    result["trades"].to_csv(paths["trades"], index=False, encoding="utf-8-sig")
    paths["trade_contexts"].write_text(
        json.dumps(result.get("trade_contexts", {}), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    result["scenarios"].to_csv(paths["scenarios"], index=False, encoding="utf-8-sig")
    result["skipped_days"].to_csv(paths["skipped_days"], index=False, encoding="utf-8-sig")
    paths["config"].write_text(json.dumps(result["config"], ensure_ascii=False, indent=2), encoding="utf-8")
    paths["report"].write_text(_render_grid_report(result), encoding="utf-8")
    return {name: str(path) for name, path in paths.items()}


def _render_grid_report(result: Mapping[str, Any]) -> str:
    payload = {
        "summary": _frame_records(result["summary"]),
        "daily": _frame_records(result["daily"]),
        "trades": _frame_records(result["trades"]),
        "trade_contexts": result.get("trade_contexts", {}),
        "context_mode": result.get("context_mode", result.get("config", {}).get("context_mode", "all")),
        "config": result["config"],
    }
    return _GRID_REPORT_TEMPLATE.replace("__GRID_PAYLOAD__", _script_json(payload))


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    """把 pandas 缺失值转换为浏览器可直接消费的 JSON null。"""
    return json.loads(frame.to_json(orient="records", force_ascii=False))


def _script_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_GRID_REPORT_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>乌龙指程序化参数网格回放</title>
<style>
:root{color:#1d2939;background:#f5f7fb;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
body{max-width:1500px;margin:0 auto;padding:26px;color:#172033}h1{margin:0 0 6px}h2{margin:30px 0 12px;font-size:19px}.muted{color:#667085}.note{padding:12px 14px;background:#fff6df;border-left:4px solid #e28b00;border-radius:7px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin:18px 0}.card,.panel,.scenario{background:#fff;border:1px solid #e4e8ef;border-radius:10px;box-shadow:0 1px 3px #dfe5ee;padding:13px}.card span,.label{display:block;color:#667085;font-size:12px}.card strong{display:block;font-size:22px;margin-top:4px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}button,select{font:inherit;padding:7px 10px;border:1px solid #cbd5e1;border-radius:7px;background:#fff}button{cursor:pointer}.scenario-list{display:grid;gap:9px}.scenario{cursor:pointer;padding:14px;border-left:5px solid #98a2b3}.scenario:hover,.scenario.selected{border-color:#1769aa;background:#f5faff}.scenario-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.scenario-title{font-weight:700;font-size:16px}.metrics{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.metric,.tag{border-radius:999px;padding:3px 8px;font-size:12px;background:#eef2f6}.good{background:#e7f6ec;color:#146c35}.bad{background:#ffebe9;color:#b42318}.warn{background:#fff4d6;color:#8a5700}.detail-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.detail-grid .panel{box-shadow:none}.pairs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}.pairs div{font-size:13px}.pairs span{display:block;color:#667085;font-size:11px}.table-wrap{max-height:520px;overflow:auto;background:#fff;border:1px solid #e4e8ef;border-radius:10px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #edf0f4;text-align:left;white-space:nowrap}thead th{background:#f7f9fc;position:sticky;top:0;z-index:1}.target-table{overflow-x:hidden}.target-table table{table-layout:fixed}.target-table th,.target-table td{white-space:normal;word-break:break-word;padding:7px 5px;font-size:11px}tr.clickable{cursor:pointer}tr.clickable:hover{background:#f5faff}.negative{color:#b42318;font-weight:650}.positive{color:#146c35;font-weight:650}details{margin-top:18px}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#f7f9fc;padding:12px;border-radius:7px;max-height:340px;overflow:auto}.hidden{display:none}.detail-button{color:#fff;background:#1769aa;border-color:#1769aa}.modal{position:fixed;inset:0;z-index:20;padding:26px;overflow:auto;background:rgba(15,23,42,.48)}.modal-panel{max-width:1440px;margin:auto;background:#fff;border-radius:12px;padding:20px;box-shadow:0 20px 80px rgba(15,23,42,.3)}.modal-topbar{display:flex;justify-content:space-between;gap:12px;align-items:center}.modal-close{background:#172033;color:#fff;border-color:#172033}.marker{background:#fee2e2;font-weight:700}.contract-title{margin:24px 0 8px;font-size:16px}@media(max-width:900px){.detail-grid{grid-template-columns:1fr}.scenario-head{display:block}.modal{padding:8px}.modal-panel{padding:14px}}
</style>
</head>
<body>
<h1>乌龙指程序化参数网格回放</h1>
<p class="muted">默认只展示发生过模拟成交的参数组合；候选事件标签仅用于事后归因。</p>
<p class="note"><strong>结果边界：</strong>本页是基于 LastPrice、区间均价和买卖一条件的快照模拟成交，不代表真实成交或可直接实盘收益。</p>
<section id="overview" class="cards"></section>

<h2>发生模拟成交的参数组合</h2>
<div class="toolbar"><label>品种 <select id="instrument-filter"><option value="">全部</option></select></label><span id="filled-count" class="muted"></span></div>
<div id="scenario-list" class="scenario-list"></div>

<section id="detail" class="hidden">
<h2 id="detail-title">组合详情</h2>
<div id="detail-cards" class="detail-grid"></div>
<h2>逐日结果</h2><div class="table-wrap"><table><thead><tr><th>日期</th><th>总成交</th><th>候选事件</th><th>正常行情</th><th>日净收益</th><th>对冲失败</th><th>报撤峰值/分钟</th></tr></thead><tbody id="daily-body"></tbody></table></div>
<h2>单笔模拟成交</h2>
<div class="toolbar">
<label>成交分类 <select id="event-filter"><option value="">全部</option><option value="detector_event_fill">候选事件成交</option><option value="normal_move_fill">正常行情成交</option></select></label>
<label>方向 <select id="direction-filter"><option value="">全部</option><option value="long">买入目标</option><option value="short">卖出目标</option></select></label>
<label>成交证据 <select id="evidence-filter"><option value="">全部</option></select></label>
<label>退出原因 <select id="exit-filter"><option value="">全部</option></select></label>
<label>退出状态 <select id="status-filter"><option value="">全部</option></select></label>
<span id="trade-count" class="muted"></span>
</div>
<div class="table-wrap"><table><thead><tr><th>成交时间</th><th>方向</th><th>成交分类</th><th>成交证据</th><th>目标入场</th><th>参考腿入场</th><th>退出原因</th><th>目标盈亏</th><th>对冲盈亏</th><th>净收益</th><th>未对冲最差</th><th>对冲后最差</th><th>保证金</th><th>复盘</th></tr></thead><tbody id="trade-body"></tbody></table></div>
<details><summary>完整生效配置</summary><pre id="config"></pre></details>
</section>

<details><summary>零成交组合（默认收起）</summary><div id="zero-list" class="scenario-list"></div></details>
<section id="trade-modal" class="modal hidden" role="dialog" aria-modal="true">
<div class="modal-panel"><div class="modal-topbar"><strong id="modal-title">单笔模拟成交复盘</strong><button id="modal-close" class="modal-close">关闭</button></div>
<p class="note">复盘窗口为目标成交前 10 秒至退出后 10 秒；参考合约关键时点均使用当时或之前最近快照。</p>
<div id="modal-cards" class="cards"></div><div id="modal-content"></div><details><summary>该笔交易原始字段</summary><pre id="modal-raw"></pre></details></div>
</section>
<script id="grid-payload" type="application/json">__GRID_PAYLOAD__</script>
<script>
const data=JSON.parse(document.getElementById('grid-payload').textContent);
const reasonLabels={insufficient_fills:'成交次数不足',insufficient_detector_event_fills:'候选事件成交不足',normal_move_fill_rate_too_high:'正常行情误成交过高',hedge_failure_rate_too_high:'对冲失败率过高',daily_loss_too_high:'单日亏损超限',order_action_limit_exceeded:'报撤动作超限',net_pnl_not_positive:'净收益不为正'};
const eventLabels={detector_event_fill:'候选事件成交',normal_move_fill:'正常行情成交'};
const statusLabels={closed:'已正常退出',hedge_failure_exit:'对冲失败退出',capital_limit_exit:'保证金限制退出',unclosed_end_of_day:'收盘未平'};
const exitLabels={reversion_exit:'回归退出',take_profit:'止盈退出',max_hold:'最长持有退出',time_exit:'最长持有退出',emergency_flatten:'紧急平仓',hedge_failure:'对冲失败',hedge_failure_exit:'对冲失败退出',reference_invalid_exit:'参考价格失效退出'};
const evidenceLabels={last_trade:'LastPrice',interval_vwap:'区间均价',top_of_book:'买卖一'};
const keyOf=r=>r.instrument+'::'+r.scenario_id;
const number=v=>v===null||v===undefined||Number.isNaN(Number(v))?'—':Number(v).toLocaleString('zh-CN',{maximumFractionDigits:2});
const display=v=>v===null||v===undefined||v===''?'—':typeof v==='number'?number(v):String(v);
const money=v=>v===null||v===undefined?'—':(Number(v)>=0?'+':'')+number(v);
const percent=v=>v===null||v===undefined?'—':(Number(v)*100).toFixed(1)+'%';
const labelEvidence=v=>(v||'—').split('+').map(x=>evidenceLabels[x]||x).join(' + ');
const reasonTags=v=>(v||'').split(';').filter(Boolean).map(x=>`<span class="tag bad">${reasonLabels[x]||x}</span>`).join('')||'<span class="tag good">通过研究筛选</span>';
const threshold=(data.config.thresholds||{}).max_peak_order_actions_per_minute;
const summary=data.summary||[],daily=data.daily||[],trades=data.trades||[],tradeContexts=data.trade_contexts||{};
const targetColumns=[['关键时点','关键时点'],['display_time','更新时间'],['LastPrice','最新成交价'],['Volume','累计成交量'],['Turnover','累计成交额'],['BidPrice1','买一价'],['BidVolume1','买一量'],['AskPrice1','卖一价'],['AskVolume1','卖一量'],['delta_volume','区间增量成交量'],['delta_turnover','区间增量成交额'],['interval_vwap','区间成交均价'],['fair_price','合理价'],['last_down_ticks','末笔向下偏离_跳'],['fair_price_reliable','合理价可靠'],['valid_peer_count','有效参考数'],['peer_contracts','有效参考合约']];
const referenceColumns=[['关键时点','关键时点'],['display_time','更新时间'],['LastPrice','最新成交价'],['Volume','累计成交量'],['Turnover','累计成交额'],['BidPrice1','买一价'],['BidVolume1','买一量'],['AskPrice1','卖一价'],['AskVolume1','卖一量'],['AveragePrice','原始平均价'],['OpenInterest','持仓量'],['UpperLimitPrice','涨停价'],['LowerLimitPrice','跌停价'],['delta_volume','区间增量成交量'],['delta_turnover','区间增量成交额'],['interval_vwap','区间成交均价']];
let selectedKey='';
function overview(){const filled=summary.filter(x=>Number(x.fill_count)>0), actions=summary.filter(x=>Number(x.peak_order_actions_per_minute)>Number(threshold)).length;const cards=[['参数组合',summary.length],['有模拟成交',filled.length],['总模拟成交',summary.reduce((n,x)=>n+Number(x.fill_count||0),0)],['候选事件成交',summary.reduce((n,x)=>n+Number(x.detector_event_fill_count||0),0)],['正常行情成交',summary.reduce((n,x)=>n+Number(x.normal_move_fill_count||0),0)],['对冲失败',summary.reduce((n,x)=>n+Number(x.hedge_failure_count||0),0)],['累计净收益',money(summary.reduce((n,x)=>n+Number(x.net_pnl||0),0))],['动作峰值超限组合',actions]];document.getElementById('overview').innerHTML=cards.map(([k,v])=>`<div class="card"><span>${k}</span><strong>${v}</strong></div>`).join('');}
function scenarioCard(row,isZero=false){const selected=keyOf(row)===selectedKey?' selected':'';const risk=Number(row.normal_move_fill_rate)>0?'warn':'';return `<article class="scenario${selected}" data-key="${keyOf(row)}"><div class="scenario-head"><div><div class="scenario-title">${row.instrument} · ${row.scenario_id}</div><div class="muted">W/D/S：${number(row.band_half_width_ticks)} / ${number(row.outer_quote_offset_ticks)} / ${number(row.reanchor_step_ticks)} ｜ 撤单/新单/对冲 ACK：${number(row.cancel_ack_latency_ms)}/${number(row.new_order_ack_latency_ms)}/${number(row.hedge_submit_latency_ms)} ms</div></div><div>${reasonTags(row.selection_reason)}</div></div><div class="metrics"><span class="metric">总成交 ${number(row.fill_count)}</span><span class="metric ${Number(row.detector_event_fill_count)>0?'good':''}">候选事件 ${number(row.detector_event_fill_count)}</span><span class="metric ${risk}">正常行情 ${number(row.normal_move_fill_count)}（${percent(row.normal_move_fill_rate)}）</span><span class="metric ${Number(row.net_pnl)<0?'bad':'good'}">净收益 ${money(row.net_pnl)}</span><span class="metric">最差日 ${money(row.worst_day_net_pnl)}</span><span class="metric ${Number(row.hedge_failure_count)>0?'bad':''}">对冲失败 ${number(row.hedge_failure_count)}</span><span class="metric ${Number(row.peak_order_actions_per_minute)>Number(threshold)?'bad':''}">动作峰值 ${number(row.peak_order_actions_per_minute)}/分</span></div></article>`;}
function renderScenarios(){const instrument=document.getElementById('instrument-filter').value;const filled=summary.filter(x=>Number(x.fill_count)>0&&(!instrument||x.instrument===instrument)).sort((a,b)=>Number(b.detector_event_fill_count)-Number(a.detector_event_fill_count)||Number(a.normal_move_fill_rate??99)-Number(b.normal_move_fill_rate??99)||Number(b.net_pnl)-Number(a.net_pnl));if(!selectedKey&&filled.length)selectedKey=keyOf(filled[0]);document.getElementById('scenario-list').innerHTML=filled.map(x=>scenarioCard(x)).join('')||'<div class="panel muted">当前筛选下没有模拟成交。</div>';document.getElementById('filled-count').textContent=`${filled.length} 个有模拟成交的组合`;document.querySelectorAll('#scenario-list .scenario').forEach(x=>x.onclick=()=>{selectedKey=x.dataset.key;renderScenarios();renderDetail();});const zero=summary.filter(x=>Number(x.fill_count)===0&&(!instrument||x.instrument===instrument));document.getElementById('zero-list').innerHTML=zero.map(x=>scenarioCard(x,true)).join('')||'<div class="panel muted">无零成交组合。</div>';}
function pairs(items){return '<div class="pairs">'+items.map(([k,v])=>`<div><span>${k}</span>${v}</div>`).join('')+'</div>'}
function renderDetail(){const row=summary.find(x=>keyOf(x)===selectedKey);const section=document.getElementById('detail');if(!row){section.classList.add('hidden');return}section.classList.remove('hidden');document.getElementById('detail-title').textContent=`${row.instrument} · ${row.scenario_id} 组合详情`;const current=trades.filter(x=>keyOf(x)===selectedKey), evidence={};current.forEach(x=>{labelEvidence(x.fill_evidence).split(' + ').forEach(x=>evidence[x]=(evidence[x]||0)+1)});const maxMargin=current.reduce((m,x)=>Math.max(m,Number(x.gross_margin||0)),0);document.getElementById('detail-cards').innerHTML=[['报价参数',pairs([['W/D/S',`${number(row.band_half_width_ticks)} / ${number(row.outer_quote_offset_ticks)} / ${number(row.reanchor_step_ticks)}`],['重定锚确认',number(row.reanchor_confirm_ms)+' ms'],['撤单 ACK',number(row.cancel_ack_latency_ms)+' ms'],['新单 ACK',number(row.new_order_ack_latency_ms)+' ms'],['对冲提交',number(row.hedge_submit_latency_ms)+' ms']])],['成交结构',pairs([['总模拟成交',number(row.fill_count)],['候选事件成交',number(row.detector_event_fill_count)],['正常行情成交',number(row.normal_move_fill_count)],['撤改单期间',number(row.fill_during_replace_count)],['成交证据',Object.entries(evidence).map(([k,v])=>k+' '+v).join('，')||'—']])],['风险结果',pairs([['净收益',money(row.net_pnl)],['胜率',percent(row.win_rate)],['最差单日',money(row.worst_day_net_pnl)],['对冲失败',number(row.hedge_failure_count)],['单笔最大保证金',number(maxMargin)],['动作峰值',number(row.peak_order_actions_per_minute)+'/分']])]].map(([title,body])=>`<div class="panel"><strong>${title}</strong>${body}</div>`).join('');const days=daily.filter(x=>keyOf(x)===selectedKey).sort((a,b)=>String(a.trade_date).localeCompare(String(b.trade_date)));document.getElementById('daily-body').innerHTML=days.map(x=>`<tr class="${Number(x.net_pnl)<0||Number(x.hedge_failure_count)>0||Number(x.peak_order_actions_per_minute)>Number(threshold)?'negative':''}"><td>${x.trade_date}</td><td>${number(x.fill_count)}</td><td>${number(x.detector_event_fill_count)}</td><td>${number(x.normal_move_fill_count)}</td><td>${money(x.net_pnl)}</td><td>${number(x.hedge_failure_count)}</td><td>${number(x.peak_order_actions_per_minute)}</td></tr>`).join('')||'<tr><td colspan="7">无逐日数据</td></tr>';setupTradeFilters(current);renderTrades();document.getElementById('config').textContent=JSON.stringify(data.config,null,2);}
function fillOptions(id,values,labels={}){const el=document.getElementById(id),old=el.value;el.innerHTML='<option value="">全部</option>'+[...new Set(values.filter(Boolean))].sort().map(x=>`<option value="${x}">${labels[x]||labelEvidence(x)}</option>`).join('');el.value=old;}
function setupTradeFilters(current){fillOptions('evidence-filter',current.map(x=>x.fill_evidence));fillOptions('exit-filter',current.map(x=>x.exit_reason),exitLabels);fillOptions('status-filter',current.map(x=>x.status),statusLabels);['event-filter','direction-filter','evidence-filter','exit-filter','status-filter'].forEach(id=>document.getElementById(id).onchange=renderTrades)}
function tradeContextKey(x){return keyOf(x)+'::'+x.trade_id}
function contextTable(rows,columns,target=false){const head=columns.map(([,label])=>`<th>${label}</th>`).join('');const body=rows.map(row=>`<tr class="${row['关键时点']?'marker':''}">${columns.map(([key])=>`<td>${display(row[key])}</td>`).join('')}</tr>`).join('')||`<tr><td colspan="${columns.length}">该窗口无可用快照。</td></tr>`;return `<div class="table-wrap${target?' target-table':''}"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`}
function openTradeDetail(trade){const context=tradeContexts[tradeContextKey(trade)];if(!context){alert(data.context_mode==='none'?'本次运行未生成成交详情（context_mode=none）。':'该笔交易未生成复盘上下文。');return}document.getElementById('modal-title').textContent=`${trade.instrument} · ${trade.scenario_id} · ${trade.trade_id}`;const phaseTimes=context.phase_times||{};const scenario=summary.find(x=>keyOf(x)===keyOf(trade))||{};const hold=trade.exit_key===null||trade.exit_key===undefined?'—':((Number(trade.exit_key)-Number(trade.fill_key))/1000).toFixed(1)+' 秒';const cards=[['目标/对冲合约',`${trade.target_contract} / ${trade.hedge_contract}`],['W / D / S',`${number(scenario.band_half_width_ticks)} / ${number(scenario.outer_quote_offset_ticks)} / ${number(scenario.reanchor_step_ticks)}`],['方向',trade.direction==='long'?'买入目标':'卖出目标'],['成交分类',eventLabels[trade.event_label]||trade.event_label],['成交证据',labelEvidence(trade.fill_evidence)],['目标成交价',number(trade.target_entry_price)],['参考合约对冲成交价',`${trade.hedge_contract || '—'}：${number(trade.hedge_entry_price)}`],['目标成交',phaseTimes['目标成交']||trade.fill_time],['参考腿成交',phaseTimes['参考腿成交']||'未成交'],['退出',phaseTimes['退出']||'未退出'],['持仓时长',hold],['退出原因',exitLabels[trade.exit_reason]||trade.exit_reason||'—'],['状态',statusLabels[trade.status]||trade.status],['目标/对冲盈亏',`${money(trade.target_pnl)} / ${money(trade.hedge_pnl)}`],['净收益',money(trade.net_pnl)],['保证金',number(trade.gross_margin)],['未对冲最差',money(trade.unhedged_worst_mark_pnl)],['对冲后最差',money(trade.hedged_worst_mark_pnl)]];document.getElementById('modal-cards').innerHTML=cards.map(([k,v])=>`<div class="card"><span>${k}</span><strong>${v}</strong></div>`).join('');let content=`<h2>目标合约 ${trade.target_contract}</h2><p class="muted">完整持仓窗口：${display(context.window.start_time)} 至 ${display(context.window.end_time)}（目标成交前 ${number(context.window.before_after_ms/1000)} 秒至退出后 ${number(context.window.before_after_ms/1000)} 秒）</p>${contextTable(context.target_rows,targetColumns,true)}`;Object.entries(context.contracts||{}).forEach(([code,part])=>{content+=`<h3 class="contract-title">${code} <span class="muted">${(part.roles||[]).join(' / ')}</span></h3>${contextTable(part.rows||[],referenceColumns)}`});document.getElementById('modal-content').innerHTML=content;document.getElementById('modal-raw').textContent=JSON.stringify(trade,null,2);document.getElementById('trade-modal').classList.remove('hidden')}
function renderTrades(){let rows=trades.filter(x=>keyOf(x)===selectedKey);const event=document.getElementById('event-filter').value,direction=document.getElementById('direction-filter').value,evidence=document.getElementById('evidence-filter').value,exit=document.getElementById('exit-filter').value,status=document.getElementById('status-filter').value;rows=rows.filter(x=>(!event||x.event_label===event)&&(!direction||x.direction===direction)&&(!evidence||x.fill_evidence===evidence)&&(!exit||x.exit_reason===exit)&&(!status||x.status===status));document.getElementById('trade-count').textContent=`${rows.length} 笔`;document.getElementById('trade-body').innerHTML=rows.sort((a,b)=>Number(a.fill_key)-Number(b.fill_key)).map(x=>`<tr><td>${x.fill_time}</td><td>${x.direction==='long'?'买入目标':'卖出目标'}</td><td><span class="tag ${x.event_label==='normal_move_fill'?'warn':'good'}">${eventLabels[x.event_label]||x.event_label}</span></td><td>${labelEvidence(x.fill_evidence)}</td><td>${number(x.target_entry_price)}</td><td>${number(x.hedge_entry_price)}</td><td>${exitLabels[x.exit_reason]||x.exit_reason||'—'}<br><span class="muted">${statusLabels[x.status]||x.status}</span></td><td>${money(x.target_pnl)}</td><td>${money(x.hedge_pnl)}</td><td class="${Number(x.net_pnl)<0?'negative':'positive'}">${money(x.net_pnl)}</td><td>${money(x.unhedged_worst_mark_pnl)}</td><td>${money(x.hedged_worst_mark_pnl)}</td><td>${number(x.gross_margin)}</td><td><button class="detail-button" data-context="${tradeContextKey(x)}">查看详情</button></td></tr>`).join('')||'<tr><td colspan="14">当前筛选下无成交。</td></tr>';document.querySelectorAll('[data-context]').forEach(button=>button.onclick=()=>openTradeDetail(trades.find(x=>tradeContextKey(x)===button.dataset.context)));}
function init(){overview();const instruments=[...new Set(summary.map(x=>x.instrument))].sort();document.getElementById('instrument-filter').innerHTML+instruments.map(x=>`<option value="${x}">${x}</option>`).join('');document.getElementById('instrument-filter').onchange=()=>{selectedKey='';renderScenarios();renderDetail()};document.getElementById('modal-close').onclick=()=>document.getElementById('trade-modal').classList.add('hidden');document.getElementById('trade-modal').onclick=e=>{if(e.target.id==='trade-modal')e.currentTarget.classList.add('hidden')};document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('trade-modal').classList.add('hidden')});renderScenarios();renderDetail();}
init();
</script>
</body>
</html>"""
