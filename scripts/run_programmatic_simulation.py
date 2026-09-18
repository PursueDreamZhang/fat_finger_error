from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.programmatic_simulation import (
    load_programmatic_simulation_config,
    load_mid_distance_replay_config,
    normalize_programmatic_simulation_config,
    run_programmatic_simulation,
    run_mid_distance_replay,
    write_programmatic_simulation_outputs,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="乌龙指程序化双向网格全天状态机回放")
    parser.add_argument("--config", help="旧版 JSON 参数文件；与 --parameters 同用时表示执行假设覆盖")
    parser.add_argument("--parameters", help="自动选参 auto_parameters.csv；启用 Mid 距离带模式")
    parser.add_argument("--input", help="Mid 距离带模式的 Tick CSV、目录或 ZIP")
    parser.add_argument("--hold-seconds", type=float, help="Mid 距离带模式的固定持有秒数")
    parser.add_argument("--contracts", help="Mid 距离带模式的合约子集，逗号分隔")
    parser.add_argument("--output-dir", help="覆盖 JSON 中的 output_dir")
    parser.add_argument("--start-date", help="覆盖 JSON 中的 trade_date_start（YYYYMMDD）")
    parser.add_argument("--end-date", help="覆盖 JSON 中的 trade_date_end（YYYYMMDD）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.parameters:
        if not args.input or args.hold_seconds is None or not args.output_dir:
            raise SystemExit("--parameters 模式必须同时提供 --input、--hold-seconds、--output-dir")
        contracts = [value.strip() for value in args.contracts.split(",")] if args.contracts else None
        execution_config = load_mid_distance_replay_config(args.config) if args.config else None
        result = run_mid_distance_replay(
            args.parameters,
            args.input,
            start_date=args.start_date or "",
            end_date=args.end_date or "",
            output_dir=args.output_dir,
            hold_seconds=args.hold_seconds,
            contracts=contracts,
            config=execution_config,
        )
        paths = write_mid_distance_replay_outputs(result)
        print(f"交易明细: {paths['trades']}")
        print(f"汇总: {paths['summary']}")
        print(f"网页报告: {paths['report']}")
        return 0

    if not args.config:
        raise SystemExit("必须提供 --config 或 --parameters")
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
