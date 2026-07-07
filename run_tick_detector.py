from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

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

    day_frames: dict[str, pd.DataFrame] = {}
    for contract_file in iter_day_contract_files(tick_day_path):
        raw = load_contract_snapshots(contract_file)
        if raw.empty or raw["parse_status"].iloc[0] != "ok":
            continue
        prepared = prepare_contract_snapshots(raw)
        if prepared.empty:
            continue
        contract_code = str(prepared["contract"].iloc[0])
        day_frames[contract_code] = prepared

    target_contracts = [
        code
        for code, frame in day_frames.items()
        if (commodity is None or str(frame["commodity"].iloc[0]).upper() == commodity.upper())
        and (contract is None or code.upper() == contract.upper())
    ]
    all_events: list[pd.DataFrame] = []
    replay_payload: dict[str, list[dict[str, object]]] = {}

    for target_code in target_contracts:
        target_df = day_frames[target_code]
        tick_size = infer_tick_size(target_df)
        if tick_size is None:
            continue
        ref_codes = select_reference_contracts(day_frames, target_code)
        reference_dfs = [day_frames[code] for code in ref_codes]
        enriched = attach_reference_metrics(target_df, reference_dfs, tick_size=tick_size)
        candidates = detect_candidate_ticks(enriched)
        events = merge_candidates(candidates)
        if events.empty:
            continue
        events = attach_recovery_metrics(events, target_df, tick_size=tick_size)
        all_events.append(events)
        for _, event in events.iterrows():
            event_id = f"{event['contract']}|{event['event_time']}"
            window = target_df.loc[
                (target_df["timestamp"] >= pd.Timestamp(event["event_time"]) - pd.Timedelta(seconds=30))
                & (target_df["timestamp"] <= pd.Timestamp(event["event_time"]) + pd.Timedelta(seconds=30))
            ]
            replay_payload[event_id] = window.to_dict("records")

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
