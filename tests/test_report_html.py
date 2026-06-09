from src.daily_screen.report_html import render_report_html


def test_render_report_returns_html_text():
    report_input_payload = {
        "overview": {"symbols": ["AU"], "start_date": "20240101", "end_date": "20240131", "candidate_count": 1, "invalid_sample_count": 1},
        "commodity_summary": [{"commodity": "AU", "candidate_count": 1, "contract_count": 1, "max_candidate_score": 72.0}],
        "suspicious_dates": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "medium",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "AU2408",
            }
        ],
        "detail_records": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "medium",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "AU2408",
                "sample_status": "valid",
                "A_score": 20.0,
                "A1_score": 12.0,
                "A2_score": 8.0,
                "C_score": 52.0,
                "C1_score": 40.0,
                "C2_score": 12.0,
                "E_score": 0.0,
                "open": 100.0,
                "high": 120.0,
                "low": 95.0,
                "close": 110.0,
                "pre_close": 101.0,
                "volume": 10000.0,
                "range_pct": 0.2273,
                "extreme_pct": 0.0909,
                "range_q90": 0.10,
                "range_q95": 0.12,
                "range_q99": 0.20,
                "extreme_q90": 0.04,
                "extreme_q95": 0.06,
                "extreme_q99": 0.08,
                "raw_structure_residual": 0.18,
                "excess_structure_residual": 0.12,
                "normalized_structure_residual": 2.0,
                "structure_q90": 0.25,
                "structure_q95": 0.50,
                "structure_q99": 1.0,
                "raw_uniqueness_gap": 0.8,
                "uniqueness_gap": 0.8,
                "uniqueness_q90": 0.25,
                "uniqueness_q95": 0.50,
                "uniqueness_q99": 1.0,
                "active_peer_count": 3,
                "peer_high_median": 112.0,
                "peer_low_median": 98.0,
                "peer_volume_median": 9000.0,
                "peer_range_median": 0.06,
                "peer_comparability_weak_flag": False,
                "target_liquidity_weak_flag": False,
                "peer_rows": [
                    {
                        "contract": "AU2406",
                        "is_target": True,
                        "volume": 10000.0,
                        "high": 120.0,
                        "low": 95.0,
                        "close": 110.0,
                        "range_pct": 0.2273,
                        "raw_structure_residual": 0.18,
                        "normalized_structure_residual": 2.0,
                        "candidate_score": 72.0,
                        "candidate_level": "medium",
                    },
                    {
                        "contract": "AU2408",
                        "is_target": False,
                        "volume": 9000.0,
                        "high": 112.0,
                        "low": 98.0,
                        "close": 105.0,
                        "range_pct": 0.1333,
                        "raw_structure_residual": 0.01,
                        "normalized_structure_residual": 0.10,
                        "candidate_score": 0.0,
                        "candidate_level": "none",
                    },
                ],
            }
        ],
        "invalid_samples": [
            {
                "detail_id": "AU|AU2408|2024-01-03",
                "commodity": "AU",
                "contract": "AU2408",
                "trade_date": "2024-01-03",
                "sample_status": "invalid_insufficient_history",
                "trigger_reasons": "样本状态=invalid_insufficient_history",
                "main_reference_contract": "AU2408",
            }
        ],
    }
    html = render_report_html(report_input_payload)
    assert "<html" in html.lower()


def test_render_report_contains_suspicious_dates_table():
    report_input_payload = {
        "overview": {"symbols": ["AU"], "start_date": "20240101", "end_date": "20240131", "candidate_count": 1, "invalid_sample_count": 1},
        "commodity_summary": [{"commodity": "AU", "candidate_count": 1, "contract_count": 1, "max_candidate_score": 72.0}],
        "suspicious_dates": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "medium",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "AU2408",
            }
        ],
        "detail_records": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "medium",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "AU2408",
                "sample_status": "valid",
                "A_score": 20.0,
                "A1_score": 12.0,
                "A2_score": 8.0,
                "C_score": 52.0,
                "C1_score": 40.0,
                "C2_score": 12.0,
                "E_score": 0.0,
                "open": 100.0,
                "high": 120.0,
                "low": 95.0,
                "close": 110.0,
                "pre_close": 101.0,
                "volume": 10000.0,
                "range_pct": 0.2273,
                "extreme_pct": 0.0909,
                "range_q90": 0.10,
                "range_q95": 0.12,
                "range_q99": 0.20,
                "extreme_q90": 0.04,
                "extreme_q95": 0.06,
                "extreme_q99": 0.08,
                "raw_structure_residual": 0.18,
                "excess_structure_residual": 0.12,
                "normalized_structure_residual": 2.0,
                "structure_q90": 0.25,
                "structure_q95": 0.50,
                "structure_q99": 1.0,
                "raw_uniqueness_gap": 0.8,
                "uniqueness_gap": 0.8,
                "uniqueness_q90": 0.25,
                "uniqueness_q95": 0.50,
                "uniqueness_q99": 1.0,
                "active_peer_count": 3,
                "peer_high_median": 112.0,
                "peer_low_median": 98.0,
                "peer_volume_median": 9000.0,
                "peer_range_median": 0.06,
                "peer_comparability_weak_flag": False,
                "target_liquidity_weak_flag": False,
                "peer_rows": [
                    {
                        "contract": "AU2406",
                        "is_target": True,
                        "volume": 10000.0,
                        "high": 120.0,
                        "low": 95.0,
                        "close": 110.0,
                        "range_pct": 0.2273,
                        "raw_structure_residual": 0.18,
                        "normalized_structure_residual": 2.0,
                        "candidate_score": 72.0,
                        "candidate_level": "medium",
                    },
                    {
                        "contract": "AU2408",
                        "is_target": False,
                        "volume": 9000.0,
                        "high": 112.0,
                        "low": 98.0,
                        "close": 105.0,
                        "range_pct": 0.1333,
                        "raw_structure_residual": 0.01,
                        "normalized_structure_residual": 0.10,
                        "candidate_score": 0.0,
                        "candidate_level": "none",
                    },
                ],
            }
        ],
        "invalid_samples": [
            {
                "detail_id": "AU|AU2408|2024-01-03",
                "commodity": "AU",
                "contract": "AU2408",
                "trade_date": "2024-01-03",
                "sample_status": "invalid_insufficient_history",
                "trigger_reasons": "样本状态=invalid_insufficient_history",
                "main_reference_contract": "AU2408",
            }
        ],
    }
    html = render_report_html(report_input_payload)
    assert "可疑日期" in html
    assert "无效样本" in html
    assert "分数 ↓" in html
    assert "record-detail-modal" in html
    assert "detailRecordsJson" in html
    assert "A 计算过程" in html
    assert "invalid-row" in html
    assert "点击行可查看完整计算链路" in html
    assert "threshold-hit" in html
    assert "同日活跃合约对比" in html
    assert "function formatDate" in html
    assert "2024-01-02 00:00:00" not in html
    assert 'id="commodityHeader" class="sortable"' in html
    assert 'id="tradeDateHeader" class="sortable"' in html
    assert "function sortRowsByColumn" in html
    assert "const sortableHeaders" in html


def test_render_report_contains_amplitude_peer_summary_section():
    report_input_payload = {
        "overview": {"symbols": ["AU"], "start_date": "20240101", "end_date": "20240131", "candidate_count": 1, "invalid_sample_count": 0},
        "commodity_summary": [{"commodity": "AU", "candidate_count": 1, "contract_count": 1, "max_candidate_score": 72.0}],
        "suspicious_dates": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "high",
                "trigger_reasons": "振幅异常；结构失真",
                "main_reference_contract": "AU2408",
            }
        ],
        "detail_records": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "high",
                "trigger_reasons": "振幅异常；结构失真",
                "main_reference_contract": "AU2408",
                "sample_status": "valid",
                "high": 120.0,
                "low": 95.0,
                "close": 110.0,
                "range_pct": 0.2273,
                "peer_high_median": 112.0,
                "peer_low_median": 98.0,
                "peer_range_median": 0.06,
            }
        ],
        "invalid_samples": [],
    }

    html = render_report_html(report_input_payload)

    assert "振幅横向统计" in html
    assert "amplitudePeerSummary" in html
    assert "computeAmplitudePeerSummary" in html


def test_render_report_contains_contract_gap_summary_section():
    report_input_payload = {
        "overview": {"symbols": ["BU"], "start_date": "20240101", "end_date": "20240131", "candidate_count": 1, "invalid_sample_count": 0},
        "commodity_summary": [{"commodity": "BU", "candidate_count": 1, "contract_count": 1, "max_candidate_score": 80.0}],
        "suspicious_dates": [
            {
                "detail_id": "BU|BU2603|2024-01-02",
                "commodity": "BU",
                "contract": "BU2603",
                "trade_date": "2024-01-02",
                "candidate_score": 80.0,
                "candidate_level": "high",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "BU2509",
            }
        ],
        "detail_records": [
            {
                "detail_id": "BU|BU2603|2024-01-02",
                "commodity": "BU",
                "contract": "BU2603",
                "trade_date": "2024-01-02",
                "candidate_score": 80.0,
                "candidate_level": "high",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "BU2509",
                "sample_status": "valid",
            }
        ],
        "invalid_samples": [],
    }

    html = render_report_html(report_input_payload)

    assert "交割月差统计" in html
    assert "contractGapSummary" in html
    assert "computeContractGapSummary" in html


def test_render_report_shows_human_readable_invalid_status_labels():
    report_input_payload = {
        "overview": {"symbols": ["AU"], "start_date": "20240101", "end_date": "20240131", "candidate_count": 0, "invalid_sample_count": 1},
        "commodity_summary": [],
        "suspicious_dates": [],
        "detail_records": [
            {
                "detail_id": "AU|AU2408|2024-01-03",
                "commodity": "AU",
                "contract": "AU2408",
                "trade_date": "2024-01-03",
                "candidate_score": 0.0,
                "candidate_level": "none",
                "trigger_reasons": "样本状态=历史样本不足",
                "main_reference_contract": "AU2408",
                "sample_status": "invalid_insufficient_history",
            }
        ],
        "invalid_samples": [
            {
                "detail_id": "AU|AU2408|2024-01-03",
                "commodity": "AU",
                "contract": "AU2408",
                "trade_date": "2024-01-03",
                "sample_status": "invalid_insufficient_history",
                "trigger_reasons": "样本状态=历史样本不足",
                "main_reference_contract": "AU2408",
            }
        ],
    }

    html = render_report_html(report_input_payload)

    assert "历史样本不足" in html
    assert ">invalid_insufficient_history<" not in html


def test_render_report_json_payload_does_not_emit_nan_literals():
    report_input_payload = {
        "overview": {"symbols": ["AU"], "start_date": "20240101", "end_date": "20240131", "candidate_count": 1, "invalid_sample_count": 0},
        "commodity_summary": [{"commodity": "AU", "candidate_count": 1, "contract_count": 1, "max_candidate_score": 72.0}],
        "suspicious_dates": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "high",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "AU2408",
            }
        ],
        "detail_records": [
            {
                "detail_id": "AU|AU2406|2024-01-02",
                "commodity": "AU",
                "contract": "AU2406",
                "trade_date": "2024-01-02",
                "candidate_score": 72.0,
                "candidate_level": "high",
                "trigger_reasons": "结构失真",
                "main_reference_contract": "AU2408",
                "sample_status": "valid",
                "range_q90": float("nan"),
            }
        ],
        "invalid_samples": [],
    }
    html = render_report_html(report_input_payload)
    payload = html.split('<script id="detailRecordsJson" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert "NaN" not in payload
    assert "null" in payload
