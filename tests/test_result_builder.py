import pandas as pd

from src.daily_screen.result_builder import build_results


def test_build_results_outputs_suspicious_dates_table():
    scored_candidates_df = pd.DataFrame(
        {
            "commodity": ["AU", "AU"],
            "contract": ["AU2406", "AU2407"],
            "trade_date": [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-02")],
            "candidate_score": [72.0, 0.0],
            "candidate_level": ["high", "none"],
            "main_reference_contract": ["AU2408", "AU2408"],
            "A_score": [12.0, 0.0],
            "C_score": [40.0, 0.0],
            "E_score": [0.0, -20.0],
            "sample_status": ["valid", "invalid_insufficient_history"],
            "active_peer_count": [3, 0],
            "peer_comparability_weak_flag": [False, False],
            "target_liquidity_weak_flag": [False, False],
            "volume": [10000, 10000],
            "peer_volume_median": [9000, 9000],
        }
    )

    result = build_results(scored_candidates_df, ["AU"], "20240101", "20240131")

    assert "suspicious_dates" in result
    assert not result["suspicious_dates"].empty
    assert len(result["suspicious_dates"]) == 1
    assert len(result["all_samples"]) == 2


def test_build_results_outputs_commodity_summary():
    scored_candidates_df = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "trade_date": [pd.Timestamp("2024-01-02")],
            "candidate_score": [72.0],
            "candidate_level": ["medium"],
            "main_reference_contract": ["AU2408"],
            "A_score": [8.0],
            "C_score": [25.0],
            "E_score": [0.0],
            "sample_status": ["valid"],
            "active_peer_count": [2],
            "peer_comparability_weak_flag": [False],
            "target_liquidity_weak_flag": [False],
            "volume": [10000],
            "peer_volume_median": [9000],
        }
    )

    result = build_results(scored_candidates_df, ["AU"], "20240101", "20240131")

    assert "commodity_summary" in result
    assert not result["commodity_summary"].empty


def test_build_results_outputs_detail_records_with_score_breakdown():
    scored_candidates_df = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2406"],
            "trade_date": [pd.Timestamp("2024-01-02")],
            "candidate_score": [72.0],
            "candidate_level": ["high"],
            "main_reference_contract": ["AU2408"],
            "sample_status": ["valid"],
            "trigger_reasons": ["结构失真"],
            "A_score": [20.0],
            "A1_score": [12.0],
            "A2_score": [8.0],
            "C_score": [52.0],
            "C1_score": [40.0],
            "C2_score": [12.0],
            "E_score": [0.0],
            "open": [100.0],
            "high": [120.0],
            "low": [95.0],
            "close": [110.0],
            "pre_close": [101.0],
            "volume": [10000.0],
            "range_pct": [0.2273],
            "extreme_pct": [0.0909],
            "range_q90": [0.10],
            "range_q95": [0.12],
            "range_q99": [0.20],
            "extreme_q90": [0.04],
            "extreme_q95": [0.06],
            "extreme_q99": [0.08],
            "raw_structure_residual": [0.18],
            "excess_structure_residual": [0.12],
            "normalized_structure_residual": [2.0],
            "structure_q90": [0.25],
            "structure_q95": [0.50],
            "structure_q99": [1.00],
            "raw_uniqueness_gap": [0.80],
            "uniqueness_gap": [0.80],
            "uniqueness_q90": [0.25],
            "uniqueness_q95": [0.50],
            "uniqueness_q99": [1.00],
            "active_peer_count": [3],
            "peer_high_median": [112.0],
            "peer_low_median": [98.0],
            "peer_volume_median": [9000.0],
            "peer_range_median": [0.06],
            "peer_comparability_weak_flag": [False],
            "target_liquidity_weak_flag": [False],
        }
    )

    result = build_results(scored_candidates_df, ["AU"], "20240101", "20240131")

    detail_records = result["report_payload"]["detail_records"]
    assert len(detail_records) == 1
    detail = detail_records[0]
    assert detail["detail_id"] == "AU|AU2406|2024-01-02"
    assert detail["A_score"] == 20.0
    assert detail["A1_score"] == 12.0
    assert detail["C2_score"] == 12.0
    assert detail["peer_range_median"] == 0.06
    assert isinstance(detail["peer_rows"], list)
    assert detail["peer_rows"][0]["contract"] == "AU2406"
