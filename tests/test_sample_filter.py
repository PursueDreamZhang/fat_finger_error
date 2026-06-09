import pandas as pd

from src.daily_screen.sample_filter import assign_sample_status


def test_marks_invalid_basic_data_when_ohlc_relationship_is_broken():
    daily_bar = pd.DataFrame(
        {
            "trade_date": ["2024-01-02"],
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "open": [10.0],
            "high": [10.0],
            "low": [12.0],
            "close": [10.5],
            "pre_close": [9.5],
            "volume": [100],
        }
    )
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "listed_date": ["2023-01-01"],
            "last_trade_date": ["2024-12-31"],
            "delivery_month": ["2406"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    assert filtered.loc[0, "sample_status"] == "invalid_basic_data"


def test_marks_invalid_low_liquidity_when_volume_is_zero():
    daily_bar = pd.DataFrame(
        {
            "trade_date": ["2024-01-02"],
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "open": [10.0],
            "high": [11.0],
            "low": [9.0],
            "close": [10.5],
            "pre_close": [9.5],
            "volume": [0],
        }
    )
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "listed_date": ["2023-01-01"],
            "last_trade_date": ["2024-12-31"],
            "delivery_month": ["2406"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    assert filtered.loc[0, "sample_status"] == "invalid_low_liquidity"


def test_marks_invalid_low_liquidity_when_recent_active_days_are_less_than_ten():
    daily_bar = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=12, freq="D"),
            "commodity": ["AU"] * 12,
            "contract": ["AU2406"] * 12,
            "open": [10.0] * 12,
            "high": [11.0] * 12,
            "low": [9.0] * 12,
            "close": [10.5] * 12,
            "pre_close": [9.5] * 12,
            "volume": [100, 100, 100, 100, 100, 100, 0, 0, 0, 0, 0, 100],
        }
    )
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "listed_date": ["2023-01-01"],
            "last_trade_date": ["2024-12-31"],
            "delivery_month": ["2406"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    assert filtered.loc[filtered.index[-1], "sample_status"] == "invalid_low_liquidity"


def test_marks_invalid_insufficient_history_when_recent_valid_history_is_less_than_ten():
    daily_bar = pd.DataFrame(
        {
            "trade_date": pd.date_range("2024-01-01", periods=12, freq="D"),
            "commodity": ["AU"] * 12,
            "contract": ["AU2406"] * 12,
            "open": [10.0] * 12,
            "high": [11.0] * 12,
            "low": [9.0] * 12,
            "close": [10.5] * 12,
            "pre_close": [9.5] * 12,
            "volume": [100] * 12,
        }
    )
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "listed_date": ["2023-01-01"],
            "last_trade_date": ["2024-12-31"],
            "delivery_month": ["2406"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    assert filtered.loc[filtered.index[10], "sample_status"] == "valid"
    assert filtered.loc[filtered.index[0], "sample_status"] == "invalid_insufficient_history"


def test_history_requirement_looks_back_to_prior_valid_samples_not_recent_rows():
    dates = pd.date_range("2024-01-01", periods=31, freq="D")
    daily_bar = pd.DataFrame(
        {
            "trade_date": dates,
            "commodity": ["AU"] * 31,
            "contract": ["AU2406"] * 31,
            "open": [10.0] * 31,
            "high": ([11.0] * 10) + ([9.0] * 20) + [11.0],
            "low": ([9.0] * 10) + ([12.0] * 20) + [9.0],
            "close": [10.5] * 31,
            "pre_close": [9.5] * 31,
            "volume": [100] * 31,
        }
    )
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "listed_date": ["2023-01-01"],
            "last_trade_date": ["2024-12-31"],
            "delivery_month": ["2406"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    assert filtered.loc[filtered.index[-1], "sample_status"] == "valid"


def test_marks_invalid_insufficient_peer_when_same_day_active_contracts_are_less_than_two():
    dates = pd.date_range("2024-01-01", periods=11, freq="D")
    rows = []
    for trade_date in dates:
        target_day = trade_date == dates[-1]
        rows.extend(
            [
                {
                    "trade_date": trade_date,
                    "commodity": "AU",
                    "contract": "AU2406",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "pre_close": 9.5,
                    "volume": 100,
                    "main_reference_contract": "AU2406",
                },
                {
                    "trade_date": trade_date,
                    "commodity": "AU",
                    "contract": "AU2407",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "pre_close": 9.5,
                    "volume": 0 if target_day else 100,
                    "main_reference_contract": "AU2406",
                },
            ]
        )
    daily_bar = pd.DataFrame(rows)
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU", "AU"],
            "contract": ["AU2406", "AU2407"],
            "listed_date": ["2023-01-01", "2023-01-01"],
            "last_trade_date": ["2024-12-31", "2024-12-31"],
            "delivery_month": ["2406", "2407"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    target = filtered[(filtered["contract"] == "AU2406") & (filtered["trade_date"] == dates[-1])]
    assert target["sample_status"].iloc[0] == "invalid_insufficient_peer"


def test_marks_invalid_insufficient_peer_when_main_reference_is_not_valid():
    dates = pd.date_range("2024-01-01", periods=11, freq="D")
    rows = []
    for trade_date in dates:
        target_day = trade_date == dates[-1]
        rows.extend(
            [
                {
                    "trade_date": trade_date,
                    "commodity": "AU",
                    "contract": "AU2406",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "pre_close": 9.5,
                    "volume": 0 if target_day else 100,
                    "main_reference_contract": "AU2406",
                },
                {
                    "trade_date": trade_date,
                    "commodity": "AU",
                    "contract": "AU2407",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "pre_close": 9.5,
                    "volume": 100,
                    "main_reference_contract": "AU2406",
                },
            ]
        )
    daily_bar = pd.DataFrame(rows)
    contract_meta = pd.DataFrame(
        {
            "commodity": ["AU", "AU"],
            "contract": ["AU2406", "AU2407"],
            "listed_date": ["2023-01-01", "2023-01-01"],
            "last_trade_date": ["2024-12-31", "2024-12-31"],
            "delivery_month": ["2406", "2407"],
        }
    )

    filtered = assign_sample_status(daily_bar, contract_meta)

    target = filtered[(filtered["contract"] == "AU2407") & (filtered["trade_date"] == dates[-1])]
    assert target["sample_status"].iloc[0] == "invalid_insufficient_peer"
