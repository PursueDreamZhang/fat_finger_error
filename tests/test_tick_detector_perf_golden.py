from __future__ import annotations

import json
import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from run_tick_detector import run_detection
from src.tick_detector.reference_selection import (
    attach_fair_price_metrics,
    select_reference_contracts,
)
from src.tick_detector.tick_io import (
    COMMODITY_PROFILES,
    iter_day_contract_files,
    load_contract_snapshots,
    prepare_contract_snapshots,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
JD_REAL_DATA_PATH = ROOT / "data/tick2026/202605/20260520"

_TICK_COLUMNS = [
    "TradingDay", "InstrumentID", "UpdateTime", "UpdateMillisec", "LastPrice",
    "Volume", "BidPrice1", "BidVolume1", "AskPrice1", "AskVolume1",
    "AveragePrice", "Turnover", "OpenInterest", "UpperLimitPrice", "LowerLimitPrice",
]

_ENRICHED_COLUMNS = [
    "market_time_key", "fair_price", "fair_uncertainty_ticks", "fair_price_reliable",
    "valid_peer_count", "peer_contracts", "last_threshold_ticks", "vwap_threshold_ticks",
    "noise_sample_count", "noise_time_span_seconds", "noise_history_reliable",
    "last_noise_median", "vwap_noise_median", "last_noise_robust_sigma",
    "vwap_noise_robust_sigma", "execution_depth_robust_sigma", "reference_blocked_reason",
]


def _tick_row(
    instrument_id: str,
    update_time: str,
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
        "UpdateMillisec": 0,
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


def _build_synthetic_day(day_dir: Path) -> None:
    target_rows: list[dict[str, object]] = []
    peer_a_rows: list[dict[str, object]] = []
    peer_b_rows: list[dict[str, object]] = []
    for sec in range(320):
        total_sec = 9 * 3600 + 30 * 60 + sec
        update_time = f"{total_sec // 3600:02d}:{total_sec // 60 % 60:02d}:{total_sec % 60:02d}"
        if sec < 300:
            target_price, target_volume, target_turnover = 100.0, 10 + sec, (10 + sec) * 1_000_000.0
        else:
            target_price = 95.0
            target_volume = 10 + sec
            target_turnover = 300_000_000.0 + target_volume * target_price * 1_000.0
        target_rows.append(
            _tick_row(
                "au2606", update_time, last_price=target_price, volume=target_volume,
                bid=target_price - 0.02, ask=target_price, turnover=target_turnover,
            )
        )
        peer_a_rows.append(
            _tick_row("au2608", update_time, volume=20 + sec, turnover=(20 + sec) * 1_000_000.0)
        )
        peer_b_rows.append(
            _tick_row("au2610", update_time, volume=18 + sec, turnover=(18 + sec) * 1_000_000.0)
        )
    day_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(day_dir / "au2606_20260520.csv", target_rows)
    _write_csv(day_dir / "au2608_20260520.csv", peer_a_rows)
    _write_csv(day_dir / "au2610_20260520.csv", peer_b_rows)


def _load_prepared_frames(day_path: Path, commodity: str) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for contract_file in iter_day_contract_files(day_path):
        raw = load_contract_snapshots(contract_file)
        if raw.empty or raw["parse_status"].iloc[0] != "ok":
            continue
        if str(raw["commodity"].iloc[0]) != commodity:
            continue
        prepared = prepare_contract_snapshots(raw)
        if not prepared.empty:
            frames[str(prepared["contract"].iloc[0])] = prepared
    return frames


def _enriched_snapshot(frame: pd.DataFrame) -> pd.DataFrame:
    snapshot = frame[_ENRICHED_COLUMNS].copy()
    for column in snapshot.columns:
        if snapshot[column].dtype == "object":
            snapshot[column] = snapshot[column].map(lambda value: "<NA>" if pd.isna(value) else str(value))
    return snapshot


def _enriched_digest(frame: pd.DataFrame) -> str:
    snapshot = _enriched_snapshot(frame)
    row_hashes = pd.util.hash_pandas_object(snapshot, index=False).to_numpy(dtype=np.uint64)
    return hashlib.sha256(row_hashes.tobytes()).hexdigest()


def _assert_event_csv_matches(actual_path: Path, golden_path: Path) -> None:
    actual = pd.read_csv(actual_path)
    golden = pd.read_csv(golden_path)
    assert list(actual.columns) == list(golden.columns)
    key_columns = [column for column in ("合约", "事件时间", "事件编号") if column in actual.columns]
    actual = actual.sort_values(key_columns, kind="stable").reset_index(drop=True)
    golden = golden.sort_values(key_columns, kind="stable").reset_index(drop=True)
    assert len(actual) == len(golden)
    for column in actual.columns:
        if pd.api.types.is_numeric_dtype(actual[column]) and pd.api.types.is_numeric_dtype(golden[column]):
            assert np.allclose(
                actual[column].to_numpy(dtype=float),
                golden[column].to_numpy(dtype=float),
                rtol=1e-6,
                atol=1e-9,
                equal_nan=True,
            ), column
        else:
            pd.testing.assert_series_equal(
                actual[column].astype(str), golden[column].astype(str), check_names=False
            )


def _event_digest(path: Path) -> str:
    events = pd.read_csv(path)
    key_columns = [column for column in ("合约", "事件时间", "事件编号") if column in events.columns]
    events = events.sort_values(key_columns, kind="stable").reset_index(drop=True)
    payload = events.to_csv(index=False, lineterminator="\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_synthetic_enriched_and_event_outputs_match_golden(tmp_path):
    day_dir = tmp_path / "20260520"
    _build_synthetic_day(day_dir)
    frames = _load_prepared_frames(day_dir, "AU")
    target = frames["AU2606"]
    refs = {code: frames[code] for code in select_reference_contracts(frames, "AU2606")}
    enriched = attach_fair_price_metrics(
        target,
        refs,
        tick_size=float(COMMODITY_PROFILES["AU"]["tick_size"]),
    )
    assert _enriched_digest(enriched) == json.loads(
        (FIXTURES / "golden_synthetic_enriched.json").read_text(encoding="utf-8")
    )["digest"]

    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir), commodity="AU", contract="AU2606", output_dir=str(output_dir)
    )
    _assert_event_csv_matches(
        output_dir / "tick_candidate_events.csv", FIXTURES / "golden_synthetic_events.csv"
    )


@pytest.mark.skipif(not (JD_REAL_DATA_PATH / "jd2606_20260520.csv").exists(), reason="JD 真数据不可用")
def test_slow_jd_enriched_and_event_outputs_match_golden(tmp_path):
    frames = _load_prepared_frames(JD_REAL_DATA_PATH, "JD")
    assert frames
    digests: dict[str, str] = {}
    for target_code, target in sorted(frames.items()):
        refs = {code: frames[code] for code in select_reference_contracts(frames, target_code) if code in frames}
        enriched = attach_fair_price_metrics(target, refs, tick_size=float(target["tick_size"].iloc[0]))
        digests[target_code] = _enriched_digest(enriched)
    golden_digests = json.loads((FIXTURES / "golden_jd_enriched.json").read_text(encoding="utf-8"))
    assert digests == golden_digests

    output_dir = tmp_path / "jd-20260520"
    run_detection(tick_day_path=str(JD_REAL_DATA_PATH), commodities="JD", output_dir=str(output_dir))
    assert _event_digest(output_dir / "tick_candidate_events.csv") == json.loads(
        (FIXTURES / "golden_jd_events.json").read_text(encoding="utf-8")
    )["digest"]
