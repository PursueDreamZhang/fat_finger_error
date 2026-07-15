#!/usr/bin/env bash
set -euo pipefail

TICK_DAY_PATH="${1:?用法: $0 <tick_day_path> <output_root>}"
OUTPUT_ROOT="${2:?用法: $0 <tick_day_path> <output_root>}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${REPO_ROOT}/venv/bin/python"
PROGRESS_LOG="${OUTPUT_ROOT}/progress.log"
START_BATCH="${START_BATCH:-1}"

mkdir -p "$OUTPUT_ROOT"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$PROGRESS_LOG"
}

# 81 个本次实验已审核品种：8 批×10，最后 1 批×1。
BATCHES=(
  "A,AD,AG,AL,AO,AP,AU,B,BC,BR"
  "BU,BZ,C,CF,CJ,CS,CU,CY,EB,EC"
  "EG,FB,FG,FU,HC,I,IC,IF,IH,IM"
  "J,JD,JM,L,LC,LG,LH,LU,M,MA"
  "NI,NR,OI,OP,P,PB,PD,PF,PG,PK"
  "PL,PP,PR,PS,PT,PX,RB,RM,RR,RS"
  "RU,SA,SC,SF,SH,SI,SM,SN,SP,SR"
  "SS,T,TA,TF,TL,TS,UR,V,WR,Y"
  "ZN"
)

TOTAL=${#BATCHES[@]}
if (( START_BATCH < 1 || START_BATCH > TOTAL )); then
  log "START_BATCH 必须在 1-${TOTAL} 之间"
  exit 2
fi
log "开始批次 ${START_BATCH}-${TOTAL}/${TOTAL}，tick_day_path=${TICK_DAY_PATH}"
for i in "${!BATCHES[@]}"; do
  batch_num=$((i + 1))
  if (( batch_num < START_BATCH )); then
    continue
  fi
  commodities="${BATCHES[$i]}"
  batch_dir="${OUTPUT_ROOT}/batch-${batch_num}"
  log "批次 ${batch_num}/${TOTAL} 开始：${commodities}"
  if "$PYTHON" "${REPO_ROOT}/run_tick_detector.py" \
    --tick-day-path "$TICK_DAY_PATH" \
    --commodities "$commodities" \
    --output-dir "$batch_dir" 2>&1 | tee -a "$PROGRESS_LOG"; then
    log "批次 ${batch_num}/${TOTAL} 完成：${batch_dir}"
  else
    status=$?
    log "批次 ${batch_num}/${TOTAL} 失败，退出码=${status}"
    exit "$status"
  fi
done
log "全部 ${TOTAL} 批完成"
