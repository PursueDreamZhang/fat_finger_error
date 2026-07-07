from __future__ import annotations

import html

import pandas as pd


def render_event_replay_html(events_df: pd.DataFrame, replay_payload: dict[str, list[dict[str, object]]]) -> str:
    event_headers = list(events_df.columns)
    event_rows = [
        "<tr>" + "".join(f"<td>{html.escape(str(row.get(column, '')))}</td>" for column in event_headers) + "</tr>"
        for row in events_df.to_dict("records")
    ]
    detail_sections = []
    for event_id, rows in replay_payload.items():
        frame = pd.DataFrame(rows)
        headers = list(frame.columns)
        body = [
            "<tr>" + "".join(f"<td>{html.escape(str(record.get(column, '')))}</td>" for column in headers) + "</tr>"
            for record in frame.to_dict("records")
        ]
        detail_sections.append(
            f"<h2>{html.escape(event_id)}</h2>"
            f"<table border='1'><thead><tr>{''.join(f'<th>{html.escape(str(h))}</th>' for h in headers)}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>"
        )
    return (
        "<html><body>"
        "<h1>tick detector replay</h1>"
        f"<table border='1'><thead><tr>{''.join(f'<th>{html.escape(str(h))}</th>' for h in event_headers)}</tr></thead>"
        f"<tbody>{''.join(event_rows)}</tbody></table>"
        f"{''.join(detail_sections)}"
        "</body></html>"
    )
