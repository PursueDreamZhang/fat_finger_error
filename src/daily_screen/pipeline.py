from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.daily_screen.data_access import load_commodity_data
from src.daily_screen.reference_selection import attach_references
from src.daily_screen.report_html import render_report_html
from src.daily_screen.result_builder import build_results
from src.daily_screen.sample_filter import assign_sample_status
from src.daily_screen.scoring import score_candidates


def analyze_commodities(
    symbols,
    start_date,
    end_date,
    output_dir: str | None = None,
    *,
    cache_dir: str | Path = "data/csv_data/data",
    config_path: str | Path = "config/local_config.json",
):
    normalized_symbols = _normalize_symbols(symbols)
    _validate_dates(start_date, end_date)

    output_path = _resolve_output_dir(normalized_symbols, output_dir)
    output_path.mkdir(parents=True, exist_ok=False)

    loaded = load_commodity_data(
        normalized_symbols,
        start_date,
        end_date,
        cache_dir=cache_dir,
        config_path=config_path,
    )
    daily_bar = loaded["daily_bar"]
    contract_meta = loaded["contract_meta"]

    referenced = attach_references(daily_bar)
    filtered = assign_sample_status(referenced, contract_meta)
    scored = score_candidates(filtered)
    built = build_results(scored, normalized_symbols, start_date, end_date)

    suspicious_dates = built["suspicious_dates"]
    commodity_summary = built["commodity_summary"]
    all_samples = built["all_samples"]
    report_payload = built["report_payload"]
    html = render_report_html(report_payload)

    html_path = output_path / "analysis_report.html"
    suspicious_dates_path = output_path / "suspicious_dates.csv"
    commodity_summary_path = output_path / "commodity_summary.csv"
    all_samples_path = output_path / "all_samples.csv"
    summary_json_path = output_path / "summary.json"

    html_path.write_text(html, encoding="utf-8")
    suspicious_dates.to_csv(suspicious_dates_path, index=False, encoding="utf-8-sig")
    commodity_summary.to_csv(commodity_summary_path, index=False, encoding="utf-8-sig")
    all_samples.to_csv(all_samples_path, index=False, encoding="utf-8-sig")
    summary_json_path.write_text(
        json.dumps(report_payload, ensure_ascii=False, default=str, indent=2),
        encoding="utf-8",
    )

    return {
        "html_report_path": str(html_path),
        "suspicious_dates": suspicious_dates.to_dict(orient="records"),
        "suspicious_dates_path": str(suspicious_dates_path),
        "commodity_summary_path": str(commodity_summary_path),
        "all_samples_path": str(all_samples_path),
        "summary_json_path": str(summary_json_path),
    }


def _normalize_symbols(symbols) -> list[str]:
    if isinstance(symbols, str):
        items = [item.strip() for item in symbols.split(",")]
    else:
        items = [str(item).strip() for item in symbols]

    normalized: list[str] = []
    for item in items:
        if not item:
            continue
        upper_item = item.upper()
        if upper_item not in normalized:
            normalized.append(upper_item)

    if not normalized:
        raise ValueError("symbols 不能为空")
    return normalized


def _validate_dates(start_date: str, end_date: str) -> None:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    end_ts = datetime.strptime(end_date, "%Y%m%d")
    if start_ts > end_ts:
        raise ValueError("start_date 不能晚于 end_date")


def _resolve_output_dir(symbols: list[str], output_dir: str | None) -> Path:
    if output_dir:
        output_path = Path(output_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path("output") / f"{timestamp}-{'_'.join(symbols)}"

    if output_path.exists():
        raise FileExistsError(f"输出目录已存在: {output_path}")
    return output_path
