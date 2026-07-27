"""从历史注释事件 CSV 严格重建可审计的确认深度。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.tick_detector.tick_io import COMMODITY_PROFILES


RECONSTRUCTION_COLUMNS = [
    "末笔向下偏离_跳", "区间均价向下偏离_跳", "一秒合并均价向下偏离_跳",
    "末笔触发阈值_跳", "区间均价触发阈值_跳",
]
OUTPUT_NAME = "tick_candidate_events_annotated_confirmed_depth.csv"
DEFAULT_OUTPUT_DIR = Path("output/20260301_20260331-range-confirmed-depth")


def _number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _derive_row(row: pd.Series) -> tuple[float | None, str, str]:
    values = {column: _number(row.get(column)) for column in RECONSTRUCTION_COLUMNS}
    if any(value is None for value in values.values()):
        return None, "legacy_depth_unverifiable", ""
    visible_hit = values["末笔向下偏离_跳"] >= values["末笔触发阈值_跳"]
    interval_hit = (
        values["区间均价向下偏离_跳"] >= values["区间均价触发阈值_跳"]
        and values["一秒合并均价向下偏离_跳"] >= values["区间均价触发阈值_跳"]
    )
    depths = []
    channels = []
    if visible_hit and values["末笔向下偏离_跳"] > 0:
        depths.append(values["末笔向下偏离_跳"])
        channels.append("visible")
    if interval_hit and values["一秒合并均价向下偏离_跳"] > 0:
        depths.append(values["一秒合并均价向下偏离_跳"])
        channels.append("interval")
    if not depths:
        return None, "legacy_depth_unverifiable", ""
    return max(depths), "legacy_components_derived", "+".join(channels)


def migrate_legacy_depth(input_path: str | Path, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_path = output_dir / OUTPUT_NAME
    if input_path.resolve() == output_path.resolve():
        raise ValueError("迁移输出不得覆盖输入注释 CSV")
    events = pd.read_csv(input_path, encoding="utf-8-sig", keep_default_na=False)
    missing_annotation = [column for column in ("交易日", "事件编号", "品种", "日线边界判定") if column not in events.columns]
    if missing_annotation:
        raise ValueError(f"输入必须是规范注释 CSV，缺少字段：{', '.join(missing_annotation)}")
    for column in RECONSTRUCTION_COLUMNS:
        if column not in events.columns:
            events[column] = ""

    derived = events.apply(_derive_row, axis=1, result_type="expand")
    derived.columns = ["事件确认深度_跳", "确认深度来源", "确认深度命中通道"]
    output = events.drop(columns=["事件确认深度_跳", "事件确认深度_基点", "确认深度来源", "确认深度命中通道"], errors="ignore").copy()
    output = pd.concat([output, derived], axis=1)

    def bps(row: pd.Series) -> float | None:
        depth = _number(row["事件确认深度_跳"])
        fair = _number(row.get("合理价"))
        commodity = str(row.get("品种", "")).strip().upper()
        profile = COMMODITY_PROFILES.get(commodity, {})
        tick_size = _number(profile.get("tick_size"))
        if depth is None or fair is None or fair <= 0 or tick_size is None:
            return None
        return depth * tick_size / fair * 10000

    output["事件确认深度_基点"] = output.apply(bps, axis=1)
    output["迁移规则版本"] = "legacy_components_v1"
    output_dir.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_path, index=False, encoding="utf-8-sig")
    return {
        "output_path": output_path,
        "input_event_count": len(events),
        "derived_event_count": int(output["确认深度来源"].eq("legacy_components_derived").sum()),
        "unverifiable_event_count": int(output["确认深度来源"].eq("legacy_depth_unverifiable").sum()),
    }
