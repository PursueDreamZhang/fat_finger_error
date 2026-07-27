from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.manual_simulation import (
    load_manual_simulation_config,
    normalize_manual_simulation_config,
    run_manual_simulation,
    write_manual_simulation_outputs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="事件条件的手工延迟回放器")
    parser.add_argument("--config", required=True, help="JSON 参数文件")
    parser.add_argument("--output-dir", help="覆盖 JSON 中的 output_dir")
    parser.add_argument("--commodities", help="覆盖 JSON 中的品种，例如 NI,FU")
    parser.add_argument("--max-events", type=int, help="仅跑排序后的前 N 条事件，用于冒烟验证")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if args.max_events is not None and args.max_events < 1:
        raise SystemExit("--max-events 必须大于等于 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_manual_simulation_config(args.config)
    if args.output_dir:
        config["output_dir"] = str(Path(args.output_dir))
    if args.commodities is not None:
        config["commodities"] = args.commodities
    if args.max_events is not None:
        config["max_events"] = args.max_events
    config = normalize_manual_simulation_config(config)

    result = run_manual_simulation(config)
    paths = write_manual_simulation_outputs(result)
    print(f"事件明细: {paths['trades']}")
    print(f"参数汇总: {paths['summary']}")
    print(f"网页报告: {paths['report']}")
    print(f"质量排除: {paths['excluded_events']}")
    print(f"实际参数: {paths['config']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
