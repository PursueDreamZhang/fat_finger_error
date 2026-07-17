#!/usr/bin/env bash
# B 方案：按品种并行跑 run_tick_detector.py（品种间完全独立，多进程绕开 GIL）。
# 用法: scripts/run_day_parallel.sh <tick_day_path> [并行度]
# 输出: output/<day>-par/<品种>/... + 合并 CSV + batch_status.json + batch_summary.html
set -uo pipefail

DAY="${1:?用法: run_day_parallel.sh <tick_day_path> [parallelism]}"
PAR="${2:-8}"
OUT="output/$(basename "$DAY")-par"
mkdir -p "$OUT/logs"
export PYTHONPATH=.
STATUS_SCRIPT="scripts/generate_batch_summary.py"

# 该目录下 validated∩present 的品种（只跑能跑的，跳过未审核品种）
COMMODITIES=$(./venv/bin/python -c "
from pathlib import Path
import re
from src.tick_detector.tick_io import COMMODITY_PROFILES
validated = {k for k, v in COMMODITY_PROFILES.items() if v.get('validation_status') == 'validated'}
present = set()
for p in Path('$DAY').glob('*.csv'):
    m = re.match(r'^([A-Za-z]+)', p.stem.rsplit('_', 1)[0])
    if m:
        present.add(m.group(1).upper())
print(' '.join(sorted(validated & present)))
")
echo "[$(basename "$DAY")] runnable=$(echo $COMMODITIES | wc -w | tr -d ' ') parallel=$PAR out=$OUT"

export DAY OUT
START=$(date +%s)
STARTED_AT=$(date '+%Y-%m-%dT%H:%M:%S%z')
./venv/bin/python "$STATUS_SCRIPT" init --output-dir "$OUT" --tick-day-path "$DAY" --started-at "$STARTED_AT" $COMMODITIES

XC=0
if [ -n "$COMMODITIES" ]; then
  # 每个品种一个检测进程；worker 前后原子更新状态，xargs -P 控制并发。
  printf '%s\n' $COMMODITIES | xargs -P "$PAR" -I {} sh -c '
    commodity="$1"
    worker_start=$(date +%s)
    worker_started_at=$(date "+%Y-%m-%dT%H:%M:%S%z")
    ./venv/bin/python scripts/generate_batch_summary.py clean \
      --output-dir "$OUT" --commodity "$commodity"
    ./venv/bin/python scripts/generate_batch_summary.py running \
      --output-dir "$OUT" --commodity "$commodity" --started-at "$worker_started_at"
    ./venv/bin/python run_tick_detector.py --only-with-events \
      --tick-day-path "$DAY" --commodity "$commodity" --output-dir "$OUT/$commodity" \
      > "$OUT/logs/$commodity.log" 2>&1
    rc=$?
    worker_end=$(date +%s)
    worker_finished_at=$(date "+%Y-%m-%dT%H:%M:%S%z")
    ./venv/bin/python scripts/generate_batch_summary.py finished \
      --output-dir "$OUT" --commodity "$commodity" \
      --started-at "$worker_started_at" --finished-at "$worker_finished_at" \
      --elapsed-seconds "$((worker_end-worker_start))" --exit-code "$rc"
    echo "  done $commodity rc=$rc"
    exit "$rc"
  ' _ {} &
  XARGS_PID=$!
  while kill -0 "$XARGS_PID" 2>/dev/null; do
    ./venv/bin/python "$STATUS_SCRIPT" monitor --output-dir "$OUT"
    sleep 5
  done
  wait "$XARGS_PID"
  XC=$?
fi
END=$(date +%s)
echo "xargs_exit=$XC  WALL=$((END-START))s"

# 清掉无事件的空品种目录（--only-with-events 下不写 HTML/CSV，只剩空目录）
find "$OUT" -maxdepth 1 -mindepth 1 -type d -empty -delete 2>/dev/null || true

FINISHED_AT=$(date '+%Y-%m-%dT%H:%M:%S%z')
./venv/bin/python "$STATUS_SCRIPT" finalize \
  --output-dir "$OUT" --finished-at "$FINISHED_AT" --elapsed-seconds "$((END-START))"
echo "combined: $OUT/tick_candidate_events.csv"
echo "summary:  $OUT/batch_summary.html"
exit "$XC"
