from __future__ import annotations

import html

import pandas as pd


EVENT_COLUMNS = [
    ("trade_date", "交易日"),
    ("commodity", "品种"),
    ("contract", "合约"),
    ("event_time", "乌龙指锚点时间"),
    ("event_start_time", "事件开始"),
    ("event_end_time", "事件结束"),
    ("event_low_price", "事件最低成交价"),
    ("event_volume", "事件成交量增量"),
    ("event_depth_ticks", "异常深度(tick)"),
    ("reference_contract_count", "参考合约数"),
    ("trigger_reasons", "命中原因"),
    ("recovery_label", "回归标签"),
]

DETAIL_COLUMNS = [
    ("timestamp", "时间"),
    ("snapshot_seq", "快照序号"),
    ("LastPrice", "最新成交价"),
    ("mid_price", "盘口中价"),
    ("spread_ticks", "买卖价差(tick)"),
    ("delta_volume", "成交量增量"),
    ("last_vs_mid_down_ticks", "成交价低于盘口(tick)"),
    ("peer_excess_down_ticks", "相对参考合约额外下跌(tick)"),
    ("snapshot_avg_trade_gap_ticks", "快照均价偏离(tick)"),
    ("event_depth_ticks", "异常深度(tick)"),
    ("__row_reason", "行内判断"),
]

REFERENCE_COLUMNS = [
    ("reference_contract", "参考合约"),
    ("current_time", "锚点使用快照时间"),
    ("current_mid_price", "锚点盘口中价"),
    ("lookback_time", "回看3秒快照时间"),
    ("lookback_mid_price", "回看3秒盘口中价"),
    ("move_ticks", "3秒涨跌(tick)"),
    ("current_age_seconds", "当前快照延迟(秒)"),
    ("lookback_age_seconds", "回看快照延迟(秒)"),
    ("used_in_peer_median", "是否纳入参考中位数"),
    ("missing_reason", "备注"),
]


def render_event_replay_html(events_df: pd.DataFrame, replay_payload: dict[str, object]) -> str:
    detail_blocks: list[str] = []
    summary_rows_meta: list[dict[str, object]] = []
    event_records = events_df.to_dict("records")
    for event in event_records:
        event_id = f"{event['contract']}|{event['event_time']}"
        payload = replay_payload.get(event_id, {})
        if isinstance(payload, list):
            rows = payload
            reference_rows: list[dict[str, object]] = []
        else:
            rows = payload.get("rows", [])
            reference_rows = payload.get("reference_rows", [])
        frame = pd.DataFrame(rows)
        if frame.empty:
            continue
        summary_rows_meta.append(
            {
                "event_id": event_id,
                "contract": event.get("contract"),
                "event_time": event.get("event_time"),
                "event_start_time": event.get("event_start_time"),
                "event_end_time": event.get("event_end_time"),
                "event_low_price": event.get("event_low_price"),
                "event_depth_ticks": event.get("event_depth_ticks"),
                "reference_contract_count": event.get("reference_contract_count"),
                "trigger_reasons": event.get("trigger_reasons"),
                "recovery_label": event.get("recovery_label"),
            }
        )
        detail_blocks.append(_render_event_section(event, frame, reference_rows, hidden=True))

    sections: list[str] = [
        "<html><head><meta charset='utf-8'>",
        "<title>乌龙指事件复盘</title>",
        _style_block(),
        "</head><body>",
        "<div id='page-top'></div>",
        "<h1>乌龙指事件复盘</h1>",
        "<p class='page-note'>点事件总览最右侧的“查看详情”会弹出详情窗口。红色行是乌龙指那一笔，看完点关闭即可继续看其他事件。</p>",
    ]
    if events_df.empty:
        sections.append("<div class='empty'>当前没有检测到疑似乌龙指事件。</div></body></html>")
        return "".join(sections)

    sections.append("<h2>事件总览</h2>")
    sections.append(_render_event_summary_table(summary_rows_meta))
    sections.append("<div id='event-detail-placeholder' class='empty'>点击上面事件总览最右侧的“查看详情”，会弹出这笔事件的详情。</div>")
    sections.extend(detail_blocks)
    sections.append("</body></html>")
    return "".join(sections)


def _render_event_summary_table(events: list[dict[str, object]]) -> str:
    headers = "".join(f"<th>{label}</th>" for _, label in EVENT_COLUMNS) + "<th>操作</th>"
    body_rows = []
    for row in events:
        row["trigger_reasons"] = _translate_trigger_reasons(row.get("trigger_reasons"))
        row["recovery_label"] = _translate_recovery_label(row.get("recovery_label"))
        event_id = _format_dom_id(str(row.get("event_id")))
        modal_href = f"#modal-{event_id}"
        body_rows.append(
            f"<tr class='event-summary-row' id='summary-{event_id}'>"
            + "".join(
                f"<td>{_format_value(row.get(key))}</td>"
                for key, _ in EVENT_COLUMNS
            )
            + f"<td class='detail-cell'><a class='detail-link' href='{modal_href}'>查看详情</a></td>"
            + "</tr>"
        )
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_event_section(event: dict[str, object], frame: pd.DataFrame, reference_rows: list[dict[str, object]], *, hidden: bool = False) -> str:
    display = frame.copy()
    display["__row_reason"] = display.apply(_row_reason, axis=1)
    anchor_rows = display.loc[display["__is_event_anchor"] == True]  # noqa: E712
    anchor = anchor_rows.iloc[0] if not anchor_rows.empty else display.iloc[0]

    summary_cards = [
        ("乌龙指时间", f"{_format_value(anchor.get('timestamp'))} / 序号 {_format_value(anchor.get('snapshot_seq'))}"),
        ("判定结果", _translate_trigger_reasons(event.get("trigger_reasons"))),
        ("最新成交价", _format_value(anchor.get("LastPrice"))),
        ("盘口中价", _format_value(anchor.get("mid_price"))),
        ("成交价低于盘口", _format_ticks(anchor.get("last_vs_mid_down_ticks"))),
        ("相对参考额外下跌", _format_ticks(anchor.get("peer_excess_down_ticks"))),
        ("异常深度", _format_ticks(anchor.get("event_depth_ticks"))),
        ("回归标签", _translate_recovery_label(event.get("recovery_label"))),
    ]

    explanation = _build_explanation(event, anchor)
    explanation_block = _render_explanation_block(explanation)
    detail_table = _render_detail_table(display)
    reference_table = _render_reference_table(reference_rows)
    title = f"{html.escape(str(event.get('contract')))} @ {html.escape(_format_value(event.get('event_time')))}"
    event_id = _format_dom_id(f"{event.get('contract')}|{event.get('event_time')}")
    return (
        f"<section class='modal-shell' id='modal-{event_id}'>"
        f"<a class='modal-backdrop' href='#page-top' aria-label='关闭弹窗'></a>"
        f"<div class='event-section modal-panel'>"
        f"<div class='modal-topbar'><a class='modal-close' href='#page-top'>关闭</a></div>"
        f"<h2>{title}</h2>"
        f"<div class='cards'>{''.join(_render_card(label, value) for label, value in summary_cards)}</div>"
        f"{explanation_block}"
        f"<h3>参考合约对照</h3>"
        f"<p class='section-note'>下面列出目标合约和参考合约在锚点时刻、回看 3 秒时的盘口中价，以及各自的 3 秒涨跌 tick。</p>"
        f"{reference_table}"
        f"<div class='legend'><span class='badge anchor'>红色行 = 被判为乌龙指的那一笔</span><span class='badge candidate'>黄色行 = 命中过候选阈值的快照</span></div>"
        f"<h3>事件前后 30 秒明细</h3>"
        f"{detail_table}"
        f"</div>"
        f"</section>"
    )


def _render_detail_table(frame: pd.DataFrame) -> str:
    headers = "".join(f"<th>{label}</th>" for _, label in DETAIL_COLUMNS)
    body_rows = []
    for _, row in frame.iterrows():
        classes = []
        if bool(row.get("__is_event_anchor")):
            classes.append("anchor-row")
        elif bool(row.get("__is_candidate")):
            classes.append("candidate-row")
        class_attr = f" class='{' '.join(classes)}'" if classes else ""
        body_rows.append(
            f"<tr{class_attr}>"
            + "".join(f"<td>{_format_value(row.get(key))}</td>" for key, _ in DETAIL_COLUMNS)
            + "</tr>"
        )
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_reference_table(reference_rows: list[dict[str, object]]) -> str:
    if not reference_rows:
        return "<div class='empty'>当前没有可展示的参考合约对照数据。</div>"
    headers = "".join(f"<th>{label}</th>" for _, label in REFERENCE_COLUMNS)
    body_rows = []
    for row in reference_rows:
        body_rows.append(
            "<tr>" + "".join(f"<td>{_format_reference_value(key, row.get(key))}</td>" for key, _ in REFERENCE_COLUMNS) + "</tr>"
        )
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _build_explanation(event: dict[str, object], anchor: pd.Series) -> list[str]:
    reasons = [
        f"这笔快照就是乌龙指那一笔：{_format_value(anchor.get('timestamp'))}，序号 {_format_value(anchor.get('snapshot_seq'))}。",
        f"它的最新成交价是 {_raw_value(anchor.get('LastPrice'))}，盘口中价是 {_raw_value(anchor.get('mid_price'))}，比盘口低 {_raw_value(anchor.get('last_vs_mid_down_ticks'))} tick。",
        f"同品种参考合约中位数只跌了 {_raw_value(anchor.get('peer_median_move_ticks'))} tick，所以它比参考合约额外多跌 {_raw_value(anchor.get('peer_excess_down_ticks'))} tick。",
        f"本次用了 {_format_value(event.get('reference_contract_count'))} 个参考合约，最终命中“{_translate_trigger_reasons(event.get('trigger_reasons'))}”。",
    ]
    if pd.notna(anchor.get("snapshot_avg_trade_gap_ticks")):
        reasons.append(
            f"快照均价偏离 {_raw_value(anchor.get('snapshot_avg_trade_gap_ticks'))} tick，说明这笔里可能还藏着低价成交。"
        )
    return reasons


def _render_explanation_block(items: list[str]) -> str:
    body = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return (
        "<details class='explain'>"
        "<summary>点击查看为什么它是乌龙指</summary>"
        f"<ul>{body}</ul>"
        "</details>"
    )


def _row_reason(row: pd.Series) -> str:
    parts: list[str] = []
    if bool(row.get("__is_event_anchor")):
        parts.append("本行就是被判为乌龙指的快照")
    elif bool(row.get("__is_candidate")):
        parts.append("本行命中过候选阈值")
    if pd.notna(row.get("last_vs_mid_down_ticks")) and float(row["last_vs_mid_down_ticks"]) >= 20:
        parts.append(f"成交价低于盘口 {_raw_value(row.get('last_vs_mid_down_ticks'))} tick")
    if pd.notna(row.get("peer_excess_down_ticks")) and float(row["peer_excess_down_ticks"]) >= 20:
        parts.append(f"相对参考额外下跌 {_raw_value(row.get('peer_excess_down_ticks'))} tick")
    if pd.notna(row.get("snapshot_avg_trade_gap_ticks")) and float(row["snapshot_avg_trade_gap_ticks"]) >= 20:
        parts.append(f"快照均价偏离 {_raw_value(row.get('snapshot_avg_trade_gap_ticks'))} tick")
    return "；".join(parts)


def _translate_trigger_reasons(value: object) -> str:
    mapping = {
        "visible_last_drop": "可见成交价砸穿盘口",
        "hidden_avg_trade_drop": "快照均价出现隐藏低价成交",
    }
    text = str(value or "")
    if not text:
        return ""
    return "、".join(mapping.get(part, part) for part in text.split(","))


def _translate_recovery_label(value: object) -> str:
    mapping = {
        "fast_trade_recovery": "10秒内成交快速回归",
        "fast_quote_recovery": "10秒内盘口快速回归",
        "slow_recovery": "30秒内缓慢回归",
        "no_recovery": "30秒内未明显回归",
        "truncated": "回归窗口被时段断点截断",
    }
    return mapping.get(str(value or ""), str(value or ""))


def _format_reference_value(key: str, value: object) -> str:
    if key == "used_in_peer_median":
        text = "是" if bool(value) else "否"
        if isinstance(value, str):
            text = value
        return text
    return _format_value(value)


def _render_card(label: str, value: str) -> str:
    return (
        "<div class='card'>"
        f"<div class='card-label'>{html.escape(label)}</div>"
        f"<div class='card-value'>{html.escape(value)}</div>"
        "</div>"
    )


def _format_ticks(value: object) -> str:
    return "" if pd.isna(value) else f"{_raw_value(value)} tick"


def _raw_value(value: object) -> str:
    if pd.isna(value):
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _format_value(value: object) -> str:
    return html.escape(_raw_value(value))


def _style_block() -> str:
    return """
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }
      h1, h2, h3 { color: #111827; }
      .page-note { color: #4b5563; }
      .event-section { margin-top: 28px; padding: 20px; background: #ffffff; border: 1px solid #e5e7eb; border-radius: 14px; }
      .event-summary-row:hover td { background: #e0f2fe; }
      .detail-cell { white-space: nowrap; }
      .detail-link { display: inline-block; background: #0f766e; color: #ffffff; border-radius: 999px; padding: 6px 12px; font-size: 12px; font-weight: 600; text-decoration: none; }
      .detail-link:hover { background: #115e59; }
      .modal-shell { position: fixed; inset: 0; z-index: 1000; display: none; align-items: flex-start; justify-content: center; padding: 32px 20px; overflow-y: auto; }
      .modal-shell:target { display: flex; }
      .modal-backdrop { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); }
      .modal-panel { position: relative; width: min(1200px, 100%); margin-top: 0; box-shadow: 0 24px 80px rgba(15, 23, 42, 0.22); z-index: 1; }
      .modal-topbar { display: flex; justify-content: flex-end; margin-bottom: 8px; }
      .modal-close { display: inline-block; background: #111827; color: #ffffff; border-radius: 999px; padding: 8px 14px; font-size: 12px; text-decoration: none; }
      .modal-close:hover { background: #1f2937; }
      .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 12px 0 16px; }
      .card { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; }
      .card-label { font-size: 12px; color: #6b7280; margin-bottom: 6px; }
      .card-value { font-size: 18px; font-weight: 700; color: #111827; word-break: break-word; }
      .explain { background: #fff7ed; border: 1px solid #fed7aa; border-radius: 10px; padding: 12px 16px; margin-bottom: 14px; }
      .explain summary { cursor: pointer; font-weight: 700; color: #9a3412; }
      .legend { margin: 10px 0 14px; }
      .badge { display: inline-block; margin-right: 10px; padding: 6px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
      .badge.anchor { background: #fee2e2; color: #991b1b; }
      .badge.candidate { background: #fef3c7; color: #92400e; }
      table { width: 100%; border-collapse: collapse; background: white; margin: 10px 0 18px; }
      th, td { border: 1px solid #e5e7eb; padding: 8px 10px; font-size: 13px; text-align: left; vertical-align: top; }
      th { background: #f3f4f6; position: sticky; top: 0; }
      .anchor-row td { background: #fee2e2; font-weight: 700; }
      .candidate-row td { background: #fef3c7; }
      .empty { padding: 16px; background: white; border-radius: 12px; border: 1px solid #e5e7eb; }
      .section-note { color: #6b7280; margin: 6px 0 10px; }
      @media (max-width: 720px) {
        .modal-shell { padding: 12px; }
        .modal-panel { padding: 14px; }
      }
    </style>
    """


def _format_dom_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value)
