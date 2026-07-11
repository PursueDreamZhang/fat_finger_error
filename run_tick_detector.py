from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import re

import pandas as pd

from src.tick_detector.event_detection import attach_recovery_metrics, detect_candidate_ticks, merge_candidates
from src.tick_detector.reference_selection import attach_reference_metrics, infer_tick_size, select_reference_contracts
from src.tick_detector.report_html import render_event_replay_html
from src.tick_detector.tick_io import iter_day_contract_files, load_contract_snapshots, prepare_contract_snapshots


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tick 级乌龙指轻量检测器")
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
            _append_contract_events(
                target_code=target_code,
                day_frames=day_frames,
                all_events=all_events,
                replay_payload=replay_payload,
            )

    if all_events:
        events_df = pd.concat(all_events, ignore_index=True)
    else:
        events_df = pd.DataFrame(
            columns=[
                "trade_date",
                "commodity",
                "contract",
                "event_time",
                "event_start_time",
                "event_end_time",
                "event_low_price",
                "event_volume",
                "event_depth_ticks",
                "recovery_denominator_ticks",
                "reference_contract_count",
                "recovery_label",
            ]
        )

    csv_path = output_path / "tick_events.csv"
    html_path = output_path / "event_replay.html"
    events_df.to_csv(csv_path, index=False)
    html_path.write_text(render_event_replay_html(events_df, replay_payload), encoding="utf-8")
    return {"tick_events_csv": str(csv_path), "event_replay_html": str(html_path)}


def _append_contract_events(
    *,
    target_code: str,
    day_frames: dict[str, pd.DataFrame],
    all_events: list[pd.DataFrame],
    replay_payload: dict[str, dict[str, object]],
) -> None:
    target_df = day_frames[target_code]
    tick_size = infer_tick_size(target_df)
    if tick_size is None:
        return
    ref_codes = select_reference_contracts(day_frames, target_code)
    reference_dfs = [day_frames[code] for code in ref_codes]
    enriched = attach_reference_metrics(target_df, reference_dfs, tick_size=tick_size)
    candidates = detect_candidate_ticks(enriched)
    events = merge_candidates(candidates)
    if events.empty:
        return
    events = attach_recovery_metrics(events, target_df, tick_size=tick_size)
    candidate_keys = {
        (pd.Timestamp(row["timestamp"]), int(row["snapshot_seq"]))
        for _, row in candidates.iterrows()
    }
    all_events.append(events)
    for _, event in events.iterrows():
        event_id = f"{event['contract']}|{event['event_time']}"
        window = enriched.loc[
            (target_df["timestamp"] >= pd.Timestamp(event["event_time"]) - pd.Timedelta(seconds=30))
            & (target_df["timestamp"] <= pd.Timestamp(event["event_time"]) + pd.Timedelta(seconds=30))
        ].copy()
        event_window = candidates.loc[
            (candidates["timestamp"] >= pd.Timestamp(event["event_start_time"]))
            & (candidates["timestamp"] <= pd.Timestamp(event["event_end_time"]))
        ].sort_values(
            ["event_depth_ticks", "timestamp", "snapshot_seq"],
            ascending=[False, True, True],
            kind="stable",
        )
        anchor_snapshot_seq = int(event_window.iloc[0]["snapshot_seq"]) if not event_window.empty else None
        anchor_timestamp = pd.Timestamp(event["event_time"])
        window["__is_candidate"] = [
            (pd.Timestamp(row["timestamp"]), int(row["snapshot_seq"])) in candidate_keys
            for _, row in window.iterrows()
        ]
        window["__is_event_anchor"] = [
            pd.Timestamp(row["timestamp"]) == anchor_timestamp
            and anchor_snapshot_seq is not None
            and int(row["snapshot_seq"]) == anchor_snapshot_seq
            for _, row in window.iterrows()
        ]
        window["__event_id"] = event_id
        replay_payload[event_id] = {
            "rows": window.to_dict("records"),
            "reference_rows": _build_reference_rows(
                target_contract=target_code,
                target_df=target_df,
                anchor_time=pd.Timestamp(event["event_time"]),
                ref_codes=ref_codes,
                day_frames=day_frames,
                tick_size=tick_size,
            ),
        }


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


def _build_reference_rows(
    *,
    target_contract: str,
    target_df: pd.DataFrame,
    anchor_time: pd.Timestamp,
    ref_codes: list[str],
    day_frames: dict[str, pd.DataFrame],
    tick_size: float,
    lookback_seconds: int = 3,
    max_reference_age_seconds: int = 3,
) -> list[dict[str, object]]:
    lookback_time = anchor_time - pd.Timedelta(seconds=lookback_seconds)
    rows: list[dict[str, object]] = []
    target_current = _take_frame_asof(target_df, anchor_time)
    target_lookback = _take_frame_asof(target_df, lookback_time)
    if target_current is not None and target_lookback is not None:
        rows.append(
            {
                "reference_contract": f"{target_contract}（目标合约）",
                "current_time": target_current["timestamp"],
                "current_mid_price": float(target_current["mid_price"]),
                "lookback_time": target_lookback["timestamp"],
                "lookback_mid_price": float(target_lookback["mid_price"]),
                "move_ticks": (float(target_current["mid_price"]) - float(target_lookback["mid_price"])) / tick_size,
                "current_age_seconds": (anchor_time - pd.Timestamp(target_current["timestamp"])).total_seconds(),
                "lookback_age_seconds": (lookback_time - pd.Timestamp(target_lookback["timestamp"])).total_seconds(),
                "used_in_peer_median": True,
                "missing_reason": "用于和参考合约横向对比，不参与参考中位数",
            }
        )
    for code in ref_codes:
        frame = day_frames[code]
        current = _take_frame_asof(frame, anchor_time)
        lookback = _take_frame_asof(frame, lookback_time)
        if current is None or lookback is None:
            rows.append(
                {
                    "reference_contract": code,
                    "used_in_peer_median": False,
                    "missing_reason": "缺少当前或回看快照",
                }
            )
            continue
        current_mid = float(current["mid_price"])
        lookback_mid = float(lookback["mid_price"])
        current_age = (anchor_time - pd.Timestamp(current["timestamp"])).total_seconds()
        lookback_age = (lookback_time - pd.Timestamp(lookback["timestamp"])).total_seconds()
        used = (
            current_mid > 0
            and lookback_mid > 0
            and current_age <= max_reference_age_seconds
            and lookback_age <= max_reference_age_seconds
        )
        rows.append(
            {
                "reference_contract": code,
                "current_time": current["timestamp"],
                "current_mid_price": current_mid,
                "lookback_time": lookback["timestamp"],
                "lookback_mid_price": lookback_mid,
                "move_ticks": (current_mid - lookback_mid) / tick_size if current_mid > 0 and lookback_mid > 0 else None,
                "current_age_seconds": current_age,
                "lookback_age_seconds": lookback_age,
                "used_in_peer_median": used,
                "missing_reason": "" if used else "年龄超阈值或价格无效",
            }
        )
    return rows


def _take_frame_asof(frame: pd.DataFrame, query_ts: pd.Timestamp) -> pd.Series | None:
    timestamps = frame["timestamp"].to_numpy(dtype="datetime64[ns]")
    position = timestamps.searchsorted(query_ts.to_datetime64(), side="right") - 1
    if position < 0:
        return None
    return frame.iloc[int(position)]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_detection(
        tick_day_path=args.tick_day_path,
        commodity=args.commodity,
        contract=args.contract,
        output_dir=args.output_dir,
    )
    return 0


def _commodity_from_contract(contract: str | None) -> str | None:
    if not contract:
        return None
    match = re.match(r"^([A-Za-z]+)", contract)
    return match.group(1).upper() if match else None


if __name__ == "__main__":
    raise SystemExit(main())
