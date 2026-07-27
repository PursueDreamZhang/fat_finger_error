from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.legacy_depth_migration import DEFAULT_OUTPUT_DIR, migrate_legacy_depth


def main() -> None:
    parser = argparse.ArgumentParser(description="将历史注释事件 CSV 迁移为确认深度口径")
    parser.add_argument("--input", required=True, help="规范 tick_candidate_events_annotated.csv")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    args = parser.parse_args()
    result = migrate_legacy_depth(args.input, args.output_dir)
    print(f"output={Path(result['output_path']).resolve()}")
    print(f"derived_event_count={result['derived_event_count']}")
    print(f"unverifiable_event_count={result['unverifiable_event_count']}")


if __name__ == "__main__":
    main()
