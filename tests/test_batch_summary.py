import json
from pathlib import Path

import pandas as pd

from scripts.generate_batch_summary import (
    clean_commodity_output,
    finalize_batch,
    init_batch,
    load_batch,
    mark_finished,
    mark_running,
)


def _write_events(output_dir: Path, commodity: str, rows: list[dict]) -> None:
    commodity_dir = output_dir / commodity
    commodity_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(commodity_dir / "tick_candidate_events.csv", index=False)


def test_batch_summary_distinguishes_hits_empty_failed_and_missing(tmp_path):
    init_batch(tmp_path, "data/tick2026/202605/20260520", ["AU", "JD", "RB", "CU"], "start")
    _write_events(tmp_path, "AU", [{
        "品种": "AU", "合约": "AU2606", "事件时间": "21:04:35.500",
        "触发原因": "visible_execution_drop,interval_execution_drop",
        "合理价": 995.33, "区间成交均价": 940.52,
        "末笔向下偏离_跳": 120, "区间均价向下偏离_跳": 90,
        "回归标签": "trade_recovered_3s",
    }])
    mark_finished(tmp_path, "AU", "start", "end", 10, 0)
    mark_finished(tmp_path, "JD", "start", "end", 11, 0)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "RB.log").write_text("trace\nboom <bad>", encoding="utf-8")
    mark_finished(tmp_path, "RB", "start", "end", 12, 2)
    (tmp_path / ".batch_status" / "CU.json").unlink()

    payload = finalize_batch(tmp_path, "end", 20)
    html = (tmp_path / "batch_summary.html").read_text(encoding="utf-8")

    assert payload["counts"] == {
        "total": 4, "completed": 3, "running": 0, "hit": 1,
        "failed": 1, "unfinished": 1, "event_count": 1,
        "down_event_count": 1, "up_event_count": 0,
    }
    assert "检测到疑似候选事件" in html
    assert "正常完成，无候选事件" in html
    assert "运行失败" in html
    assert "状态缺失" in html
    assert "boom &lt;bad&gt;" in html
    assert "AU/event_replay_AU.html" in html
    assert "AU/tick_candidate_events.csv" in html
    assert "logs/RB.log" in html
    assert json.loads((tmp_path / "batch_status.json").read_text(encoding="utf-8"))["counts"]["hit"] == 1


def test_hits_are_sorted_by_event_count_and_combined(tmp_path):
    init_batch(tmp_path, "day", ["AU", "JD"], "start")
    _write_events(tmp_path, "AU", [{"品种": "AU", "合约": "AU1", "事件时间": "10:00", "末笔向下偏离_跳": 3}])
    _write_events(tmp_path, "JD", [
        {"品种": "JD", "合约": "JD1", "事件时间": "09:00", "区间均价向下偏离_跳": 5},
        {"品种": "JD", "合约": "JD2", "事件时间": "11:00", "区间均价向下偏离_跳": 7},
    ])
    mark_finished(tmp_path, "AU", "start", "end", 1, 0)
    mark_finished(tmp_path, "JD", "start", "end", 1, 0)

    finalize_batch(tmp_path, "end", 2)
    html = (tmp_path / "batch_summary.html").read_text(encoding="utf-8")
    combined = pd.read_csv(tmp_path / "tick_candidate_events.csv")

    hit_section = html.split("<h2>命中品种总览</h2>", 1)[1].split("<h2>全部品种运行状态</h2>", 1)[0]
    assert hit_section.index("JD") < hit_section.index("AU")
    assert len(combined) == 3


def test_batch_summary_counts_directions_and_uses_directional_maxima(tmp_path):
    init_batch(tmp_path, "day", ["AU"], "start")
    _write_events(tmp_path, "AU", [
        {"品种": "AU", "合约": "AU1", "异常方向": "down", "末笔向下偏离_跳": 12},
        {"品种": "AU", "合约": "AU1", "异常方向": "up", "末笔向上偏离_跳": 30},
    ])
    mark_finished(tmp_path, "AU", "start", "end", 1, 0)
    payload = finalize_batch(tmp_path, "end", 2)
    status = payload["commodities"][0]
    assert payload["counts"]["down_event_count"] == 1
    assert payload["counts"]["up_event_count"] == 1
    assert status["max_down_ticks"] == 12
    assert status["max_up_ticks"] == 30


def test_running_transition_and_bad_or_empty_csv_do_not_crash(tmp_path):
    init_batch(tmp_path, "day", ["AU", "JD"], "start")
    mark_running(tmp_path, "AU", "start")
    _, statuses = load_batch(tmp_path)
    assert statuses[0]["status"] == "running"

    (tmp_path / "AU").mkdir()
    (tmp_path / "AU" / "tick_candidate_events.csv").write_text("", encoding="utf-8")
    _write_events(tmp_path, "JD", [{"unexpected": "value"}])
    mark_finished(tmp_path, "AU", "start", "end", 1, 0)
    mark_finished(tmp_path, "JD", "start", "end", 1, 0)

    payload = finalize_batch(tmp_path, "end", 2)
    assert payload["counts"]["completed"] == 2
    assert (tmp_path / "batch_summary.html").exists()


def test_finalize_keeps_empty_source_csv_headers(tmp_path):
    init_batch(tmp_path, "day", ["AU"], "start")
    _write_events(tmp_path, "AU", [])
    pd.DataFrame(columns=["品种", "异常方向", "末笔向上偏离_跳"]).to_csv(
        tmp_path / "AU" / "tick_candidate_events.csv", index=False
    )
    mark_finished(tmp_path, "AU", "start", "end", 1, 0)
    finalize_batch(tmp_path, "end", 2)
    assert pd.read_csv(tmp_path / "tick_candidate_events.csv").columns.tolist() == [
        "品种", "异常方向", "末笔向上偏离_跳"
    ]


def test_clean_commodity_output_removes_only_generated_files(tmp_path):
    commodity_dir = tmp_path / "AU"
    commodity_dir.mkdir()
    (commodity_dir / "tick_candidate_events.csv").write_text("old", encoding="utf-8")
    (commodity_dir / "event_replay_AU.html").write_text("old", encoding="utf-8")
    (commodity_dir / "keep.txt").write_text("keep", encoding="utf-8")

    clean_commodity_output(tmp_path, "AU")

    assert not (commodity_dir / "tick_candidate_events.csv").exists()
    assert not (commodity_dir / "event_replay_AU.html").exists()
    assert (commodity_dir / "keep.txt").exists()
