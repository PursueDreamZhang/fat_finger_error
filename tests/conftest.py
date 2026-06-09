from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    cache_path = tmp_path / "cache"
    cache_path.mkdir()
    return cache_path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "local_config.json"
    path.write_text('{"tushare_token":"test-token"}', encoding="utf-8")
    return path


@pytest.fixture
def sample_contract_cache(cache_dir: Path) -> Path:
    df = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "open": [10, 11, 12],
            "high": [11, 12, 13],
            "low": [9, 10, 11],
            "close": [10.5, 11.5, 12.5],
            "volume": [100, 120, 140],
        }
    )
    path = cache_dir / "future_AU2406_20240101_20240131.csv"
    df.to_csv(path, index=False)
    return path
