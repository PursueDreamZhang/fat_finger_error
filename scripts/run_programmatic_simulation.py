from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.programmatic_simulation import (
    load_programmatic_simulation_config,
    normalize_programmatic_simulation_config,
    run_programmatic_simulation,
    write_programmatic_simulation_outputs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="乌龙指程序化双向网格全天状态机回放")
    parser.add_argument("--config", required=True, help="JSON 参数文件")
    parser.add_argument("--output-dir", help="覆盖 JSON 中的 output_dir")
    parser.add_argument("--start-date", help="覆盖 JSON 中的 trade_date_start（YYYYMMDD）")
    parser.add_argument("--end-date", help="覆盖 JSON 中的 trade_date_end（YYYYMMDD）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_programmatic_simulation_config(args.config)
    if args.output_dir:
        config["output_dir"] = str(Path(args.output_dir))
    if args.start_date:
        config["trade_date_start"] = args.start_date
    if args.end_date:
        config["trade_date_end"] = args.end_date
    config = normalize_programmatic_simulation_config(config)
    result = run_programmatic_simulation(config)
    paths = write_programmatic_simulation_outputs(result)
    print(f"状态转换: {paths['transitions']}")
    print(f"订单生命周期: {paths['orders']}")
    print(f"交易明细: {paths['trades']}")
    print(f"汇总: {paths['summary']}")
    print(f"网页报告: {paths['report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
