from __future__ import annotations

import pandas as pd

ROLLING_WINDOW = 20
ROLLING_MIN_VALID_SAMPLES = 10


def assign_sample_status(daily_bar: pd.DataFrame, contract_meta: pd.DataFrame) -> pd.DataFrame:
    df = daily_bar.copy()
    if df.empty:
        df["sample_status"] = pd.Series(dtype="object")
        return df

    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values(["commodity", "contract", "trade_date"]).reset_index(drop=True)
    meta = contract_meta.copy()
    if not meta.empty:
        meta["listed_date"] = pd.to_datetime(meta["listed_date"], errors="coerce")
        meta["last_trade_date"] = pd.to_datetime(meta["last_trade_date"], errors="coerce")
        df = df.merge(meta, on=["commodity", "contract"], how="left")
    else:
        df["listed_date"] = pd.NaT
        df["last_trade_date"] = pd.NaT
        df["delivery_month"] = pd.NA

    df["sample_status"] = "valid"

    invalid_basic = (
        df["open"].isna()
        | df["high"].isna()
        | df["low"].isna()
        | df["close"].isna()
        | df["pre_close"].isna()
        | (df["high"] < df["low"])
        | (df["open"] < df["low"])
        | (df["open"] > df["high"])
        | (df["close"] < df["low"])
        | (df["close"] > df["high"])
        | (df["pre_close"] <= 0)
        | (df["volume"] < 0)
    )
    df.loc[invalid_basic, "sample_status"] = "invalid_basic_data"

    low_liquidity = (df["sample_status"] == "valid") & (
        (df["volume"] == 0)
    )
    df.loc[low_liquidity, "sample_status"] = "invalid_low_liquidity"

    observed_history_count = (
        df.groupby(["commodity", "contract"], sort=False)["trade_date"]
        .transform(lambda series: series.shift(1).rolling(ROLLING_WINDOW, min_periods=1).count())
    )
    recent_active_days = (
        df.groupby(["commodity", "contract"], sort=False)["volume"]
        .transform(lambda series: series.gt(0).shift(1).rolling(ROLLING_WINDOW, min_periods=1).sum())
    )
    insufficient_recent_activity = (
        (df["sample_status"] == "valid")
        & (observed_history_count >= ROLLING_MIN_VALID_SAMPLES)
        & (recent_active_days < ROLLING_MIN_VALID_SAMPLES)
    )
    df.loc[insufficient_recent_activity, "sample_status"] = "invalid_low_liquidity"

    insufficient_meta = (df["sample_status"] == "valid") & (
        df["listed_date"].isna() | df["last_trade_date"].isna()
    )
    df.loc[insufficient_meta, "sample_status"] = "invalid_insufficient_history"

    listed_days = (df["trade_date"] - df["listed_date"]).dt.days
    days_to_last_trade = (df["last_trade_date"] - df["trade_date"]).dt.days
    lifecycle_edge = (df["sample_status"] == "valid") & (
        (listed_days < 10) | (days_to_last_trade < 10)
    )
    df.loc[lifecycle_edge, "sample_status"] = "invalid_lifecycle_edge"

    prior_valid_sample_count = (
        df.groupby(["commodity", "contract"], sort=False)["sample_status"]
        .transform(lambda series: series.eq("valid").cumsum().shift(1).fillna(0))
    )
    insufficient_history = (
        (df["sample_status"] == "valid")
        & (prior_valid_sample_count < ROLLING_MIN_VALID_SAMPLES)
    )
    df.loc[insufficient_history, "sample_status"] = "invalid_insufficient_history"

    if "main_reference_contract" in df.columns:
        reference_status = (
            df.loc[:, ["commodity", "trade_date", "contract", "sample_status"]]
            .rename(columns={"contract": "main_reference_contract", "sample_status": "main_reference_status"})
            .drop_duplicates(subset=["commodity", "trade_date", "main_reference_contract"])
        )
        df = df.merge(
            reference_status,
            on=["commodity", "trade_date", "main_reference_contract"],
            how="left",
        )
        analyzable_counts = (
            df.assign(is_analyzable=df["sample_status"] == "valid")
            .groupby(["commodity", "trade_date"], sort=False)["is_analyzable"]
            .transform("sum")
        )
        insufficient_peer = (
            (df["sample_status"] == "valid")
            & (
                df["main_reference_contract"].isna()
                | df["main_reference_status"].ne("valid")
                | (analyzable_counts < 2)
            )
        )
        df.loc[insufficient_peer, "sample_status"] = "invalid_insufficient_peer"
        df = df.drop(columns=["main_reference_status"])

    return df
