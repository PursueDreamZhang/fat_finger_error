#!/usr/bin/env bash
# 区间批量运行脚本：按天复用 run_day_parallel.sh，最后生成区间总汇总。
set -uo pipefail

START_DATE=""
END_DATE=""
COMMODITIES=""
TOTAL_PAR="10"
TARGET_WORKERS="2"
DATA_ROOT="data/tick2026"
OUTPUT_ROOT="output"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --start-date) START_DATE="${2:-}"; shift 2 ;;
    --end-date) END_DATE="${2:-}"; shift 2 ;;
    --commodities) COMMODITIES="${2:-}"; shift 2 ;;
    --total-parallel) TOTAL_PAR="${2:-}"; shift 2 ;;
    --target-workers) TARGET_WORKERS="${2:-}"; shift 2 ;;
    --data-root) DATA_ROOT="${2:-}"; shift 2 ;;
    --output-root) OUTPUT_ROOT="${2:-}"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$START_DATE" ] || { echo "--start-date 必填" >&2; exit 2; }
[ -n "$END_DATE" ] || { echo "--end-date 必填" >&2; exit 2; }
[ -n "$COMMODITIES" ] || { echo "--commodities 必填" >&2; exit 2; }

RANGE_OUT="$OUTPUT_ROOT/${START_DATE}_${END_DATE}-range"
MISSING_DAYS_FILE="$RANGE_OUT/missing_days.txt"
mkdir -p "$RANGE_OUT"
: > "$MISSING_DAYS_FILE"

RANGE_RC=0
CURRENT="$START_DATE"
while [ "$CURRENT" -le "$END_DATE" ]; do
  YEAR_MONTH="${CURRENT%??}"
  ZIP_PATH="$DATA_ROOT/$YEAR_MONTH/$CURRENT.zip"
  DIR_PATH="$DATA_ROOT/$YEAR_MONTH/$CURRENT"
  DAY_PATH=""
  if [ -f "$ZIP_PATH" ]; then
    DAY_PATH="$ZIP_PATH"
  elif [ -d "$DIR_PATH" ]; then
    DAY_PATH="$DIR_PATH"
  else
    echo "$CURRENT" >> "$MISSING_DAYS_FILE"
    RANGE_RC=1
  fi
  if [ -n "$DAY_PATH" ]; then
    bash scripts/run_day_parallel.sh "$DAY_PATH" "$TOTAL_PAR" "$TARGET_WORKERS" \
      --commodities "$COMMODITIES" --output-root "$OUTPUT_ROOT" || RANGE_RC=1
  fi
  CURRENT=$(python3 - <<'PY' "$CURRENT"
from datetime import datetime, timedelta
import sys
value = datetime.strptime(sys.argv[1], "%Y%m%d") + timedelta(days=1)
print(value.strftime("%Y%m%d"))
PY
)
done

./venv/bin/python scripts/generate_range_summary.py \
  --start-date "$START_DATE" \
  --end-date "$END_DATE" \
  --commodities "$COMMODITIES" \
  --output-dir "$RANGE_OUT" \
  --daily-output-root "$OUTPUT_ROOT" \
  --missing-days-file "$MISSING_DAYS_FILE" || RANGE_RC=1

echo "summary: $RANGE_OUT/range_summary.html"
exit "$RANGE_RC"
