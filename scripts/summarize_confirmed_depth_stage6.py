"""汇总阶段 6 确认深度候选的全天回放结果。"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


DEFAULT_ROOT = Path("output/programmatic_grid_stage6_confirmed_depth_202603")


def _quantile_for(group: str, shape_index: int) -> str:
    return "P90" if group == "P90_tail" else {1: "P50", 2: "P70", 3: "P85"}[shape_index]


def summarize_stage6(output_root: str | Path = DEFAULT_ROOT) -> tuple[pd.DataFrame, pd.DataFrame]:
    output_root = Path(output_root)
    result_frames: list[pd.DataFrame] = []
    for summary_path in sorted(output_root.glob("*/**/programmatic_grid_summary.csv")):
        group_dir = summary_path.parent
        contract = group_dir.parent.name
        group = group_dir.name
        summary = pd.read_csv(summary_path, encoding="utf-8-sig", keep_default_na=False)
        summary["target_contract"] = contract
        summary["research_group"] = group
        summary["shape_index"] = summary["scenario_id"].str.extract(r"Q(\d+)", expand=False).astype(int)
        summary["quantile"] = summary["shape_index"].map(lambda value: _quantile_for(group, int(value)))
        summary["replay_output_dir"] = str(group_dir)
        result_frames.append(summary)
    if not result_frames:
        raise FileNotFoundError(f"未找到阶段 6 回放 summary：{output_root}")
    results = pd.concat(result_frames, ignore_index=True)
    results["passed"] = results["selection_reason"].eq("")
    results.to_csv(output_root / "stage6_replay_results.csv", index=False, encoding="utf-8-sig")

    rows: list[dict[str, object]] = []
    for (contract, group, quantile), frame in results.groupby(["target_contract", "research_group", "quantile"], sort=True):
        ranked = frame.sort_values(
            ["passed", "normal_move_fill_rate", "hedge_failure_rate", "net_pnl", "peak_order_actions_per_minute"],
            ascending=[False, True, True, False, True],
        ).iloc[0]
        rows.append({
            "target_contract": contract, "research_group": group, "quantile": quantile,
            "scenario_count": len(frame), "passed_scenario_count": int(frame["passed"].sum()),
            "best_scenario_id": ranked["scenario_id"], "best_latency": f"L{int(ranked['scenario_id'].split('-L')[1]):02d}",
            "total_touch_ticks": float(ranked["band_half_width_ticks"] + ranked["outer_quote_offset_ticks"]),
            "W_ticks": float(ranked["band_half_width_ticks"]), "D_ticks": float(ranked["outer_quote_offset_ticks"]),
            "S_ticks": float(ranked["reanchor_step_ticks"]), "detector_event_fill_count": int(ranked["detector_event_fill_count"]),
            "normal_move_fill_rate": float(ranked["normal_move_fill_rate"]), "hedge_failure_count": int(ranked["hedge_failure_count"]),
            "hedge_failure_rate": float(ranked["hedge_failure_rate"]), "net_pnl": float(ranked["net_pnl"]),
            "worst_day_net_pnl": float(ranked["worst_day_net_pnl"]), "peak_order_actions_per_minute": int(ranked["peak_order_actions_per_minute"]),
            "decision": "replay_passed" if bool(ranked["passed"]) else "rejected_by_replay_thresholds",
            "selection_reason": ranked["selection_reason"],
        })
    acceptance = pd.DataFrame(rows)
    acceptance.to_csv(output_root / "stage6_acceptance_summary.csv", index=False, encoding="utf-8-sig")
    lines = [
        "# 阶段 6：确认深度真实样本验收与全天回放",
        "",
        "输入为阶段 5 固定迁移产物；回放范围为 20260301–20260331。每个合约的 P50/P70/P85 使用一个低侧研究形状，P90 单独作为尾部候选；每个形状保留既有 3 个 latency profile。",
        "",
        f"- 回放场景：{len(results)}",
        f"- 通过回放阈值场景：{int(results['passed'].sum())}",
        f"- 研究候选分组：{len(acceptance)}",
        "",
        "## 分组验收",
        "",
        "|合约|分组|分位数|最佳场景|T|W|D|S|候选事件成交|正常行情成交率|对冲失败率|净收益|最差日|动作峰值|结论|",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for _, row in acceptance.sort_values(["target_contract", "research_group", "quantile"]).iterrows():
        vals = [row["target_contract"], row["research_group"], row["quantile"], row["best_scenario_id"], row["total_touch_ticks"], row["W_ticks"], row["D_ticks"], row["S_ticks"], row["detector_event_fill_count"], f"{row['normal_move_fill_rate']:.3f}", f"{row['hedge_failure_rate']:.3f}", row["net_pnl"], row["worst_day_net_pnl"], row["peak_order_actions_per_minute"], row["decision"]]
        lines.append("|" + "|".join(str(value) for value in vals) + "|")
    lines.extend([
        "",
        "## 判定口径",
        "",
        "回放器同时检查候选事件成交、normal_move_fill、对冲失败、净收益、最差日和报撤动作峰值。任何阈值失败都不视为研究候选通过；生成器候选仍不是最终参数选择。",
    ])
    (output_root / "stage6_acceptance_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results, acceptance


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总确认深度阶段 6 全天回放结果")
    parser.add_argument("--output-root", default=str(DEFAULT_ROOT))
    args = parser.parse_args()
    results, acceptance = summarize_stage6(args.output_root)
    print(f"scenarios={len(results)}")
    print(f"passed={int(results['passed'].sum())}")
    print(acceptance.to_string(index=False))


if __name__ == "__main__":
    main()
