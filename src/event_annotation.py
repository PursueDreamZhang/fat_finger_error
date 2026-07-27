"""为检测事件绑定可审计的日线边界审查结果（只读 CSV，不读取 Tick 数据）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


EVENT_KEY_COLUMNS = ["交易日", "事件编号"]
BOUNDARY_COLUMNS = ["交易日", "事件编号", "日线边界判定", "排除原因", "规则或数据版本"]
SUPPORTED_BOUNDARY_VALUES = {"保留", "建议排除"}
ANNOTATION_COLUMNS = [
    "日线边界规则或数据版本",
    "日线边界清单SHA256",
    "源事件CSV SHA256",
]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: str | Path) -> pd.DataFrame:
    return pd.read_csv(path, encoding="utf-8-sig", dtype=str, keep_default_na=False)


def _missing(frame: pd.DataFrame, required: list[str]) -> list[str]:
    return [column for column in required if column not in frame.columns]


def _normalise(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for column in EVENT_KEY_COLUMNS:
        if column in out.columns:
            out[column] = out[column].astype(str).str.strip()
    return out


def _keys(frame: pd.DataFrame) -> pd.Series:
    return frame[EVENT_KEY_COLUMNS].astype(str).agg("\x1f".join, axis=1)


def _manifest_commodities(value: Any) -> list[str]:
    if isinstance(value, str):
        return sorted({item.strip() for item in value.split(",") if item.strip()})
    if isinstance(value, list):
        return sorted({str(item).strip() for item in value if str(item).strip()})
    raise ValueError("manifest.commodities 必须是列表或逗号分隔字符串")


def annotate_events(
    source_events_path: str | Path,
    boundary_csv_path: str | Path,
    manifest_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    """校验范围绑定输入并输出完整注释 CSV。"""
    source_events_path = Path(source_events_path)
    boundary_csv_path = Path(boundary_csv_path)
    manifest_path = Path(manifest_path)
    output_path = Path(output_path)
    source = _normalise(_read_csv(source_events_path))
    boundary = _normalise(_read_csv(boundary_csv_path))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    missing_source = _missing(source, EVENT_KEY_COLUMNS + ["品种"])
    if missing_source:
        raise ValueError(f"源事件 CSV 缺少必需字段：{', '.join(missing_source)}")
    missing_boundary = _missing(boundary, BOUNDARY_COLUMNS)
    if missing_boundary:
        raise ValueError(f"日线边界清单缺少必需字段：{', '.join(missing_boundary)}")
    source_keys = _keys(source)
    boundary_keys = _keys(boundary)
    if source_keys.duplicated().any():
        raise ValueError("源事件 CSV 的复合键 (交易日, 事件编号) 不唯一")
    if boundary_keys.duplicated().any():
        raise ValueError("日线边界清单的复合键 (交易日, 事件编号) 不唯一")
    unmatched = sorted(set(boundary_keys) - set(source_keys))
    if unmatched:
        raise ValueError(f"日线边界清单包含源 CSV 不存在的事件键：{unmatched[0]}")
    invalid_values = sorted(set(boundary["日线边界判定"]) - SUPPORTED_BOUNDARY_VALUES)
    if invalid_values:
        raise ValueError(f"日线边界判定包含不支持的值：{', '.join(invalid_values)}")

    source_hash = sha256_file(source_events_path)
    if str(manifest.get("source_events_sha256", "")).lower() != source_hash:
        raise ValueError("manifest.source_events_sha256 与源事件 CSV 不匹配")
    actual_dates = (str(source["交易日"].min()), str(source["交易日"].max())) if not source.empty else ("", "")
    expected_dates = (str(manifest.get("trade_date_start", "")), str(manifest.get("trade_date_end", "")))
    if actual_dates != expected_dates:
        raise ValueError(f"manifest 日期范围不匹配：实际 {actual_dates[0]}~{actual_dates[1]}，声明 {expected_dates[0]}~{expected_dates[1]}")
    actual_commodities = sorted(set(source["品种"]))
    if actual_commodities != _manifest_commodities(manifest.get("commodities")):
        raise ValueError("manifest.commodities 与源事件 CSV 不匹配")
    if int(manifest.get("expected_event_count", -1)) != len(source):
        raise ValueError("manifest.expected_event_count 与源事件 CSV 不匹配")
    rule_version = str(manifest.get("boundary_rule_or_data_version", "")).strip()
    if not rule_version:
        raise ValueError("manifest 缺少 boundary_rule_or_data_version")
    declared_boundary_hash = manifest.get("boundary_csv_sha256")
    boundary_hash = sha256_file(boundary_csv_path)
    if declared_boundary_hash is not None and str(declared_boundary_hash).lower() != boundary_hash:
        raise ValueError("manifest.boundary_csv_sha256 与日线边界清单不匹配")

    annotations = boundary.set_index(boundary_keys)[["日线边界判定"]]
    output = source.copy()
    output["日线边界判定"] = [annotations.loc[key, "日线边界判定"] if key in annotations.index else "保留" for key in source_keys]
    output["日线边界规则或数据版本"] = rule_version
    output["日线边界清单SHA256"] = boundary_hash
    output["源事件CSV SHA256"] = source_hash
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(output_path, index=False, encoding="utf-8-sig")
    return {
        "output_path": output_path,
        "source_events_sha256": source_hash,
        "boundary_csv_sha256": boundary_hash,
        "trade_date_start": actual_dates[0],
        "trade_date_end": actual_dates[1],
        "event_count": len(output),
    }
