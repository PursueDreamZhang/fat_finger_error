#!/usr/bin/env python3
"""Run the standalone Mid + LastPrice historical anomaly analysis."""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path

from src.mid_analyzer import (
    Config,
    DEFAULT_HORIZONS,
    DEFAULT_THRESHOLDS,
    analyze_sources,
    discover_sources,
    write_outputs,
)


def _list_arg(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _date_arg(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) != 8 or not value.isdigit():
        raise argparse.ArgumentTypeError("日期必须是 YYYYMMDD")
    return value


def _positive_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("数值不能小于 0")
    return result


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是整数") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return result


def _thresholds(value: str) -> tuple[float, ...]:
    values = tuple(sorted({float(item.strip()) / 100 for item in value.split(",") if item.strip()}))
    if not values or any(not math.isfinite(item) or item <= 0 for item in values):
        raise argparse.ArgumentTypeError("至少提供一个正数阈值（百分比）")
    return values


def _horizons(value: str) -> tuple[int, ...]:
    try:
        values = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("观察窗口必须是整数秒") from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("至少提供一个正数观察窗口")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mid + LastPrice 历史异常检测")
    parser.add_argument("--input", default="data/tick2026", help="CSV、ZIP、单日目录或数据根目录")
    parser.add_argument("--symbols", default="", help="品种筛选，例如 SA,FG,PX")
    parser.add_argument("--contracts", default="", help="合约筛选，例如 SA605,PX604")
    parser.add_argument("--start-date", type=_date_arg, help="起始交易日 YYYYMMDD")
    parser.add_argument("--end-date", type=_date_arg, help="结束交易日 YYYYMMDD")
    parser.add_argument("--output-dir", help="输出目录；省略时写入 output/时间戳-mid")
    parser.add_argument("--thresholds", default=",".join(str(value * 100) for value in DEFAULT_THRESHOLDS), type=_thresholds, help="阈值百分比，逗号分隔")
    parser.add_argument("--horizons", default=",".join(str(value) for value in DEFAULT_HORIZONS), type=_horizons, help="恢复观察窗口秒数")
    parser.add_argument("--event-merge-seconds", type=_positive_float, default=5.0, help="候选合并窗口，默认 5 秒")
    parser.add_argument("--session-gap-seconds", type=_positive_float, default=300.0, help="时段断点阈值，默认 300 秒")
    parser.add_argument("--sample-delay-tolerance", type=_positive_float, default=1.0, help="恢复取样允许晚到秒数，默认 1 秒")
    parser.add_argument("--mfe-mae-horizon", type=_positive_int, default=5, help="MFE/MAE 观察窗口，默认 5 秒")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date 不能晚于 --end-date")
    discovery = discover_sources(args.input)
    cfg = Config(
        thresholds=args.thresholds,
        horizons=args.horizons,
        event_merge_seconds=args.event_merge_seconds,
        session_gap_seconds=args.session_gap_seconds,
        sample_delay_tolerance=args.sample_delay_tolerance,
        mfe_mae_horizon=args.mfe_mae_horizon,
    )
    result = analyze_sources(
        discovery,
        cfg,
        symbols=_list_arg(args.symbols),
        contracts=_list_arg(args.contracts),
        start_date=args.start_date,
        end_date=args.end_date,
    )

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        label = "-".join(_list_arg(args.symbols) or ["all"])
        output_dir = Path("output") / f"{stamp}-mid-{label}"
    run_args = vars(args).copy()
    run_args["thresholds"] = [value * 100 for value in args.thresholds]
    run_args["horizons"] = list(args.horizons)
    write_outputs(result, output_dir, cfg, run_args)
    print(f"完成：{output_dir / 'analysis_report.html'}")
    print(f"事件数：{len(result.events)}；读取来源：{result.manifest['selected_source_count']}；状态：{result.manifest['status']}")
    return 1 if result.manifest["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
