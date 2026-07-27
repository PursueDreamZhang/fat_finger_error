from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import resource
import statistics
import sys
import tempfile
from time import perf_counter
from typing import Any, Mapping

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.programmatic_grid import (  # noqa: E402
    load_programmatic_grid_config,
    run_programmatic_grid,
    write_programmatic_grid_outputs,
)


FRAME_KEYS = {
    "summary": ["instrument", "scenario_id"],
    "daily": ["instrument", "scenario_id", "trade_date"],
    "trades": ["instrument", "scenario_id", "trade_id"],
    "skipped_days": ["instrument", "trade_date", "reason"],
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="程序化网格回放性能基准")
    parser.add_argument("--config", required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("--core-only", action="store_true", help="仅阶段 0 基准：不构造成交详情，必须配合 --no-write")
    parser.add_argument("--context-mode", choices=["all", "none", "selected"], default="all")
    parser.add_argument("--fair-cache-dir")
    parser.add_argument("--workers", type=int, default=1)
    return parser


def _validate_runtime_options(
    *, context_mode: str, fair_cache_dir: str | None, workers: int, core_only: bool = False, no_write: bool = False
) -> None:
    if workers != 1:
        raise ValueError("workers>1 需在阶段 7 完成后使用；当前基线固定 workers=1")
    if core_only and not no_write:
        raise ValueError("--core-only 只用于无写出核心基准，必须与 --no-write 一起使用")


def _canonical_frame(frame: pd.DataFrame, keys: list[str]) -> dict[str, Any]:
    ordered = frame.copy()
    if not ordered.empty and all(key in ordered.columns for key in keys):
        ordered = ordered.sort_values(keys, kind="stable").reset_index(drop=True)
    return {
        "columns": list(ordered.columns),
        "records": json.loads(ordered.to_json(orient="records", force_ascii=False)),
    }


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _context_digest(contexts: Any) -> str:
    """逐笔哈希，避免基线时复制完整的大型详情 JSON。"""
    digest = hashlib.sha256()
    if not isinstance(contexts, Mapping):
        return digest.hexdigest()
    for key in sorted(contexts):
        digest.update(str(key).encode("utf-8"))
        digest.update(b"\0")
        digest.update(json.dumps(contexts[key], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def result_signature(result: Mapping[str, Any]) -> dict[str, Any]:
    """生成可跨行顺序比较的核心结果签名；上下文列表顺序保持原样。"""
    frames: dict[str, dict[str, Any]] = {}
    for name, keys in FRAME_KEYS.items():
        frame = result.get(name)
        if not isinstance(frame, pd.DataFrame):
            frame = pd.DataFrame()
        canonical = _canonical_frame(frame, keys)
        frames[name] = {
            "rows": len(canonical["records"]),
            "sha256": _digest(canonical),
        }
    contexts = result.get("trade_contexts", {})
    context_digest = _context_digest(contexts)
    payload = {"frames": frames, "trade_contexts_sha256": context_digest}
    return {
        **payload,
        "trade_context_count": len(contexts) if isinstance(contexts, Mapping) else 0,
        "overall_sha256": _digest(payload),
    }


def _peak_rss_bytes() -> int | None:
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, ValueError):
        return None
    return value if sys.platform == "darwin" else value * 1024


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _benchmark_result(args: argparse.Namespace, runs: list[dict[str, Any]]) -> dict[str, Any]:
    all_signatures = {item["signature"]["overall_sha256"] for item in runs}
    segment_names = sorted({name for item in runs for name in item["timings"]})
    return {
        "config": str(Path(args.config)),
        "date_range": [args.start_date, args.end_date],
        "repeat": len(runs),
        "no_write": bool(args.no_write),
        "core_only": bool(getattr(args, "core_only", False)),
        "context_mode": args.context_mode,
        "workers": args.workers,
        "signatures_consistent": len(all_signatures) == 1,
        "median_seconds": {
            "core": _median([item["total_seconds"] for item in runs]),
            "write": _median([item["write_seconds"] for item in runs]),
            **{name: _median([item["timings"].get(name, 0.0) for item in runs]) for name in segment_names},
        },
        "runs": runs,
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if args.repeat < 1:
        raise ValueError("repeat 必须大于 0")
    _validate_runtime_options(
        context_mode=args.context_mode,
        fair_cache_dir=args.fair_cache_dir,
        workers=args.workers,
        core_only=bool(getattr(args, "core_only", False)),
        no_write=bool(args.no_write),
    )
    base_config = load_programmatic_grid_config(args.config)
    base_config["context_mode"] = args.context_mode
    base_config["fair_cache_dir"] = args.fair_cache_dir or ""
    base_config["base"]["trade_date_start"] = args.start_date
    base_config["base"]["trade_date_end"] = args.end_date
    output_root = Path(args.output_dir)
    runs: list[dict[str, Any]] = []

    for index in range(1, args.repeat + 1):
        if args.no_write:
            output_context = tempfile.TemporaryDirectory(prefix="programmatic-grid-benchmark-")
            run_dir = Path(output_context.name)
        else:
            output_root.mkdir(parents=True, exist_ok=True)
            run_dir = Path(tempfile.mkdtemp(prefix=f"run_{index:02d}_", dir=output_root))
            output_context = None
        try:
            config = dict(base_config)
            config["base"] = dict(base_config["base"])
            config["output_dir"] = str(run_dir)
            timings: dict[str, float] = {}
            started = perf_counter()
            result = run_programmatic_grid(config, timings=timings, include_trade_contexts=not bool(getattr(args, "core_only", False)))
            total_seconds = perf_counter() - started
            write_seconds = 0.0
            if not args.no_write:
                started = perf_counter()
                write_programmatic_grid_outputs(result)
                write_seconds = perf_counter() - started
            runs.append(
                {
                    "run": index,
                    "total_seconds": total_seconds,
                    "write_seconds": write_seconds,
                    "timings": timings,
                    "peak_rss_bytes": _peak_rss_bytes(),
                    "signature": result_signature(result),
                    "output_dir": str(run_dir) if not args.no_write else None,
                }
            )
            del result
            gc.collect()
        finally:
            if output_context is not None:
                output_context.cleanup()

    return _benchmark_result(args, runs)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_benchmark(args)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
