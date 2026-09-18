from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
import multiprocessing as mp
from pathlib import Path
import re

import numpy as np
import pandas as pd

from src.tick_detector.event_detection import (
    attach_recovery_metrics,
    detect_candidate_ticks,
    extract_candidate_ticks,
    merge_candidates,
)
from src.tick_detector.reference_selection import (
    attach_fair_price_metrics,
    select_reference_contracts,
)
from src.tick_detector.report_html import render_event_replay_html
from src.tick_detector.tick_io import (
    COMMODITY_PROFILES,
    iter_day_contract_files,
    load_contract_snapshots,
    load_daily_bounds,
    normalize_contract_code,
    prepare_contract_snapshots,
)

DETECTOR_VERSION = "aggregated-tick-v2"

_WORKER_DAY_FRAMES: dict[str, pd.DataFrame] = {}

# 中文 CSV 列定义（设计文档 §9）：(内部英文键, 中文表头)
CSV_COLUMNS: list[tuple[str, str]] = [
    ("detector_version", "检测器版本"),
    ("parameter_profile", "参数档"),
    ("validation_status", "验证状态"),
    ("event_id", "事件编号"),
    ("display_trade_date", "交易日"),
    ("event_anchor_time", "事件时间"),
    ("interval_confirmation_end_time", "区间均价确认结束时间"),
    ("commodity", "品种"),
    ("contract", "合约"),
    ("event_start_key", "事件开始序号"),
    ("event_anchor_start_key", "事件锚点开始序号"),
    ("event_anchor_end_key", "事件锚点结束序号"),
    ("event_end_key", "事件结束序号"),
    ("event_direction", "异常方向"),
    ("trigger_reasons", "触发原因"),
    ("fair_price", "合理价"),
    ("fair_uncertainty_ticks", "合理价不确定性_跳"),
    ("valid_peer_count", "有效参考合约数"),
    ("peer_contracts", "参考合约列表"),
    ("baseline_min_samples", "价差基线最少样本数"),
    ("noise_sample_count", "噪声样本数"),
    ("noise_time_span_seconds", "噪声覆盖秒数"),
    ("last_price_anchor", "最新成交价"),
    ("interval_vwap_anchor", "区间成交均价"),
    ("last_down_ticks", "末笔向下偏离_跳"),
    ("last_down_bps", "末笔向下偏离_基点"),
    ("last_up_ticks", "末笔向上偏离_跳"),
    ("last_up_bps", "末笔向上偏离_基点"),
    ("vwap_down_ticks", "区间均价向下偏离_跳"),
    ("vwap_down_bps", "区间均价向下偏离_基点"),
    ("vwap_up_ticks", "区间均价向上偏离_跳"),
    ("vwap_up_bps", "区间均价向上偏离_基点"),
    ("combined_vwap_1s", "一秒合并成交均价"),
    ("combined_vwap_down_ticks", "一秒合并均价向下偏离_跳"),
    ("combined_vwap_up_ticks", "一秒合并均价向上偏离_跳"),
    ("last_threshold_ticks", "末笔触发阈值_跳"),
    ("vwap_threshold_ticks", "区间均价触发阈值_跳"),
    ("onset_ticks", "突发偏离_跳"),
    ("event_depth_ticks", "事件确认深度_跳"),
    ("event_depth_bps", "事件确认深度_基点"),
    ("depth_source", "确认深度来源"),
    ("interval_delta_volume", "区间增量成交量"),
    ("interval_delta_turnover", "区间增量成交额"),
    ("event_volume", "事件成交量"),
    ("notional_shortfall", "名义成交额缺口"),
    ("notional_excess", "名义成交额超额"),
    ("recovery_label", "回归标签"),
    ("visible_recovered_seconds", "可见末笔恢复确认秒数"),
    ("interval_recovered_seconds", "区间均价恢复确认秒数"),
    ("quote_recovered_seconds", "买一恢复确认秒数"),
    ("ask_recovered_seconds", "卖一恢复确认秒数"),
    ("data_quality_flags", "数据质量标记"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聚合 Tick 乌龙指候选检测器")
    parser.add_argument("--tick-day-path", required=True, help="单日 tick 目录或 zip 路径")
    parser.add_argument("--commodity", required=False, help="可选，限制检测目标品种")
    parser.add_argument("--commodities", required=False, help="可选，逗号分隔品种列表，如 AU,AG,CU")
    parser.add_argument("--contract", required=False, help="可选，限制检测目标合约")
    parser.add_argument("--output-dir", required=False, help="输出目录")
    parser.add_argument(
        "--only-with-events",
        action="store_true",
        help="无候选事件的品种不输出 HTML/CSV（批量跑批用，避免空品种堆积）",
    )
    parser.add_argument(
        "--target-workers",
        type=int,
        default=1,
        help="同一品种内并行检测的目标合约进程数（默认 1）",
    )
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.commodity and args.commodities:
        parser.error("--commodity 不能与 --commodities 同时使用")
    if args.commodities is not None and not [code.strip() for code in args.commodities.split(",") if code.strip()]:
        parser.error("--commodities 不能为空或只包含逗号/空白")
    if args.contract and args.commodities and len([code for code in args.commodities.split(",") if code.strip()]) > 1:
        parser.error("--contract 不能与多个 --commodities 同时使用")
    if args.contract and args.commodities:
        selected = args.commodities.strip().upper()
        if _commodity_from_contract(args.contract) != selected:
            parser.error("--contract 必须属于 --commodities 指定的品种")
    if args.contract and args.commodity and _commodity_from_contract(args.contract) != args.commodity.upper():
        parser.error("--contract 必须属于 --commodity 指定的品种")
    if args.target_workers < 1:
        parser.error("--target-workers 必须大于等于 1")
    if not args.output_dir:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output_dir = str(Path("output") / f"{timestamp}-tick-detector")
    return args


def run_detection(
    *,
    tick_day_path: str,
    output_dir: str,
    commodity: str | None = None,
    contract: str | None = None,
    commodities: str | None = None,
    only_with_events: bool = False,
    target_workers: int = 1,
) -> dict[str, object]:
    if target_workers < 1:
        raise ValueError("target_workers 必须大于等于 1")
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    if commodities:
        target_commodities = [code.strip().upper() for code in commodities.split(",") if code.strip()]
    elif commodity:
        target_commodities = [commodity.upper()]
    elif contract:
        target_commodities = [_commodity_from_contract(contract)]
    else:
        target_commodities = None
    all_events: list[pd.DataFrame] = []
    contract_filter = contract.upper() if contract else None
    commodity_files = _group_contract_files_by_commodity(tick_day_path, target_commodities)
    if target_commodities:
        requested = set(target_commodities)
        missing_data = sorted(requested - set(commodity_files))
        missing_profiles = sorted(requested - set(COMMODITY_PROFILES))
        unvalidated = sorted(
            code for code in requested
            if code in COMMODITY_PROFILES and COMMODITY_PROFILES[code].get("validation_status") != "validated"
        )
        if missing_data or missing_profiles or unvalidated:
            raise ValueError(
                f"请求品种不可运行：数据缺失={missing_data}，profile缺失={missing_profiles}，未审核={unvalidated}"
            )
    commodity_items = list(commodity_files.items())
    html_files: list[str] = []
    daily_bounds: dict | None = None
    daily_bounds_loaded = False

    for commodity_index, (commodity_code, contract_files) in enumerate(commodity_items, start=1):
        print(
            f"[{commodity_index}/{len(commodity_items)}] 处理品种 {commodity_code}，文件数 {len(contract_files)}",
            flush=True,
        )
        day_frames: dict[str, pd.DataFrame] = {}
        for file_index, contract_file in enumerate(contract_files, start=1):
            if file_index == 1 or file_index % 20 == 0 or file_index == len(contract_files):
                print(
                    f"  - 装载 {commodity_code} 合约文件 {file_index}/{len(contract_files)}：{contract_file.file_name}",
                    flush=True,
                )
            raw = load_contract_snapshots(contract_file)
            if raw.empty or raw["parse_status"].iloc[0] != "ok":
                continue
            if not daily_bounds_loaded:
                trade_date = str(raw["trade_date"].iloc[0]) if "trade_date" in raw.columns else ""
                daily_bounds = load_daily_bounds(trade_date) if trade_date else None
                daily_bounds_loaded = True
                if daily_bounds is not None:
                    print(
                        f"  - 加载日线边界 {trade_date}（{len(daily_bounds)} 合约），守卫A 用日线[Low,High]",
                        flush=True,
                    )
                else:
                    print(f"  - 未找到 {trade_date} 日线，守卫A 退回涨跌停带", flush=True)
            prepared = prepare_contract_snapshots(raw, daily_bounds=daily_bounds)
            if prepared.empty:
                continue
            contract_code = str(prepared["contract"].iloc[0])
            day_frames[contract_code] = prepared

        # 目标以外不足 3 个真实同品种合约时，无法满足成交量前三参考约束，直接跳过。
        commodity_targets = [] if len(day_frames) < 4 else [
            code for code in sorted(day_frames) if contract_filter is None or code.upper() == contract_filter
        ]
        print(
            f"  - {commodity_code} 有效合约 {len(day_frames)} 个，待检测目标 {len(commodity_targets)} 个",
            flush=True,
        )
        commodity_events: list[pd.DataFrame] = []
        commodity_payload: dict[str, dict[str, object]] = {}
        commodity_diagnostics: list[dict[str, object]] = []
        target_results = _detect_targets(
            commodity_code,
            commodity_targets,
            day_frames,
            target_workers=target_workers,
        )
        for target_code in commodity_targets:
            events, diag, marked = target_results[target_code]
            commodity_diagnostics.append(diag)
            if events is not None and not events.empty:
                all_events.append(events)
                commodity_events.append(events)
                _build_replay_payload(events, target_code, marked, day_frames, commodity_payload, daily_bounds)

        commodity_events_df = _build_events_df(commodity_events)
        if commodity_events_df.empty and only_with_events:
            print(f"  - {commodity_code} 无候选事件，跳过输出（--only-with-events）", flush=True)
            continue
        html_path = output_path / f"event_replay_{commodity_code}.html"
        html_path.write_text(
            render_event_replay_html(commodity_events_df, commodity_payload, commodity_diagnostics),
            encoding="utf-8",
        )
        html_files.append(str(html_path))
        print(f"  - 写出 {html_path.name}", flush=True)

    events_df = _build_events_df(all_events)
    events_df_chinese = _map_to_chinese_csv(events_df)
    csv_path = output_path / "tick_candidate_events.csv"
    if not events_df.empty or not only_with_events:
        events_df_chinese.to_csv(csv_path, index=False)
    if len(html_files) == 1:
        legacy_html_path = output_path / "event_replay.html"
        legacy_html_path.write_text(Path(html_files[0]).read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "tick_candidate_events_csv": str(csv_path),
        "event_replay_htmls": html_files,
        "event_replay_html": str(output_path / "event_replay.html") if len(html_files) == 1 else None,
    }


def _detect_contract(
    target_code: str,
    day_frames: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame | None, dict[str, object], pd.DataFrame | None]:
    """对单个目标执行检测链，返回事件 DataFrame 与合约诊断。"""
    target_df = day_frames[target_code]
    validation_status = str(target_df["validation_status"].iloc[0]) if "validation_status" in target_df.columns else "unvalidated_commodity"
    parameter_profile = target_df["parameter_profile"].iloc[0] if "parameter_profile" in target_df.columns else None
    tick_size = float(target_df["tick_size"].iloc[0]) if "tick_size" in target_df.columns else np.nan
    multiplier = int(target_df["contract_multiplier"].iloc[0]) if "contract_multiplier" in target_df.columns and pd.notna(target_df["contract_multiplier"].iloc[0]) else None

    raw_rows = int(target_df.attrs.get("raw_row_count", len(target_df)))
    merged_rows = len(target_df)

    # 非 validated 品种：只记诊断，不检测
    if validation_status != "validated" or multiplier is None or np.isnan(tick_size):
        diag = _build_diagnostics(
            target_code, target_df, raw_rows, merged_rows,
            parameter_profile, validation_status,
            candidates_count=0,
        )
        return None, diag, None

    ref_codes = select_reference_contracts(day_frames, target_code)
    reference_frames = {code: day_frames[code] for code in ref_codes if code in day_frames}
    enriched = attach_fair_price_metrics(
        target_df,
        reference_frames,
        tick_size=tick_size,
        top_volume_peer_contracts=set(ref_codes[:3]),
    )
    # marked = 全行检测帧（含双向偏离），供复盘窗口展示。
    marked = detect_candidate_ticks(enriched, return_marked=True)
    candidates = extract_candidate_ticks(marked)
    events = merge_candidates(candidates, enriched)
    if events.empty:
        diag = _build_diagnostics(
            target_code, enriched, raw_rows, merged_rows,
            parameter_profile, validation_status,
            candidates_count=0,
        )
        return events, diag, None

    profile = {"tick_size": tick_size, "contract_multiplier": multiplier}
    events = attach_recovery_metrics(events, enriched, reference_frames, profile)
    events = _finalize_event_fields(events, enriched, tick_size, multiplier, parameter_profile, validation_status)
    diag = _build_diagnostics(
        target_code, enriched, raw_rows, merged_rows,
        parameter_profile, validation_status,
        candidates_count=len(events),
    )
    return events, diag, marked


def _detect_targets(
    commodity_code: str,
    target_codes: list[str],
    day_frames: dict[str, pd.DataFrame],
    *,
    target_workers: int,
) -> dict[str, tuple[pd.DataFrame | None, dict[str, object], pd.DataFrame | None]]:
    """按目标合约并行检测；fork 共享只读日数据，避免重复装载和序列化。"""
    workers = min(target_workers, len(target_codes))
    if workers <= 1 or "fork" not in mp.get_all_start_methods():
        results = {}
        for index, target_code in enumerate(target_codes, start=1):
            print(f"    * 检测 {commodity_code} 目标 {index}/{len(target_codes)}：{target_code}", flush=True)
            results[target_code] = _detect_contract(
                target_code,
                day_frames,
            )
        return results

    print(f"    * {commodity_code} 品种内并行：{workers} 个进程", flush=True)
    results = {}
    context = mp.get_context("fork")
    try:
        executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            initializer=_init_target_worker,
            initargs=(day_frames,),
        )
    except (OSError, PermissionError, NotImplementedError) as exc:
        print(f"    * 当前环境不能创建子进程，退回串行：{exc}", flush=True)
        return _detect_targets(
            commodity_code,
            target_codes,
            day_frames,
            target_workers=1,
        )
    with executor:
        futures = {executor.submit(_detect_contract_worker, code): code for code in target_codes}
        completed = 0
        for future in as_completed(futures):
            target_code, result = future.result()
            results[target_code] = result
            completed += 1
            print(f"    * 完成 {commodity_code} 目标 {completed}/{len(target_codes)}：{target_code}", flush=True)
    return results


def _init_target_worker(day_frames: dict[str, pd.DataFrame]) -> None:
    global _WORKER_DAY_FRAMES
    _WORKER_DAY_FRAMES = day_frames


def _detect_contract_worker(
    target_code: str,
) -> tuple[str, tuple[pd.DataFrame | None, dict[str, object], pd.DataFrame | None]]:
    return target_code, _detect_contract(
        target_code,
        _WORKER_DAY_FRAMES,
    )


def _build_diagnostics(
    contract: str,
    enriched: pd.DataFrame,
    raw_rows: int,
    merged_rows: int,
    parameter_profile: str | None,
    validation_status: str,
    *,
    candidates_count: int,
) -> dict[str, object]:
    """统计合约运行诊断（设计文档 §9 HTML 顶部）。"""
    n = len(enriched)
    metadata_blocked = int((enriched.get("validation_status", pd.Series(["validated"] * n)) != "validated").sum()) if "validation_status" in enriched.columns else 0
    session_blocked = int((~enriched.get("is_tradable_session", pd.Series([True] * n))).sum()) if "is_tradable_session" in enriched.columns else 0
    peer_blocked = int(enriched.get("reference_blocked_reason", pd.Series([""] * n)).eq("insufficient_peers").sum()) if "reference_blocked_reason" in enriched.columns else 0
    fair_blocked = int(enriched.get("reference_blocked_reason", pd.Series([""] * n)).eq("fair_uncertainty_exceeded").sum()) if "reference_blocked_reason" in enriched.columns else 0
    noise_blocked = int(enriched.get("reference_blocked_reason", pd.Series([""] * n)).eq("insufficient_noise_history").sum()) if "reference_blocked_reason" in enriched.columns else 0
    counter_unconfirmed = 0
    counter_lag = 0
    if "data_quality_flags" in enriched.columns:
        counter_unconfirmed = int(enriched["data_quality_flags"].astype(str).str.contains("counter_sync_unconfirmed").sum())
        counter_lag = int(enriched["data_quality_flags"].astype(str).str.contains("counter_lag_suspect").sum())

    tradable = int(enriched.get("is_tradable_session", pd.Series([True] * n)).sum()) if "is_tradable_session" in enriched.columns else n
    volumes = pd.to_numeric(enriched.get("Volume", pd.Series(dtype=float)), errors="coerce").dropna()
    return {
        "contract": contract,
        "parameter_profile": parameter_profile,
        "validation_status": validation_status,
        "day_total_volume": int(volumes.iloc[-1]) if not volumes.empty else None,
        "raw_rows": raw_rows,
        "merged_rows": merged_rows,
        "tradable_rows": tradable,
        "metadata_blocked_rows": metadata_blocked,
        "session_blocked_rows": session_blocked,
        "peer_blocked_rows": peer_blocked,
        "fair_blocked_rows": fair_blocked,
        "noise_blocked_rows": noise_blocked,
        "counter_unconfirmed_rows": counter_unconfirmed,
        "counter_lag_rows": counter_lag,
        "candidate_events": candidates_count,
    }


def _finalize_event_fields(
    events: pd.DataFrame,
    enriched: pd.DataFrame,
    tick_size: float,
    multiplier: int,
    parameter_profile: str | None,
    validation_status: str,
) -> pd.DataFrame:
    """补充事件级展示与派生字段。"""
    events = events.copy()
    if "event_direction" not in events:
        events["event_direction"] = "down"
    else:
        events["event_direction"] = events["event_direction"].astype("string").str.strip().str.lower().replace("", "down").fillna("down")
    events["detector_version"] = DETECTOR_VERSION
    events["parameter_profile"] = parameter_profile
    events["validation_status"] = validation_status
    events["event_id"] = [
        f"{row['contract']}|{row['event_anchor_time']}" + ("|up" if str(row.get("event_direction", "down")) == "up" else "")
        for _, row in events.iterrows()
    ]
    events["last_down_bps"] = events["last_down_ticks"] * tick_size / events["fair_price"] * 10000
    events["last_up_bps"] = events["last_up_ticks"] * tick_size / events["fair_price"] * 10000
    events["vwap_down_bps"] = events["vwap_down_ticks"] * tick_size / events["fair_price"] * 10000
    events["vwap_up_bps"] = events["vwap_up_ticks"] * tick_size / events["fair_price"] * 10000
    events["event_depth_bps"] = events["event_depth_ticks"] * tick_size / events["fair_price"] * 10000
    events["depth_source"] = "detector_event_depth"
    # 区间增量成交量/额（锚点帧）
    anchor_dv = []
    anchor_dt = []
    interval_vwap = []
    last_price = []
    for _, ev in events.iterrows():
        ak = int(ev["event_anchor_key"])
        match = enriched.loc[enriched["market_time_key"] == ak]
        if not match.empty:
            r = match.iloc[0]
            anchor_dv.append(float(r["delta_volume"]) if pd.notna(r["delta_volume"]) else np.nan)
            anchor_dt.append(float(r["delta_turnover"]) if pd.notna(r["delta_turnover"]) else np.nan)
            interval_vwap.append(float(r["interval_vwap"]) if pd.notna(r["interval_vwap"]) else np.nan)
            last_price.append(float(r["LastPrice"]))
        else:
            anchor_dv.append(np.nan)
            anchor_dt.append(np.nan)
            interval_vwap.append(np.nan)
            last_price.append(np.nan)
    events["interval_delta_volume"] = anchor_dv
    events["interval_delta_turnover"] = anchor_dt
    events["interval_vwap_anchor"] = interval_vwap
    events["last_price_anchor"] = last_price
    events["baseline_min_samples"] = 20
    # 名义成交额缺口 = max(0, fair_price - interval_vwap) * delta_volume * multiplier
    events["notional_shortfall"] = np.where(
        (events["event_direction"].eq("down")) & (events["fair_price"] > events["interval_vwap_anchor"]) & events["interval_delta_volume"].notna(),
        np.maximum(0, events["fair_price"] - events["interval_vwap_anchor"]) * events["interval_delta_volume"] * multiplier,
        0.0,
    )
    events["notional_excess"] = np.where(
        (events["event_direction"].eq("up")) & (events["interval_vwap_anchor"] > events["fair_price"]) & events["interval_delta_volume"].notna(),
        np.maximum(0, events["interval_vwap_anchor"] - events["fair_price"]) * events["interval_delta_volume"] * multiplier,
        0.0,
    )
    # 确认结束时间映射到 display_time
    events["interval_confirmation_end_time"] = events["interval_confirmation_end_time"].apply(_key_to_display_time, enriched=enriched)
    return events


def _key_to_display_time(key: object, enriched: pd.DataFrame) -> str:
    if pd.isna(key):
        return ""
    match = enriched.loc[enriched["market_time_key"] == int(key)]
    if not match.empty:
        return str(match.iloc[0]["display_time"])
    return ""


def _build_events_df(all_events: list[pd.DataFrame]) -> pd.DataFrame:
    if all_events:
        return pd.concat(all_events, ignore_index=True)
    # 空候选：用去重后的内部英文列
    seen: set[str] = set()
    unique_cols = []
    for eng, _ in CSV_COLUMNS:
        if eng not in seen:
            unique_cols.append(eng)
            seen.add(eng)
    return pd.DataFrame(columns=unique_cols)


def _map_to_chinese_csv(events_df: pd.DataFrame) -> pd.DataFrame:
    """内部英文 DataFrame 列映射为中文 CSV 表头；排除双下划线内部字段。"""
    out = pd.DataFrame(index=events_df.index, columns=[header for _, header in CSV_COLUMNS])
    seen_english: set[str] = set()
    for eng_key, cn_header in CSV_COLUMNS:
        if eng_key in events_df.columns and eng_key not in seen_english:
            out[cn_header] = events_df[eng_key].values
            seen_english.add(eng_key)
    return out


def _build_replay_payload(
    events: pd.DataFrame,
    target_code: str,
    target_frame: pd.DataFrame,
    day_frames: dict[str, pd.DataFrame],
    replay_payload: dict[str, dict[str, object]],
    daily_bounds: dict[str, tuple[float, float, float]] | None,
) -> None:
    """为每个事件构建 [锚点前10秒, 锚点后10秒] 同窗口 replay payload。

    target_frame 用检测后的全行帧（含 fair_price / 双向偏离），
    这样目标检测明细能展示检测派生列；参考合约窗口仍取 day_frames 的原始帧。
    """
    target_df = target_frame
    daily_bound = (daily_bounds or {}).get(normalize_contract_code(target_code))
    for _, event in events.iterrows():
        event_id = str(event["event_id"])
        anchor_key = int(event["event_anchor_key"])
        window_start = anchor_key - 10 * 1000
        window_end = anchor_key + 10 * 1000
        window = target_df.loc[
            (target_df["market_time_key"] >= window_start)
            & (target_df["market_time_key"] <= window_end)
        ].copy()
        # 目标检测明细
        target_detail = _build_target_detail(window, event)
        # 参考合约原始窗口（只展示原始行情字段）
        peer_windows = _build_peer_raw_windows(event, day_frames, window_start, window_end)
        replay_payload[event_id] = {
            "target_detail": target_detail,
            "peer_windows": peer_windows,
            "daily_low": daily_bound[0] if daily_bound else None,
            "daily_high": daily_bound[1] if daily_bound else None,
        }


def _build_target_detail(window: pd.DataFrame, event: pd.Series) -> list[dict[str, object]]:
    """目标合约检测明细表：原始快照字段 + 检测字段。"""
    detail_cols = [
        "display_trade_date", "contract", "display_time", "UpdateMillisec",
        "LastPrice", "Volume", "Turnover", "BidPrice1", "BidVolume1",
        "AskPrice1", "AskVolume1", "AveragePrice", "OpenInterest",
        "UpperLimitPrice", "LowerLimitPrice",
        "delta_volume", "delta_turnover", "interval_vwap",
        "fair_price", "last_down_ticks", "last_up_ticks", "vwap_down_ticks", "vwap_up_ticks",
        "combined_vwap_down_ticks", "combined_vwap_up_ticks", "down_trigger_reasons", "up_trigger_reasons",
    ]
    # market_time_key 不展示但需保留以标记候选锚点
    available = [c for c in detail_cols if c in window.columns]
    if "market_time_key" in window.columns:
        available.append("market_time_key")
    anchor_key = int(event["event_anchor_key"])
    records = []
    for _, row in window[available].iterrows():
        rec = row.to_dict()
        rec["__is_candidate_anchor"] = int(row.get("market_time_key", -1)) == anchor_key
        records.append(rec)
    return records


def _build_peer_raw_windows(
    event: pd.Series,
    day_frames: dict[str, pd.DataFrame],
    window_start: int,
    window_end: int,
) -> dict[str, list[dict[str, object]]]:
    """每个参与 fair price 的参考合约的同窗口原始快照表。"""
    peer_contracts = event.get("__valid_peer_contracts", [])
    if not peer_contracts:
        peer_contracts = str(event.get("peer_contracts", "")).split(",")
        peer_contracts = [p for p in peer_contracts if p]
    result: dict[str, list[dict[str, object]]] = {}
    raw_cols = [
        "display_trade_date", "contract", "display_time", "UpdateMillisec",
        "LastPrice", "Volume", "Turnover", "BidPrice1", "BidVolume1",
        "AskPrice1", "AskVolume1", "AveragePrice", "OpenInterest",
        "UpperLimitPrice", "LowerLimitPrice",
        "delta_volume", "delta_turnover", "interval_vwap",
    ]
    for code in peer_contracts:
        frame = day_frames.get(code)
        if frame is None:
            result[code] = []
            continue
        window = frame.loc[
            (frame["market_time_key"] >= window_start)
            & (frame["market_time_key"] <= window_end)
        ].copy()
        if not window.empty:
            anchor_key = int(event["event_anchor_key"])
            reference_anchor_index = (
                window.assign(
                    __anchor_distance=(window["market_time_key"] - anchor_key).abs(),
                )
                .sort_values(["__anchor_distance", "market_time_key"], kind="stable")
                .index[0]
            )
            window["__is_reference_anchor"] = window.index == reference_anchor_index
        available = [c for c in raw_cols if c in window.columns]
        for internal_col in ("market_time_key", "__is_reference_anchor"):
            if internal_col in window.columns:
                available.append(internal_col)
        result[code] = window[available].to_dict("records") if available else []
    return result


def _group_contract_files_by_commodity(
    tick_day_path: str,
    target_commodities: list[str] | None,
) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for contract_file in iter_day_contract_files(tick_day_path):
        commodity_code = _commodity_from_file_name(contract_file.file_name)
        if commodity_code is None:
            continue
        if target_commodities and commodity_code not in target_commodities:
            continue
        grouped.setdefault(commodity_code, []).append(contract_file)
    return dict(sorted(grouped.items()))


def _commodity_from_file_name(file_name: str) -> str | None:
    stem = Path(file_name).stem.rsplit("_", 1)[0]
    if any(keyword in stem for keyword in ("主力连续", "当月连续", "下月连续", "当季连续", "下季连续", "隔季连续")):
        return None
    match = re.match(r"^([A-Za-z]+)", stem)
    return match.group(1).upper() if match else None


def _commodity_from_contract(contract: str | None) -> str | None:
    if not contract:
        return None
    match = re.match(r"^([A-Za-z]+)", contract)
    return match.group(1).upper() if match else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_detection(
        tick_day_path=args.tick_day_path,
        commodity=args.commodity,
        commodities=args.commodities,
        contract=args.contract,
        output_dir=args.output_dir,
        only_with_events=args.only_with_events,
        target_workers=args.target_workers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
