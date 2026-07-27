from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.programmatic_parameter_generator import (
    build_parameter_shapes,
    load_events,
    load_parameter_generator_config,
    write_parameter_shapes,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="从检测事件生成每个合约的低侧 T/W/D/S 组合")
    parser.add_argument("--input", required=True, help="tick_candidate_events_annotated.csv")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config", help="可选 JSON；覆盖筛选门槛或 T/W/S 组合公式")
    args = parser.parse_args()
    shapes = build_parameter_shapes(load_events(args.input), load_parameter_generator_config(args.config))
    print(f"shapes={write_parameter_shapes(shapes, args.output_dir).resolve()}")


if __name__ == "__main__":
    main()
