from __future__ import annotations

import pytest

from run_tick_detector import parse_args


def test_tick_day_path_is_required():
    with pytest.raises(SystemExit):
        parse_args([])


def test_output_dir_defaults_to_tick_detector_suffix():
    args = parse_args(["--tick-day-path", "data/tick2026/202605/20260520.zip"])
    assert args.output_dir.endswith("-tick-detector")
    assert args.output_dir.startswith("output/")


def test_commodity_and_contract_only_affect_target_filters():
    args = parse_args(
        [
            "--tick-day-path",
            "data/tick2026/202605/20260520.zip",
            "--commodity",
            "AU",
            "--contract",
            "AU2606",
        ]
    )
    assert args.tick_day_path == "data/tick2026/202605/20260520.zip"
    assert args.commodity == "AU"
    assert args.contract == "AU2606"
