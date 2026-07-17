from __future__ import annotations

import argparse
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import run_tick_detector


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统计 Tick 检测器各阶段耗时")
    parser.add_argument("tick_day_path")
    parser.add_argument("--commodities")
    parser.add_argument("--commodity")
    parser.add_argument("--contract")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def _wrap(name: str, function: Callable[..., Any], totals: dict[str, float]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            totals[name] += elapsed
            print(f"[timing] {name}: {elapsed:.3f}s", flush=True)

    return wrapped


def main() -> None:
    args = _parse_args()
    if args.commodity and args.commodities:
        raise SystemExit("--commodity 与 --commodities 不能同时使用")

    totals: dict[str, float] = defaultdict(float)
    timed_functions = (
        "load_contract_snapshots",
        "prepare_contract_snapshots",
        "attach_fair_price_metrics",
        "detect_candidate_ticks",
        "merge_candidates",
        "attach_recovery_metrics",
        "render_event_replay_html",
    )
    for name in timed_functions:
        setattr(run_tick_detector, name, _wrap(name, getattr(run_tick_detector, name), totals))

    started = time.perf_counter()
    run_tick_detector.run_detection(
        tick_day_path=args.tick_day_path,
        output_dir=args.output_dir,
        commodity=args.commodity,
        commodities=args.commodities,
        contract=args.contract,
    )
    totals["run_detection"] = time.perf_counter() - started
    print("[timing] totals", flush=True)
    for name, elapsed in totals.items():
        print(f"[timing] {name}: {elapsed:.3f}s", flush=True)
    print(f"[timing] output_dir: {Path(args.output_dir).resolve()}", flush=True)


if __name__ == "__main__":
    main()
