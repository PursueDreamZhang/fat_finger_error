import json

import pandas as pd
import pytest

from src.programmatic_parameter_generator import (
    ParameterGeneratorConfig,
    build_parameter_shapes,
    load_events,
    load_parameter_generator_config,
    write_parameter_shapes,
)


SOURCE_SHA = "a" * 64
BOUNDARY_SHA = "b" * 64


def _events(rows=8, contract="NI2605"):
    return pd.DataFrame([{
        "品种": "NI", "合约": contract, "交易日": f"202603{index // 2 + 1:02d}", "事件编号": str(index),
        "突发偏离_跳": float(index + 1), "事件确认深度_跳": float(index + 2),
        "事件确认深度_基点": float(index + 2) / 10, "确认深度来源": "detector_event_depth",
        "日线边界规则或数据版本": "test-v1", "日线边界清单SHA256": BOUNDARY_SHA, "源事件CSV SHA256": SOURCE_SHA,
        "回归标签": "trade_recovered_3s",
        "可见末笔恢复确认秒数": 0.5, "区间均价恢复确认秒数": "", "买一恢复确认秒数": 1.0,
        "有效参考合约数": 3, "合理价不确定性_跳": 1.0, "数据质量标记": "", "日线边界判定": "保留",
    } for index in range(rows)])


def test_missing_columns_are_rejected(tmp_path):
    path = tmp_path / "bad.csv"
    pd.DataFrame({"品种": ["NI"]}).to_csv(path, index=False)
    with pytest.raises(ValueError, match="缺少必需字段"):
        load_events(path)


@pytest.mark.parametrize("column", ["日线边界规则或数据版本", "日线边界清单SHA256", "源事件CSV SHA256"])
def test_range_binding_metadata_cannot_be_blank_or_bypassed(column):
    events = _events()
    events[column] = ""
    with pytest.raises(ValueError, match="范围绑定元数据不能为空"):
        build_parameter_shapes(events)


def test_load_events_rejects_blank_range_binding_metadata(tmp_path):
    events = _events()
    events["源事件CSV SHA256"] = ""
    path = tmp_path / "blank-range-binding.csv"
    events.to_csv(path, index=False)
    with pytest.raises(ValueError, match="范围绑定元数据不能为空"):
        load_events(path)


def test_range_binding_metadata_must_be_consistent_and_sha256_shaped():
    events = _events()
    events.loc[0, "日线边界规则或数据版本"] = "other-version"
    with pytest.raises(ValueError, match="逐行一致"):
        build_parameter_shapes(events)
    events = _events()
    events.loc[0, "源事件CSV SHA256"] = "not-a-sha256"
    with pytest.raises(ValueError, match="SHA-256"):
        build_parameter_shapes(events)


def test_each_contract_gets_p70_p85_and_w_s_combinations():
    shapes = build_parameter_shapes(_events())
    assert list(shapes.columns) == ["commodity", "target_contract", "T_pct", "W_pct", "D_pct", "S_pct"]
    assert len(shapes) == 8
    assert shapes["T_pct"].between(0, 100).all()
    assert (shapes["T_pct"] - shapes["W_pct"] - shapes["D_pct"]).abs().lt(1e-12).all()


def test_onset_does_not_change_shapes_but_missing_bps_is_excluded():
    left = build_parameter_shapes(_events())
    changed = _events()
    changed["突发偏离_跳"] = 999.0
    pd.testing.assert_frame_equal(left, build_parameter_shapes(changed))
    changed["事件确认深度_基点"] = ""
    assert build_parameter_shapes(changed).empty
    changed = _events()
    changed["事件确认深度_基点"] = 10000
    assert build_parameter_shapes(changed).empty


def test_invalid_events_are_filtered_and_insufficient_contracts_are_omitted():
    events = _events()
    events.loc[0, "事件确认深度_跳"] = float("nan")
    other = _events(rows=4, contract="NI2606")
    assert build_parameter_shapes(pd.concat([events, other], ignore_index=True), ParameterGeneratorConfig(min_eligible_samples=8)).empty


def test_missing_depth_source_is_not_a_usable_event():
    events = _events()
    events.loc[0, "确认深度来源"] = pd.NA
    assert build_parameter_shapes(events, ParameterGeneratorConfig(min_eligible_samples=8)).empty


@pytest.mark.parametrize("kwargs", [
    {"min_eligible_samples": 0},
    {"total_touch_quantiles": (0,)},
    {"total_touch_quantiles": (1,)},
    {"width_ratios": (0,)},
    {"width_ratios": (1,)},
    {"step_ratios": (0,)},
    {"step_ratios": (1.1,)},
])
def test_parameter_config_rejects_invalid_combination_values(kwargs):
    with pytest.raises(ValueError):
        ParameterGeneratorConfig(**kwargs)


def test_json_config_can_limit_combination_dimensions(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"total_touch_quantiles": [0.5], "width_ratios": [0.4], "step_ratios": [0.5]}), encoding="utf-8")
    shapes = build_parameter_shapes(_events(), load_parameter_generator_config(path))
    assert shapes.iloc[0]["T_pct"] == pytest.approx(0.0055)
    assert len(shapes) == 1


def test_explicit_three_quantiles_restores_p50():
    shapes = build_parameter_shapes(_events(), ParameterGeneratorConfig(total_touch_quantiles=(0.5, 0.7, 0.85)))

    assert set(shapes["T_pct"].round(6)) == {0.0055, 0.0069, 0.00795}
    assert len(shapes) == 12


def test_default_threshold_is_four_eligible_events():
    assert not build_parameter_shapes(_events(rows=4)).empty
    assert build_parameter_shapes(_events(rows=3)).empty


def test_directional_input_only_generates_low_side_down_parameters():
    events = _events()
    events["异常方向"] = ["down"] * 4 + ["up"] * 4
    pd.testing.assert_frame_equal(build_parameter_shapes(events), build_parameter_shapes(_events(rows=4)))
    events["异常方向"] = "up"
    assert build_parameter_shapes(events).empty
    events = _events()
    events["异常方向"] = "sideways"
    with pytest.raises(ValueError, match="异常方向"):
        build_parameter_shapes(events)


def test_output_is_one_csv_with_only_contract_and_shape_columns(tmp_path):
    path = write_parameter_shapes(build_parameter_shapes(_events()), tmp_path)
    assert path.name == "parameter_shapes.csv"
    assert list(pd.read_csv(path).columns) == ["commodity", "target_contract", "T_pct", "W_pct", "D_pct", "S_pct"]
