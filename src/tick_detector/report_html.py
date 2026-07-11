from __future__ import annotations

import html

import pandas as pd


# 事件摘要表中文表头（设计文档 §9）
EVENT_SUMMARY_COLUMNS = [
    ("event_id", "事件编号"),
    ("display_trade_date", "交易日"),
    ("event_anchor_time", "事件时间"),
    ("commodity", "品种"),
    ("contract", "合约"),
    ("trigger_reasons", "触发原因"),
    ("fair_price", "合理价"),
    ("interval_vwap_anchor", "区间成交均价"),
    ("last_down_ticks", "末笔向下偏离_跳"),
    ("vwap_down_ticks", "区间均价向下偏离_跳"),
    ("onset_ticks", "突发偏离_跳"),
    ("event_volume", "事件成交量"),
    ("recovery_label", "回归标签"),
    ("visible_recovered_seconds", "可见末笔恢复确认秒数"),
    ("interval_recovered_seconds", "区间均价恢复确认秒数"),
]

# 目标检测明细表中文表头
TARGET_DETAIL_COLUMNS = [
    ("display_trade_date", "交易日"),
    ("contract", "合约代码"),
    ("display_time", "更新时间"),
    ("LastPrice", "最新成交价"),
    ("Volume", "累计成交量"),
    ("Turnover", "累计成交额"),
    ("BidPrice1", "买一价"),
    ("BidVolume1", "买一量"),
    ("AskPrice1", "卖一价"),
    ("AskVolume1", "卖一量"),
    ("AveragePrice", "原始平均价"),
    ("OpenInterest", "持仓量"),
    ("UpperLimitPrice", "涨停价"),
    ("LowerLimitPrice", "跌停价"),
    ("delta_volume", "区间增量成交量"),
    ("delta_turnover", "区间增量成交额"),
    ("interval_vwap", "区间成交均价"),
    ("fair_price", "合理价"),
    ("last_down_ticks", "末笔向下偏离_跳"),
    ("vwap_down_ticks", "区间均价向下偏离_跳"),
    ("__is_candidate_anchor", "是否候选锚点"),
]

# 参考合约原始快照表中文表头（只允许原始行情字段）
PEER_RAW_COLUMNS = [
    ("display_trade_date", "交易日"),
    ("contract", "合约代码"),
    ("display_time", "更新时间"),
    ("LastPrice", "最新成交价"),
    ("Volume", "累计成交量"),
    ("Turnover", "累计成交额"),
    ("BidPrice1", "买一价"),
    ("BidVolume1", "买一量"),
    ("AskPrice1", "卖一价"),
    ("AskVolume1", "卖一量"),
    ("AveragePrice", "原始平均价"),
    ("OpenInterest", "持仓量"),
    ("UpperLimitPrice", "涨停价"),
    ("LowerLimitPrice", "跌停价"),
]

# 合约运行诊断表中文表头
DIAGNOSTICS_COLUMNS = [
    ("contract", "合约"),
    ("parameter_profile", "参数档"),
    ("validation_status", "验证状态"),
    ("raw_rows", "原始行数"),
    ("merged_rows", "同时间键合并后行数"),
    ("tradable_rows", "可检测行数"),
    ("metadata_blocked_rows", "元数据阻断行数"),
    ("session_blocked_rows", "交易时段阻断行数"),
    ("peer_blocked_rows", "参考合约阻断行数"),
    ("noise_blocked_rows", "历史噪声不足行数"),
    ("counter_unconfirmed_rows", "计数器同步未确认行数"),
    ("counter_lag_rows", "计数器错位嫌疑行数"),
    ("candidate_events", "候选事件数"),
]


def render_event_replay_html(
    events_df: pd.DataFrame,
    replay_payload: dict[str, object],
    contract_diagnostics: list[dict[str, object]],
) -> str:
    """渲染中文 HTML 报告：合约诊断 -> 事件总览 -> 每事件检测窗口。

    设计文档 §9：空候选先渲染合约诊断表，再显示无候选提示。
    """
    sections: list[str] = [
        "<html><head><meta charset='utf-8'>",
        "<title>聚合 Tick 乌龙指候选事件复盘</title>",
        _style_block(),
        "</head><body>",
        "<div id='page-top'></div>",
        "<h1>聚合 Tick 乌龙指候选事件复盘</h1>",
        "<p class='page-note'>聚合快照无法还原逐笔最低价。所有输出均为「疑似候选」，不宣称已确认交易所错单。</p>",
    ]

    # 顶部：合约运行诊断
    sections.append("<h2>合约运行诊断</h2>")
    sections.append(_render_diagnostics_table(contract_diagnostics))

    if events_df.empty:
        sections.append('<div class="empty">当前没有检测到疑似乌龙指候选事件。可结合上方诊断表判断是「确实无事件」还是「整条检测链被阻断」。</div>')
        sections.append("</body></html>")
        return "".join(sections)

    # 事件总览
    sections.append("<h2>候选事件总览</h2>")
    sections.append('<p class="section-note">点击最右侧「查看详情」弹出事件检测窗口。</p>')
    sections.append(_render_event_summary_table(events_df))

    # 每个事件的检测窗口
    detail_blocks: list[str] = []
    for event in events_df.to_dict("records"):
        event_id = str(event.get("event_id") or f"{event.get('contract')}|{event.get('event_anchor_time')}")
        payload = replay_payload.get(event_id, {})
        detail_blocks.append(_render_event_section(event, payload))
    sections.extend(detail_blocks)
    sections.append("</body></html>")
    return "".join(sections)


def _render_diagnostics_table(diagnostics: list[dict[str, object]]) -> str:
    if not diagnostics:
        return "<div class='empty'>无合约诊断数据。</div>"
    headers = "".join(f"<th>{label}</th>" for _, label in DIAGNOSTICS_COLUMNS)
    body_rows = []
    for row in diagnostics:
        cells = "".join(f"<td>{_format_diag_value(row.get(key))}</td>" for key, _ in DIAGNOSTICS_COLUMNS)
        cls = "unvalidated" if str(row.get("validation_status")) != "validated" else ""
        body_rows.append(f"<tr class='{cls}'>{cells}</tr>")
    return f"<table class='diagnostics'><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_event_summary_table(events_df: pd.DataFrame) -> str:
    headers = "".join(f"<th>{label}</th>" for _, label in EVENT_SUMMARY_COLUMNS) + "<th>操作</th>"
    body_rows = []
    for _, row in events_df.iterrows():
        event_id = _format_dom_id(str(row.get("event_id") or ""))
        modal_href = f"#modal-{event_id}"
        cells = "".join(f"<td>{_format_value(row.get(key))}</td>" for key, _ in EVENT_SUMMARY_COLUMNS)
        body_rows.append(
            f"<tr class='event-summary-row' id='summary-{event_id}'>"
            + cells
            + f"<td class='detail-cell'><a class='detail-link' href='{modal_href}'>查看详情</a></td>"
            + "</tr>"
        )
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_event_section(event: dict[str, object], payload: dict[str, object]) -> str:
    target_detail = payload.get("target_detail", []) if isinstance(payload, dict) else []
    peer_windows: dict[str, list] = payload.get("peer_windows", {}) if isinstance(payload, dict) else {}
    event_id = _format_dom_id(str(event.get("event_id") or f"{event.get('contract')}|{event.get('event_anchor_time')}"))
    title = f"{html.escape(str(event.get('contract', '')))} @ {html.escape(_format_value(event.get('event_anchor_time')))}"

    summary_cards = [
        ("事件时间", _format_value(event.get("event_anchor_time"))),
        ("触发原因", _translate_reasons(event.get("trigger_reasons"))),
        ("合理价", _format_value(event.get("fair_price"))),
        ("区间成交均价", _format_value(event.get("interval_vwap_anchor"))),
        ("最新成交价", _format_value(event.get("last_price_anchor"))),
        ("事件成交量", _format_value(event.get("event_volume"))),
        ("回归标签", _translate_recovery(event.get("recovery_label"))),
    ]

    detail_table = _render_target_detail_table(target_detail)
    peer_blocks = _render_peer_blocks(peer_windows)

    return (
        f"<section class='modal-shell' id='modal-{event_id}'>"
        f"<a class='modal-backdrop' href='#page-top' aria-label='关闭弹窗'></a>"
        f"<div class='event-section modal-panel'>"
        f"<div class='modal-topbar'><a class='modal-close' href='#page-top'>关闭</a></div>"
        f"<h2>{title}</h2>"
        f"<div class='cards'>{''.join(_render_card(l, v) for l, v in summary_cards)}</div>"
        f"<h3>目标合约检测明细</h3>"
        f"<p class='section-note'>窗口为锚点前 60 秒至后 10 秒。红色行为候选锚点。</p>"
        f"{detail_table}"
        f"<h3>参考合约原始快照</h3>"
        f"<p class='section-note'>每个参与合理价计算的参考合约在同一窗口的原始行情。</p>"
        f"{peer_blocks}"
        f"</div>"
        f"</section>"
    )


def _render_target_detail_table(rows: list[dict[str, object]]) -> str:
    if not rows:
        return "<div class='empty'>该窗口无检测明细。</div>"
    headers = "".join(f"<th>{label}</th>" for _, label in TARGET_DETAIL_COLUMNS)
    body_rows = []
    for row in rows:
        cls = "anchor-row" if row.get("__is_candidate_anchor") else ""
        cells = "".join(f"<td>{_format_value(row.get(key))}</td>" for key, _ in TARGET_DETAIL_COLUMNS)
        body_rows.append(f"<tr class='{cls}'>{cells}</tr>")
    return f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"


def _render_peer_blocks(peer_windows: dict[str, list]) -> str:
    if not peer_windows:
        return "<div class='empty'>无参与合理价计算的参考合约。</div>"
    blocks: list[str] = []
    for code, rows in peer_windows.items():
        blocks.append(f"<h4>{html.escape(str(code))}</h4>")
        if not rows:
            blocks.append("<div class='empty'>该窗口无原始快照。</div>")
            continue
        headers = "".join(f"<th>{label}</th>" for _, label in PEER_RAW_COLUMNS)
        body_rows = []
        for row in rows:
            cells = "".join(f"<td>{_format_value(row.get(key))}</td>" for key, _ in PEER_RAW_COLUMNS)
            body_rows.append(f"<tr>{cells}</tr>")
        blocks.append(f"<table><thead><tr>{headers}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>")
    return "".join(blocks)


def _translate_reasons(value: object) -> str:
    mapping = {
        "visible_execution_drop": "可见末笔成交异常",
        "interval_execution_drop": "区间均价异常",
    }
    text = str(value or "")
    if not text:
        return ""
    return "、".join(mapping.get(p, p) for p in text.split(","))


def _translate_recovery(value: object) -> str:
    mapping = {
        "trade_recovered_3s": "3秒内成交通道恢复",
        "quote_only_recovered_3s": "3秒内仅报价恢复",
        "trade_recovered_10s": "10秒内成交通道恢复",
        "quote_only_recovered_10s": "10秒内仅报价恢复",
        "persistent_10s": "10秒未恢复",
        "truncated": "窗口被断点截断",
    }
    return mapping.get(str(value or ""), str(value or ""))


def _format_diag_value(value: object) -> str:
    if value is None:
        return ""
    return _format_value(value)


def _render_card(label: str, value: str) -> str:
    return (
        "<div class='card'>"
        f"<div class='card-label'>{html.escape(label)}</div>"
        f"<div class='card-value'>{html.escape(str(value))}</div>"
        "</div>"
    )


def _format_value(value: object) -> str:
    return html.escape(_raw_value(value))


def _raw_value(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _format_dom_id(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value)


def _style_block() -> str:
    return """
    <style>
      body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", sans-serif; margin: 24px; color: #1f2937; background: #f8fafc; }
      h1, h2, h3, h4 { color: #111827; }
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
      .diagnostics th { background: #fef3c7; }
      .diagnostics tr.unvalidated td { background: #fee2e2; }
      table { width: 100%; border-collapse: collapse; background: white; margin: 10px 0 18px; }
      th, td { border: 1px solid #e5e7eb; padding: 8px 10px; font-size: 13px; text-align: left; vertical-align: top; }
      th { background: #f3f4f6; position: sticky; top: 0; }
      .anchor-row td { background: #fee2e2; font-weight: 700; }
      .empty { padding: 16px; background: white; border-radius: 12px; border: 1px solid #e5e7eb; }
      .section-note { color: #6b7280; margin: 6px 0 10px; }
      @media (max-width: 720px) {
        .modal-shell { padding: 12px; }
        .modal-panel { padding: 14px; }
      }
    </style>
    """
