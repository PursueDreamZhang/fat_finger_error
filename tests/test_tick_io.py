from __future__ import annotations

import math
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from src.tick_detector.tick_io import (
    MAX_CONFIRMATION_GAP_SECONDS,
    MAX_DATA_GAP_SECONDS,
    iter_day_contract_files,
    load_contract_snapshots,
    prepare_contract_snapshots,
)


TICK_COLUMNS = [
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
    trading_day: int = 20260520,
    last_price: float = 100.0,
    volume: int = 10,
    bid_price: float = 99.98,
    ask_price: float = 100.0,
    average_price: float = 100.0,
    turnover: float = 1000000.0,
) -> dict[str, object]:
    return {
        "TradingDay": trading_day,
        "InstrumentID": instrument_id,
        "UpdateTime": update_time,
        "UpdateMillisec": millisec,
        "LastPrice": last_price,
        "Volume": volume,
        "BidPrice1": bid_price,
        "BidVolume1": 1,
        "AskPrice1": ask_price,
        "AskVolume1": 1,
        "AveragePrice": average_price,
        "Turnover": turnover,
        "OpenInterest": 100,
        "UpperLimitPrice": 200.0,
        "LowerLimitPrice": 50.0,
    }


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    pd.DataFrame(rows, columns=TICK_COLUMNS).to_csv(path, index=False)


def _au_raw(rows: list[dict[str, object]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["commodity"] = "AU"
    df["contract"] = "AU2606"
    df["parse_status"] = "ok"
    df["trade_date"] = "20260520"
    return df


def test_max_data_gap_constants_are_defined():
    assert MAX_DATA_GAP_SECONDS == 3
    assert MAX_CONFIRMATION_GAP_SECONDS == 1


def test_iter_day_contract_files_reads_directory_csvs(tmp_path):
    _write_csv(tmp_path / "au2606_20260520.csv", [_tick_row("au2606", "09:00:00", 0)])
    _write_csv(tmp_path / "ag2606_20260520.csv", [_tick_row("ag2606", "09:00:00", 0)])

    files = list(iter_day_contract_files(tmp_path))

    assert [f.file_name for f in files] == ["ag2606_20260520.csv", "au2606_20260520.csv"]
    assert all(f.source_type == "directory" for f in files)


def test_iter_day_contract_files_reads_flat_zip_csvs(tmp_path):
    csv_text = pd.DataFrame([_tick_row("au2606", "09:00:00", 0)], columns=TICK_COLUMNS).to_csv(index=False)
    zip_path = tmp_path / "20260520.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("au2606_20260520.csv", csv_text)
        zf.writestr("nested/ignore.txt", "x")

    files = list(iter_day_contract_files(zip_path))

    assert [f.file_name for f in files] == ["au2606_20260520.csv"]
    assert files[0].source_type == "zip"


def test_continuous_alias_does_not_enter_target_pool_when_instrument_matches_real_contract(tmp_path):
    real_rows = [_tick_row("AP605", "09:00:00", 0, trading_day=20260105)]
    alias_rows = [_tick_row("AP605", "09:00:00", 0, trading_day=20260105)]
    _write_csv(tmp_path / "AP605_20260105.csv", real_rows)
    _write_csv(tmp_path / "AP主力连续_20260105.csv", alias_rows)

    loaded = [load_contract_snapshots(f) for f in iter_day_contract_files(tmp_path)]
    parsed = pd.concat(loaded, ignore_index=True)
    target_pool = parsed.loc[parsed["parse_status"] == "ok", ["commodity", "contract"]].drop_duplicates()

    assert (parsed["parse_status"] == "continuous_alias").sum() == 1
    assert target_pool.to_dict("records") == [{"commodity": "AP", "contract": "AP605"}]


def test_load_contract_snapshots_parses_real_contract_metadata(tmp_path):
    _write_csv(tmp_path / "au2606_20260520.csv", [_tick_row("au2606", "09:00:00", 0)])

    [contract_file] = list(iter_day_contract_files(tmp_path))
    df = load_contract_snapshots(contract_file)

    assert df["commodity"].iloc[0] == "AU"
    assert df["contract"].iloc[0] == "AU2606"
    assert df["parse_status"].iloc[0] == "ok"
    assert df["trade_date"].iloc[0] == "20260520"


# ---------------------------------------------------------------------------
# 元数据 profile
# ---------------------------------------------------------------------------


def test_au_profile_marks_validated():
    raw = _au_raw([_tick_row("au2606", "09:01:30", 0)])
    df = prepare_contract_snapshots(raw)

    assert df["parameter_profile"].iloc[0] == "AU_V1"
    assert df["validation_status"].iloc[0] == "validated"
    assert df["tick_size"].iloc[0] == pytest.approx(0.02)
    assert df["contract_multiplier"].iloc[0] == 1000


def test_non_au_commodity_marked_unvalidated():
    raw = pd.DataFrame([_tick_row("jd2606", "09:01:30", 0)])
    raw["commodity"] = "JD"
    raw["contract"] = "JD2606"
    raw["parse_status"] = "ok"
    raw["trade_date"] = "20260520"
    df = prepare_contract_snapshots(raw)

    assert df["validation_status"].iloc[0] == "unvalidated_commodity"


# ---------------------------------------------------------------------------
# 交易日时间轴与 market_time_key
# ---------------------------------------------------------------------------


def test_market_time_key_orders_night_then_midnight_then_day():
    raw = _au_raw(
        [
            _tick_row("au2606", "00:15:00", 0),   # 午夜后,夜盘后段
            _tick_row("au2606", "21:04:35", 500),  # 夜盘开盘段
            _tick_row("au2606", "09:05:00", 0),    # 日盘
        ]
    )
    df = prepare_contract_snapshots(raw)

    assert df["display_time"].tolist() == ["21:04:35.500", "00:15:00.000", "09:05:00.000"]
    assert df["display_trade_date"].tolist() == ["20260520", "20260520", "20260520"]
    keys = df["market_time_key"].tolist()
    assert keys == sorted(keys)


def test_market_time_key_is_monotonic_within_night_session_across_midnight():
    raw = _au_raw(
        [
            _tick_row("au2606", "21:00:00", 0),
            _tick_row("au2606", "23:59:59", 500),
            _tick_row("au2606", "00:00:00", 0),
            _tick_row("au2606", "00:00:01", 0),
        ]
    )
    df = prepare_contract_snapshots(raw)
    keys = df["market_time_key"].tolist()
    assert keys == sorted(keys)
    # 午夜后第一条 cycle 应大于午夜前最后一条
    assert df["market_time_key"].iloc[2] > df["market_time_key"].iloc[1]


# ---------------------------------------------------------------------------
# 同一时间键合并
# ---------------------------------------------------------------------------


def test_same_market_time_key_rows_are_collapsed_before_diff():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:00:00", 0, last_price=100.0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "09:00:00", 0, last_price=100.2, volume=110, turnover=11000000.0),
            _tick_row("au2606", "09:00:00", 0, last_price=100.4, volume=120, turnover=12000000.0),
        ]
    )
    df = prepare_contract_snapshots(raw)

    assert len(df) == 1
    assert df["LastPrice"].iloc[0] == pytest.approx(100.4)
    assert df["Volume"].iloc[0] == 120
    assert df["snapshot_seq_start"].iloc[0] == 0
    assert df["snapshot_seq_end"].iloc[0] == 2


def test_diff_only_between_different_time_keys_after_collapse():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "09:01:31", 0, volume=130, turnover=13000000.0),
        ]
    )
    df = prepare_contract_snapshots(raw)

    assert math.isnan(df["delta_volume"].iloc[0])
    assert df["delta_volume"].iloc[1] == pytest.approx(30.0)


def test_diff_blocked_on_session_first_row_and_reset():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:00:00", 0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "13:30:00", 0, volume=200, turnover=20000000.0),
        ]
    )
    df = prepare_contract_snapshots(raw)

    # 13:30 是新 session 首条,不可差分
    assert math.isnan(df["delta_volume"].iloc[1])


def test_diff_blocked_on_non_tradable_row():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "10:20:00", 0, volume=150, turnover=15000000.0),  # 休盘
        ]
    )
    df = prepare_contract_snapshots(raw)

    assert math.isnan(df["delta_volume"].iloc[1])


def test_diff_blocked_on_volume_reset_or_zero_delta():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "09:01:31", 0, volume=50, turnover=5000000.0),   # 回退
            _tick_row("au2606", "09:01:32", 0, volume=50, turnover=5000000.0),   # 无增量
        ]
    )
    df = prepare_contract_snapshots(raw)

    assert math.isnan(df["delta_volume"].iloc[1])
    assert math.isnan(df["delta_volume"].iloc[2])


# ---------------------------------------------------------------------------
# 开盘保护
# ---------------------------------------------------------------------------


def test_open_guard_protects_first_59_seconds_after_session_open():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:00:00", 0),    # 首条,不差分
            _tick_row("au2606", "09:00:30", 0),     # 60s 保护期内
            _tick_row("au2606", "09:01:00", 0),     # 60s 后允许
            _tick_row("au2606", "10:30:30", 0),     # 10:30 重开保护期内
            _tick_row("au2606", "10:31:00", 0),     # 10:31 允许
            _tick_row("au2606", "13:30:30", 0),     # 13:30 重开保护期内
            _tick_row("au2606", "21:00:30", 0),     # 夜盘保护期内
            _tick_row("au2606", "21:01:00", 0),     # 夜盘 60s 后
        ]
    )
    df = prepare_contract_snapshots(raw)
    guard_by_time = dict(zip(df["display_time"], df["is_open_protected"]))

    assert guard_by_time["09:00:00.000"] is True       # 首条保护
    assert guard_by_time["09:00:30.000"] is True       # 60s 保护期内
    assert guard_by_time["09:01:00.000"] is False      # 60s 后允许
    assert guard_by_time["10:30:30.000"] is True       # 10:30 重开保护期内
    assert guard_by_time["10:31:00.000"] is False      # 60s 后允许
    assert guard_by_time["13:30:30.000"] is True       # 13:30 重开保护期内
    assert guard_by_time["21:00:30.000"] is True       # 夜盘保护期内
    assert guard_by_time["21:01:00.000"] is False      # 夜盘 60s 后


# ---------------------------------------------------------------------------
# interval_vwap
# ---------------------------------------------------------------------------


def test_interval_vwap_uses_delta_turnover_over_delta_volume_divided_by_multiplier():
    # AU multiplier=1000; AveragePrice = Turnover/Volume（元/手单位）
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0, average_price=100000.0),
            _tick_row(
                "au2606", "09:01:31", 0,
                last_price=99.0, volume=170, turnover=16700000.0, average_price=98235.294,
            ),
        ]
    )
    df = prepare_contract_snapshots(raw)
    row = df.iloc[1]

    # delta_turnover=6700000, delta_volume=70 -> 6700000/70/1000 = 95.71...
    assert row["delta_volume"] == pytest.approx(70.0)
    assert row["delta_turnover"] == pytest.approx(6700000.0)
    assert row["interval_vwap"] == pytest.approx(95.714285, rel=1e-4)


def test_average_price_unit_check_only_not_counter_evidence():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0, average_price=100000.0),
            _tick_row(
                "au2606", "09:01:31", 0,
                volume=170, turnover=16700000.0, average_price=98235.294,
            ),
        ]
    )
    df = prepare_contract_snapshots(raw)

    # avg_trade_price_enabled 只校验单位（AveragePrice=Turnover/Volume）
    assert "avg_trade_price_enabled" in df.columns
    assert bool(df["avg_trade_price_enabled"].iloc[1]) is True


# ---------------------------------------------------------------------------
# session_state / data_quality_flags
# ---------------------------------------------------------------------------


def test_session_state_and_is_tradable_session_classified():
    raw = _au_raw(
        [
            _tick_row("au2606", "08:59:59", 0),
            _tick_row("au2606", "09:00:00", 0),
            _tick_row("au2606", "10:20:00", 0),
            _tick_row("au2606", "10:30:00", 0),
            _tick_row("au2606", "11:30:00", 0),
            _tick_row("au2606", "13:30:00", 0),
            _tick_row("au2606", "15:00:00", 0),
            _tick_row("au2606", "21:00:00", 0),
            _tick_row("au2606", "00:30:00", 0),
        ]
    )
    df = prepare_contract_snapshots(raw)
    state_by_time = dict(zip(df["display_time"], df["session_state"]))
    tradable_by_time = dict(zip(df["display_time"], df["is_tradable_session"]))

    assert state_by_time["08:59:59.000"] == "pre_open_snapshot"
    assert state_by_time["09:00:00.000"] == "continuous_trading"
    assert state_by_time["10:20:00.000"] == "intermission"
    assert state_by_time["10:30:00.000"] == "continuous_trading"
    assert state_by_time["11:30:00.000"] == "lunch"
    assert state_by_time["13:30:00.000"] == "continuous_trading"
    assert state_by_time["15:00:00.000"] == "closed_break"
    assert state_by_time["21:00:00.000"] == "night_trading"
    assert state_by_time["00:30:00.000"] == "night_trading"

    assert tradable_by_time["08:59:59.000"] is False
    assert tradable_by_time["09:00:00.000"] is True
    assert tradable_by_time["10:20:00.000"] is False
    assert tradable_by_time["10:30:00.000"] is True
    assert tradable_by_time["11:30:00.000"] is False
    assert tradable_by_time["13:30:00.000"] is True
    assert tradable_by_time["15:00:00.000"] is False
    assert tradable_by_time["21:00:00.000"] is True
    assert tradable_by_time["00:30:00.000"] is True


def test_display_fields_preserved_from_original():
    raw = _au_raw(
        [_tick_row("au2606", "21:04:35", 500, trading_day=20260520)]
    )
    df = prepare_contract_snapshots(raw)

    assert df["display_trade_date"].iloc[0] == "20260520"
    assert df["display_time"].iloc[0] == "21:04:35.500"
    # 不含内部 cycle_millis 列
    assert "cycle_millis" not in df.columns


def test_data_quality_flags_column_present():
    raw = _au_raw([_tick_row("au2606", "09:01:30", 0)])
    df = prepare_contract_snapshots(raw)
    assert "data_quality_flags" in df.columns
