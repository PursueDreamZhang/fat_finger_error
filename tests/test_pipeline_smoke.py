from __future__ import annotations

import pandas as pd

from src.daily_screen.pipeline import analyze_commodities


def _row(code, date, close, *, vol=1000):
    return {
        "code": code,
        "date": date,
        "pre_close": close,
        "pre_settle": close,
        "open": close,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "vol": vol,
    }


def _build_fixture(tmp_path) -> str:
    # 数据跨度须远大于分析窗口,确保窗口内样本 lifecycle 合法:
    #   listed_days = trade_date - listed_date >= 10 且 days_to_last_trade >= 10
    # 窗口 20250115-20250128;数据 20241201-20250228(前有 ~6 周历史供 rolling,后留 ~1 个月避免 lifecycle_edge)
    dates: list[str] = []
    for month, year in ((12, 2024), (1, 2025), (2, 2025)):
        for day in range(1, 29):
            if day % 7 in (0, 6):  # 粗略跳周末
                continue
            dates.append(f"{year}{month:02d}{day:02d}")
    dates = sorted(set(dates))
    for idx, date in enumerate(dates):
        year = int(date[:4])
        year_dir = tmp_path / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        base = 100 + idx
        rows = [
            _row("A2505.DCE", date, base, vol=2000),
            _row("A2507.DCE", date, base + 1, vol=1500),
        ]
        pd.DataFrame(rows).to_parquet(year_dir / f"{date}.parquet")
    return str(tmp_path)


def test_pipeline_runs_on_local_parquet_fixture(tmp_path):
    data_dir = _build_fixture(tmp_path / "data")
    output_dir = tmp_path / "out"
    result = analyze_commodities(
        symbols=["A"],
        start_date="20250115",
        end_date="20250228",
        output_dir=str(output_dir),
        data_dir=data_dir,
    )
    assert (output_dir / "analysis_report.html").exists()
    assert (output_dir / "suspicious_dates.csv").exists()
    assert (output_dir / "summary.json").exists()
    assert "html_report_path" in result
    samples = pd.read_csv(output_dir / "all_samples.csv")
    # 锁住"fixture 真的产生有效样本",而不是跑一条全无效的通路
    assert (samples["sample_status"] == "valid").any(), "fixture 应至少产生一个有效样本"
    # 锁住正常主路径:有效样本确实被评分过、结果列齐备
    # (防止 scoring/result_builder 回归导致 candidate_score 列缺失或全空,而 suspicious_dates 永远空却仍判绿)
    assert "candidate_score" in samples.columns and "candidate_level" in samples.columns
    valid_scored = samples.loc[
        (samples["sample_status"] == "valid") & samples["candidate_score"].notna()
    ]
    assert len(valid_scored) > 0, "应有有效样本被评分(candidate_score 非空)"