#!/usr/bin/env python3
"""聚合区间内每日 B 模式产物，生成区间级候选事件汇总。"""
from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd


STATUS_LABELS = {
    "complete": "完成",
    "failed": "失败",
    "missing_day": "数据缺失",
    "missing_output": "缺少日汇总",
    "incomplete": "未完成",
}


def _parse_symbols(value: str) -> list[str]:
    symbols = [part.strip().upper() for part in value.split(",") if part.strip()]
    if not symbols:
        raise argparse.ArgumentTypeError("品种列表不能为空")
    return symbols


def _load_missing_days(path: str | None) -> list[str]:
    if not path:
        return []
    content = Path(path).read_text(encoding="utf-8")
    return [line.strip() for line in content.splitlines() if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成区间级 B 模式汇总 HTML")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--commodities", required=True, type=_parse_symbols)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--daily-output-root", default="output")
    parser.add_argument("--missing-days-file")
    return parser


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _filter_events(csv_path: Path, symbols: set[str]) -> tuple[pd.DataFrame, str | None]:
    if not csv_path.exists():
        return pd.DataFrame(), None
    try:
        events = pd.read_csv(csv_path)
    except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as exc:
        return pd.DataFrame(), str(exc)
    if "品种" in events.columns:
        events = events[events["品种"].astype(str).str.upper().isin(symbols)].copy()
    else:
        events = pd.DataFrame()
    return events, None


def generate_range_summary(
    *,
    start_date: str,
    end_date: str,
    commodities: list[str],
    output_dir: Path,
    daily_output_root: Path,
    missing_days: list[str],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = set(commodities)
    days = [f"{day}-par" for day in pd.date_range(start_date, end_date, freq="D").strftime("%Y%m%d")]
    missing_set = set(missing_days)
    day_rows: list[dict[str, Any]] = []
    commodity_rows: list[dict[str, Any]] = []
    event_parts: list[pd.DataFrame] = []

    for day_dir_name in days:
        day = day_dir_name[:-4]
        rel_summary = f"../{day_dir_name}/batch_summary.html"
        if day in missing_set:
            day_rows.append({
                "day": day,
                "status": "missing_day",
                "events": 0,
                "hits": 0,
                "failed": 0,
                "elapsed_seconds": None,
                "summary_path": rel_summary,
                "error": "数据目录或 zip 不存在",
            })
            continue
        batch_dir = daily_output_root / day_dir_name
        payload = _read_json(batch_dir / "batch_status.json")
        if not payload:
            day_rows.append({
                "day": day,
                "status": "missing_output",
                "events": 0,
                "hits": 0,
                "failed": 0,
                "elapsed_seconds": None,
                "summary_path": rel_summary,
                "error": "缺少 batch_status.json",
            })
            continue
        statuses = payload.get("commodities", [])
        filtered_statuses = [row for row in statuses if str(row.get("commodity", "")).upper() in symbols]
        filtered_counts = Counter(str(row.get("status")) for row in filtered_statuses)
        events, warning = _filter_events(batch_dir / "tick_candidate_events.csv", symbols)
        failure_error = next(
            (
                str(row.get("error"))
                for row in filtered_statuses
                if row.get("status") == "failed" and row.get("error")
            ),
            None,
        )
        if not events.empty:
            event_parts.append(events)
        status = "complete"
        if filtered_counts.get("failed", 0):
            status = "failed"
        elif any(row.get("status") not in {"success_with_events", "success_no_events"} for row in filtered_statuses):
            status = "incomplete"
        day_rows.append({
            "day": day,
            "status": status,
            "events": int(len(events)),
            "hits": int(filtered_counts.get("success_with_events", 0)),
            "failed": int(filtered_counts.get("failed", 0)),
            "elapsed_seconds": payload.get("elapsed_seconds"),
            "summary_path": rel_summary,
            "error": failure_error or warning,
        })

    combined = pd.concat(event_parts, ignore_index=True) if event_parts else pd.DataFrame()
    if not combined.empty:
        commodity_rows = _build_commodity_rows(combined)
    combined.to_csv(output_dir / "tick_candidate_events.csv", index=False)

    counts = {
        "total_days": len(days),
        "completed_days": sum(row["status"] == "complete" for row in day_rows),
        "failed_days": sum(row["status"] == "failed" for row in day_rows),
        "missing_days": sum(row["status"] == "missing_day" for row in day_rows),
        "missing_output_days": sum(row["status"] == "missing_output" for row in day_rows),
        "incomplete_days": sum(row["status"] == "incomplete" for row in day_rows),
        "event_count": int(len(combined)),
        "hit_commodities": len({str(row["品种"]) for _, row in combined.iterrows()}) if not combined.empty else 0,
    }
    payload = {
        "start_date": start_date,
        "end_date": end_date,
        "commodities": commodities,
        "counts": counts,
        "days": day_rows,
        "commodity_summary": commodity_rows,
    }
    (output_dir / "range_status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "range_summary.html").write_text(_render_html(payload, combined), encoding="utf-8")
    return payload


def _build_commodity_rows(events: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    grouped = events.groupby("品种", dropna=False)
    for commodity, group in grouped:
        reasons = Counter()
        labels = Counter()
        for value in group.get("触发原因", pd.Series(dtype=object)).dropna().astype(str):
            for part in value.split(","):
                part = part.strip()
                if part:
                    reasons[part] += 1
        for value in group.get("回归标签", pd.Series(dtype=object)).dropna().astype(str):
            labels[value] += 1
        rows.append({
            "commodity": commodity,
            "events": int(len(group)),
            "days": int(group["交易日"].astype(str).nunique()) if "交易日" in group else 0,
            "contracts": int(group["合约"].astype(str).nunique()) if "合约" in group else 0,
            "top_reason": reasons.most_common(1)[0][0] if reasons else "",
            "top_label": labels.most_common(1)[0][0] if labels else "",
        })
    return sorted(rows, key=lambda row: (-row["events"], str(row["commodity"])))


def _h(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return html.escape(str(value))


def _duration(value: Any) -> str:
    if value is None:
        return "—"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{seconds / 60:.1f} 分钟" if seconds >= 60 else f"{seconds:.1f} 秒"


def _link(path: str, label: str) -> str:
    return f"<a href='{html.escape(path, quote=True)}'>{_h(label)}</a>"


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{_h(header)}</th>" for header in headers)
    body = "".join(rows) or f"<tr><td colspan='{len(headers)}'>无数据</td></tr>"
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def _render_html(payload: dict[str, Any], events: pd.DataFrame) -> str:
    counts = payload["counts"]
    hit_days = [row for row in payload["days"] if row["status"] == "complete" and row["events"] > 0]
    cards = [
        ("开始日期", payload["start_date"]),
        ("结束日期", payload["end_date"]),
        ("品种列表", ",".join(payload["commodities"])),
        ("应跑天数", counts["total_days"]),
        ("成功天数", counts["completed_days"]),
        ("缺失天数", counts["missing_days"]),
        ("失败天数", counts["failed_days"]),
        ("总事件数", counts["event_count"]),
        ("命中品种数", counts["hit_commodities"]),
    ]
    day_rows = []
    for row in hit_days:
        day_rows.append(
            "<tr>" + "".join(
                f"<td>{cell}</td>" for cell in [
                    _h(row["day"]),
                    _h(row["events"]),
                    _h(row["hits"]),
                    _link(row["summary_path"], "查看日汇总"),
                    _h(_duration(row.get("elapsed_seconds"))),
                ]
            ) + "</tr>"
        )
    hit_day_count = len(hit_days)
    commodity_rows = []
    for row in payload["commodity_summary"]:
        commodity_rows.append(
            "<tr>" + "".join(
                f"<td>{cell}</td>" for cell in [
                    _h(row["commodity"]),
                    _h(row["events"]),
                    _h(row["days"]),
                    _h(row["contracts"]),
                    _h(row["top_reason"]),
                    _h(row["top_label"]),
                ]
            ) + "</tr>"
        )
    abnormal_rows = []
    for row in payload["days"]:
        if row["status"] in {"missing_day", "missing_output", "failed", "incomplete"}:
            abnormal_rows.append(
                "<tr>" + "".join(
                    f"<td>{cell}</td>" for cell in [
                        _h(row["day"]),
                        _h(STATUS_LABELS.get(row["status"], row["status"])),
                        _h(row.get("error")),
                        _link(row["summary_path"], "查看日汇总"),
                    ]
                ) + "</tr>"
            )
    return "".join([
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>区间疑似乌龙指检测汇总</title>",
        "<style>",
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:24px;color:#172033;background:#f6f8fb}",
        "h1,h2{color:#0f2445}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}",
        ".card{background:#fff;padding:13px;border-radius:9px;box-shadow:0 1px 4px #dfe5ee}.card span{display:block;color:#65728a;font-size:12px}",
        ".card strong{display:block;margin-top:5px;word-break:break-all}.table-wrap{overflow:auto;background:#fff;border-radius:9px;margin-bottom:24px}",
        "table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #e6eaf0;text-align:left;white-space:nowrap}",
        "th{background:#edf2f8;position:sticky;top:0}a{color:#1769aa}.note{padding:12px 14px;background:#fff7d6;border-radius:8px}",
        "</style></head><body>",
        "<h1>区间疑似乌龙指检测汇总</h1>",
        "<p class='note'>本页展示的是疑似乌龙指候选事件，不代表已确认的交易所错单。</p>",
        "<div class='cards'>",
        "".join(f"<div class='card'><span>{_h(label)}</span><strong>{_h(value)}</strong></div>" for label, value in cards),
        "</div>",
        "<h2>时间日期汇总</h2>",
        _table(["日期", "候选事件数", "命中品种数", "日汇总", "耗时"], day_rows),
        f"<p class='note'>共 {hit_day_count} 个日期出现疑似乌龙指候选事件。</p>",
        "<h2>品种汇总</h2>",
        _table(["品种", "累计事件数", "涉及交易日数", "涉及合约数", "主要触发原因", "主要回归标签"], commodity_rows),
        "<h2>失败与异常</h2>",
        _table(["日期", "状态", "备注", "日汇总"], abnormal_rows),
        "</body></html>",
    ])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = generate_range_summary(
        start_date=args.start_date,
        end_date=args.end_date,
        commodities=args.commodities,
        output_dir=Path(args.output_dir),
        daily_output_root=Path(args.daily_output_root),
        missing_days=_load_missing_days(args.missing_days_file),
    )
    print(f"days={payload['counts']['total_days']} events={payload['counts']['event_count']}")
    print(f"summary: {Path(args.output_dir) / 'range_summary.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
