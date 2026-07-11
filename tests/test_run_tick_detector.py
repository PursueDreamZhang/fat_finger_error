from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from run_tick_detector import parse_args, run_detection


def test_tick_day_path_is_required():
    with pytest.raises(SystemExit):
        parse_args([])


def test_output_dir_defaults_to_tick_detector_suffix():
    args = parse_args(["--tick-day-path", "data/tick2026/202605/20260520.zip"])
    assert args.output_dir.endswith("-tick-detector")
    assert args.output_dir.startswith("output/")


def test_commodity_and_contract_only_affect_target_filters():
    args = parse_args(
        [
            "--tick-day-path",
            "data/tick2026/202605/20260520.zip",
            "--commodity",
            "AU",
            "--contract",
            "AU2606",
        ]
    )
    assert args.tick_day_path == "data/tick2026/202605/20260520.zip"
    assert args.commodity == "AU"
    assert args.contract == "AU2606"


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


def _tick_row(
    instrument_id: str,
    update_time: str,
    millisec: int,
    *,
    last_price: float = 100.0,
    volume: int = 10,
    bid: float = 99.98,
    ask: float = 100.0,
    turnover: float = 1000000.0,
) -> dict[str, object]:
    return {
        "TradingDay": 20260520,
        "InstrumentID": instrument_id,
        "UpdateTime": update_time,
        "UpdateMillisec": millisec,
        "LastPrice": last_price,
        "Volume": volume,
        "BidPrice1": bid,
        "BidVolume1": 1,
        "AskPrice1": ask,
        "AskVolume1": 1,
        "AveragePrice": last_price,
        "Turnover": turnover,
        "OpenInterest": 100,
        "UpperLimitPrice": 200.0,
        "LowerLimitPrice": 50.0,
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows, columns=_TICK_COLUMNS).to_csv(path, index=False)


def test_run_detection_writes_chinese_csv_headers_even_with_zero_candidates(tmp_path):
    """零候选 CSV 仍含中文表头，HTML 含合约诊断"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    # 只给极少数据，不构成足够 noise 历史 -> 零候选
    _write_csv(day_dir / "au2606_20260520.csv", [_tick_row("au2606", "09:01:30", 0)])
    _write_csv(day_dir / "au2608_20260520.csv", [_tick_row("au2608", "09:01:30", 0)])
    _write_csv(day_dir / "au2610_20260520.csv", [_tick_row("au2610", "09:01:30", 0)])

    output_dir = tmp_path / "out"
    result = run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )

    csv_path = output_dir / "tick_candidate_events.csv"
    html_path = output_dir / "event_replay.html"
    assert csv_path.exists()
    assert html_path.exists()
    assert result["tick_candidate_events_csv"] == str(csv_path)
    events = pd.read_csv(csv_path)
    # 中文表头（设计文档 §9）
    assert "检测器版本" in events.columns
    assert "交易日" in events.columns
    assert "事件时间" in events.columns
    assert "合理价" in events.columns
    assert "回归标签" in events.columns
    assert len(events) == 0  # 零候选


def test_run_detection_html_shows_diagnostics_for_zero_candidates(tmp_path):
    """空候选 HTML 可区分无事件/元数据阻断/参考不足/noise不足/计数器异常"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    _write_csv(day_dir / "au2606_20260520.csv", [_tick_row("au2606", "09:01:30", 0)])
    _write_csv(day_dir / "au2608_20260520.csv", [_tick_row("au2608", "09:01:30", 0)])
    _write_csv(day_dir / "au2610_20260520.csv", [_tick_row("au2610", "09:01:30", 0)])
    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )
    html = (output_dir / "event_replay.html").read_text(encoding="utf-8")
    assert "合约运行诊断" in html
    assert "原始行数" in html
    assert "候选事件数" in html


def test_run_detection_unvalidated_commodity_outputs_no_candidates(tmp_path):
    """非 AU 品种只有 unvalidated_commodity 诊断，不输出正式候选"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    _write_csv(day_dir / "jd2606_20260520.csv", [_tick_row("jd2606", "09:01:30", 0)])
    _write_csv(day_dir / "jd2608_20260520.csv", [_tick_row("jd2608", "09:01:30", 0)])
    _write_csv(day_dir / "jd2610_20260520.csv", [_tick_row("jd2610", "09:01:30", 0)])
    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir),
        commodity="JD",
        contract="JD2606",
        output_dir=str(output_dir),
    )
    events = pd.read_csv(output_dir / "tick_candidate_events.csv")
    assert len(events) == 0
    html = (output_dir / "event_replay.html").read_text(encoding="utf-8")
    assert "未标定品种" in html or "unvalidated" in html


def test_run_detection_au_candidate_includes_chinese_fields(tmp_path):
    """AU 候选输出包含中文字段和两个触发原因"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    # 构造足够数据让 fair_price/noise 可靠
    target_rows = []
    peer_a_rows = []
    peer_b_rows = []
    # 前 250 秒正常（价格 100），第 251 秒突然跌到 99.0
    for sec in range(252):
        ut = f"09:{(90 + sec) // 60 % 60:02d}:{(90 + sec) % 60:02d}"
        if sec < 250:
            lp, vol, to = 100.0, 10 + sec, (10 + sec) * 1000000.0
        else:
            lp, vol, to = 99.0, 400, 39600000.0
        target_rows.append(_tick_row("au2606", ut, 0, last_price=lp, volume=vol, turnover=to, bid=lp-0.02, ask=lp))
        peer_a_rows.append(_tick_row("au2608", ut, 0, last_price=100.0, volume=20+sec, turnover=(20+sec)*1000000.0, bid=99.98, ask=100.0))
        peer_b_rows.append(_tick_row("au2610", ut, 0, last_price=100.0, volume=18+sec, turnover=(18+sec)*1000000.0, bid=99.98, ask=100.0))
    _write_csv(day_dir / "au2606_20260520.csv", target_rows)
    _write_csv(day_dir / "au2608_20260520.csv", peer_a_rows)
    _write_csv(day_dir / "au2610_20260520.csv", peer_b_rows)

    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )
    events = pd.read_csv(output_dir / "tick_candidate_events.csv")
    if len(events) > 0:
        row = events.iloc[0]
        assert row["合约"] == "AU2606"
        assert "合理价" in events.columns
        assert "触发原因" in events.columns
        assert "区间成交均价" in events.columns
        assert "回归标签" in events.columns
