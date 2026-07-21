#!/usr/bin/env bash
# 区间批量运行脚本：按天复用 run_day_parallel.sh，最后生成区间总汇总。
set -uo pipefail

START_DATE=""
END_DATE=""
COMMODITIES=""
TOTAL_PAR="10"
TARGET_WORKERS="2"
DAY_PARALLEL="1"
DATA_ROOT="data/tick2026"
OUTPUT_ROOT="output"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --start-date) START_DATE="${2:-}"; shift 2 ;;
    --end-date) END_DATE="${2:-}"; shift 2 ;;
    --commodities) COMMODITIES="${2:-}"; shift 2 ;;
    --total-parallel) TOTAL_PAR="${2:-}"; shift 2 ;;
    --target-workers) TARGET_WORKERS="${2:-}"; shift 2 ;;
    --day-parallel) DAY_PARALLEL="${2:-}"; shift 2 ;;
    --data-root) DATA_ROOT="${2:-}"; shift 2 ;;
    --output-root) OUTPUT_ROOT="${2:-}"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$START_DATE" ] || { echo "--start-date 必填" >&2; exit 2; }
[ -n "$END_DATE" ] || { echo "--end-date 必填" >&2; exit 2; }
[ -n "$COMMODITIES" ] || { echo "--commodities 必填" >&2; exit 2; }
[ "$DAY_PARALLEL" -ge 1 ] 2>/dev/null || { echo "--day-parallel 必须是正整数" >&2; exit 2; }
[ "$TOTAL_PAR" -ge 1 ] 2>/dev/null || { echo "--total-parallel 必须是正整数" >&2; exit 2; }
[ "$TARGET_WORKERS" -ge 1 ] 2>/dev/null || { echo "--target-workers 必须是正整数" >&2; exit 2; }

RANGE_OUT="$OUTPUT_ROOT/${START_DATE}_${END_DATE}-range"
MISSING_DAYS_FILE="$RANGE_OUT/missing_days.txt"
RUNNABLE_DAYS_FILE="$RANGE_OUT/runnable_days.txt"
mkdir -p "$RANGE_OUT"
: > "$MISSING_DAYS_FILE"
: > "$RUNNABLE_DAYS_FILE"

RANGE_RC=0
./venv/bin/python - "$START_DATE" "$END_DATE" "$DATA_ROOT" "$MISSING_DAYS_FILE" "$RUNNABLE_DAYS_FILE" <<'PY'
from datetime import datetime, timedelta
from pathlib import Path
import sys

start = datetime.strptime(sys.argv[1], "%Y%m%d")
end = datetime.strptime(sys.argv[2], "%Y%m%d")
data_root = Path(sys.argv[3])
missing_path = Path(sys.argv[4])
runnable_path = Path(sys.argv[5])
missing = []
runnable = []
current = start
while current <= end:
    day = current.strftime("%Y%m%d")
    year_month = day[:6]
    zip_path = data_root / year_month / f"{day}.zip"
    dir_path = data_root / year_month / day
    if zip_path.is_file():
        runnable.append(str(zip_path))
    elif dir_path.is_dir():
        runnable.append(str(dir_path))
    else:
        missing.append(day)
    current += timedelta(days=1)
missing_path.write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
runnable_path.write_text("\n".join(runnable) + ("\n" if runnable else ""), encoding="utf-8")
PY

if [ -s "$MISSING_DAYS_FILE" ]; then
  RANGE_RC=1
fi

PER_DAY_TOTAL_PAR=$((TOTAL_PAR / DAY_PARALLEL))
if [ "$PER_DAY_TOTAL_PAR" -lt 1 ]; then
  PER_DAY_TOTAL_PAR=1
fi

RUNNABLE_COUNT=$(wc -l < "$RUNNABLE_DAYS_FILE" | tr -d ' ')
MISSING_COUNT=$(wc -l < "$MISSING_DAYS_FILE" | tr -d ' ')
echo "range=$START_DATE..$END_DATE runnable_days=$RUNNABLE_COUNT missing_days=$MISSING_COUNT day_parallel=$DAY_PARALLEL per_day_total=$PER_DAY_TOTAL_PAR target_workers=$TARGET_WORKERS"

if [ -s "$RUNNABLE_DAYS_FILE" ]; then
  if ! xargs -P "$DAY_PARALLEL" -I {} sh -c '
    bash scripts/run_day_parallel.sh "$1" "$2" "$3" --commodities "$4" --output-root "$5"
  ' _ {} "$PER_DAY_TOTAL_PAR" "$TARGET_WORKERS" "$COMMODITIES" "$OUTPUT_ROOT" < "$RUNNABLE_DAYS_FILE"; then
    RANGE_RC=1
  fi
fi

./venv/bin/python scripts/generate_range_summary.py \
  --start-date "$START_DATE" \
  --end-date "$END_DATE" \
  --commodities "$COMMODITIES" \
  --output-dir "$RANGE_OUT" \
  --daily-output-root "$OUTPUT_ROOT" \
  --missing-days-file "$MISSING_DAYS_FILE" || RANGE_RC=1

echo "summary: $RANGE_OUT/range_summary.html"
exit "$RANGE_RC"
