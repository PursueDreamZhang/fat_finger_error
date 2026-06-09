from dataclasses import dataclass


@dataclass
class AnalysisRequest:
    symbols: list[str]
    start_date: str
    end_date: str
    output_dir: str | None = None
