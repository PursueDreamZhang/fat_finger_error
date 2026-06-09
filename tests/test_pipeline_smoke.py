from src.daily_screen.pipeline import analyze_commodities


def test_analyze_commodities_accepts_symbols_and_date_range(tmp_path, cache_dir, config_path, sample_contract_cache):
    result = analyze_commodities(
        symbols=["AU"],
        start_date="20240101",
        end_date="20240131",
        output_dir=str(tmp_path / "report"),
        cache_dir=cache_dir,
        config_path=config_path,
    )

    assert "html_report_path" in result
    assert "suspicious_dates" in result
    assert "all_samples_path" in result
