from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path


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


def run_detection(*, tick_day_path: str, commodity: str | None, contract: str | None, output_dir: str) -> int:
    raise NotImplementedError("tick detector pipeline is not implemented yet")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_detection(
        tick_day_path=args.tick_day_path,
        commodity=args.commodity,
        contract=args.contract,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
