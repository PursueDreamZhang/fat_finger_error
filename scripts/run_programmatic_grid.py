from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.programmatic_grid import load_programmatic_grid_config, run_programmatic_grid, write_programmatic_grid_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="运行程序化乌龙指参数网格回放")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--fair-cache-dir")
    args = parser.parse_args()
    config = load_programmatic_grid_config(args.config)
    if args.output_dir:
        config["output_dir"] = args.output_dir
    if args.start_date or args.end_date:
        if args.start_date:
            config["base"]["trade_date_start"] = args.start_date
        if args.end_date:
            config["base"]["trade_date_end"] = args.end_date
    if args.fair_cache_dir:
        config["fair_cache_dir"] = args.fair_cache_dir
    result = run_programmatic_grid(config)
    paths = write_programmatic_grid_outputs(result)
    print(f"summary={Path(paths['summary']).resolve()}")
    print(f"report={Path(paths['report']).resolve()}")
    print(result["summary"].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
