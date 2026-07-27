from __future__ import annotations

import pandas as pd
import pytest

from src.legacy_depth_migration import migrate_legacy_depth
from src.programmatic_parameter_generator import load_events


BASE = {
    "交易日": "20260301", "事件编号": "E1", "品种": "NI", "合约": "NI2605",
    "日线边界判定": "保留", "日线边界规则或数据版本": "guard-v1",
    "日线边界清单SHA256": "b" * 64, "源事件CSV SHA256": "a" * 64,
    "触发原因": "visible_execution_drop", "合理价": 16000,
    "回归标签": "trade_recovered_3s", "可见末笔恢复确认秒数": 0.5,
    "区间均价恢复确认秒数": "", "买一恢复确认秒数": 1.0,
    "有效参考合约数": 3, "合理价不确定性_跳": 1, "数据质量标记": "",
    "突发偏离_跳": 3,
    "末笔向下偏离_跳": 10, "区间均价向下偏离_跳": 5,
    "一秒合并均价向下偏离_跳": 4, "末笔触发阈值_跳": 8,
    "区间均价触发阈值_跳": 8,
}


def _write_source(tmp_path, rows):
    path = tmp_path / "annotated.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    return path


def test_migration_uses_only_actual_hit_channels_and_derives_bps(tmp_path):
    rows = [
        BASE,
        {**BASE, "事件编号": "E2", "末笔向下偏离_跳": 3, "区间均价向下偏离_跳": 20, "一秒合并均价向下偏离_跳": 25},
        {**BASE, "事件编号": "E3", "末笔向下偏离_跳": 10, "区间均价向下偏离_跳": 30, "一秒合并均价向下偏离_跳": 5},
        {**BASE, "事件编号": "E4", "末笔向下偏离_跳": 3, "区间均价向下偏离_跳": 5, "一秒合并均价向下偏离_跳": 4},
    ]
    source = _write_source(tmp_path, rows)
    result = migrate_legacy_depth(source, tmp_path / "out")
    output = pd.read_csv(result["output_path"], encoding="utf-8-sig", keep_default_na=False)
    assert list(output["事件确认深度_跳"].astype(str)) == ["10.0", "25.0", "10.0", ""]
    assert list(output["确认深度命中通道"]) == ["visible", "interval", "visible", ""]
    assert output.loc[0, "确认深度来源"] == "legacy_components_derived"
    assert output.loc[3, "确认深度来源"] == "legacy_depth_unverifiable"
    assert float(output.loc[0, "事件确认深度_基点"]) == pytest.approx(62.5)


def test_missing_combined_vwap_is_unverifiable_and_source_is_not_overwritten(tmp_path):
    source = _write_source(tmp_path, [{**BASE, "一秒合并均价向下偏离_跳": ""}])
    result = migrate_legacy_depth(source, tmp_path / "out")
    assert result["unverifiable_event_count"] == 1
    assert source.resolve() != result["output_path"].resolve()


def test_migrated_csv_satisfies_generator_contract(tmp_path):
    source = _write_source(tmp_path, [BASE])
    result = migrate_legacy_depth(source, tmp_path / "out")
    events = load_events(result["output_path"])
    assert events.loc[0, "事件确认深度_跳"] == 10
    assert events.loc[0, "确认深度来源"] == "legacy_components_derived"
