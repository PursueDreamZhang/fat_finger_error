"""从确认深度候选生成阶段 6 的少量全天回放研究配置。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES = ROOT / "output/programmatic_parameter_candidates_202603_confirmed_depth/parameter_candidates.csv"
DEFAULT_STATISTICS = ROOT / "output/programmatic_parameter_candidates_202603_confirmed_depth/parameter_event_statistics.csv"
DEFAULT_OUTPUT = ROOT / "output/programmatic_grid_stage6_confirmed_depth_202603"
EVENTS_CSV = "output/20260301_20260331-range-confirmed-depth/tick_candidate_events_annotated_confirmed_depth.csv"

INSTRUMENTS = {
    "AP605": {"commodity": "AP", "fair_reference_contracts": ["AP604", "AP610", "AP612"], "hedge_contract": "AP604"},
    "PX605": {"commodity": "PX", "fair_reference_contracts": ["PX604", "PX606", "PX607"], "hedge_contract": "PX604"},
    "EB2606": {"commodity": "EB", "fair_reference_contracts": ["EB2605", "EB2607", "EB2608"], "hedge_contract": "EB2605"},
    "L2609": {"commodity": "L", "fair_reference_contracts": ["L2608", "L2610", "L2611"], "hedge_contract": "L2608"},
}


def _ceil_tick(value: float) -> int:
    return max(1, math.ceil(float(value) - 1e-12))


def _base_config(example_path: Path, output_dir: Path, target_contract: str) -> dict:
    config = json.loads(example_path.read_text(encoding="utf-8"))
    spec = INSTRUMENTS[target_contract]
    config["output_dir"] = str(output_dir)
    config["base"]["output_dir"] = str(output_dir)
    config["base"]["events_csv"] = EVENTS_CSV
    config["instruments"] = [{"name": target_contract, "target_contract": target_contract, **spec}]
    config["fair_cache_dir"] = str(output_dir / "fair_cache")
    config["context_mode"] = "none"
    return config


def prepare_stage6_configs(
    candidates_path: str | Path = DEFAULT_CANDIDATES,
    statistics_path: str | Path = DEFAULT_STATISTICS,
    output_root: str | Path = DEFAULT_OUTPUT,
    example_path: str | Path = ROOT / "config/programmatic_grid.example.json",
) -> pd.DataFrame:
    candidates = pd.read_csv(candidates_path, encoding="utf-8-sig", keep_default_na=False)
    statistics = pd.read_csv(statistics_path, encoding="utf-8-sig", keep_default_na=False)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []

    for target_contract, spec in INSTRUMENTS.items():
        stat_rows = statistics[statistics["target_contract"].eq(target_contract)]
        if stat_rows.empty:
            rows.append({"target_contract": target_contract, "replay_status": "skipped_missing_statistics"})
            continue
        stat = stat_rows.iloc[0]
        if stat["candidate_status"] != "research_candidate_ready":
            rows.append({
                "target_contract": target_contract,
                "replay_status": "skipped_not_ready",
                "candidate_status": stat["candidate_status"],
                "reason": "未达到 research_candidate_ready，不把统计结果直接当作回放参数",
            })
            continue

        regular = candidates[
            candidates["target_contract"].eq(target_contract)
            & candidates["T_source_quantile"].isin(["P50", "P70", "P85"])
            & (pd.to_numeric(candidates["W_heuristic_ratio"], errors="coerce") == 0.40)
            & candidates["S_mode"].eq("W_x_0.50")
        ].sort_values("T_source_quantile", key=lambda column: column.map({"P50": 0, "P70": 1, "P85": 2}))
        if set(regular["T_source_quantile"]) != {"P50", "P70", "P85"}:
            raise ValueError(f"{target_contract} 缺少 P50/P70/P85 低侧候选")

        regular_shapes = []
        for _, candidate in regular.iterrows():
            shape = {"W": int(candidate["W_ticks"]), "D": int(candidate["D_ticks"]), "S": int(candidate["S_ticks"])}
            regular_shapes.append(shape)
            rows.append({
                "target_contract": target_contract, "replay_group": "P50_P70_P85",
                "source_quantile": candidate["T_source_quantile"], "total_touch_ticks": int(candidate["total_touch_ticks"]),
                **shape, "W_heuristic_ratio": candidate["W_heuristic_ratio"],
                "replay_status": "prepared", "candidate_status": candidate["candidate_status"],
            })
        regular_dir = output_root / target_contract / "P50_P70_P85"
        regular_config = _base_config(Path(example_path), regular_dir, target_contract)
        regular_config["quote_shapes"] = regular_shapes
        regular_config["stage6_metadata"] = {"target_contract": target_contract, "research_group": "P50_P70_P85", "hypothesis": "确认深度低侧候选", "reference_selection": "人工冻结同品种相邻合约"}
        regular_dir.mkdir(parents=True, exist_ok=True)
        (regular_dir / "stage6_config.json").write_text(json.dumps(regular_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        p90 = _ceil_tick(float(stat["confirmed_depth_p90_ticks"]))
        width = _ceil_tick(p90 * 0.40)
        distance = p90 - width
        if distance <= 0:
            raise ValueError(f"{target_contract} P90 计算得到 D<=0")
        tail_shape = {"W": width, "D": distance, "S": _ceil_tick(width * 0.50)}
        rows.append({
            "target_contract": target_contract, "replay_group": "P90_tail",
            "source_quantile": "P90", "total_touch_ticks": p90, **tail_shape,
            "W_heuristic_ratio": 0.40, "replay_status": "prepared",
            "candidate_status": stat["candidate_status"], "risk_label": "tail_depth_candidate",
        })
        tail_dir = output_root / target_contract / "P90_tail"
        tail_config = _base_config(Path(example_path), tail_dir, target_contract)
        tail_config["quote_shapes"] = [tail_shape]
        tail_config["stage6_metadata"] = {"target_contract": target_contract, "research_group": "P90_tail", "hypothesis": "确认深度 P90 尾部候选", "risk_label": "tail_depth_candidate", "reference_selection": "人工冻结同品种相邻合约"}
        tail_dir.mkdir(parents=True, exist_ok=True)
        (tail_dir / "stage6_config.json").write_text(json.dumps(tail_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    selection = pd.DataFrame(rows)
    selection.to_csv(output_root / "stage6_candidate_selection.csv", index=False, encoding="utf-8-sig")
    (output_root / "stage6_selection_summary.json").write_text(json.dumps({"events_csv": EVENTS_CSV, "instruments": INSTRUMENTS, "replay_scope": "P50/P70/P85 low-side plus separate P90 tail", "selection_count": int((selection["replay_status"] == "prepared").sum())}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return selection


def main() -> None:
    parser = argparse.ArgumentParser(description="准备确认深度阶段 6 全天回放配置")
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--statistics", default=str(DEFAULT_STATISTICS))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    selection = prepare_stage6_configs(args.candidates, args.statistics, args.output_root)
    print(selection.to_string(index=False))


if __name__ == "__main__":
    main()
