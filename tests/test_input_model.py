from src.daily_screen.input_model import AnalysisRequest


def test_analysis_request_stores_symbols_and_date_range():
    request = AnalysisRequest(
        symbols=["AU", "JD"],
        start_date="20240101",
        end_date="20241231",
    )

    assert request.symbols == ["AU", "JD"]
    assert request.start_date == "20240101"
    assert request.end_date == "20241231"
    assert request.output_dir is None
