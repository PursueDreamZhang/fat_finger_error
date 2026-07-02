#!/bin/sh

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

if [ "$#" -ne 2 ]; then
  echo "用法: sh docs/new/run_daily_screen.sh AU 20250101-20260601" >&2
  exit 1
fi

SYMBOL=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')
DATE_RANGE=$2

START_DATE=${DATE_RANGE%-*}
END_DATE=${DATE_RANGE#*-}

if [ -z "$SYMBOL" ] || [ -z "$START_DATE" ] || [ -z "$END_DATE" ] || [ "$START_DATE" = "$DATE_RANGE" ] || [ "$END_DATE" = "$DATE_RANGE" ]; then
  echo "日期参数格式错误，应为 20250101-20260601" >&2
  exit 1
fi

case $START_DATE in
  ???????? ) ;;
  * )
    echo "开始日期格式错误，应为 8 位 YYYYMMDD" >&2
    exit 1
    ;;
esac

case $END_DATE in
  ???????? ) ;;
  * )
    echo "结束日期格式错误，应为 8 位 YYYYMMDD" >&2
    exit 1
    ;;
esac

"$REPO_DIR/venv/bin/python" "$REPO_DIR/run_daily_screen.py" \
  --symbols "$SYMBOL" \
  --start-date "$START_DATE" \
  --end-date "$END_DATE"
