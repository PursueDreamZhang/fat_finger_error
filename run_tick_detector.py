from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re

import numpy as np
import pandas as pd

from src.tick_detector.event_detection import (
    attach_recovery_metrics,
    detect_candidate_ticks,
    merge_candidates,
)
from src.tick_detector.reference_selection import (
    attach_fair_price_metrics,
    select_reference_contracts,
)
from src.tick_detector.report_html import render_event_replay_html
from src.tick_detector.tick_io import (
    iter_day_contract_files,
    load_contract_snapshots,
    prepare_contract_snapshots,
)

DETECTOR_VERSION = "aggregated-tick-v1"

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
    ("event_anchor_key", "事件锚点开始序号"),
    ("event_anchor_key", "事件锚点结束序号"),
    ("event_end_key", "事件结束序号"),
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
    ("vwap_down_ticks", "区间均价向下偏离_跳"),
    ("vwap_down_bps", "区间均价向下偏离_基点"),
    ("combined_vwap_1s", "一秒合并成交均价"),
    ("combined_vwap_down_ticks", "一秒合并均价向下偏离_跳"),
    ("last_threshold_ticks", "末笔触发阈值_跳"),
    ("vwap_threshold_ticks", "区间均价触发阈值_跳"),
    ("onset_ticks", "突发偏离_跳"),
    ("interval_delta_volume", "区间增量成交量"),
    ("interval_delta_turnover", "区间增量成交额"),
    ("event_volume", "事件成交量"),
    ("notional_shortfall", "名义成交额缺口"),
    ("recovery_label", "回归标签"),
    ("visible_recovered_seconds", "可见末笔恢复确认秒数"),
    ("interval_recovered_seconds", "区间均价恢复确认秒数"),
    ("quote_recovered_seconds", "买一恢复确认秒数"),
    ("data_quality_flags", "数据质量标记"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聚合 Tick 乌龙指候选检测器")
    parser.add_argument("--tick-day-path", required=True, help="单日 tick 目录或 zip 路径")
    parser.add_argument("--commodity", required=False, help="可选，限制检测目标品种")
    parser.add_argument("--contract", required=False, help="可选，限制检测目标合约")
    parser.add_argument("--output-dir", required=False, help="输出目录")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.output_dir:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output_dir = str(Path("output") / f"{timestamp}-tick-detector")
    return args


def run_detection(*, tick_day_path: str, commodity: str | None, contract: str | None, output_dir: str) -> dict[str, str]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target_commodity = commodity.upper() if commodity else _commodity_from_contract(contract)
    all_events: list[pd.DataFrame] = []
    replay_payload: dict[str, dict[str, object]] = {}
    contract_diagnostics: list[dict[str, object]] = []
    contract_filter = contract.upper() if contract else None
    commodity_files = _group_contract_files_by_commodity(tick_day_path, target_commodity)
    commodity_items = list(commodity_files.items())

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
            prepared = prepare_contract_snapshots(raw)
            if prepared.empty:
                continue
            contract_code = str(prepared["contract"].iloc[0])
            day_frames[contract_code] = prepared

        commodity_targets = [
            code for code in sorted(day_frames) if contract_filter is None or code.upper() == contract_filter
        ]
        print(
            f"  - {commodity_code} 有效合约 {len(day_frames)} 个，待检测目标 {len(commodity_targets)} 个",
            flush=True,
        )
        for target_index, target_code in enumerate(commodity_targets, start=1):
            print(
                f"    * 检测 {commodity_code} 目标 {target_index}/{len(commodity_targets)}：{target_code}",
                flush=True,
            )
            events, diag = _detect_contract(target_code, day_frames)
            contract_diagnostics.append(diag)
            if events is not None and not events.empty:
                all_events.append(events)
                _build_replay_payload(events, target_code, day_frames, replay_payload)

    events_df = _build_events_df(all_events)
    events_df_chinese = _map_to_chinese_csv(events_df)
    csv_path = output_path / "tick_candidate_events.csv"
    html_path = output_path / "event_replay.html"
    events_df_chinese.to_csv(csv_path, index=False)
    html_path.write_text(render_event_replay_html(events_df, replay_payload, contract_diagnostics), encoding="utf-8")
    return {"tick_candidate_events_csv": str(csv_path), "event_replay_html": str(html_path)}


def _detect_contract(
    target_code: str,
    day_frames: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame | None, dict[str, object]]:
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
        return None, diag

    ref_codes = select_reference_contracts(day_frames, target_code)
    reference_frames = {code: day_frames[code] for code in ref_codes if code in day_frames}
    enriched = attach_fair_price_metrics(target_df, reference_frames, tick_size=tick_size)
    candidates = detect_candidate_ticks(enriched)
    events = merge_candidates(candidates, enriched)
    if events.empty:
        diag = _build_diagnostics(
            target_code, enriched, raw_rows, merged_rows,
            parameter_profile, validation_status,
            candidates_count=0,
        )
        return events, diag

    profile = {"tick_size": tick_size, "contract_multiplier": multiplier}
    events = attach_recovery_metrics(events, enriched, reference_frames, profile)
    events = _finalize_event_fields(events, enriched, tick_size, multiplier, parameter_profile, validation_status)
    diag = _build_diagnostics(
        target_code, enriched, raw_rows, merged_rows,
        parameter_profile, validation_status,
        candidates_count=len(events),
    )
    return events, diag


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
    return {
        "contract": contract,
        "parameter_profile": parameter_profile,
        "validation_status": validation_status,
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
    events["detector_version"] = DETECTOR_VERSION
    events["parameter_profile"] = parameter_profile
    events["validation_status"] = validation_status
    events["event_id"] = [
        f"{row['contract']}|{row['event_anchor_time']}"
        for _, row in events.iterrows()
    ]
    events["last_down_bps"] = events["last_down_ticks"] * tick_size / events["fair_price"] * 10000
    events["vwap_down_bps"] = events["vwap_down_ticks"] * tick_size / events["fair_price"] * 10000
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
        (events["fair_price"] > events["interval_vwap_anchor"]) & events["interval_delta_volume"].notna(),
        np.maximum(0, events["fair_price"] - events["interval_vwap_anchor"]) * events["interval_delta_volume"] * multiplier,
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
    out = pd.DataFrame(index=range(len(events_df)))
    seen_english: set[str] = set()
    for eng_key, cn_header in CSV_COLUMNS:
        if eng_key in events_df.columns and eng_key not in seen_english:
            out[cn_header] = events_df[eng_key].values
            seen_english.add(eng_key)
        else:
            out[cn_header] = pd.NA
    return out


def _build_replay_payload(
    events: pd.DataFrame,
    target_code: str,
    day_frames: dict[str, pd.DataFrame],
    replay_payload: dict[str, dict[str, object]],
) -> None:
    """为每个事件构建 [锚点前60秒, 锚点后10秒] 同窗口 replay payload。"""
    target_df = day_frames[target_code]
    for _, event in events.iterrows():
        event_id = str(event["event_id"])
        anchor_key = int(event["event_anchor_key"])
        window_start = anchor_key - 60 * 1000
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
        }


def _build_target_detail(window: pd.DataFrame, event: pd.Series) -> list[dict[str, object]]:
    """目标合约检测明细表：原始快照字段 + 检测字段。"""
    detail_cols = [
        "display_trade_date", "contract", "display_time", "UpdateMillisec",
        "LastPrice", "Volume", "Turnover", "BidPrice1", "BidVolume1",
        "AskPrice1", "AskVolume1", "AveragePrice", "OpenInterest",
        "UpperLimitPrice", "LowerLimitPrice",
        "delta_volume", "delta_turnover", "interval_vwap",
        "fair_price", "last_down_ticks", "vwap_down_ticks",
    ]
    available = [c for c in detail_cols if c in window.columns]
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
    ]
    for code in peer_contracts:
        frame = day_frames.get(code)
        if frame is None:
            result[code] = []
            continue
        window = frame.loc[
            (frame["market_time_key"] >= window_start)
            & (frame["market_time_key"] <= window_end)
        ]
        available = [c for c in raw_cols if c in window.columns]
        result[code] = window[available].to_dict("records") if available else []
    return result


def _group_contract_files_by_commodity(
    tick_day_path: str,
    target_commodity: str | None,
) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for contract_file in iter_day_contract_files(tick_day_path):
        commodity_code = _commodity_from_file_name(contract_file.file_name)
        if commodity_code is None:
            continue
        if target_commodity and commodity_code != target_commodity:
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
        contract=args.contract,
        output_dir=args.output_dir,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
