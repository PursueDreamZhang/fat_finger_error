from __future__ import annotations

import pandas as pd
import pytest

from scripts import derive_commodity_profiles as profiles


def test_derive_multiplier_uses_median_positive_deltas():
    df = pd.DataFrame(
        {
            "delta_volume": [1] * 10,
            "delta_turnover": [1000] * 10,
            "LastPrice": [100] * 10,
        }
    )

    assert profiles._derive_multiplier(df) == 10


def test_derive_multiplier_requires_ten_valid_samples():
    df = pd.DataFrame(
        {"delta_volume": [1] * 9, "delta_turnover": [1000] * 9, "LastPrice": [100] * 9}
    )

    assert profiles._derive_multiplier(df) is None


def test_raw_delta_helper_uses_snapshot_order():
    df = profiles._add_raw_deltas(pd.DataFrame({"Volume": [10, 12], "Turnover": [1000, 1240]}))

    assert pd.isna(df["delta_volume"].iloc[0])
    assert df["delta_volume"].iloc[1] == 2
    assert df["delta_turnover"].iloc[1] == 240


def test_derive_tick_size_and_rejects_float_noise():
    df = pd.DataFrame(
        {
            "BidPrice1": [100.00, 100.02, 100.04, 100.06],
            "AskPrice1": [100.02, 100.04, 100.06, 100.08],
            "LastPrice": [100.02, 100.04, 100.06, 100.08],
        }
    )
    assert profiles._derive_tick_size(df) == pytest.approx(0.02)

    noisy = df.copy()
    noisy.loc[1, "LastPrice"] = 100.02000001
    with pytest.raises(ValueError, match="过小"):
        profiles._derive_tick_size(noisy)


def test_file_name_and_source_date_helpers():
    assert profiles._commodity_from_file_name("au2606_20260520.csv") == "AU"
    assert profiles._commodity_from_file_name("au主力连续_20260520.csv") is None
    assert profiles._source_date("data/tick2026/202605/20260520") == "20260520"


def test_missing_commodities_uses_fixed_87_code_set():
    missing = profiles._missing_commodities({"AU": {}})

    assert "AU" not in missing
    assert "RR" in missing
    assert len(missing) == 86
