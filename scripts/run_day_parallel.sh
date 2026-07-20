#!/usr/bin/env bash
# B 方案：品种间并行 + 品种内目标合约并行，总进程数受统一预算约束。
# 用法: scripts/run_day_parallel.sh <tick_day_path> [总进程预算] [品种内进程数] [--commodities AU,AG]
# 输出: output/<day>-par/<品种>/... + 合并 CSV + batch_status.json + batch_summary.html
set -uo pipefail

if [ "$#" -lt 1 ]; then
  echo "用法: run_day_parallel.sh <tick_day_path> [total_parallelism] [target_workers] [--commodities AU,AG]" >&2
  exit 2
fi

DAY="$1"
shift
TOTAL_PAR="10"
TARGET_WORKERS="2"
if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then
  TOTAL_PAR="$1"
  shift
fi
if [ "$#" -gt 0 ] && [ "${1#--}" = "$1" ]; then
  TARGET_WORKERS="$1"
  shift
fi
COMMODITIES_FILTER=""
OUTPUT_ROOT="output"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --commodities)
      [ "$#" -ge 2 ] || { echo "--commodities 需要逗号分隔的品种列表" >&2; exit 2; }
      COMMODITIES_FILTER="$2"
      shift 2
      ;;
    --output-root)
      [ "$#" -ge 2 ] || { echo "--output-root 需要目录路径" >&2; exit 2; }
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    *)
      echo "未知参数: $1" >&2
      exit 2
      ;;
  esac
done

DAY_NAME="$(basename "$DAY")"
DAY_NAME="${DAY_NAME%.zip}"
DAY_NAME="${DAY_NAME%.ZIP}"
case "$TOTAL_PAR:$TARGET_WORKERS" in
  *[!0-9:]*|0:*|*:0) echo "总进程预算和品种内进程数必须是正整数" >&2; exit 2 ;;
esac
if [ "$TARGET_WORKERS" -gt "$TOTAL_PAR" ]; then
  TARGET_WORKERS="$TOTAL_PAR"
fi
OUTER_PAR=$((TOTAL_PAR / TARGET_WORKERS))
OUT="$OUTPUT_ROOT/${DAY_NAME}-par"
mkdir -p "$OUT/logs"
export PYTHONPATH=.
STATUS_SCRIPT="scripts/generate_batch_summary.py"

# 该目录下 validated∩present 的品种（只跑能跑的，跳过未审核品种）
COMMODITIES=$(./venv/bin/python -c "
from src.tick_detector.tick_io import COMMODITY_PROFILES
from run_tick_detector import _commodity_from_file_name as commodity_from_file_name
filter_text = '''$COMMODITIES_FILTER'''.strip()
requested = None
if filter_text:
    requested = {part.strip().upper() for part in filter_text.split(',') if part.strip()}
    if not requested:
        raise SystemExit('请求品种列表为空')
validated = {k for k, v in COMMODITY_PROFILES.items() if v.get('validation_status') == 'validated'}
present = set()
from src.tick_detector.tick_io import iter_day_contract_files
for contract_file in iter_day_contract_files('$DAY'):
    code = commodity_from_file_name(contract_file.file_name)
    if code:
        present.add(code)
selected = validated & present
if requested is not None:
    selected &= requested
print(' '.join(sorted(selected)))
")
RUNNABLE_COUNT=$(printf '%s\n' "$COMMODITIES" | wc -w | tr -d ' ')
echo "[$DAY_NAME] runnable=$RUNNABLE_COUNT total_parallel=$TOTAL_PAR outer_parallel=$OUTER_PAR target_workers=$TARGET_WORKERS out=$OUT commodities=${COMMODITIES_FILTER:-ALL}"

export DAY OUT TARGET_WORKERS
START=$(date +%s)
STARTED_AT=$(date '+%Y-%m-%dT%H:%M:%S%z')
./venv/bin/python "$STATUS_SCRIPT" init --output-dir "$OUT" --tick-day-path "$DAY" --started-at "$STARTED_AT" $COMMODITIES

XC=0
if [ -n "$COMMODITIES" ]; then
  # 每个品种一个检测进程；worker 前后原子更新状态，xargs -P 控制并发。
  printf '%s\n' $COMMODITIES | xargs -P "$OUTER_PAR" -I {} sh -c '
    commodity="$1"
    worker_start=$(date +%s)
    worker_started_at=$(date "+%Y-%m-%dT%H:%M:%S%z")
    ./venv/bin/python scripts/generate_batch_summary.py clean \
      --output-dir "$OUT" --commodity "$commodity"
    ./venv/bin/python scripts/generate_batch_summary.py running \
      --output-dir "$OUT" --commodity "$commodity" --started-at "$worker_started_at"
    ./venv/bin/python run_tick_detector.py --only-with-events \
      --target-workers "$TARGET_WORKERS" \
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
