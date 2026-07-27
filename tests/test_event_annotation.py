from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from src.event_annotation import annotate_events, sha256_file
from src.programmatic_parameter_generator import load_events


def _write_inputs(tmp_path):
    events = pd.DataFrame([
        {"交易日": "20260301", "事件编号": "C1", "品种": "NI", "合约": "NI2605"},
        {"交易日": "20260302", "事件编号": "C1", "品种": "NI", "合约": "NI2605"},
    ])
    events_path = tmp_path / "tick_candidate_events.csv"
    events.to_csv(events_path, index=False, encoding="utf-8-sig")
    boundary = pd.DataFrame([{
        "交易日": "20260302", "事件编号": "C1", "日线边界判定": "建议排除",
        "排除原因": "daily_low_guard", "规则或数据版本": "guard-v1",
    }])
    boundary_path = tmp_path / "exclude_by_daily_guard.csv"
    boundary.to_csv(boundary_path, index=False, encoding="utf-8-sig")
    manifest = {
        "source_events_sha256": sha256_file(events_path),
        "trade_date_start": "20260301", "trade_date_end": "20260302",
        "commodities": ["NI"], "expected_event_count": 2,
        "boundary_rule_or_data_version": "guard-v1",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return events_path, boundary_path, manifest_path


def test_annotation_binds_by_composite_key_and_is_generator_input(tmp_path):
    events_path, boundary_path, manifest_path = _write_inputs(tmp_path)
    output = tmp_path / "tick_candidate_events_annotated.csv"
    annotate_events(events_path, boundary_path, manifest_path, output)
    annotated = pd.read_csv(output, encoding="utf-8-sig", keep_default_na=False)
    assert list(annotated["日线边界判定"]) == ["保留", "建议排除"]
    assert annotated.loc[0, "日线边界规则或数据版本"] == "guard-v1"
    assert annotated.loc[0, "日线边界清单SHA256"] == sha256_file(boundary_path)
    assert annotated.loc[0, "源事件CSV SHA256"] == sha256_file(events_path)


@pytest.mark.parametrize("manifest_change,match", [
    ({"source_events_sha256": "bad"}, "sha256"),
    ({"trade_date_start": "20260302"}, "日期范围"),
    ({"commodities": ["AU"]}, "commodities"),
    ({"expected_event_count": 3}, "expected_event_count"),
])
def test_annotation_rejects_manifest_mismatch(tmp_path, manifest_change, match):
    events_path, boundary_path, manifest_path = _write_inputs(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(manifest_change)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        annotate_events(events_path, boundary_path, manifest_path, tmp_path / "out.csv")


def test_annotation_rejects_incomplete_or_unmatched_boundary(tmp_path):
    events_path, boundary_path, manifest_path = _write_inputs(tmp_path)
    pd.DataFrame([{ "交易日": "20260301", "事件编号": "C1" }]).to_csv(boundary_path, index=False)
    with pytest.raises(ValueError, match="缺少必需字段"):
        annotate_events(events_path, boundary_path, manifest_path, tmp_path / "out.csv")

    boundary = pd.DataFrame([{
        "交易日": "20260303", "事件编号": "C9", "日线边界判定": "建议排除",
        "排除原因": "x", "规则或数据版本": "guard-v1",
    }])
    boundary.to_csv(boundary_path, index=False)
    with pytest.raises(ValueError, match="不存在"):
        annotate_events(events_path, boundary_path, manifest_path, tmp_path / "out.csv")
