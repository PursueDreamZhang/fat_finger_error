#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
极简乌龙指历史统计脚本：Mid + LastPrice + DeltaVolume

核心思想：
1. Mid = (BidPrice1 + AskPrice1) / 2
2. 只有 DeltaVolume > 0 时，LastPrice 才视为“有新成交可检查”
3. Deviation = (LastPrice - Mid) / Mid
4. 对多个阈值分别统计向上/向下候选
5. 同时输出 Raw 与 Strict：
   - Raw: 只看 Last-Mid 偏离
   - Strict: 向下要求 Last < Bid1，向上要求 Last > Ask1
6. 合并连续候选为独立事件，统计未来 1/3/5/10/30/60 秒 Mid 恢复、MFE、MAE

依赖：pandas, numpy
安装：pip install pandas numpy

示例：
python fatfinger_analyzer.py --input ./data --output ./result
python fatfinger_analyzer.py --input ./SA609_20260515.csv --output ./result
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Dict, Tuple, Optional

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = [
    "TradingDay",
    "InstrumentID",
    "UpdateTime",
    "LastPrice",
    "Volume",
    "BidPrice1",
    "AskPrice1",
]

DEFAULT_THRESHOLDS = [0.0025, 0.005, 0.0075, 0.01, 0.0125, 0.015, 0.02, 0.025, 0.03]
DEFAULT_HORIZONS = [1, 3, 5, 10, 30, 60]


@dataclass
class Config:
    thresholds: List[float]
    horizons: List[int]
    event_merge_seconds: float = 5.0
    session_gap_seconds: float = 300.0
    mfe_mae_horizon: int = 60


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="极简乌龙指历史统计：Mid + LastPrice")
    parser.add_argument("--input", required=True, help="CSV 文件、目录或 glob，例如 ./data/*.csv")
    parser.add_argument("--output", required=True, help="输出目录")
    parser.add_argument(
        "--thresholds",
        default="0.25,0.5,0.75,1,1.25,1.5,2,2.5,3",
        help="阈值百分比，逗号分隔，例如 0.5,1,1.5,2",
    )
    parser.add_argument(
        "--horizons",
        default="1,3,5,10,30,60",
        help="恢复观察秒数，逗号分隔",
    )
    parser.add_argument(
        "--event-merge-seconds",
        type=float,
        default=5.0,
        help="候选事件合并窗口，默认 5 秒",
    )
    parser.add_argument(
        "--session-gap-seconds",
        type=float,
        default=300.0,
        help="相邻记录超过该秒数视为新时段，默认 300 秒",
    )
    return parser.parse_args()


def resolve_files(input_expr: str) -> List[Path]:
    p = Path(input_expr)
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted(p.rglob("*.csv"))

    # 支持 glob 表达式
    parent = p.parent if str(p.parent) not in ("", ".") else Path(".")
    files = sorted(parent.glob(p.name))
    return [x for x in files if x.is_file() and x.suffix.lower() == ".csv"]


def parse_time_of_day_seconds(df: pd.DataFrame) -> np.ndarray:
    """
    按文件原始行顺序构造单调时间轴（秒）。

    不完全依赖 TradingDay 的“自然日语义”，因为国内期货夜盘的 TradingDay/ActionDay
    容易造成理解差异。这里主要目的是计算记录之间真实的相对时间间隔。
    """
    times = pd.to_datetime(df["UpdateTime"].astype(str), format="%H:%M:%S", errors="coerce")
    sec = (
        times.dt.hour.fillna(0).to_numpy(dtype=float) * 3600
        + times.dt.minute.fillna(0).to_numpy(dtype=float) * 60
        + times.dt.second.fillna(0).to_numpy(dtype=float)
    )

    if "UpdateMillisec" in df.columns:
        ms = pd.to_numeric(df["UpdateMillisec"], errors="coerce").fillna(0).to_numpy(dtype=float)
        sec = sec + ms / 1000.0

    out = np.empty(len(sec), dtype=float)
    day_offset = 0.0
    prev = None

    for i, s in enumerate(sec):
        current = s + day_offset
        # 时钟大幅向后跳，视为跨自然日（例如 23:00 -> 09:00）
        if prev is not None and current < prev - 3600:
            day_offset += 86400.0
            current = s + day_offset
        out[i] = current
        prev = current

    return out


def preprocess(df: pd.DataFrame, source_file: str, cfg: Config) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"缺少字段: {missing}")

    x = df.copy()
    x["source_file"] = source_file
    x["_row_order"] = np.arange(len(x), dtype=np.int64)

    numeric_cols = [
        "LastPrice", "Volume", "BidPrice1", "AskPrice1",
        "BidVolume1", "AskVolume1", "UpdateMillisec"
    ]
    for col in numeric_cols:
        if col in x.columns:
            x[col] = pd.to_numeric(x[col], errors="coerce")

    # 保留文件原始行顺序，不对同秒多条做去重
    x["ts"] = parse_time_of_day_seconds(x)

    # 每个文件通常只有一个合约；即使混有多个合约，也分别处理
    parts = []
    for instrument, g in x.groupby("InstrumentID", sort=False, dropna=False):
        g = g.sort_values("_row_order", kind="stable").copy()

        g["valid_bbo"] = (
            g["BidPrice1"].gt(0)
            & g["AskPrice1"].gt(0)
            & g["BidPrice1"].le(g["AskPrice1"])
        )
        g["valid_last"] = g["LastPrice"].gt(0)
        g["mid"] = np.where(
            g["valid_bbo"],
            (g["BidPrice1"] + g["AskPrice1"]) / 2.0,
            np.nan,
        )
        g["spread"] = np.where(g["valid_bbo"], g["AskPrice1"] - g["BidPrice1"], np.nan)
        g["spread_pct"] = np.where(g["mid"].gt(0), g["spread"] / g["mid"], np.nan)

        g["delta_volume_raw"] = g["Volume"].diff()
        g["volume_reset"] = g["delta_volume_raw"].lt(0)
        g["delta_volume"] = g["delta_volume_raw"].where(g["delta_volume_raw"].ge(0))
        g["has_new_trade"] = g["delta_volume"].gt(0)

        g["dt"] = g["ts"].diff()
        new_segment = (
            g["dt"].isna()
            | g["dt"].lt(0)
            | g["dt"].gt(cfg.session_gap_seconds)
            | g["volume_reset"]
        )
        g["segment_id"] = new_segment.cumsum().astype(int)

        valid_dev = g["valid_bbo"] & g["valid_last"] & g["has_new_trade"] & g["mid"].gt(0)
        g["deviation"] = np.where(
            valid_dev,
            (g["LastPrice"] - g["mid"]) / g["mid"],
            np.nan,
        )
        g["abs_deviation"] = g["deviation"].abs()

        # Strict：最新成交已经跑出当前一档盘口
        g["outside_bbo_down"] = valid_dev & g["LastPrice"].lt(g["BidPrice1"])
        g["outside_bbo_up"] = valid_dev & g["LastPrice"].gt(g["AskPrice1"])

        # 前一个有效 Mid，作为事件前锚点候选
        prev_mid = g["mid"].where(g["valid_bbo"]).ffill().shift(1)
        prev_segment = g["segment_id"].shift(1)
        prev_mid = prev_mid.where(prev_segment.eq(g["segment_id"]))
        g["prev_mid"] = prev_mid

        parts.append(g)

    return pd.concat(parts, ignore_index=True) if parts else x


def quantile_row(values: pd.Series, instrument: str, source_file: str) -> Dict[str, float]:
    v = pd.to_numeric(values, errors="coerce").dropna()
    if v.empty:
        return {
            "source_file": source_file,
            "InstrumentID": instrument,
            "new_trade_records": 0,
        }
    qs = v.quantile([0.90, 0.95, 0.99, 0.995, 0.999, 0.9999])
    return {
        "source_file": source_file,
        "InstrumentID": instrument,
        "new_trade_records": int(v.size),
        "q90_pct": qs.loc[0.90] * 100,
        "q95_pct": qs.loc[0.95] * 100,
        "q99_pct": qs.loc[0.99] * 100,
        "q99_5_pct": qs.loc[0.995] * 100,
        "q99_9_pct": qs.loc[0.999] * 100,
        "q99_99_pct": qs.loc[0.9999] * 100,
        "max_pct": v.max() * 100,
    }


def merge_candidates(cand: pd.DataFrame, merge_seconds: float) -> pd.DataFrame:
    """把连续候选合并为独立事件，每个簇保留绝对偏离最大的代表点。"""
    if cand.empty:
        return cand.copy()

    c = cand.sort_values(["segment_id", "ts", "_row_order"], kind="stable").copy()
    new_event = (
        c["segment_id"].ne(c["segment_id"].shift(1))
        | c["ts"].sub(c["ts"].shift(1)).gt(merge_seconds)
        | c["ts"].sub(c["ts"].shift(1)).lt(0)
    )
    c["event_cluster"] = new_event.cumsum().astype(int)

    idx = c.groupby("event_cluster", sort=False)["abs_deviation"].idxmax()
    rep = c.loc[idx].sort_values(["ts", "_row_order"], kind="stable").copy()
    rep["cluster_candidate_rows"] = c.groupby("event_cluster").size().reindex(rep["event_cluster"]).to_numpy()
    return rep


def get_future_mid(
    g: pd.DataFrame,
    row_pos: int,
    horizon: float,
) -> Tuple[float, Optional[int]]:
    """在同一 segment 中，取事件时刻+horizon 之后第一条有效 Mid。"""
    seg = g.iloc[row_pos]["segment_id"]
    target = g.iloc[row_pos]["ts"] + horizon

    # 向后搜索，直到超出 segment；事件数量通常远少于总行数，因此循环成本可控
    for j in range(row_pos + 1, len(g)):
        row = g.iloc[j]
        if row["segment_id"] != seg:
            break
        if row["ts"] >= target and pd.notna(row["mid"]):
            return float(row["mid"]), j
    return np.nan, None


def future_window_mid(g: pd.DataFrame, row_pos: int, horizon: float) -> pd.Series:
    seg = g.iloc[row_pos]["segment_id"]
    start_ts = g.iloc[row_pos]["ts"]
    end_ts = start_ts + horizon

    sub = g.iloc[row_pos + 1 :]
    sub = sub[(sub["segment_id"] == seg) & (sub["ts"] <= end_ts)]
    return pd.to_numeric(sub["mid"], errors="coerce").dropna()


def analyze_events_for_group(
    g: pd.DataFrame,
    threshold: float,
    side: str,
    mode: str,
    cfg: Config,
) -> Tuple[pd.DataFrame, int]:
    if side not in ("down", "up"):
        raise ValueError(side)
    if mode not in ("raw", "strict"):
        raise ValueError(mode)

    base = g["deviation"].notna()
    if side == "down":
        mask = base & g["deviation"].le(-threshold)
        if mode == "strict":
            mask &= g["outside_bbo_down"]
    else:
        mask = base & g["deviation"].ge(threshold)
        if mode == "strict":
            mask &= g["outside_bbo_up"]

    cand = g.loc[mask].copy()
    candidate_rows = len(cand)
    events = merge_candidates(cand, cfg.event_merge_seconds)
    if events.empty:
        return events, candidate_rows

    # 为了通过 iloc 高效定位，建立原始行号 -> 当前 g 位置映射
    pos_map = {int(row_order): pos for pos, row_order in enumerate(g["_row_order"].to_numpy())}

    out_rows = []
    for _, ev in events.iterrows():
        row_order = int(ev["_row_order"])
        row_pos = pos_map[row_order]

        event_mid = float(ev["mid"])
        anchor_mid = float(ev["prev_mid"]) if pd.notna(ev["prev_mid"]) else event_mid
        event_last = float(ev["LastPrice"])

        if side == "down":
            fill_price = event_mid * (1.0 - threshold)
        else:
            fill_price = event_mid * (1.0 + threshold)

        detail = {
            "source_file": ev["source_file"],
            "TradingDay": ev["TradingDay"],
            "InstrumentID": ev["InstrumentID"],
            "UpdateTime": ev["UpdateTime"],
            "UpdateMillisec": ev.get("UpdateMillisec", np.nan),
            "row_order": row_order,
            "side": side,
            "mode": mode,
            "threshold_pct": threshold * 100,
            "event_mid": event_mid,
            "anchor_mid": anchor_mid,
            "event_last": event_last,
            "bid1": ev["BidPrice1"],
            "ask1": ev["AskPrice1"],
            "spread": ev["spread"],
            "spread_pct": ev["spread_pct"] * 100 if pd.notna(ev["spread_pct"]) else np.nan,
            "delta_volume": ev["delta_volume"],
            "deviation_pct": ev["deviation"] * 100,
            "abs_deviation_pct": ev["abs_deviation"] * 100,
            "mid_move_pct": ((event_mid - anchor_mid) / anchor_mid * 100) if anchor_mid else np.nan,
            "sim_fill_price": fill_price,
            "cluster_candidate_rows": ev.get("cluster_candidate_rows", 1),
        }

        for h in cfg.horizons:
            future_mid, _ = get_future_mid(g, row_pos, h)
            detail[f"mid_after_{h}s"] = future_mid
            if pd.isna(future_mid):
                recovery = np.nan
            else:
                if side == "down":
                    denom = anchor_mid - fill_price
                    recovery = (future_mid - fill_price) / denom if denom > 0 else np.nan
                else:
                    denom = fill_price - anchor_mid
                    recovery = (fill_price - future_mid) / denom if denom > 0 else np.nan
            detail[f"recovery_ratio_{h}s"] = recovery
            detail[f"recovery50_{h}s"] = bool(recovery >= 0.5) if pd.notna(recovery) else np.nan
            detail[f"recovery80_{h}s"] = bool(recovery >= 0.8) if pd.notna(recovery) else np.nan
            detail[f"recovery100_{h}s"] = bool(recovery >= 1.0) if pd.notna(recovery) else np.nan

        path = future_window_mid(g, row_pos, cfg.mfe_mae_horizon)
        if path.empty or fill_price <= 0:
            mfe = np.nan
            mae = np.nan
        else:
            if side == "down":
                mfe = max(0.0, (path.max() - fill_price) / fill_price)
                mae = max(0.0, (fill_price - path.min()) / fill_price)
            else:
                mfe = max(0.0, (fill_price - path.min()) / fill_price)
                mae = max(0.0, (path.max() - fill_price) / fill_price)

        detail[f"mfe_{cfg.mfe_mae_horizon}s_pct"] = mfe * 100 if pd.notna(mfe) else np.nan
        detail[f"mae_{cfg.mfe_mae_horizon}s_pct"] = mae * 100 if pd.notna(mae) else np.nan
        out_rows.append(detail)

    return pd.DataFrame(out_rows), candidate_rows


def summarize_events(events: pd.DataFrame, candidate_rows: int, cfg: Config) -> Dict[str, float]:
    if events.empty:
        return {
            "candidate_rows": candidate_rows,
            "independent_events": 0,
            "active_days": 0,
            "events_per_day": 0.0,
        }

    # TradingDay 在不同数据源里可能是字符串/整数，这里只做“有多少不同值”的统计
    active_days = max(1, events["TradingDay"].astype(str).nunique())
    row = {
        "candidate_rows": int(candidate_rows),
        "independent_events": int(len(events)),
        "active_days": int(active_days),
        "events_per_day": len(events) / active_days,
    }

    for h in cfg.horizons:
        for level in (50, 80, 100):
            col = f"recovery{level}_{h}s"
            s = events[col].dropna()
            row[f"{col}_rate"] = float(s.astype(float).mean()) if not s.empty else np.nan

        ratio_col = f"recovery_ratio_{h}s"
        s = pd.to_numeric(events[ratio_col], errors="coerce").dropna()
        row[f"recovery_ratio_{h}s_median"] = float(s.median()) if not s.empty else np.nan

    mfe_col = f"mfe_{cfg.mfe_mae_horizon}s_pct"
    mae_col = f"mae_{cfg.mfe_mae_horizon}s_pct"
    mfe = pd.to_numeric(events[mfe_col], errors="coerce").dropna()
    mae = pd.to_numeric(events[mae_col], errors="coerce").dropna()

    row["mfe_mean_pct"] = float(mfe.mean()) if not mfe.empty else np.nan
    row["mfe_median_pct"] = float(mfe.median()) if not mfe.empty else np.nan
    row["mae_mean_pct"] = float(mae.mean()) if not mae.empty else np.nan
    row["mae_median_pct"] = float(mae.median()) if not mae.empty else np.nan
    row["mae_p95_pct"] = float(mae.quantile(0.95)) if not mae.empty else np.nan
    return row


def safe_read_csv(path: Path) -> pd.DataFrame:
    # 优先 utf-8-sig；失败再尝试 gbk
    try:
        return pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="gbk", low_memory=False)


def main() -> None:
    args = parse_args()
    files = resolve_files(args.input)
    if not files:
        raise SystemExit(f"没有找到 CSV: {args.input}")

    thresholds = [float(x.strip()) / 100.0 for x in args.thresholds.split(",") if x.strip()]
    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    cfg = Config(
        thresholds=sorted(set(thresholds)),
        horizons=sorted(set(horizons)),
        event_merge_seconds=args.event_merge_seconds,
        session_gap_seconds=args.session_gap_seconds,
        mfe_mae_horizon=max(horizons),
    )

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_events = []
    summary_rows = []
    quantile_rows = []
    quality_rows = []

    for n, path in enumerate(files, 1):
        print(f"[{n}/{len(files)}] 分析 {path.name}")
        try:
            raw = safe_read_csv(path)
            df = preprocess(raw, path.name, cfg)
        except Exception as e:
            print(f"  跳过：{e}")
            quality_rows.append({"source_file": path.name, "status": "error", "message": str(e)})
            continue

        for instrument, g in df.groupby("InstrumentID", sort=False, dropna=False):
            g = g.sort_values("_row_order", kind="stable").reset_index(drop=True)
            new_trade = g.loc[g["deviation"].notna(), "abs_deviation"]
            quantile_rows.append(quantile_row(new_trade, str(instrument), path.name))

            quality_rows.append({
                "source_file": path.name,
                "InstrumentID": instrument,
                "rows": len(g),
                "valid_bbo_rows": int(g["valid_bbo"].sum()),
                "new_trade_records": int(g["has_new_trade"].sum()),
                "deviation_records": int(g["deviation"].notna().sum()),
                "volume_reset_rows": int(g["volume_reset"].sum()),
                "status": "ok",
                "message": "",
            })

            for threshold in cfg.thresholds:
                for side in ("down", "up"):
                    for mode in ("raw", "strict"):
                        events, candidate_rows = analyze_events_for_group(
                            g, threshold, side, mode, cfg
                        )
                        if not events.empty:
                            all_events.append(events)

                        s = summarize_events(events, candidate_rows, cfg)
                        s.update({
                            "source_file": path.name,
                            "InstrumentID": instrument,
                            "side": side,
                            "mode": mode,
                            "threshold_pct": threshold * 100,
                        })
                        summary_rows.append(s)

    events_df = pd.concat(all_events, ignore_index=True) if all_events else pd.DataFrame()
    file_summary_df = pd.DataFrame(summary_rows)
    quantiles_df = pd.DataFrame(quantile_rows)
    quality_df = pd.DataFrame(quality_rows)

    # 文件级结果
    file_summary_df.to_csv(out_dir / "threshold_summary_by_file.csv", index=False, encoding="utf-8-sig")
    quantiles_df.to_csv(out_dir / "deviation_distribution.csv", index=False, encoding="utf-8-sig")
    quality_df.to_csv(out_dir / "data_quality.csv", index=False, encoding="utf-8-sig")
    if not events_df.empty:
        events_df.to_csv(out_dir / "event_details.csv", index=False, encoding="utf-8-sig")

    # 跨文件聚合。优先从 event_details 重新聚合恢复指标；没有事件时也保留计数结果。
    if not file_summary_df.empty:
        count_agg = (
            file_summary_df
            .groupby(["InstrumentID", "side", "mode", "threshold_pct"], dropna=False)
            .agg(
                candidate_rows=("candidate_rows", "sum"),
                independent_events=("independent_events", "sum"),
                active_days=("active_days", "sum"),
            )
            .reset_index()
        )
        count_agg["events_per_day"] = np.where(
            count_agg["active_days"].gt(0),
            count_agg["independent_events"] / count_agg["active_days"],
            np.nan,
        )

        if not events_df.empty:
            grouped = []
            keys = ["InstrumentID", "side", "mode", "threshold_pct"]
            for key, evg in events_df.groupby(keys, dropna=False):
                row = dict(zip(keys, key if isinstance(key, tuple) else (key,)))
                # 这里只用事件明细计算恢复率 / MFE / MAE，计数从 count_agg 合并
                for h in cfg.horizons:
                    for level in (50, 80, 100):
                        col = f"recovery{level}_{h}s"
                        s = evg[col].dropna()
                        row[f"{col}_rate"] = float(s.astype(float).mean()) if not s.empty else np.nan
                    rc = pd.to_numeric(evg[f"recovery_ratio_{h}s"], errors="coerce").dropna()
                    row[f"recovery_ratio_{h}s_median"] = float(rc.median()) if not rc.empty else np.nan

                mfe = pd.to_numeric(evg[f"mfe_{cfg.mfe_mae_horizon}s_pct"], errors="coerce").dropna()
                mae = pd.to_numeric(evg[f"mae_{cfg.mfe_mae_horizon}s_pct"], errors="coerce").dropna()
                row["mfe_mean_pct"] = float(mfe.mean()) if not mfe.empty else np.nan
                row["mfe_median_pct"] = float(mfe.median()) if not mfe.empty else np.nan
                row["mae_mean_pct"] = float(mae.mean()) if not mae.empty else np.nan
                row["mae_median_pct"] = float(mae.median()) if not mae.empty else np.nan
                row["mae_p95_pct"] = float(mae.quantile(0.95)) if not mae.empty else np.nan
                grouped.append(row)

            metrics_df = pd.DataFrame(grouped)
            aggregate_df = count_agg.merge(metrics_df, on=keys, how="left")
        else:
            aggregate_df = count_agg

        aggregate_df = aggregate_df.sort_values(
            ["InstrumentID", "side", "mode", "threshold_pct"], kind="stable"
        )
        aggregate_df.to_csv(out_dir / "threshold_summary.csv", index=False, encoding="utf-8-sig")

    print("\n完成。输出文件：")
    for p in sorted(out_dir.glob("*.csv")):
        print(" -", p)


if __name__ == "__main__":
    main()
