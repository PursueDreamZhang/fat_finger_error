from __future__ import annotations

import pytest
import pandas as pd

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


def test_run_detection_writes_event_outputs_and_keeps_peer_loading(tmp_path):
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()

    def row(instrument_id: str, update_time: str, millisec: int, last_price: float, volume: int, bid: float, ask: float, turnover: float) -> dict[str, object]:
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

    pd.DataFrame(
        [
            row("au2606", "09:00:00", 0, 100.00, 10, 100.00, 100.00, 1000000.0),
            row("au2606", "09:00:03", 0, 99.90, 12, 99.90, 99.90, 1200000.0),
        ]
    ).to_csv(day_dir / "au2606_20260520.csv", index=False)
    pd.DataFrame(
        [
            row("au2608", "09:00:00", 0, 100.00, 20, 100.00, 100.00, 2000000.0),
            row("au2608", "09:00:03", 0, 100.04, 22, 100.04, 100.04, 2200880.0),
        ]
    ).to_csv(day_dir / "au2608_20260520.csv", index=False)
    pd.DataFrame(
        [
            row("au2610", "09:00:00", 0, 100.00, 18, 100.00, 100.00, 1800000.0),
            row("au2610", "09:00:03", 0, 100.02, 20, 100.02, 100.02, 2000400.0),
        ]
    ).to_csv(day_dir / "au2610_20260520.csv", index=False)

    output_dir = tmp_path / "out"
    result = run_detection(
        tick_day_path=str(day_dir),
        commodity="AU",
        contract="AU2606",
        output_dir=str(output_dir),
    )

    csv_path = output_dir / "tick_events.csv"
    html_path = output_dir / "event_replay.html"
    assert csv_path.exists()
    assert html_path.exists()
    assert result["tick_events_csv"] == str(csv_path)
    events = pd.read_csv(csv_path)
    assert set(
        [
            "trade_date",
            "commodity",
            "contract",
            "event_time",
            "event_start_time",
            "event_end_time",
            "event_low_price",
            "event_volume",
            "event_depth_ticks",
            "recovery_denominator_ticks",
            "reference_contract_count",
            "recovery_label",
        ]
    ).issubset(events.columns)
    assert events["contract"].eq("AU2606").all()
