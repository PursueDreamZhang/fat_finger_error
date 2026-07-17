from __future__ import annotations

import math
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from src.tick_detector.tick_io import (
    COMMODITY_PROFILES,
    MAX_CONFIRMATION_GAP_SECONDS,
    MAX_DATA_GAP_SECONDS,
    iter_day_contract_files,
    load_contract_snapshots,
    load_daily_bounds,
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
    upper_limit: float = 200.0,
    lower_limit: float = 50.0,
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
        "UpperLimitPrice": upper_limit,
        "LowerLimitPrice": lower_limit,
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


def _ap_raw(rows: list[dict[str, object]]) -> pd.DataFrame:
    """AP：tick_size=1、contract_multiplier=1，均价=ΔTurnover/ΔVolume，便于复刻真实计数器毛刺。"""
    df = pd.DataFrame(rows)
    df["commodity"] = "AP"
    df["contract"] = "AP610"
    df["parse_status"] = "ok"
    df["trade_date"] = "20260519"
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


def test_experimental_profiles_cover_81_approved_commodities():
    excluded = {"BB", "JR", "PM", "RI", "WH", "ZC"}

    assert len(COMMODITY_PROFILES) == 81
    assert excluded.isdisjoint(COMMODITY_PROFILES)
    assert COMMODITY_PROFILES["CU"]["tick_size"] == 10
    assert COMMODITY_PROFILES["CU"]["contract_multiplier"] == 5
    assert COMMODITY_PROFILES["AG"]["tick_size"] == 1
    assert COMMODITY_PROFILES["AG"]["contract_multiplier"] == 15
    assert all(profile["validation_status"] == "validated" for profile in COMMODITY_PROFILES.values())


def test_unconfigured_commodity_marked_unvalidated():
    raw = pd.DataFrame([_tick_row("xx2606", "09:01:30", 0)])
    raw["commodity"] = "XX"
    raw["contract"] = "XX2606"
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


def test_single_row_after_collapsed_group_keeps_snapshot_sequence_range():
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "09:01:30", 0, volume=101, turnover=10100000.0),
            _tick_row("au2606", "09:01:31", 0, volume=102, turnover=10200000.0),
        ]
    )

    df = prepare_contract_snapshots(raw)

    assert df["snapshot_seq_start"].tolist() == [0, 2]
    assert df["snapshot_seq_end"].tolist() == [1, 2]


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


def test_diff_blocked_on_intra_session_data_gap_over_3s():
    """同一 session 内相邻时间键 gap > MAX_DATA_GAP_SECONDS(3s) 视为数据断点,不可差分。"""
    raw = _au_raw(
        [
            _tick_row("au2606", "09:01:30", 0, volume=100, turnover=10000000.0),
            _tick_row("au2606", "09:01:31", 0, volume=110, turnover=11000000.0),   # gap=1s 连续
            _tick_row("au2606", "09:01:36", 0, volume=200, turnover=20000000.0),   # gap=5s > 3s 断点
        ]
    )
    df = prepare_contract_snapshots(raw)
    by_time = dict(zip(df["display_time"], df["delta_volume"]))

    assert by_time["09:01:31.000"] == pytest.approx(10.0)   # gap=1s 连续,可差分
    assert math.isnan(by_time["09:01:36.000"])               # gap=5s > 3s,断点不可差分


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
# 成交额计数器守卫（累计 Turnover 回退 → 失效区间均价）
# 复刻 AP610/20260519 13:36:30 真实毛刺：累计成交额秒内先回退再跳升，
# 合并后 ΔTurnover 仍为正但严重偏小，算出离谱的 interval_vwap。
# ---------------------------------------------------------------------------


def test_interval_vwap_invalidated_when_cumulative_turnover_drops_within_frame():
    raw = _ap_raw(
        [
            _tick_row("AP610", "09:01:30", 0, last_price=7394, volume=66658, turnover=493935780.0, average_price=7410.0, upper_limit=8069, lower_limit=6873),
            _tick_row("AP610", "09:01:31", 0, last_price=7393, volume=66672, turnover=494039520.0, average_price=7410.0, upper_limit=8069, lower_limit=6873),
            _tick_row("AP610", "09:01:32", 0, last_price=7393, volume=66679, turnover=494024711.0, average_price=7409.0, upper_limit=8069, lower_limit=6873),  # 累计成交额回退
            _tick_row("AP610", "09:01:32", 0, last_price=7393, volume=66687, turnover=494083983.0, average_price=7409.0, upper_limit=8069, lower_limit=6873),
        ]
    )
    df = prepare_contract_snapshots(raw)
    by_time = dict(zip(df["display_time"], df["interval_vwap"]))
    flag_by_time = dict(zip(df["display_time"], df["data_quality_flags"]))

    # 干净帧均价正常（ΔV=14, ΔT=103740 -> 7410）
    assert by_time["09:01:31.000"] == pytest.approx(7410.0, rel=1e-4)
    # 累计成交额回退帧：ΔT 被失效，区间均价不产生
    assert math.isnan(by_time["09:01:32.000"])
    assert "turnover_counter_desync" in flag_by_time["09:01:32.000"]
    # 干净帧不应被误标
    assert "turnover_counter_desync" not in flag_by_time["09:01:31.000"]


# ---------------------------------------------------------------------------
# 价位带兜底（无日线时：区间均价必须落在涨跌停价内）
# 真实成交（含接近涨停/跌停的尖刺）必在 [跌停, 涨停] 内；越界必为成交额污染。
# ---------------------------------------------------------------------------


def test_interval_vwap_invalidated_beyond_limit_band_but_near_limit_spike_kept():
    # AP610：tick=1, multiplier=1, 涨跌停 [6873, 8069]
    raw = _ap_raw(
        [
            _tick_row("AP610", "09:01:30", 0, last_price=7400, volume=1000, turnover=7400000.0, average_price=7400.0, upper_limit=8069, lower_limit=6873),
            # 干净帧：vwap=7400，带内保留
            _tick_row("AP610", "09:01:31", 0, last_price=7400, volume=1005, turnover=7437000.0, average_price=7400.0, upper_limit=8069, lower_limit=6873),
            # 真实尖刺：ΔV=15, ΔT=120000 -> vwap=8000，接近涨停 8069 仍带内 -> 必须保留
            _tick_row("AP610", "09:01:32", 0, last_price=7400, volume=1020, turnover=7557000.0, average_price=7408.82, upper_limit=8069, lower_limit=6873),
            # 污染：ΔV=15, ΔT=135000 -> vwap=9000，突破涨停 8069 -> 失效
            _tick_row("AP610", "09:01:33", 0, last_price=7400, volume=1035, turnover=7692000.0, average_price=7431.88, upper_limit=8069, lower_limit=6873),
        ]
    )
    df = prepare_contract_snapshots(raw)
    by_time = dict(zip(df["display_time"], df["interval_vwap"]))
    flag_by_time = dict(zip(df["display_time"], df["data_quality_flags"]))

    # 干净帧保留
    assert by_time["09:01:31.000"] == pytest.approx(7400.0, rel=1e-4)
    # 接近涨停的真实尖刺保留（涨跌停带不误杀贴近边界的真实成交）
    assert by_time["09:01:32.000"] == pytest.approx(8000.0, rel=1e-4)
    assert "vwap_beyond_limit" not in flag_by_time["09:01:32.000"]
    # 突破涨跌停的均价失效
    assert math.isnan(by_time["09:01:33.000"])
    assert "vwap_beyond_limit" in flag_by_time["09:01:33.000"]


# ---------------------------------------------------------------------------
# 价位带兜底（有日线时：日线 [Low,High]±容差，比涨跌停带更紧，抓中度污染）
# ---------------------------------------------------------------------------


def _ap610_rows():
    """AP610：tick=1, multiplier=1, 涨跌停 [6873,8069]。vwap 依次 7400 / 7500 / 7457。"""
    return _ap_raw(
        [
            _tick_row("AP610", "09:01:30", 0, last_price=7400, volume=1000, turnover=7400000.0, average_price=7400.0, upper_limit=8069, lower_limit=6873),
            # 干净：vwap=7400（日线[7356,7455]内）
            _tick_row("AP610", "09:01:31", 0, last_price=7400, volume=1005, turnover=7437000.0, average_price=7400.0, upper_limit=8069, lower_limit=6873),
            # 中度污染：vwap=7500（在涨跌停内、但超日线high 7455+3容差=7458）-> 日线抓、涨跌停漏
            _tick_row("AP610", "09:01:32", 0, last_price=7400, volume=1020, turnover=7549500.0, average_price=7401.47, upper_limit=8069, lower_limit=6873),
            # 容差边界：vwap=7457（超日线high 7455 仅 2 跳，≤3 容差）-> 放行
            _tick_row("AP610", "09:01:33", 0, last_price=7400, volume=1035, turnover=7661355.0, average_price=7402.28, upper_limit=8069, lower_limit=6873),
        ]
    )


def test_daily_bound_catches_within_limit_corruption_and_respects_tolerance():
    # 日线 AP2610 [7356,7455], pre_settle=7471（=涨跌停中点，同日校验通过）
    daily_bounds = {"AP2610": (7356.0, 7455.0, 7471.0)}
    df = prepare_contract_snapshots(_ap610_rows(), daily_bounds=daily_bounds)
    by_time = dict(zip(df["display_time"], df["interval_vwap"]))
    flag_by_time = dict(zip(df["display_time"], df["data_quality_flags"]))

    # 干净帧保留
    assert by_time["09:01:31.000"] == pytest.approx(7400.0, rel=1e-4)
    # 中度污染（涨跌停内、日线外）被日线边界失效
    assert math.isnan(by_time["09:01:32.000"])
    assert "vwap_beyond_daily" in flag_by_time["09:01:32.000"]
    # 容差内（超日线high仅2跳）放行
    assert by_time["09:01:33.000"] == pytest.approx(7457.0, rel=1e-4)
    assert "vwap_beyond_daily" not in flag_by_time["09:01:33.000"]


def test_daily_bound_falls_back_to_limit_when_sameday_check_fails():
    # pre_settle=9999 与涨跌停中点 7471 不符 -> 同日校验失败 -> 退回涨跌停带
    daily_bounds = {"AP2610": (7356.0, 7455.0, 9999.0)}
    df = prepare_contract_snapshots(_ap610_rows(), daily_bounds=daily_bounds)
    by_time = dict(zip(df["display_time"], df["interval_vwap"]))
    flag_by_time = dict(zip(df["display_time"], df["data_quality_flags"]))

    # 7500 在涨跌停 [6873,8069] 内 -> 回退后不再失效（日线边界未被采用）
    assert by_time["09:01:32.000"] == pytest.approx(7500.0, rel=1e-4)
    assert "vwap_beyond_daily" not in flag_by_time["09:01:32.000"]


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


# ---------------------------------------------------------------------------
# load_daily_bounds：日线 parquet → 归一化 {合约: (low,high,pre_settle)}
# ---------------------------------------------------------------------------


def _write_daily_parquet(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cols = ["code", "date", "pre_close", "pre_settle", "open", "high", "low", "close", "settle", "vol"]
    pd.DataFrame(rows, columns=cols).to_parquet(path)


def test_load_daily_bounds_builds_normalized_map(tmp_path):
    _write_daily_parquet(
        tmp_path / "2026" / "20260519.parquet",
        [
            {"code": "AP2610.CZCE", "date": 20260519, "pre_close": 7471, "pre_settle": 7471, "open": 7400, "high": 7455, "low": 7356, "close": 7400, "settle": 7400, "vol": 100},
            {"code": "au2606.SHFE", "date": 20260519, "pre_close": 1000, "pre_settle": 1000, "open": 1000, "high": 1006, "low": 995, "close": 1000, "settle": 1000, "vol": 100},
            {"code": "AP.CZCE", "date": 20260519, "pre_close": 7471, "pre_settle": 7471, "open": 0, "high": 0, "low": 0, "close": 0, "settle": 7471, "vol": 0},  # 连续合约+空柱 -> 排除
        ],
    )
    m = load_daily_bounds("20260519", daily_root=str(tmp_path))
    assert m == {
        "AP2610": (7356.0, 7455.0, 7471.0),
        "AU2606": (995.0, 1006.0, 1000.0),
    }


def test_load_daily_bounds_returns_none_when_file_missing(tmp_path):
    assert load_daily_bounds("20260101", daily_root=str(tmp_path)) is None
