#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
乌龙指策略自动选参工具

输入：
    1. volatility_quantiles.csv
    2. event_details.csv

输出：
    1. auto_parameters.csv
    2. candidate_evaluation.csv

核心流程：
    SafeDistance
        -> 候选距离筛选
        -> EventCount / IsolationRate / MonthlyOpportunity
        -> 选择最小合格 TargetDistance
        -> Min / Max 价值带

距离统一使用小数：
    0.01 = 1%
    0.0025 = 0.25%

依赖：
    pip install pandas numpy
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import numpy as np
import pandas as pd


def parse_args():
    p = argparse.ArgumentParser(description="乌龙指策略自动选参")

    p.add_argument("--volatility", required=True,
                   help="volatility_quantiles.csv")
    p.add_argument("--events", required=True,
                   help="event_details.csv")
    p.add_argument("--output", required=True,
                   help="输出目录")

    p.add_argument("--horizon", type=int, default=5,
                   help="正常波动和 FollowRatio 观察窗口，默认 5 秒")
    p.add_argument("--percentile", type=float, default=99.9,
                   help="正常波动分位数，默认 99.9")
    p.add_argument("--safety-factor", type=float, default=1.2,
                   help="安全系数，默认 1.2")

    p.add_argument("--candidate-step", type=float, default=0.0025,
                   help="候选距离步长，小数形式；0.0025=0.25%%")
    p.add_argument("--max-candidate-distance", type=float, default=0.05,
                   help="候选距离最大值，默认 0.05=5%%")

    p.add_argument("--min-events", type=int, default=30,
                   help="候选距离最低事件数，默认 30")
    p.add_argument("--max-follow-ratio", type=float, default=0.25,
                   help="FollowRatio <= 此值定义为孤立事件，默认 0.25")
    p.add_argument("--min-isolation-rate", type=float, default=0.70,
                   help="最低孤立事件率，默认 0.70")
    p.add_argument("--min-monthly-opportunity", type=float, default=3.0,
                   help="最低月均孤立事件数，默认 3")

    p.add_argument("--band-ratio", type=float, default=0.30,
                   help="对称价值带比例，默认 0.30")
    p.add_argument("--trading-days-per-month", type=float, default=21.0,
                   help="每月交易日，默认 21")
    p.add_argument("--observed-trading-days", type=float, default=None,
                   help="历史样本总交易日数；建议显式传入，如 120")

    p.add_argument("--mode", choices=["strict", "raw", "all"], default="strict",
                   help="event_details 的事件模式，默认 strict")

    return p.parse_args()


def q_column(percentile: float) -> str:
    if float(percentile).is_integer():
        return f"Q{int(percentile)}"
    return f"Q{percentile}"


def normalize_direction(v: str) -> str:
    x = str(v).strip().lower()
    if x in {"buy", "down", "long"}:
        return "BUY"
    if x in {"sell", "up", "short"}:
        return "SELL"
    return str(v).upper()


def load_safe_distances(path: str, horizon: int,
                        percentile: float,
                        safety_factor: float) -> pd.DataFrame:
    df = pd.read_csv(path)

    required = {
        "InstrumentID",
        "HorizonSeconds",
        "Direction",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"volatility_quantiles.csv 缺少字段: {sorted(missing)}"
        )

    qcol = q_column(percentile)
    if qcol not in df.columns:
        raise ValueError(
            f"volatility_quantiles.csv 没有 {qcol}。"
            f"当前列: {list(df.columns)}"
        )

    x = df[df["HorizonSeconds"] == horizon].copy()

    if x.empty:
        raise ValueError(f"没有 HorizonSeconds={horizon} 的数据")

    x["Direction"] = x["Direction"].map(normalize_direction)
    x["SafeQuantile"] = pd.to_numeric(x[qcol], errors="coerce")
    x["SafeDistance"] = x["SafeQuantile"] * safety_factor

    return x[
        [
            "InstrumentID",
            "Direction",
            "SafeQuantile",
            "SafeDistance",
        ]
    ].dropna()


def prepare_events(path: str, mode: str, horizon: int):
    df = pd.read_csv(path)

    required = {
        "InstrumentID",
        "side",
        "abs_deviation_pct",
        "event_mid",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"event_details.csv 缺少字段: {sorted(missing)}"
        )

    x = df.copy()

    if mode != "all" and "mode" in x.columns:
        available_modes = set(x["mode"].dropna().astype(str))
        if mode in available_modes:
            x = x[x["mode"].astype(str) == mode].copy()
        else:
            # strict 没有任何记录时，不能静默回退 raw
            raise ValueError(
                f"事件文件里没有 mode={mode}。"
                f"可用模式: {sorted(available_modes)}。"
                f"如需 raw，请显式传 --mode raw"
            )

    x["Direction"] = x["side"].map(normalize_direction)

    # pct 字段在旧脚本里是“百分点”：
    # 1.25 表示 1.25%，这里统一转为小数 0.0125。
    x["AbsDeviation"] = (
        pd.to_numeric(x["abs_deviation_pct"], errors="coerce") / 100.0
    )

    # event_details 会因为多个 threshold 重复保存同一个事件。
    # 优先根据真实事件身份去重。
    identity = [
        c for c in [
            "source_file",
            "TradingDay",
            "InstrumentID",
            "side",
            "row_order",
        ]
        if c in x.columns
    ]

    if identity:
        # 同一事件保留一条；abs_deviation_pct 是事件真实偏离，不依赖 threshold。
        x = (
            x.sort_values(
                identity + (["threshold_pct"] if "threshold_pct" in x.columns else [])
            )
            .drop_duplicates(identity, keep="first")
            .copy()
        )

    follow_col = f"follow_ratio_{horizon}s"

    if follow_col in x.columns:
        x["FollowRatio"] = pd.to_numeric(
            x[follow_col], errors="coerce"
        )
        follow_source = "window_max"
    else:
        mid_after_col = f"mid_after_{horizon}s"
        if mid_after_col not in x.columns:
            raise ValueError(
                f"event_details.csv 既没有 {follow_col}，"
                f"也没有 {mid_after_col}，无法计算 FollowRatio"
            )

        x["_event_mid"] = pd.to_numeric(
            x["event_mid"], errors="coerce"
        )
        x["_mid_after"] = pd.to_numeric(
            x[mid_after_col], errors="coerce"
        )

        def endpoint_follow(row):
            m0 = row["_event_mid"]
            m1 = row["_mid_after"]
            dev = row["AbsDeviation"]

            if (
                pd.isna(m0)
                or pd.isna(m1)
                or pd.isna(dev)
                or m0 <= 0
                or dev <= 0
            ):
                return np.nan

            if row["Direction"] == "BUY":
                move = max(0.0, (m0 - m1) / m0)
            elif row["Direction"] == "SELL":
                move = max(0.0, (m1 - m0) / m0)
            else:
                return np.nan

            return move / dev

        x["FollowRatio"] = x.apply(endpoint_follow, axis=1)
        follow_source = "endpoint_approx"

    return x, follow_source


def infer_observed_days(events: pd.DataFrame):
    if "TradingDay" not in events.columns:
        return None

    days = events["TradingDay"].dropna().astype(str).nunique()
    return float(days) if days > 0 else None


def candidate_grid(safe_distance: float,
                   step: float,
                   max_distance: float):
    if step <= 0:
        raise ValueError("candidate-step 必须 > 0")

    start_n = math.ceil((safe_distance - 1e-12) / step)
    start = start_n * step

    values = []
    v = start
    while v <= max_distance + 1e-12:
        values.append(round(v, 10))
        v += step

    return values


def evaluate_one(instrument: str,
                 direction: str,
                 safe_distance: float,
                 events: pd.DataFrame,
                 args,
                 observed_days: float,
                 follow_source: str):

    e = events[
        (events["InstrumentID"].astype(str) == str(instrument))
        & (events["Direction"] == direction)
    ].copy()

    rows = []

    for d in candidate_grid(
        safe_distance=safe_distance,
        step=args.candidate_step,
        max_distance=args.max_candidate_distance,
    ):
        hit = e[e["AbsDeviation"] >= d - 1e-12].copy()

        event_count = int(len(hit))
        valid_follow = hit["FollowRatio"].dropna()

        isolated_count = int(
            (valid_follow <= args.max_follow_ratio).sum()
        )

        follow_valid_count = int(len(valid_follow))

        isolation_rate = (
            isolated_count / follow_valid_count
            if follow_valid_count > 0
            else np.nan
        )

        monthly_opportunity = (
            isolated_count / observed_days * args.trading_days_per_month
            if observed_days and observed_days > 0
            else np.nan
        )

        pass_safe = d >= safe_distance - 1e-12
        pass_events = event_count >= args.min_events
        pass_isolation = (
            pd.notna(isolation_rate)
            and isolation_rate >= args.min_isolation_rate
        )
        pass_opportunity = (
            pd.notna(monthly_opportunity)
            and monthly_opportunity >= args.min_monthly_opportunity
        )

        qualified = (
            pass_safe
            and pass_events
            and pass_isolation
            and pass_opportunity
        )

        rows.append({
            "InstrumentID": instrument,
            "Direction": direction,
            "CandidateDistance": d,
            "SafeDistance": safe_distance,
            "PassSafeDistance": pass_safe,
            "EventCount": event_count,
            "FollowValidCount": follow_valid_count,
            "PassMinEvents": pass_events,
            "IsolatedEventCount": isolated_count,
            "IsolationRate": isolation_rate,
            "PassIsolationRate": pass_isolation,
            "MonthlyOpportunity": monthly_opportunity,
            "PassMonthlyOpportunity": pass_opportunity,
            "Qualified": qualified,
            "FollowRatioSource": follow_source,
        })

    evaluation = pd.DataFrame(rows)

    qualified = evaluation[evaluation["Qualified"]].copy()

    if qualified.empty:
        result = {
            "InstrumentID": instrument,
            "Direction": direction,
            "SafeDistance": safe_distance,
            "TargetDistance": np.nan,
            "MinDistance": np.nan,
            "MaxDistance": np.nan,
            "EventCount": 0,
            "IsolatedEventCount": 0,
            "IsolationRate": np.nan,
            "MonthlyOpportunity": np.nan,
            "FollowRatioSource": follow_source,
            "ObservedTradingDays": observed_days,
            "Status": "NO_QUALIFIED_DISTANCE",
        }
    else:
        # 选择最小合格距离
        best = qualified.sort_values("CandidateDistance").iloc[0]

        target = float(best["CandidateDistance"])
        min_distance = target * (1.0 - args.band_ratio)
        max_distance = target * (1.0 + args.band_ratio)

        result = {
            "InstrumentID": instrument,
            "Direction": direction,
            "SafeDistance": safe_distance,
            "TargetDistance": target,
            "MinDistance": min_distance,
            "MaxDistance": max_distance,
            "EventCount": int(best["EventCount"]),
            "IsolatedEventCount": int(best["IsolatedEventCount"]),
            "IsolationRate": float(best["IsolationRate"]),
            "MonthlyOpportunity": float(best["MonthlyOpportunity"]),
            "FollowRatioSource": follow_source,
            "ObservedTradingDays": observed_days,
            "Status": "OK",
        }

    return result, evaluation


def main():
    args = parse_args()

    safe = load_safe_distances(
        path=args.volatility,
        horizon=args.horizon,
        percentile=args.percentile,
        safety_factor=args.safety_factor,
    )

    events, follow_source = prepare_events(
        path=args.events,
        mode=args.mode,
        horizon=args.horizon,
    )

    if args.observed_trading_days is not None:
        observed_days = float(args.observed_trading_days)
        observed_days_source = "cli"
    else:
        observed_days = infer_observed_days(events)
        observed_days_source = "events_only_estimate"

        print(
            "WARNING: 未传 --observed-trading-days，"
            "当前只能按 event_details 中出现过的 TradingDay 估算。"
        )
        print(
            "这会忽略完全没有异常事件的交易日，"
            "可能高估 MonthlyOpportunity。"
        )

    if not observed_days or observed_days <= 0:
        raise ValueError(
            "无法确定历史总交易日数。"
            "请使用 --observed-trading-days 显式传入，例如 120。"
        )

    final_rows = []
    eval_frames = []

    for _, r in safe.iterrows():
        result, evaluation = evaluate_one(
            instrument=str(r["InstrumentID"]),
            direction=str(r["Direction"]),
            safe_distance=float(r["SafeDistance"]),
            events=events,
            args=args,
            observed_days=observed_days,
            follow_source=follow_source,
        )

        result["ObservedDaysSource"] = observed_days_source
        final_rows.append(result)
        eval_frames.append(evaluation)

    final = pd.DataFrame(final_rows)
    evaluation = (
        pd.concat(eval_frames, ignore_index=True)
        if eval_frames
        else pd.DataFrame()
    )

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    final_path = out / "auto_parameters.csv"
    eval_path = out / "candidate_evaluation.csv"

    final.to_csv(final_path, index=False)
    evaluation.to_csv(eval_path, index=False)

    print("\n=== 自动选参结果 ===")

    if final.empty:
        print("没有结果")
    else:
        show = final.copy()

        for c in [
            "SafeDistance",
            "TargetDistance",
            "MinDistance",
            "MaxDistance",
        ]:
            show[c] = show[c].map(
                lambda x: (
                    f"{x * 100:.4f}%"
                    if pd.notna(x)
                    else "-"
                )
            )

        if "IsolationRate" in show.columns:
            show["IsolationRate"] = show["IsolationRate"].map(
                lambda x: (
                    f"{x * 100:.2f}%"
                    if pd.notna(x)
                    else "-"
                )
            )

        cols = [
            "InstrumentID",
            "Direction",
            "SafeDistance",
            "TargetDistance",
            "MinDistance",
            "MaxDistance",
            "EventCount",
            "IsolationRate",
            "MonthlyOpportunity",
            "Status",
        ]

        print(show[cols].to_string(index=False))

    print("\n输出:")
    print(final_path)
    print(eval_path)

    if follow_source == "endpoint_approx":
        print(
            "\nWARNING: 当前 FollowRatio 使用 mid_after_"
            f"{args.horizon}s 的终点近似。"
        )
        print(
            "推荐后续升级异常事件脚本，直接输出窗口内最大同方向 "
            f"follow_ratio_{args.horizon}s。"
        )


if __name__ == "__main__":
    main()
