#!/usr/bin/env python3
"""B 模式批次状态记录、事件合并与 HTML 汇总。"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd


STATUS_DIR_NAME = ".batch_status"
FINAL_STATUSES = {"success_with_events", "success_no_events", "failed"}
STATUS_LABELS = {
    "pending": "等待运行",
    "running": "运行中",
    "success_with_events": "检测到疑似候选事件",
    "success_no_events": "正常完成，无候选事件",
    "failed": "运行失败",
    "missing": "状态缺失",
}
EVENT_COLUMNS = [
    "品种", "合约", "事件时间", "触发原因", "合理价", "区间成交均价",
    "末笔向下偏离_跳", "区间均价向下偏离_跳", "回归标签",
]


def _status_dir(output_dir: Path) -> Path:
    return output_dir / STATUS_DIR_NAME


def _status_path(output_dir: Path, commodity: str) -> Path:
    return _status_dir(output_dir) / f"{commodity}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_events(path: Path) -> tuple[pd.DataFrame, str | None]:
    if not path.exists():
        return pd.DataFrame(), None
    try:
        return pd.read_csv(path), None
    except (OSError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as exc:
        return pd.DataFrame(), f"事件 CSV 读取失败：{exc}"


def init_batch(output_dir: Path, tick_day_path: str, commodities: list[str], started_at: str) -> None:
    status_dir = _status_dir(output_dir)
    status_dir.mkdir(parents=True, exist_ok=True)
    for old in status_dir.glob("*.json"):
        old.unlink()
    manifest = {
        "tick_day_path": tick_day_path,
        "trade_day": Path(tick_day_path).stem,
        "started_at": started_at,
        "commodities": commodities,
    }
    _atomic_write_json(status_dir / "manifest.json", manifest)
    for commodity in commodities:
        _atomic_write_json(_status_path(output_dir, commodity), _base_status(commodity, "pending"))


def _base_status(commodity: str, status: str) -> dict[str, Any]:
    return {
        "commodity": commodity,
        "status": status,
        "started_at": None,
        "finished_at": None,
        "elapsed_seconds": None,
        "event_count": 0,
        "contract_count": 0,
        "output_dir": commodity,
        "event_replay_html": f"{commodity}/event_replay_{commodity}.html",
        "events_csv": f"{commodity}/tick_candidate_events.csv",
        "log_file": f"logs/{commodity}.log",
        "exit_code": None,
        "error": None,
    }


def mark_running(output_dir: Path, commodity: str, started_at: str) -> None:
    status = _base_status(commodity, "running")
    status["started_at"] = started_at
    _atomic_write_json(_status_path(output_dir, commodity), status)


def clean_commodity_output(output_dir: Path, commodity: str) -> None:
    """清理该品种本次会生成的文件，避免重跑读取上次候选结果。"""
    commodity_dir = output_dir / commodity
    if not commodity_dir.exists():
        return
    for name in ("tick_candidate_events.csv", "event_replay.html", f"event_replay_{commodity}.html"):
        (commodity_dir / name).unlink(missing_ok=True)


def mark_finished(
    output_dir: Path,
    commodity: str,
    started_at: str,
    finished_at: str,
    elapsed_seconds: float,
    exit_code: int,
) -> None:
    status = _base_status(commodity, "failed")
    status.update({
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "exit_code": exit_code,
    })
    events, warning = _read_events(output_dir / commodity / "tick_candidate_events.csv")
    if exit_code == 0:
        status["event_count"] = len(events)
        status["contract_count"] = int(events["合约"].nunique()) if "合约" in events else 0
        status["status"] = "success_with_events" if len(events) else "success_no_events"
        status["error"] = warning
    else:
        log_path = output_dir / "logs" / f"{commodity}.log"
        status["error"] = _last_log_line(log_path) or f"检测进程退出码 {exit_code}"
    _atomic_write_json(_status_path(output_dir, commodity), status)


def _last_log_line(path: Path) -> str | None:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]
    except OSError:
        return None
    return lines[-1][:500] if lines else None


def load_batch(output_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _read_json(_status_dir(output_dir) / "manifest.json") or {}
    statuses = []
    for commodity in manifest.get("commodities", []):
        status = _read_json(_status_path(output_dir, commodity))
        statuses.append(status or _base_status(commodity, "missing"))
    return manifest, statuses


def progress_counts(statuses: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(statuses),
        "completed": sum(s.get("status") in FINAL_STATUSES for s in statuses),
        "running": sum(s.get("status") == "running" for s in statuses),
        "hit": sum(s.get("status") == "success_with_events" for s in statuses),
        "failed": sum(s.get("status") == "failed" for s in statuses),
    }


def print_progress(output_dir: Path) -> None:
    _, statuses = load_batch(output_dir)
    counts = progress_counts(statuses)
    print(
        "[monitor] completed={completed}/{total} running={running} hit={hit} failed={failed}".format(**counts),
        flush=True,
    )


def finalize_batch(output_dir: Path, finished_at: str, elapsed_seconds: float) -> dict[str, Any]:
    manifest, statuses = load_batch(output_dir)
    event_parts = []
    for status in statuses:
        events, warning = _read_events(output_dir / str(status["events_csv"]))
        if warning and not status.get("error"):
            status["error"] = warning
        if not events.empty:
            event_parts.append(events)
            _attach_event_stats(status, events)
    combined = pd.concat(event_parts, ignore_index=True) if event_parts else pd.DataFrame()
    combined.to_csv(output_dir / "tick_candidate_events.csv", index=False)
    counts = progress_counts(statuses)
    payload = {
        **manifest,
        "finished_at": finished_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "counts": {
            **counts,
            "unfinished": counts["total"] - counts["completed"],
            "event_count": len(combined),
        },
        "commodities": statuses,
    }
    _atomic_write_json(output_dir / "batch_status.json", payload)
    (output_dir / "batch_summary.html").write_text(
        render_summary_html(payload, combined), encoding="utf-8"
    )
    return payload


def _attach_event_stats(status: dict[str, Any], events: pd.DataFrame) -> None:
    status["event_count"] = len(events)
    status["contract_count"] = int(events["合约"].nunique()) if "合约" in events else 0
    status["first_event_time"] = _series_edge(events, "事件时间", "min")
    status["last_event_time"] = _series_edge(events, "事件时间", "max")
    status["trigger_reasons"] = _join_values(events, "触发原因")
    candidates = []
    for column in ("末笔向下偏离_跳", "区间均价向下偏离_跳"):
        if column in events:
            values = pd.to_numeric(events[column], errors="coerce")
            if values.notna().any():
                candidates.append(float(values.max()))
    status["max_down_ticks"] = max(candidates) if candidates else None


def _series_edge(frame: pd.DataFrame, column: str, method: str) -> str | None:
    if column not in frame or frame[column].dropna().empty:
        return None
    values = frame[column].dropna().astype(str)
    return str(getattr(values, method)())


def _join_values(frame: pd.DataFrame, column: str) -> str:
    if column not in frame:
        return ""
    parts: set[str] = set()
    for value in frame[column].dropna().astype(str):
        parts.update(part.strip() for part in value.split(",") if part.strip())
    return "、".join(sorted(parts))


def render_summary_html(payload: dict[str, Any], events: pd.DataFrame) -> str:
    statuses = list(payload.get("commodities", []))
    hits = sorted(
        (s for s in statuses if s.get("status") == "success_with_events"),
        key=lambda s: (-int(s.get("event_count", 0)), str(s.get("commodity", ""))),
    )
    failures = [s for s in statuses if s.get("status") in {"failed", "missing"} or s.get("error")]
    counts = payload.get("counts", {})
    return "".join([
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>B 模式全品种检测汇总</title>", _style(), "</head><body>",
        "<h1>B 模式全品种检测汇总</h1>",
        "<p class='note'>本页展示的是疑似乌龙指候选事件，不代表已确认的交易所错单。</p>",
        _overview(payload, counts),
        "<h2>命中品种总览</h2>",
        _commodity_table(hits, empty="本批次没有检测到疑似候选事件。", hit_table=True),
        "<h2>全部品种运行状态</h2>", _commodity_table(statuses),
        "<h2>事件明细</h2>", _event_table(events),
        "<h2>失败与异常</h2>", _failure_table(failures),
        "</body></html>",
    ])


def _overview(payload: dict[str, Any], counts: dict[str, Any]) -> str:
    cards = [
        ("交易日", payload.get("trade_day")),
        ("数据目录", payload.get("tick_day_path")),
        ("开始时间", payload.get("started_at")),
        ("结束时间", payload.get("finished_at")),
        ("总耗时", _duration(payload.get("elapsed_seconds"))),
        ("预计品种", counts.get("total", 0)),
        ("已完成", counts.get("completed", 0)),
        ("命中品种", counts.get("hit", 0)),
        ("失败", counts.get("failed", 0)),
        ("未完成", counts.get("unfinished", 0)),
        ("候选事件", counts.get("event_count", 0)),
    ]
    return "<div class='cards'>" + "".join(
        f"<div class='card'><span>{_h(label)}</span><strong>{_h(value)}</strong></div>" for label, value in cards
    ) + "</div>"


def _commodity_table(statuses: list[dict[str, Any]], empty: str = "无品种状态。", hit_table: bool = False) -> str:
    if not statuses:
        return f"<div class='empty'>{_h(empty)}</div>"
    headers = ["品种", "状态", "事件数", "命中合约数"]
    if hit_table:
        headers += ["最早事件", "最晚事件", "触发原因", "最大偏离_跳"]
    headers += ["耗时", "复盘", "CSV", "日志"]
    rows = []
    for status in statuses:
        cells = [
            _h(status.get("commodity")),
            _status_badge(str(status.get("status"))),
            _h(status.get("event_count", 0)),
            _h(status.get("contract_count", 0)),
        ]
        if hit_table:
            cells += [
                _h(status.get("first_event_time")), _h(status.get("last_event_time")),
                _h(status.get("trigger_reasons")), _h(status.get("max_down_ticks")),
            ]
        cells += [
            _h(_duration(status.get("elapsed_seconds"))),
            _link(status.get("event_replay_html"), "查看复盘", exists_for=status.get("status") == "success_with_events"),
            _link(status.get("events_csv"), "查看 CSV", exists_for=status.get("status") == "success_with_events"),
            _link(status.get("log_file"), "查看日志", exists_for=True),
        ]
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return _table(headers, rows)


def _event_table(events: pd.DataFrame) -> str:
    if events.empty:
        return "<div class='empty'>无候选事件明细。</div>"
    columns = [column for column in EVENT_COLUMNS if column in events.columns]
    rows = []
    for _, event in events.iterrows():
        cells = [_h(event.get(column)) for column in columns]
        commodity = str(event.get("品种", ""))
        cells.append(_link(f"{commodity}/event_replay_{commodity}.html", "查看复盘", exists_for=bool(commodity)))
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    return _table(columns + ["复盘"], rows)


def _failure_table(statuses: list[dict[str, Any]]) -> str:
    if not statuses:
        return "<div class='empty success'>没有运行失败或异常。</div>"
    rows = []
    for status in statuses:
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in [
            _h(status.get("commodity")), _status_badge(str(status.get("status"))),
            _h(status.get("exit_code")), _h(status.get("error")),
            _link(status.get("log_file"), "查看日志", exists_for=True), _h("可按品种重跑"),
        ]) + "</tr>")
    return _table(["品种", "状态", "退出码", "错误摘要", "日志", "建议"], rows)


def _table(headers: list[str], rows: list[str]) -> str:
    head = "".join(f"<th>{_h(header)}</th>" for header in headers)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def _status_badge(status: str) -> str:
    return f"<span class='status {html.escape(status, quote=True)}'>{_h(STATUS_LABELS.get(status, status))}</span>"


def _link(path: Any, label: str, *, exists_for: bool) -> str:
    if not path or not exists_for:
        return "—"
    return f"<a href='{html.escape(str(path), quote=True)}'>{_h(label)}</a>"


def _duration(value: Any) -> str:
    if value is None:
        return "—"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{seconds / 60:.1f} 分钟" if seconds >= 60 else f"{seconds:.1f} 秒"


def _h(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return html.escape(str(value))


def _style() -> str:
    return """<style>
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:24px;color:#172033;background:#f6f8fb}h1,h2{color:#0f2445}.note,.empty{padding:12px 14px;background:#fff7d6;border-radius:8px}.success{background:#eaf8ef}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:18px 0}.card{background:#fff;padding:13px;border-radius:9px;box-shadow:0 1px 4px #dfe5ee}.card span{display:block;color:#65728a;font-size:12px}.card strong{display:block;margin-top:5px;word-break:break-all}.table-wrap{overflow:auto;background:#fff;border-radius:9px;margin-bottom:24px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:9px 10px;border-bottom:1px solid #e6eaf0;text-align:left;white-space:nowrap}th{background:#edf2f8;position:sticky;top:0}.status{display:inline-block;padding:3px 8px;border-radius:12px;background:#e8edf4}.success_with_events{background:#ffe0df;color:#a11}.success_no_events{background:#dff4e6;color:#176b37}.failed,.missing{background:#2b3340;color:#fff}.running{background:#fff0c7}a{color:#1769aa}
</style>"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="B 模式批次状态与汇总")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--output-dir", required=True)
    init.add_argument("--tick-day-path", required=True)
    init.add_argument("--started-at", required=True)
    init.add_argument("commodities", nargs="*")
    running = sub.add_parser("running")
    running.add_argument("--output-dir", required=True)
    running.add_argument("--commodity", required=True)
    running.add_argument("--started-at", required=True)
    clean = sub.add_parser("clean")
    clean.add_argument("--output-dir", required=True)
    clean.add_argument("--commodity", required=True)
    finished = sub.add_parser("finished")
    finished.add_argument("--output-dir", required=True)
    finished.add_argument("--commodity", required=True)
    finished.add_argument("--started-at", required=True)
    finished.add_argument("--finished-at", required=True)
    finished.add_argument("--elapsed-seconds", type=float, required=True)
    finished.add_argument("--exit-code", type=int, required=True)
    monitor = sub.add_parser("monitor")
    monitor.add_argument("--output-dir", required=True)
    finalize = sub.add_parser("finalize")
    finalize.add_argument("--output-dir", required=True)
    finalize.add_argument("--finished-at", required=True)
    finalize.add_argument("--elapsed-seconds", type=float, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    if args.command == "init":
        init_batch(output_dir, args.tick_day_path, args.commodities, args.started_at)
    elif args.command == "running":
        mark_running(output_dir, args.commodity, args.started_at)
    elif args.command == "clean":
        clean_commodity_output(output_dir, args.commodity)
    elif args.command == "finished":
        mark_finished(output_dir, args.commodity, args.started_at, args.finished_at, args.elapsed_seconds, args.exit_code)
    elif args.command == "monitor":
        print_progress(output_dir)
    elif args.command == "finalize":
        payload = finalize_batch(output_dir, args.finished_at, args.elapsed_seconds)
        print(f"merged_events: {payload['counts']['event_count']}")
        print(f"commodities_with_events: {payload['counts']['hit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
