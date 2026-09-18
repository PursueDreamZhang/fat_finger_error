from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd

from src.mid_analyzer import Config, Source, SourceDiscovery, analyze_sources, csv_columns, discover_sources, prepare_group
from src.mid_analyzer_report import render_report_html


def _rows(times, lasts, volumes, *, bid=99, ask=101):
    return pd.DataFrame(
        {
            "TradingDay": ["20260303"] * len(times),
            "InstrumentID": ["X2601"] * len(times),
            "UpdateTime": times,
            "UpdateMillisec": [0] * len(times),
            "LastPrice": lasts,
            "Volume": volumes,
            "BidPrice1": [bid] * len(times),
            "BidVolume1": [10] * len(times),
            "AskPrice1": [ask] * len(times),
            "AskVolume1": [10] * len(times),
        }
    )


def _source(path: Path) -> Source:
    return Source(path, None, path.name, "20260303", "X2601", 0, "csv")


def test_same_second_raw_and_strict_keep_representative_and_anchor(tmp_path):
    path = tmp_path / "X2601_20260303.csv"
    frame = _rows(
        ["09:00:00", "09:00:01", "09:00:02", "09:00:02", "09:00:03", "09:00:04", "09:00:05", "09:00:06"],
        [100, 100, 90, 91, 100, 100, 100, 100],
        range(8),
    )
    frame.to_csv(path, index=False)
    result = analyze_sources(SourceDiscovery([_source(path)]), Config(thresholds=(0.05,), horizons=(1, 3), mfe_mae_horizon=3))

    assert len(result.events) == 2  # Raw + Strict, one cluster each
    strict = result.events.loc[result.events["mode"] == "strict"].iloc[0]
    assert strict["event_start_row_order"] == 2
    assert strict["event_end_row_order"] == 3
    assert strict["row_order"] == 2  # equal deviation chooses the earliest row
    assert strict["anchor_mid"] == 100
    assert strict["event_start_time"] == "09:00:02.000"
    assert strict["event_end_time"] == "09:00:02.000"
    assert strict["recovery_ratio_1s"] == 1
    assert bool(strict["mfe_mae_window_complete"])
    assert result.windows[strict["window_id"]][2]["is_representative"] is True


def test_default_recovery_outputs_only_1_3_5_seconds():
    cfg = Config()
    assert cfg.horizons == (1, 3, 5)
    assert cfg.mfe_mae_horizon == 5
    columns = csv_columns(cfg)
    assert "recovery_ratio_5s" in columns["event"]
    assert not any("_10s" in column or "_30s" in column or "_60s" in column for column in columns["event"])


def test_no_new_trade_and_outside_bbo_rule(tmp_path):
    path = tmp_path / "X2601_20260303.csv"
    frame = _rows(["09:00:00", "09:00:01", "09:00:02"], [100, 80, 80], [0, 0, 1], bid=79, ask=81)
    frame.to_csv(path, index=False)
    result = analyze_sources(SourceDiscovery([_source(path)]), Config(thresholds=(0.05,), horizons=(1,), mfe_mae_horizon=1))
    # The stale LastPrice at volume 0 cannot trigger; the new trade is inside BBO.
    assert result.events.empty
    assert int(result.quality.iloc[0]["deviation_records"]) == 1


def test_invalid_time_volume_reset_start_new_segments():
    frame = _rows(
        ["09:00:00", "09:00:01", "bad", "09:00:03", "09:00:02"],
        [100] * 5,
        [0, 1, 2, 3, 1],
    )
    prepared = prepare_group(frame, _source(Path("X2601_20260303.csv")), Config())
    assert prepared["segment_id"].tolist() == [1, 1, 2, 3, 4]
    assert prepared["has_new_trade"].tolist() == [False, True, False, False, False]
    assert int(prepared["volume_reset"].sum()) == 1


def test_recovery_late_sample_is_missing(tmp_path):
    path = tmp_path / "X2601_20260303.csv"
    frame = _rows(["09:00:00", "09:00:01", "09:00:02", "09:00:05"], [100, 100, 90, 100], [0, 1, 2, 3])
    frame.to_csv(path, index=False)
    result = analyze_sources(SourceDiscovery([_source(path)]), Config(thresholds=(0.05,), horizons=(1,), mfe_mae_horizon=1))
    event = result.events.loc[result.events["mode"] == "strict"].iloc[0]
    assert pd.isna(event["mid_after_1s"])
    assert pd.isna(event["recovery_ratio_1s"])
    row = result.summary.loc[(result.summary["mode"] == "strict") & (result.summary["side"] == "down")].iloc[0]
    assert row["recovery_valid_n_1s"] == 0
    assert row["recovery_missing_n_1s"] == 1


def test_directory_deduplicates_csv_and_daily_zip(tmp_path):
    csv_path = tmp_path / "X2601_20260303.csv"
    frame = _rows(["09:00:00"], [100], [0])
    frame.to_csv(csv_path, index=False)
    zip_path = tmp_path / "20260303.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(csv_path, "20260303/X2601_20260303.csv")
    discovery = discover_sources(tmp_path)
    assert len(discovery.selected) == 1
    assert len(discovery.duplicates) == 1
    assert discovery.selected[0].source_type == "csv"


def test_turnover_columns_do_not_affect_results():
    frame = _rows(["09:00:00", "09:00:01", "09:00:02"], [100, 100, 90], [0, 1, 2])
    source = _source(Path("X2601_20260303.csv"))
    left = prepare_group(frame, source, Config())[["deviation", "mid"]]
    frame["Turnover"] = [0, 999999, 1]
    frame["AveragePrice"] = [100, 100, 100]
    right = prepare_group(frame, source, Config())[["deviation", "mid"]]
    pd.testing.assert_frame_equal(left, right)


def test_report_is_standalone_and_handles_empty_events():
    html = render_report_html(
        pd.DataFrame(columns=["InstrumentID", "side", "mode", "threshold_pct"]),
        pd.DataFrame(),
        pd.DataFrame(),
        {},
        {"status": "ok"},
    )
    assert "Mid + LastPrice 异常检测报告" in html
    assert "<dialog" in html and "function draw" in html
    assert "JSON.parse" in html
    assert "60秒完整" not in html
    assert "mfeMaeHorizon" in html
