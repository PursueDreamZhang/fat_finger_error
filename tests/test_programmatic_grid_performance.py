from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.programmatic_grid import load_programmatic_grid_config, run_programmatic_grid


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_programmatic_grid.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "programmatic_grid" / "20260302"
SPEC = importlib.util.spec_from_file_location("benchmark_programmatic_grid", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def _result(*, reverse: bool = False, contexts: dict | None = None) -> dict:
    summary = pd.DataFrame([
        {"instrument": "NI2605", "scenario_id": "Q02-L01", "net_pnl": np.nan},
        {"instrument": "FU2606", "scenario_id": "Q01-L01", "net_pnl": 10.0},
    ])
    daily = pd.DataFrame([
        {"instrument": "NI2605", "scenario_id": "Q02-L01", "trade_date": "20260302", "fill_count": 1},
        {"instrument": "FU2606", "scenario_id": "Q01-L01", "trade_date": "20260302", "fill_count": 0},
    ])
    trades = pd.DataFrame([
        {"instrument": "NI2605", "scenario_id": "Q02-L01", "trade_id": "t2", "net_pnl": -5.0},
        {"instrument": "NI2605", "scenario_id": "Q02-L01", "trade_id": "t1", "net_pnl": 2.0},
    ])
    if reverse:
        summary = summary.iloc[::-1]
        daily = daily.iloc[::-1]
        trades = trades.iloc[::-1]
    return {
        "summary": summary,
        "daily": daily,
        "trades": trades,
        "skipped_days": pd.DataFrame(columns=["instrument", "trade_date", "reason"]),
        "trade_contexts": contexts if contexts is not None else {"NI2605::Q02-L01::t1": {"target_rows": []}},
    }


def test_result_signature_ignores_frame_row_order_and_normalizes_nan():
    assert benchmark.result_signature(_result())["overall_sha256"] == benchmark.result_signature(_result(reverse=True))["overall_sha256"]


def test_result_signature_keeps_context_content_in_the_contract():
    baseline = benchmark.result_signature(_result())
    changed = benchmark.result_signature(_result(contexts={"NI2605::Q02-L01::t1": {"target_rows": [{"LastPrice": 1}]}}))
    assert baseline["overall_sha256"] != changed["overall_sha256"]


@pytest.mark.parametrize(
    ("fair_cache_dir", "workers"),
    [(None, 2)],
)
def test_stage_zero_rejects_future_runtime_modes(fair_cache_dir, workers):
    with pytest.raises(ValueError):
        benchmark._validate_runtime_options(
            context_mode="all",
            fair_cache_dir=fair_cache_dir,
            workers=workers,
        )


def test_core_only_requires_no_write():
    with pytest.raises(ValueError):
        benchmark._validate_runtime_options(
            context_mode="all", fair_cache_dir=None, workers=1, core_only=True, no_write=False,
        )


def test_no_write_benchmark_uses_temp_runs_and_returns_segment_medians(monkeypatch, tmp_path):
    monkeypatch.setattr(benchmark, "load_programmatic_grid_config", lambda _path: {"base": {}, "output_dir": "unused"})

    def fake_run(_config, *, timings, include_trade_contexts=True):
        assert include_trade_contexts is True
        timings.update({"data_load": 0.1, "fair_prepare": 0.2, "state_machine": 0.3})
        return _result()

    monkeypatch.setattr(benchmark, "run_programmatic_grid", fake_run)
    args = SimpleNamespace(
        config="unused.json", start_date="20260302", end_date="20260302", repeat=2,
        output_dir=str(tmp_path / "persistent-output"), no_write=True,
        context_mode="all", fair_cache_dir=None, workers=1, core_only=False,
    )

    result = benchmark.run_benchmark(args)

    assert result["signatures_consistent"] is True
    assert result["median_seconds"]["state_machine"] == pytest.approx(0.3)
    assert all(item["output_dir"] is None for item in result["runs"])
    assert not (tmp_path / "persistent-output").exists()


def test_stage_zero_manifests_have_expected_contract_fields():
    stage0 = json.loads((FIXTURE_DIR / "stage0_smoke_manifest.json").read_text(encoding="utf-8"))
    full = json.loads((FIXTURE_DIR / "full_smoke_artifact_manifest.json").read_text(encoding="utf-8"))

    assert len(stage0["result_signature"]) == 64
    assert stage0["files"]["programmatic_grid_trades.csv"]["data_rows"] == 1
    assert full["files"]["programmatic_grid_trades.csv"]["data_rows"] == 711
    assert all(len(item["sha256"]) == 64 for item in full["files"].values())


@pytest.mark.skipif(
    os.environ.get("RUN_PROGRAMMATIC_GRID_STAGE0_SMOKE") != "1",
    reason="需要本地 2026-03-02 tick 数据；阶段 0 手工验收时显式开启",
)
def test_stage_zero_real_smoke_matches_baseline_signature():
    manifest = json.loads((FIXTURE_DIR / "stage0_smoke_manifest.json").read_text(encoding="utf-8"))
    config = load_programmatic_grid_config("config/programmatic_grid.stage0_smoke.json")

    result = run_programmatic_grid(config)

    assert benchmark.result_signature(result)["overall_sha256"] == manifest["result_signature"]
