import pandas as pd

from src.daily_screen.scoring import score_candidates


def test_scoring_outputs_ace_columns():
    rows = []
    dates = pd.date_range("2024-01-01", periods=12, freq="D")
    for trade_date in dates:
        rows.extend(
            [
                {
                    "commodity": "AU",
                    "contract": "AU2406",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 1000,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
                {
                    "commodity": "AU",
                    "contract": "AU2407",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 900,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
            ]
        )

    scored = score_candidates(pd.DataFrame(rows))

    assert {
        "A_score",
        "C_score",
        "E_score",
        "candidate_score",
        "candidate_level",
        "active_peer_count",
        "peer_comparability_weak_flag",
        "target_liquidity_weak_flag",
    } <= set(scored.columns)


def test_invalid_sample_becomes_zero_score():
    invalid_input_df = pd.DataFrame(
        {
            "commodity": ["AU", "AU"],
            "contract": ["AU2406", "AU2407"],
            "trade_date": [pd.Timestamp("2024-01-12")] * 2,
            "open": [100.0, 100.0],
            "high": [101.0, 101.0],
            "low": [80.0, 99.0],
            "close": [100.0, 100.0],
            "pre_close": [99.0, 99.0],
            "volume": [1000, 900],
            "sample_status": ["invalid_low_liquidity", "valid"],
            "main_reference_contract": ["AU2406", "AU2406"],
        }
    )

    scored = score_candidates(invalid_input_df)
    target = scored.loc[scored["contract"] == "AU2406"].iloc[0]

    assert target["A_score"] == 0
    assert target["C_score"] == 0
    assert target["E_score"] == -20
    assert target["candidate_score"] == 0
    assert target["candidate_level"] == "none"


def test_zero_active_peer_is_invalid():
    single_contract_df = pd.DataFrame(
        {
            "commodity": ["AU"] * 12,
            "contract": ["AU2406"] * 12,
            "trade_date": pd.date_range("2024-01-01", periods=12, freq="D"),
            "open": [100.0] * 12,
            "high": [101.0] * 12,
            "low": [99.0] * 12,
            "close": [100.0] * 12,
            "pre_close": [99.0] * 12,
            "volume": [1000] * 12,
            "sample_status": ["valid"] * 12,
            "main_reference_contract": ["AU2406"] * 12,
        }
    )

    scored = score_candidates(single_contract_df)
    target = scored.iloc[-1]

    assert target["active_peer_count"] == 0
    assert target["E_score"] == -20
    assert target["candidate_score"] == 0
    assert target["candidate_level"] == "none"


def test_one_active_peer_is_scoreable_but_low_confidence():
    rows = []
    dates = pd.date_range("2024-01-01", periods=12, freq="D")
    for trade_date in dates:
        rows.extend(
            [
                {
                    "commodity": "AU",
                    "contract": "AU2406",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0 if trade_date != pd.Timestamp("2024-01-12") else 60.0,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 1000,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
                {
                    "commodity": "AU",
                    "contract": "AU2407",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.2,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 900,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
            ]
        )

    scored = score_candidates(pd.DataFrame(rows))
    target = scored[(scored["contract"] == "AU2406") & (scored["trade_date"] == pd.Timestamp("2024-01-12"))].iloc[0]

    assert target["active_peer_count"] == 1
    assert target["E_score"] == -10
    assert target["candidate_score"] > 0


def test_target_liquidity_weak_flag_requires_peer_volume_median_rule():
    rows = []
    dates = pd.date_range("2024-01-01", periods=12, freq="D")
    for trade_date in dates:
        weak_day = trade_date == pd.Timestamp("2024-01-12")
        rows.extend(
            [
                {
                    "commodity": "AU",
                    "contract": "AU2406",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 98.0,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 1000 if not weak_day else 10,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
                {
                    "commodity": "AU",
                    "contract": "AU2407",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 98.5,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 900,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
                {
                    "commodity": "AU",
                    "contract": "AU2408",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 98.8,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 800,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2406",
                },
            ]
        )

    scored = score_candidates(pd.DataFrame(rows))
    target = scored[(scored["contract"] == "AU2406") & (scored["trade_date"] == pd.Timestamp("2024-01-12"))].iloc[0]

    assert target["target_liquidity_weak_flag"]
    assert target["E_score"] == -10


def test_c_penalizes_isolated_spike_more_than_broad_market_wave():
    rows = []
    dates = pd.date_range("2024-01-01", periods=13, freq="D")
    contracts = ["AU2402", "AU2403", "AU2404", "AU2405", "AU2406"]

    for trade_date in dates:
        broad_wave_day = trade_date == pd.Timestamp("2024-01-11")
        isolated_spike_day = trade_date == pd.Timestamp("2024-01-13")

        for idx, contract in enumerate(contracts):
            base_close = 100.0
            volume = 1200 - idx * 100
            high = 101.0 + idx * 0.02
            low = 99.0 - idx * 0.02

            if contract == "AU2403" and not broad_wave_day and not isolated_spike_day:
                high = 103.5
                low = 99.0

            if broad_wave_day:
                high = 110.0 + idx * 0.1
                low = 90.0 - idx * 0.1
                if contract == "AU2403":
                    high = 111.0
                    low = 90.0

            if isolated_spike_day:
                high = 101.0 + idx * 0.02
                low = 99.0 - idx * 0.02
                if contract == "AU2403":
                    high = 101.0
                    low = 80.0

            rows.append(
                {
                    "commodity": "AU",
                    "contract": contract,
                    "trade_date": trade_date,
                    "open": base_close,
                    "high": high,
                    "low": low,
                    "close": base_close,
                    "pre_close": base_close,
                    "volume": volume,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2402",
                }
            )

    scored = score_candidates(pd.DataFrame(rows))
    broad = scored[(scored["contract"] == "AU2403") & (scored["trade_date"] == pd.Timestamp("2024-01-11"))].iloc[0]
    spike = scored[(scored["contract"] == "AU2403") & (scored["trade_date"] == pd.Timestamp("2024-01-13"))].iloc[0]

    assert broad["C_score"] < spike["C_score"]


def test_scoring_uses_prior_valid_history_not_recent_rows_only():
    rows = []
    dates = pd.date_range("2024-01-01", periods=32, freq="D")
    for index, trade_date in enumerate(dates, start=1):
        target_is_valid = index <= 11 or index == 32
        target_low = 99.0
        if index == 32:
            target_low = 60.0

        rows.extend(
            [
                {
                    "commodity": "AU",
                    "contract": "AU2406",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": target_low,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 1000 if target_is_valid else 0,
                    "sample_status": "valid" if target_is_valid else "invalid_low_liquidity",
                    "main_reference_contract": "AU2407",
                },
                {
                    "commodity": "AU",
                    "contract": "AU2407",
                    "trade_date": trade_date,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.2,
                    "close": 100.0,
                    "pre_close": 99.0,
                    "volume": 900,
                    "sample_status": "valid",
                    "main_reference_contract": "AU2407",
                },
            ]
        )

    scored = score_candidates(pd.DataFrame(rows))
    target = scored[(scored["contract"] == "AU2406") & (scored["trade_date"] == dates[-1])].iloc[0]

    assert target["A_score"] > 0
    assert target["range_q90"] > 0
