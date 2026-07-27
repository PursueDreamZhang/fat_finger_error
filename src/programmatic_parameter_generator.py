"""从规范乌龙指事件 CSV 生成每个合约唯一的一组低侧 T/W/D/S 深度。"""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
import math
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = {
    "品种", "合约", "交易日", "事件编号", "突发偏离_跳", "事件确认深度_跳",
    "事件确认深度_基点", "确认深度来源", "回归标签",
    "可见末笔恢复确认秒数", "区间均价恢复确认秒数", "买一恢复确认秒数",
    "有效参考合约数", "合理价不确定性_跳", "数据质量标记", "日线边界判定",
    "日线边界规则或数据版本", "日线边界清单SHA256", "源事件CSV SHA256",
}
RANGE_BINDING_COLUMNS = ["日线边界规则或数据版本", "日线边界清单SHA256", "源事件CSV SHA256"]
RANGE_BINDING_HASH_COLUMNS = {"日线边界清单SHA256", "源事件CSV SHA256"}
NUMERIC_COLUMNS = ["事件确认深度_跳", "有效参考合约数", "合理价不确定性_跳"]
SHAPE_COLUMNS = ["commodity", "target_contract", "T_ticks", "W_ticks", "D_ticks", "S_ticks"]


@dataclass(frozen=True)
class ParameterGeneratorConfig:
    min_eligible_samples: int = 8
    max_fair_uncertainty_ticks: float = 10.0
    allowed_data_quality: frozenset[str] = frozenset({""})
    allowed_daily_boundary: frozenset[str] = frozenset({"保留"})
    allowed_regression_labels: frozenset[str] = frozenset({
        "trade_recovered_3s", "quote_only_recovered_3s", "persistent_10s",
        "trade_recovered_10s", "quote_only_recovered_10s", "truncated",
    })
    total_touch_quantile: float = 0.70
    width_ratio: float = 0.40

    def __post_init__(self) -> None:
        if not isinstance(self.min_eligible_samples, int) or self.min_eligible_samples < 1:
            raise ValueError("min_eligible_samples 必须是正整数")
        for name, value in (
            ("max_fair_uncertainty_ticks", self.max_fair_uncertainty_ticks),
            ("total_touch_quantile", self.total_touch_quantile),
            ("width_ratio", self.width_ratio),
        ):
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} 必须是数字") from exc
            if not math.isfinite(number):
                raise ValueError(f"{name} 必须是有限数字")
            if name == "max_fair_uncertainty_ticks" and number < 0:
                raise ValueError(f"{name} 不能为负数")
            if name != "max_fair_uncertainty_ticks" and not 0 < number < 1:
                raise ValueError(f"{name} 必须满足 0 < value < 1")


def load_parameter_generator_config(path: str | Path | None = None) -> ParameterGeneratorConfig:
    if path is None:
        return ParameterGeneratorConfig()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = {item.name for item in fields(ParameterGeneratorConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"生成器配置包含未知字段：{', '.join(unknown)}")
    for name in ("allowed_data_quality", "allowed_daily_boundary", "allowed_regression_labels"):
        if name in raw:
            raw[name] = frozenset(raw[name])
    return ParameterGeneratorConfig(**raw)


def _validate_input_contract(events: pd.DataFrame) -> None:
    missing = sorted(REQUIRED_COLUMNS - set(events.columns))
    if missing:
        raise ValueError(f"输入 CSV 缺少必需字段：{', '.join(missing)}")
    if events.empty:
        return
    for column in RANGE_BINDING_COLUMNS:
        values = events[column].astype("string").str.strip()
        if values.isna().any() or values.eq("").any():
            raise ValueError(f"范围绑定元数据不能为空：{column}")
        if column in RANGE_BINDING_HASH_COLUMNS and (~values.str.fullmatch(r"[0-9a-fA-F]{64}")).any():
            raise ValueError(f"范围绑定元数据不是有效 SHA-256：{column}")
        if values.nunique(dropna=True) != 1:
            raise ValueError(f"范围绑定元数据必须逐行一致：{column}")


def _normalize_events(events: pd.DataFrame) -> pd.DataFrame:
    _validate_input_contract(events)
    normalized = events.copy()
    for column in NUMERIC_COLUMNS:
        normalized[column] = pd.to_numeric(normalized[column].replace("", pd.NA), errors="coerce")
    for column in ("品种", "合约", "回归标签", "数据质量标记", "日线边界判定"):
        normalized[column] = normalized[column].astype(str).str.strip()
    normalized["确认深度来源"] = normalized["确认深度来源"].astype("string").str.strip()
    return normalized


def load_events(path: str | Path) -> pd.DataFrame:
    """读取并规范化输入；范围绑定缺失或不一致时立即失败。"""
    return _normalize_events(pd.read_csv(path, keep_default_na=False))


def _ceil_tick(value: float) -> int:
    return max(1, math.ceil(float(value) - 1e-12))


def _is_eligible(row: pd.Series, config: ParameterGeneratorConfig) -> bool:
    source = row["确认深度来源"]
    return (
        row["数据质量标记"] in config.allowed_data_quality
        and row["日线边界判定"] in config.allowed_daily_boundary
        and row["回归标签"] in config.allowed_regression_labels
        and pd.notna(row["有效参考合约数"])
        and row["有效参考合约数"] >= 2
        and pd.notna(row["合理价不确定性_跳"])
        and row["合理价不确定性_跳"] <= float(config.max_fair_uncertainty_ticks)
        and pd.notna(row["事件确认深度_跳"])
        and row["事件确认深度_跳"] > 0
        and pd.notna(source)
        and bool(str(source).strip())
    )


def build_parameter_shapes(events: pd.DataFrame, config: ParameterGeneratorConfig | None = None) -> pd.DataFrame:
    """每个合格合约只生成一组 P70 / 40% / 半 W 的 T/W/D/S。"""
    config = config or ParameterGeneratorConfig()
    events = _normalize_events(events)
    eligible = events.loc[events.apply(_is_eligible, axis=1, config=config)]
    shapes: list[dict[str, int | str]] = []
    for (commodity, contract), group in eligible.groupby(["品种", "合约"], sort=True):
        if len(group) < config.min_eligible_samples:
            continue
        total = _ceil_tick(group["事件确认深度_跳"].quantile(float(config.total_touch_quantile)))
        width = _ceil_tick(total * float(config.width_ratio))
        distance = total - width
        if distance <= 0:
            continue
        shapes.append({
            "commodity": commodity,
            "target_contract": contract,
            "T_ticks": total,
            "W_ticks": width,
            "D_ticks": distance,
            "S_ticks": _ceil_tick(width * 0.50),
        })
    return pd.DataFrame(shapes, columns=SHAPE_COLUMNS)


def write_parameter_shapes(shapes: pd.DataFrame, output_dir: str | Path) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "parameter_shapes.csv"
    shapes.to_csv(path, index=False)
    return path
