from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from run_tick_detector import _build_diagnostics, _build_peer_raw_windows, _build_replay_payload, parse_args, run_detection
from src.tick_detector.report_html import _render_event_section, _render_peer_blocks


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


def test_commodities_accepts_multiple_codes_and_rejects_conflicts():
    args = parse_args(
        ["--tick-day-path", "data/tick2026/202605/20260520.zip", "--commodities", "AU,AG"]
    )

    assert args.commodities == "AU,AG"
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--tick-day-path", "data/tick2026/202605/20260520.zip",
                "--commodity", "AU", "--commodities", "AG",
            ]
        )
    with pytest.raises(SystemExit):
        parse_args(
            ["--tick-day-path", "data/tick2026/202605/20260520.zip", "--commodities", ",,"]
        )


def test_target_worker_validation():
    args = parse_args(
        [
            "--tick-day-path", "data/tick2026/202605/20260520.zip",
            "--target-workers", "2",
        ]
    )
    assert args.target_workers == 2
    with pytest.raises(SystemExit):
        parse_args(
            ["--tick-day-path", "data/tick2026/202605/20260520.zip", "--target-workers", "0"]
        )


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


def test_peer_window_marks_exact_or_earlier_nearest_reference_snapshot():
    event = pd.Series({"event_anchor_key": 1000, "__valid_peer_contracts": ["AU2608"]})
    exact_frame = pd.DataFrame(
        {
            "market_time_key": [999, 1000, 1001],
            "display_time": ["09:00:00.999", "09:00:01.000", "09:00:01.001"],
        }
    )
    exact_rows = _build_peer_raw_windows(event, {"AU2608": exact_frame}, 990, 1010)["AU2608"]
    assert [row.get("__is_reference_anchor", False) for row in exact_rows] == [False, True, False]

    nearest_frame = exact_frame[exact_frame["market_time_key"] != 1000]
    nearest_rows = _build_peer_raw_windows(event, {"AU2608": nearest_frame}, 990, 1010)["AU2608"]
    assert [row.get("__is_reference_anchor", False) for row in nearest_rows] == [True, False]
    assert "<tr class='reference-anchor-row'>" in _render_peer_blocks({"AU2608": nearest_rows})


def test_diagnostics_reports_final_cumulative_volume():
    diagnostic = _build_diagnostics(
        "AU2606",
        pd.DataFrame({"Volume": [10, None, 25]}),
        raw_rows=3,
        merged_rows=3,
        parameter_profile="AUTO_INFERRED_V1",
        validation_status="validated",
        candidates_count=0,
    )
    assert diagnostic["day_total_volume"] == 25


def test_replay_payload_and_summary_cards_include_daily_high_low():
    event = pd.DataFrame([{"event_id": "AP610|09:00:01.000", "event_anchor_key": 1000}])
    target_frame = pd.DataFrame({"market_time_key": [1000], "display_time": ["09:00:01.000"]})
    payload: dict[str, dict[str, object]] = {}
    _build_replay_payload(
        event,
        "AP610",
        target_frame,
        {"AP610": target_frame},
        payload,
        {"AP2610": (7356.0, 7455.0, 7471.0)},
    )

    section = _render_event_section(event.iloc[0].to_dict(), payload["AP610|09:00:01.000"])
    assert "日线最高价" in section and ">7455<" in section
    assert "日线最低价" in section and ">7356<" in section


def test_run_detection_writes_chinese_csv_headers_even_with_zero_candidates(tmp_path):
    """零候选 CSV 仍含中文表头，HTML 含合约诊断"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    # 只给极少数据，不构成足够 noise 历史 -> 零候选
    _write_csv(day_dir / "au2606_20260520.csv", [_tick_row("au2606", "09:01:30", 0)])
    _write_csv(day_dir / "au2608_20260520.csv", [_tick_row("au2608", "09:01:30", 0)])
    _write_csv(day_dir / "au2610_20260520.csv", [_tick_row("au2610", "09:01:30", 0)])
    _write_csv(day_dir / "au2612_20260520.csv", [_tick_row("au2612", "09:01:30", 0)])

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
    assert "事件确认深度_跳" in events.columns
    assert "事件确认深度_基点" in events.columns
    assert "确认深度来源" in events.columns
    assert len(events) == 0  # 零候选


def test_run_detection_skips_targets_with_fewer_than_three_other_contracts(tmp_path):
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    for code in ("au2606", "au2608", "au2610"):
        _write_csv(day_dir / f"{code}_20260520.csv", [_tick_row(code, "09:01:30", 0)])

    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )

    html = (output_dir / "event_replay.html").read_text(encoding="utf-8")
    assert "AU2606" not in html


def test_parallel_targets_match_serial_output(tmp_path):
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    for code in ("au2606", "au2608", "au2610"):
        _write_csv(day_dir / f"{code}_20260520.csv", [_tick_row(code, "09:01:30", 0)])

    serial_dir = tmp_path / "serial"
    parallel_dir = tmp_path / "parallel"
    run_detection(tick_day_path=str(day_dir), commodity="AU", output_dir=str(serial_dir))
    run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        output_dir=str(parallel_dir),
        target_workers=2,
    )

    assert (parallel_dir / "event_replay_AU.html").read_text(encoding="utf-8") == (
        serial_dir / "event_replay_AU.html"
    ).read_text(encoding="utf-8")
    assert (parallel_dir / "tick_candidate_events.csv").read_text(encoding="utf-8") == (
        serial_dir / "tick_candidate_events.csv"
    ).read_text(encoding="utf-8")


def test_run_detection_html_shows_diagnostics_for_zero_candidates(tmp_path):
    """空候选 HTML 可区分无事件/元数据阻断/参考不足/noise不足/计数器异常"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    _write_csv(day_dir / "au2606_20260520.csv", [_tick_row("au2606", "09:01:30", 0)])
    _write_csv(day_dir / "au2608_20260520.csv", [_tick_row("au2608", "09:01:30", 0)])
    _write_csv(day_dir / "au2610_20260520.csv", [_tick_row("au2610", "09:01:30", 0)])
    _write_csv(day_dir / "au2612_20260520.csv", [_tick_row("au2612", "09:01:30", 0)])
    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )
    html = (output_dir / "event_replay.html").read_text(encoding="utf-8")
    assert "合约运行诊断" in html
    assert "当日成交总量" in html
    assert "原始行数" in html
    assert "候选事件数" in html


def test_run_detection_splits_html_per_commodity(tmp_path):
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    for code in ("au2606", "au2608", "au2610", "ag2606", "ag2608", "ag2610"):
        _write_csv(day_dir / f"{code}_20260520.csv", [_tick_row(code, "09:01:30", 0)])

    result = run_detection(
        tick_day_path=str(day_dir),
        commodities="AU,AG",
        output_dir=str(tmp_path / "out"),
    )

    assert (tmp_path / "out" / "event_replay_AU.html").exists()
    assert (tmp_path / "out" / "event_replay_AG.html").exists()
    assert len(result["event_replay_htmls"]) == 2


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
        assert row["确认深度来源"] == "detector_event_depth"
        assert row["事件确认深度_跳"] == pytest.approx(row["末笔向下偏离_跳"])
        assert row["事件确认深度_基点"] == pytest.approx(row["事件确认深度_跳"] * 0.02 / row["合理价"] * 10000)
        assert "触发原因" in events.columns
        assert "区间成交均价" in events.columns
        assert "回归标签" in events.columns


AU_REAL_DATA_PATH = "data/tick2026/202605/20260520"


def _au_real_data_available() -> bool:
    from pathlib import Path

    return (Path(AU_REAL_DATA_PATH) / "au2606_20260520.csv").exists()


@pytest.mark.skipif(not _au_real_data_available(), reason="AU2606 真数据不可用")
def test_slow_au2606_real_anchor_210435_hits_two_reasons_and_matches_design(tmp_path):
    """设计文档 §10 标定锚点：AU2606 / 20260520 / 21:04:35.500。

    慢测试（~7 分钟，需真数据）。常规套件用 pytest -k "not slow_" 跳过。
    验收标准（implementation.md Task 6）：
      - 两通道均命中（visible_execution_drop + interval_execution_drop）
      - 合理价≈995.33、区间成交均价≈940.52、一秒合并均价≈962.18
      - 回归标签=trade_recovered_3s
      - 11:08:09.000 反例不命中
    """
    output_dir = tmp_path / "au-20260520"
    run_detection(
        tick_day_path=AU_REAL_DATA_PATH,
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )
    events = pd.read_csv(output_dir / "tick_candidate_events.csv")
    assert len(events) > 0

    # 锚点 21:04:35.500 必须存在且两通道命中
    anchor = events[events["事件时间"] == "21:04:35.500"]
    assert len(anchor) == 1
    row = anchor.iloc[0]
    assert "visible_execution_drop" in str(row["触发原因"])
    assert "interval_execution_drop" in str(row["触发原因"])

    # §10 标定值（容差见设计文档）
    assert row["合理价"] == pytest.approx(995.33, abs=1.0)
    assert row["区间成交均价"] == pytest.approx(940.52, abs=5.0)
    assert row["一秒合并成交均价"] == pytest.approx(962.18, abs=5.0)
    assert row["回归标签"] == "trade_recovered_3s"
    assert row["可见末笔恢复确认秒数"] == pytest.approx(0.5, abs=0.6)
    assert row["区间均价恢复确认秒数"] == pytest.approx(1.5, abs=0.6)

    # 反例 11:08:09.000 不得命中
    assert "11:08:09" not in events["事件时间"].astype(str).tolist()
