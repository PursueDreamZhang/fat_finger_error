from __future__ import annotations

import math
import zipfile
from pathlib import Path

import pandas as pd

from src.tick_detector.tick_io import (
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


def test_prepare_contract_snapshots_filters_to_day_session_and_preserves_snapshot_seq_order():
    raw = pd.DataFrame(
        [
            _tick_row("au2606", "08:59:59", 0, last_price=100.0, volume=10, turnover=1000000.0),
            _tick_row("au2606", "09:00:00", 0, last_price=100.0, volume=10, turnover=1000000.0),
            _tick_row("au2606", "09:00:00", 0, last_price=100.2, volume=12, turnover=1200400.0),
            _tick_row("au2606", "10:30:00", 0, last_price=100.4, volume=14, turnover=1401200.0),
        ]
    )
    raw["commodity"] = "AU"
    raw["contract"] = "AU2606"
    raw["parse_status"] = "ok"
    raw["trade_date"] = "20260520"

    df = prepare_contract_snapshots(raw)

    assert df["snapshot_seq"].tolist() == [1, 2, 3]
    assert df["is_tradable_session"].all()
    assert df["session_state"].eq("continuous_trading").all()
    assert math.isnan(df["delta_volume"].iloc[0])
    assert df["delta_volume"].iloc[1] == 2
    assert math.isnan(df["delta_volume"].iloc[2])
    assert df["night_session_coverage"].tolist() == [True, True, True]


def test_prepare_contract_snapshots_disables_avg_trade_trigger_when_multiplier_check_fails():
    raw = pd.DataFrame(
        [
            _tick_row("au2606", "09:00:00", 0, last_price=100.0, volume=10, average_price=100.0, turnover=1000000.0),
            _tick_row("au2606", "09:00:01", 0, last_price=100.2, volume=12, average_price=100.2, turnover=1000201.0),
        ]
    )
    raw["commodity"] = "AU"
    raw["contract"] = "AU2606"
    raw["parse_status"] = "ok"
    raw["trade_date"] = "20260520"

    df = prepare_contract_snapshots(raw)

    assert bool(df["avg_trade_price_enabled"].iloc[1]) is False
    assert pd.isna(df["snapshot_avg_trade_price"].iloc[1])
