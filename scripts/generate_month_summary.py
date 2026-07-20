#!/usr/bin/env python3
"""合并每日 B 模式产物，生成单月候选事件汇总。"""
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

import pandas as pd


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 B 模式月度汇总 HTML")
    parser.add_argument("--data-root", required=True, help="月度 tick 数据目录，如 data/tick2026/202605")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root)
    days = sorted({
        *(path.name for path in data_root.iterdir() if path.is_dir()),
        *(path.stem for path in data_root.glob('*.zip')),
        *(path.stem for path in data_root.glob('*.ZIP')),
    })
    daily_rows, event_parts = [], []
    for day in days:
        batch_dir = Path(args.output_root) / f"{day}-par"
        status_path = batch_dir / "batch_status.json"
        if not status_path.exists():
            daily_rows.append({"day": day, "status": "missing", "events": 0, "hits": 0, "failed": 0})
            continue
        status = json.loads(status_path.read_text(encoding="utf-8"))
        counts = status.get("counts", {})
        daily_rows.append({
            "day": day,
            "status": "complete" if counts.get("unfinished", 0) == 0 else "incomplete",
            "events": counts.get("event_count", 0),
            "hits": counts.get("hit", 0),
            "failed": counts.get("failed", 0),
            "elapsed": status.get("elapsed_seconds"),
        })
        csv_path = batch_dir / "tick_candidate_events.csv"
        if csv_path.exists():
            try:
                events = pd.read_csv(csv_path)
            except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError):
                continue
            if not events.empty:
                events.insert(0, "批次日期", day)
                event_parts.append(events)

    events = pd.concat(event_parts, ignore_index=True) if event_parts else pd.DataFrame()
    events.to_csv(output_dir / "tick_candidate_events.csv", index=False)
    (output_dir / "month_summary.html").write_text(_render(daily_rows, events), encoding="utf-8")
    print(f"days={len(days)} events={len(events)}")
    print(f"summary: {output_dir / 'month_summary.html'}")
    return 0


def _render(days: list[dict[str, object]], events: pd.DataFrame) -> str:
    def esc(value: object) -> str:
        return html.escape("" if value is None else str(value))

    day_rows = "".join(
        "<tr>"
        f"<td>{esc(row['day'])}</td><td>{esc(row['status'])}</td>"
        f"<td>{esc(row['events'])}</td><td>{esc(row['hits'])}</td><td>{esc(row['failed'])}</td>"
        f"<td>{esc(row.get('elapsed'))}</td>"
        f"<td><a href='../{esc(row['day'])}-par/batch_summary.html'>查看日汇总</a></td>"
        "</tr>"
        for row in days
    )
    columns = [column for column in ("批次日期", "品种", "合约", "事件时间", "触发原因", "合理价", "区间成交均价", "回归标签") if column in events]
    event_rows = "".join(
        "<tr>" + "".join(f"<td>{esc(row[column])}</td>" for column in columns) + "</tr>"
        for _, row in events.iterrows()
    ) or "<tr><td colspan='8'>本月无候选事件。</td></tr>"
    return f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<title>2026 年 5 月疑似乌龙指汇总</title>
<style>body{{font-family:system-ui,sans-serif;margin:24px;color:#172033}}table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border:1px solid #d9e0ea;padding:7px;text-align:left}}th{{background:#f3f6fa}}.cards{{display:flex;gap:12px}}.card{{background:#f3f6fa;padding:12px;border-radius:6px}}</style>
<h1>2026 年 5 月疑似乌龙指检测汇总</h1>
<p>候选事件仅供人工复盘，不代表已确认错单。</p>
<div class='cards'><div class='card'>交易日：{len(days)}</div><div class='card'>候选事件：{len(events)}</div></div>
<h2>每日运行情况</h2><table><tr><th>日期</th><th>状态</th><th>事件</th><th>命中品种</th><th>失败</th><th>耗时（秒）</th><th>日汇总</th></tr>{day_rows}</table>
<h2>候选事件明细</h2><table><tr>{''.join(f'<th>{esc(column)}</th>' for column in columns)}</tr>{event_rows}</table>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
