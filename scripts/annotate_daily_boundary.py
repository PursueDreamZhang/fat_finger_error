from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.event_annotation import annotate_events


def main() -> None:
    parser = argparse.ArgumentParser(description="按范围 manifest 将日线边界审查结果绑定到检测事件 CSV")
    parser.add_argument("--events", required=True, help="原始 tick_candidate_events.csv")
    parser.add_argument("--boundary", required=True, help="最小日线边界清单 CSV")
    parser.add_argument("--manifest", required=True, help="范围 manifest JSON")
    parser.add_argument("--output", required=True, help="规范 tick_candidate_events_annotated.csv")
    args = parser.parse_args()
    result = annotate_events(args.events, args.boundary, args.manifest, args.output)
    print(f"output={Path(result['output_path']).resolve()}")
    print(f"event_count={result['event_count']}")


if __name__ == "__main__":
    main()
