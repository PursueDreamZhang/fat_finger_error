from __future__ import annotations

import numpy as np
import pandas as pd


ACTIVE_CONTRACT_LIMIT = 5
ROLLING_WINDOW = 20
ROLLING_MIN_PERIODS = 10


def score_candidates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    base_columns = [
        "A_score",
        "A1_score",
        "A2_score",
        "C_score",
        "C1_score",
        "C2_score",
        "E_score",
        "candidate_score",
        "candidate_level",
        "range_pct",
        "extreme_pct",
        "raw_structure_residual",
        "excess_structure_residual",
        "normalized_structure_residual",
        "structure_residual",
        "uniqueness_gap",
        "raw_uniqueness_gap",
        "active_peer_count",
        "peer_volume_median",
        "peer_high_median",
        "peer_low_median",
        "peer_range_median",
        "peer_comparability_weak_flag",
        "target_liquidity_weak_flag",
    ]
    if out.empty:
        for column in base_columns:
            out[column] = pd.Series(dtype="float64" if column.endswith("_score") or column.endswith("_pct") or column.endswith("_median") or column == "candidate_score" or column == "uniqueness_gap" or column == "active_peer_count" else "object")
        return out

    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out = out.sort_values(["commodity", "contract", "trade_date"]).reset_index(drop=True)

    close_denom = out["close"].replace(0, np.nan)
    out["range_pct"] = (out["high"] - out["low"]) / close_denom
    out["extreme_pct"] = (
        np.maximum((out["high"] - out["close"]).abs(), (out["low"] - out["close"]).abs())
        / close_denom
    )

    out = _attach_same_day_peer_metrics(out)
    out = _attach_history_metrics(out)

    a1_score, a2_score = _score_a_components(out)
    c1_score, c2_score = _score_c_components(out)
    out["A1_score"] = a1_score
    out["A2_score"] = a2_score
    out["A_score"] = (a1_score + a2_score).fillna(0.0).clip(upper=20)
    out["C1_score"] = c1_score
    out["C2_score"] = c2_score
    out["C_score"] = (c1_score + c2_score).fillna(0.0).clip(upper=60)
    out["E_score"] = _score_e(out)

    invalid_mask = (out["sample_status"] != "valid") | (out["active_peer_count"] <= 0)
    out.loc[invalid_mask, ["A_score", "C_score"]] = 0.0
    out.loc[invalid_mask, "E_score"] = -20.0

    out["candidate_score"] = (out["A_score"] + out["C_score"] + out["E_score"]).clip(lower=0, upper=100)
    out.loc[invalid_mask, "candidate_score"] = 0.0
    out["candidate_level"] = _assign_candidate_level(out)
    out.loc[invalid_mask, "candidate_level"] = "none"
    return out


def _attach_same_day_peer_metrics(df: pd.DataFrame) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for (_, trade_date), group in df.groupby(["commodity", "trade_date"], sort=False):
        day = group.copy()
        active = day.loc[
            (day["sample_status"] == "valid")
            & (day["volume"] > 0)
        ].sort_values(["volume", "contract"], ascending=[False, True]).head(ACTIVE_CONTRACT_LIMIT)

        active_contracts = set(active["contract"].tolist())
        active_count = len(active_contracts)
        day["active_contract_count"] = active_count

        peer_high_medians: list[float] = []
        peer_low_medians: list[float] = []
        peer_volume_medians: list[float] = []
        peer_range_medians: list[float] = []
        active_peer_counts: list[int] = []
        raw_structure_residuals: list[float] = []
        excess_structure_residuals: list[float] = []
        normalized_structure_residuals: list[float] = []

        for _, row in day.iterrows():
            peers = active.loc[active["contract"] != row["contract"]]
            active_peer_counts.append(int(len(peers)))

            if peers.empty:
                peer_high_medians.append(np.nan)
                peer_low_medians.append(np.nan)
                peer_volume_medians.append(np.nan)
                peer_range_medians.append(np.nan)
                raw_structure_residuals.append(np.nan)
                excess_structure_residuals.append(np.nan)
                normalized_structure_residuals.append(np.nan)
                continue

            peer_high_median = float(peers["high"].median())
            peer_low_median = float(peers["low"].median())
            peer_volume_median = float(peers["volume"].median())
            peer_range_series = ((peers["high"] - peers["low"]) / peers["close"].replace(0, np.nan)).dropna()
            peer_range_median = float(peer_range_series.median()) if not peer_range_series.empty else np.nan

            upper_residual = max(0.0, (float(row["high"]) - peer_high_median) / float(row["close"])) if row["close"] else np.nan
            lower_residual = max(0.0, (peer_low_median - float(row["low"])) / float(row["close"])) if row["close"] else np.nan
            raw_structure_residual = np.nanmax([upper_residual, lower_residual])
            if pd.isna(peer_range_median) or peer_range_median <= 0:
                excess_structure_residual = np.nan
                normalized_structure_residual = np.nan
            else:
                excess_structure_residual = max(0.0, raw_structure_residual - peer_range_median)
                normalized_structure_residual = excess_structure_residual / peer_range_median

            peer_high_medians.append(peer_high_median)
            peer_low_medians.append(peer_low_median)
            peer_volume_medians.append(peer_volume_median)
            peer_range_medians.append(peer_range_median)
            raw_structure_residuals.append(raw_structure_residual)
            excess_structure_residuals.append(excess_structure_residual)
            normalized_structure_residuals.append(normalized_structure_residual)

        day["active_peer_count"] = active_peer_counts
        day["peer_high_median"] = peer_high_medians
        day["peer_low_median"] = peer_low_medians
        day["peer_volume_median"] = peer_volume_medians
        day["peer_range_median"] = peer_range_medians
        day["raw_structure_residual"] = raw_structure_residuals
        day["excess_structure_residual"] = excess_structure_residuals
        day["normalized_structure_residual"] = normalized_structure_residuals
        day["structure_residual"] = raw_structure_residuals
        parts.append(day)

    out = pd.concat(parts, ignore_index=True)

    uniqueness_gap_parts: list[pd.DataFrame] = []
    for _, day in out.groupby(["commodity", "trade_date"], sort=False):
        day = day.copy()
        day["uniqueness_gap"] = 0.0
        day["raw_uniqueness_gap"] = 0.0
        for index, row in day.iterrows():
            peers = day.loc[
                day["contract"] != row["contract"],
                "normalized_structure_residual",
            ].dropna()
            if peers.empty or pd.isna(row["normalized_structure_residual"]):
                day.at[index, "uniqueness_gap"] = np.nan
                day.at[index, "raw_uniqueness_gap"] = np.nan
                continue
            raw_uniqueness_gap = float(row["normalized_structure_residual"]) - float(peers.median())
            day.at[index, "raw_uniqueness_gap"] = raw_uniqueness_gap
            day.at[index, "uniqueness_gap"] = max(0.0, raw_uniqueness_gap)
        uniqueness_gap_parts.append(day)

    return pd.concat(uniqueness_gap_parts, ignore_index=True)


def _attach_history_metrics(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    history_parts: list[pd.DataFrame] = []

    for _, group in out.groupby(["commodity", "contract"], sort=False):
        item = group.copy().sort_values("trade_date")

        valid_history_mask = item["sample_status"] == "valid"
        item["range_q90"] = _rolling_quantile_on_eligible(item["range_pct"], valid_history_mask, 0.90)
        item["range_q95"] = _rolling_quantile_on_eligible(item["range_pct"], valid_history_mask, 0.95)
        item["range_q99"] = _rolling_quantile_on_eligible(item["range_pct"], valid_history_mask, 0.99)

        item["extreme_q90"] = _rolling_quantile_on_eligible(item["extreme_pct"], valid_history_mask, 0.90)
        item["extreme_q95"] = _rolling_quantile_on_eligible(item["extreme_pct"], valid_history_mask, 0.95)
        item["extreme_q99"] = _rolling_quantile_on_eligible(item["extreme_pct"], valid_history_mask, 0.99)

        c_history_mask = valid_history_mask & (item["active_peer_count"] >= 2)
        item["structure_q90"] = _rolling_quantile_on_eligible(item["normalized_structure_residual"], c_history_mask, 0.90)
        item["structure_q95"] = _rolling_quantile_on_eligible(item["normalized_structure_residual"], c_history_mask, 0.95)
        item["structure_q99"] = _rolling_quantile_on_eligible(item["normalized_structure_residual"], c_history_mask, 0.99)

        item["uniqueness_q90"] = _rolling_quantile_on_eligible(item["uniqueness_gap"], c_history_mask, 0.90)
        item["uniqueness_q95"] = _rolling_quantile_on_eligible(item["uniqueness_gap"], c_history_mask, 0.95)
        item["uniqueness_q99"] = _rolling_quantile_on_eligible(item["uniqueness_gap"], c_history_mask, 0.99)

        item["rolling_median_volume"] = _rolling_median_on_eligible(item["volume"], valid_history_mask)
        item["rolling_median_active_peer_count"] = _rolling_median_on_eligible(
            item["active_peer_count"], c_history_mask
        )
        history_parts.append(item)

    out = pd.concat(history_parts, ignore_index=True)

    rolling_peer_floor = np.maximum(2, out["rolling_median_active_peer_count"].fillna(2) - 1)
    out["peer_comparability_weak_flag"] = (
        (out["sample_status"] == "valid")
        & (out["active_peer_count"] >= 1)
        & (out["active_peer_count"] < rolling_peer_floor)
    )

    out["target_liquidity_weak_flag"] = (
        (out["sample_status"] == "valid")
        & out["rolling_median_volume"].notna()
        & out["peer_volume_median"].notna()
        & (out["volume"] < 0.2 * out["rolling_median_volume"])
        & (out["volume"] < 0.3 * out["peer_volume_median"])
    )
    return out


def _score_a_components(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    a1 = pd.Series(0.0, index=df.index, dtype="float64")
    a1 = a1.mask(_meets_threshold(df["range_pct"], df["range_q90"]), 4.0)
    a1 = a1.mask(_meets_threshold(df["range_pct"], df["range_q95"]), 8.0)
    a1 = a1.mask(_meets_threshold(df["range_pct"], df["range_q99"]), 12.0)

    a2 = pd.Series(0.0, index=df.index, dtype="float64")
    a2 = a2.mask(_meets_threshold(df["extreme_pct"], df["extreme_q90"]), 3.0)
    a2 = a2.mask(_meets_threshold(df["extreme_pct"], df["extreme_q95"]), 5.0)
    a2 = a2.mask(_meets_threshold(df["extreme_pct"], df["extreme_q99"]), 8.0)
    return a1.fillna(0.0), a2.fillna(0.0)


def _score_c_components(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    c1 = pd.Series(0.0, index=df.index, dtype="float64")
    c1 = c1.mask(_meets_threshold_with_fallback(df["normalized_structure_residual"], df["structure_q90"], 0.25), 12.0)
    c1 = c1.mask(_meets_threshold_with_fallback(df["normalized_structure_residual"], df["structure_q95"], 0.50), 24.0)
    c1 = c1.mask(_meets_threshold_with_fallback(df["normalized_structure_residual"], df["structure_q99"], 1.00), 40.0)

    c2 = pd.Series(0.0, index=df.index, dtype="float64")
    c2 = c2.mask(_meets_threshold_with_fallback(df["uniqueness_gap"], df["uniqueness_q90"], 0.25), 5.0)
    c2 = c2.mask(_meets_threshold_with_fallback(df["uniqueness_gap"], df["uniqueness_q95"], 0.50), 10.0)
    c2 = c2.mask(_meets_threshold_with_fallback(df["uniqueness_gap"], df["uniqueness_q99"], 1.00), 20.0)
    return c1.fillna(0.0), c2.fillna(0.0)


def _score_e(df: pd.DataFrame) -> pd.Series:
    score = pd.Series(0.0, index=df.index, dtype="float64")
    score = score.mask((df["sample_status"] != "valid") | (df["active_peer_count"] <= 0), -20.0)
    score = score.mask(
        (df["sample_status"] == "valid")
        & (df["active_peer_count"] >= 1)
        & (
            df["peer_comparability_weak_flag"]
            | df["target_liquidity_weak_flag"]
            | (df["active_peer_count"] == 1)
        ),
        -10.0,
    )
    score = score.mask(
        (df["sample_status"] == "valid")
        & (df["active_peer_count"] == 2)
        & ~df["peer_comparability_weak_flag"]
        & ~df["target_liquidity_weak_flag"],
        -5.0,
    )
    return score


def _assign_candidate_level(df: pd.DataFrame) -> pd.Series:
    levels = pd.Series("none", index=df.index, dtype="object")
    levels.loc[df["candidate_score"] >= 30] = "low"
    levels.loc[df["candidate_score"] >= 50] = "medium"
    levels.loc[df["candidate_score"] >= 65] = "high"
    levels.loc[df["C_score"] < 20] = "low"
    levels.loc[(df["candidate_score"] >= 65) & (df["C_score"] < 35)] = "medium"
    levels.loc[df["candidate_score"] < 30] = "none"
    return levels


def _rolling_quantile_on_eligible(series: pd.Series, eligible_mask: pd.Series, quantile: float) -> pd.Series:
    eligible_series = series.loc[eligible_mask & series.notna()]
    if eligible_series.empty:
        return pd.Series(np.nan, index=series.index, dtype="float64")
    rolled = eligible_series.shift(1).rolling(ROLLING_WINDOW, min_periods=ROLLING_MIN_PERIODS).quantile(quantile)
    return rolled.reindex(series.index)


def _rolling_median_on_eligible(series: pd.Series, eligible_mask: pd.Series) -> pd.Series:
    eligible_series = series.loc[eligible_mask & series.notna()]
    if eligible_series.empty:
        return pd.Series(np.nan, index=series.index, dtype="float64")
    rolled = eligible_series.shift(1).rolling(ROLLING_WINDOW, min_periods=ROLLING_MIN_PERIODS).median()
    return rolled.reindex(series.index)


def _meets_threshold(values: pd.Series, thresholds: pd.Series) -> pd.Series:
    return values.notna() & thresholds.notna() & (thresholds > 0) & (values >= thresholds)


def _meets_threshold_with_fallback(values: pd.Series, thresholds: pd.Series, fallback: float) -> pd.Series:
    positive_threshold_mask = thresholds.notna() & (thresholds > 0)
    return values.notna() & (
        (positive_threshold_mask & (values >= thresholds))
        | (~positive_threshold_mask & (values >= fallback))
    )
