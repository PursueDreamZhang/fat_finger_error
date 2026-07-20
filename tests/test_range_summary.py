from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pandas as pd

from scripts.generate_batch_summary import finalize_batch, init_batch, mark_finished
from scripts.generate_range_summary import generate_range_summary


def _write_events(output_dir: Path, commodity: str, rows: list[dict]) -> None:
    commodity_dir = output_dir / commodity
    commodity_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(commodity_dir / "tick_candidate_events.csv", index=False)


def _write_day_batch(output_root: Path, day: str, commodities: list[str], event_rows: dict[str, list[dict]]) -> None:
    day_dir = output_root / f"{day}-par"
    init_batch(day_dir, f"data/tick2026/{day[:6]}/{day}", commodities, "start")
    for commodity in commodities:
        rows = event_rows.get(commodity, [])
        if rows:
            _write_events(day_dir, commodity, rows)
        mark_finished(day_dir, commodity, "start", "end", 3, 0)
    finalize_batch(day_dir, "end", 9)


def test_generate_range_summary_distinguishes_complete_failed_and_missing(tmp_path):
    output_root = tmp_path / "output"
    _write_day_batch(
        output_root,
        "20260520",
        ["AU", "AG"],
        {
            "AU": [{
                "交易日": "20260520",
                "品种": "AU",
                "合约": "AU2606",
                "事件时间": "21:04:35.500",
                "触发原因": "visible_execution_drop,<bad>",
                "合理价": 995.33,
                "区间成交均价": 940.52,
                "回归标签": "trade_recovered_3s",
            }],
            "AG": [{
                "交易日": "20260520",
                "品种": "AG",
                "合约": "AG2606",
                "事件时间": "21:05:00.000",
                "触发原因": "visible_execution_drop",
                "合理价": 995.33,
                "区间成交均价": 940.52,
                "回归标签": "trade_recovered_3s",
            }],
        },
    )
    failed_dir = output_root / "20260521-par"
    init_batch(failed_dir, "day", ["AU", "AG"], "start")
    (failed_dir / "logs").mkdir(parents=True, exist_ok=True)
    (failed_dir / "logs" / "AG.log").write_text("boom <bad>", encoding="utf-8")
    mark_finished(failed_dir, "AU", "start", "end", 1, 0)
    mark_finished(failed_dir, "AG", "start", "end", 1, 2)
    finalize_batch(failed_dir, "end", 2)

    out_dir = tmp_path / "range"
    payload = generate_range_summary(
        start_date="20260520",
        end_date="20260522",
        commodities=["AU", "AG"],
        output_dir=out_dir,
        daily_output_root=output_root,
        missing_days=["20260522"],
    )

    assert payload["counts"] == {
        "total_days": 3,
        "completed_days": 1,
        "failed_days": 1,
        "missing_days": 1,
        "missing_output_days": 0,
        "incomplete_days": 0,
        "event_count": 2,
        "hit_commodities": 2,
    }
    html = (out_dir / "range_summary.html").read_text(encoding="utf-8")
    hit_section = html.split("<h2>时间日期汇总</h2>", 1)[1].split("<h2>品种汇总</h2>", 1)[0]
    assert "20260520" in hit_section
    assert "20260521" not in hit_section
    assert "20260522" not in hit_section
    assert "<th>命中品种</th>" in hit_section
    assert "<td>AG, AU</td>" in hit_section
    assert "事件明细" not in html
    assert "boom &lt;bad&gt;" in html
    assert "失败" in html
    assert "../20260520-par/batch_summary.html" in html
    assert json.loads((out_dir / "range_status.json").read_text(encoding="utf-8"))["counts"]["failed_days"] == 1


_TICK_COLUMNS = [
    "TradingDay",
    "InstrumentID",
    "UpdateTime",
    "UpdateMillisec",
    "LastPrice",
    "Volume",
    "BidPrice1",
    "BidVolume1",
    "AskPrice1",
    "AskVolume1",
    "AveragePrice",
    "Turnover",
    "OpenInterest",
    "UpperLimitPrice",
    "LowerLimitPrice",
]


def _tick_row(instrument_id: str, update_time: str) -> dict[str, object]:
    return {
        "TradingDay": 20260520,
        "InstrumentID": instrument_id,
        "UpdateTime": update_time,
        "UpdateMillisec": 0,
        "LastPrice": 100.0,
        "Volume": 10,
        "BidPrice1": 99.98,
        "BidVolume1": 1,
        "AskPrice1": 100.0,
        "AskVolume1": 1,
        "AveragePrice": 100.0,
        "Turnover": 1000000.0,
        "OpenInterest": 100,
        "UpperLimitPrice": 200.0,
        "LowerLimitPrice": 50.0,
    }


def _write_tick_csv(path: Path, instrument_id: str) -> None:
    pd.DataFrame([_tick_row(instrument_id, "09:01:30")], columns=_TICK_COLUMNS).to_csv(path, index=False)


def test_run_range_parallel_smoke_generates_range_html_and_nonzero_on_missing_day(tmp_path):
    data_root = tmp_path / "data"
    output_root = tmp_path / "output"
    day_dir = data_root / "202605" / "20260520"
    day_dir.mkdir(parents=True)
    _write_tick_csv(day_dir / "au2606_20260520.csv", "au2606")
    _write_tick_csv(day_dir / "au2608_20260520.csv", "au2608")
    _write_tick_csv(day_dir / "au2610_20260520.csv", "au2610")

    result = subprocess.run(
        [
            "bash",
            "scripts/run_range_parallel.sh",
            "--start-date",
            "20260520",
            "--end-date",
            "20260521",
            "--commodities",
            "AU,ZZ",
            "--total-parallel",
            "2",
            "--target-workers",
            "1",
            "--data-root",
            str(data_root),
            "--output-root",
            str(output_root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert (output_root / "20260520-par" / "batch_summary.html").exists()
    assert (output_root / "20260520_20260521-range" / "range_summary.html").exists()
    status = json.loads((output_root / "20260520_20260521-range" / "range_status.json").read_text(encoding="utf-8"))
    assert status["counts"]["missing_days"] == 1
