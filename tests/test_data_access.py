import pandas as pd

from src.daily_screen.data_access import (
    _fill_meta_dates_from_daily,
    _list_contract_cache_files,
    load_commodity_data,
)


def test_load_commodity_data_returns_daily_bar_and_contract_meta(cache_dir, config_path, sample_contract_cache):
    result = load_commodity_data(
        symbols=["AU"],
        start_date="20240101",
        end_date="20240201",
        cache_dir=cache_dir,
        config_path=config_path,
    )

    assert "daily_bar" in result
    assert "contract_meta" in result
    assert not result["daily_bar"].empty
    assert not result["contract_meta"].empty


def test_load_commodity_data_keeps_only_requested_symbols(cache_dir, config_path, sample_contract_cache):
    result = load_commodity_data(
        symbols=["JD"],
        start_date="20240101",
        end_date="20240201",
        cache_dir=cache_dir,
        config_path=config_path,
    )

    assert set(result["daily_bar"]["commodity"].unique()) <= {"JD"}


def test_load_commodity_data_uses_fallback_contracts_when_meta_lookup_is_empty(
    cache_dir,
    config_path,
    monkeypatch,
):
    import src.daily_screen.data_access as data_access

    monkeypatch.setattr(data_access, "_fetch_symbol_meta_from_tushare", lambda symbol, config: pd.DataFrame())
    monkeypatch.setattr(data_access, "_fetch_symbol_meta_from_akshare", lambda symbol, cache_dir: pd.DataFrame())
    monkeypatch.setattr(
        data_access,
        "_discover_contracts_from_fallback_source",
        lambda symbol, start_date, end_date: ["JD2501"],
    )

    def fake_daily_loader(*, contract_code, start_date, end_date, cache_dir, config, meta_row):
        assert contract_code == "JD2501"
        return pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-01-29", "2024-01-30"]),
                "commodity": ["JD", "JD"],
                "contract": ["JD2501", "JD2501"],
                "open": [3601.0, 3688.0],
                "high": [3761.0, 3689.0],
                "low": [3601.0, 3652.0],
                "close": [3678.0, 3657.0],
                "pre_close": [3684.0, 3678.0],
                "volume": [758.0, 342.0],
            }
        )

    monkeypatch.setattr(data_access, "_load_contract_daily_with_cache", fake_daily_loader)

    result = load_commodity_data(
        symbols=["JD"],
        start_date="20240101",
        end_date="20240201",
        cache_dir=cache_dir,
        config_path=config_path,
    )

    assert list(result["contract_meta"]["contract"]) == ["JD2501"]
    assert result["contract_meta"].iloc[0]["listed_date"] == "2024-01-29"
    assert result["contract_meta"].iloc[0]["last_trade_date"] == "2024-01-30"
    assert list(result["daily_bar"]["contract"].unique()) == ["JD2501"]


def test_fill_meta_dates_from_daily_keeps_missing_dates_without_crashing():
    meta_df = pd.DataFrame(
        {
            "commodity": ["TA", "TA"],
            "contract": ["TA2401", "TA2402"],
            "listed_date": [pd.NaT, pd.NaT],
            "last_trade_date": [pd.NaT, pd.NaT],
            "delivery_month": ["2401", "2402"],
            "ts_code": [None, None],
        }
    )
    daily_frames = [
        pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "commodity": ["TA", "TA"],
                "contract": ["TA2401", "TA2401"],
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
                "pre_close": [1.0, 1.0],
                "volume": [1.0, 1.0],
            }
        )
    ]

    filled = _fill_meta_dates_from_daily(meta_df, daily_frames)

    assert filled.loc[filled["contract"] == "TA2401", "listed_date"].iloc[0] == "2024-01-02"
    assert pd.isna(filled.loc[filled["contract"] == "TA2402", "listed_date"].iloc[0])


def test_load_commodity_data_drops_contracts_without_dates_or_daily_data(cache_dir, config_path, monkeypatch):
    import src.daily_screen.data_access as data_access

    monkeypatch.setattr(
        data_access,
        "_load_symbol_meta",
        lambda **kwargs: pd.DataFrame(
            {
                "commodity": ["TA", "TA"],
                "contract": ["TA2303", "TA2401"],
                "listed_date": [pd.NaT, pd.NaT],
                "last_trade_date": [pd.NaT, pd.NaT],
                "delivery_month": ["2303", "2401"],
                "ts_code": [None, None],
            }
        ),
    )
    monkeypatch.setattr(data_access, "_discover_contracts_for_symbol", lambda symbol, meta_df, cache_dir: ["TA2303", "TA2401"])

    def fake_daily_loader(*, contract_code, start_date, end_date, cache_dir, config, meta_row):
        if contract_code == "TA2303":
            return pd.DataFrame(columns=["trade_date", "commodity", "contract", "open", "high", "low", "close", "pre_close", "volume"])
        return pd.DataFrame(
            {
                "trade_date": pd.to_datetime(["2024-01-05", "2024-01-08"]),
                "commodity": ["TA", "TA"],
                "contract": ["TA2401", "TA2401"],
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
                "pre_close": [1.0, 1.0],
                "volume": [1.0, 1.0],
            }
        )

    monkeypatch.setattr(data_access, "_load_contract_daily_with_cache", fake_daily_loader)

    result = load_commodity_data(
        symbols=["TA"],
        start_date="20240101",
        end_date="20240131",
        cache_dir=cache_dir,
        config_path=config_path,
    )

    assert list(result["contract_meta"]["contract"]) == ["TA2401"]
    assert list(result["daily_bar"]["contract"].unique()) == ["TA2401"]


def test_list_contract_cache_files_uses_actual_csv_date_bounds(cache_dir):
    df = pd.DataFrame(
        {
            "trade_date": ["2022-03-15", "2022-03-18", "2023-03-14"],
            "commodity": ["TA", "TA", "TA"],
            "contract": ["TA2303", "TA2303", "TA2303"],
            "open": [1.0, 1.0, 1.0],
            "high": [1.0, 1.0, 1.0],
            "low": [1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0],
            "pre_close": [1.0, 1.0, 1.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )
    path = cache_dir / "future_TA2303_20220116_20251217.csv"
    df.to_csv(path, index=False)

    cache_files = _list_contract_cache_files("TA2303", cache_dir)

    assert len(cache_files) == 1
    assert cache_files[0].start_date == "20220315"
    assert cache_files[0].end_date == "20230314"
