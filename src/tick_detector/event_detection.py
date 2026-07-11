from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.tick_detector.tick_io import MAX_DATA_GAP_SECONDS

# 1 秒均价确认窗的采样完整性条件（比数据断点更严格）
MAX_CONFIRMATION_GAP_SECONDS = 1

# onset 窗口
ONSET_WINDOW_SECONDS = 3
ONSET_MIN_TICKS = 8
ONSET_K_SIGMA = 5

# 事件合并
MERGE_WINDOW_SECONDS = 10


def detect_candidate_ticks(
    df: pd.DataFrame,
    *,
    return_marked: bool = False,
) -> pd.DataFrame:
    """双成交通道检测：visible_execution_drop / interval_execution_drop + onset。

    设计文档 §6（有向成交信号）、§7（突发性与候选触发）、§7.1（区间均价计数器确认）。
    时间契约统一使用 market_time_key（毫秒）。
    """
    if df.empty:
        return df.copy()
    out = df.sort_values("market_time_key", kind="stable").reset_index(drop=True).copy()
    n = len(out)
    keys = out["market_time_key"].to_numpy()
    tick_size = float(out["tick_size"].iloc[0]) if "tick_size" in out.columns else 0.02
    multiplier = int(out["contract_multiplier"].iloc[0]) if "contract_multiplier" in out.columns else 1000

    # 预计算每行的有向偏离
    last_down_ticks = np.full(n, np.nan)
    vwap_down_ticks = np.full(n, np.nan)
    for i in range(n):
        fp = out.at[i, "fair_price"]
        if not np.isfinite(fp) or fp <= 0:
            continue
        last = out.at[i, "LastPrice"]
        if np.isfinite(last) and last > 0:
            last_down_ticks[i] = (fp - last) / tick_size
        vwap = out.at[i, "interval_vwap"]
        if np.isfinite(vwap) and vwap > 0:
            vwap_down_ticks[i] = (fp - vwap) / tick_size
    out["last_down_ticks"] = last_down_ticks
    out["vwap_down_ticks"] = vwap_down_ticks

    candidate_mask = np.zeros(n, dtype=bool)
    trigger_reasons: list[list[str]] = [[] for _ in range(n)]
    combined_vwap_1s = np.full(n, np.nan)
    combined_vwap_down_ticks = np.full(n, np.nan)
    confirmation_end = np.full(n, np.nan)
    data_quality = [list(out.at[i, "data_quality_flags"].split(",")) if str(out.at[i, "data_quality_flags"]) else [] for i in range(n)]

    for i in range(n):
        # 基础阻断条件（设计文档 §4.4）
        if not _is_data_eligible(out, i):
            continue
        dv = out.at[i, "delta_volume"]
        if not (np.isfinite(dv) and dv > 0):
            continue
        if not bool(out.at[i, "fair_price_reliable"]):
            continue
        if not bool(out.at[i, "noise_history_reliable"]):
            continue
        if str(out.at[i, "validation_status"]) != "validated":
            continue
        if bool(out.at[i, "is_open_protected"]):
            continue

        last_thr = float(out.at[i, "last_threshold_ticks"])
        vwap_thr = float(out.at[i, "vwap_threshold_ticks"])

        # visible_execution_drop：末笔低于 fair_price 超阈值（独立判断，不等待未来数据）
        visible_hit = (
            np.isfinite(last_down_ticks[i])
            and last_down_ticks[i] >= last_thr
        )

        # interval_execution_drop：区间均价超阈值 + 完整 1s 合并仍异常
        interval_hit = False
        if np.isfinite(vwap_down_ticks[i]) and vwap_down_ticks[i] >= vwap_thr:
            conf = _compute_1s_confirmation(out, i, tick_size, multiplier)
            combined_vwap_1s[i] = conf["combined_vwap"]
            confirmation_end[i] = conf["confirmation_end_key"]
            if conf["block_reason"]:
                data_quality[i].append(conf["block_reason"])
            if conf["window_complete"]:
                cv = (float(out.at[i, "fair_price"]) - conf["combined_vwap"]) / tick_size
                combined_vwap_down_ticks[i] = cv
                if cv >= vwap_thr:
                    interval_hit = True
                else:
                    # 单帧异常但合并后正常
                    data_quality[i].append("counter_lag_suspect")
            # 窗口不完整时区间分支不触发（counter_sync_unconfirmed 由 block_reason 记录）

        if not (visible_hit or interval_hit):
            continue

        # onset：过去 3 秒深度
        pre_depth, onset_ok, onset_val = _check_onset(
            out, i, last_down_ticks, vwap_down_ticks, tick_size
        )
        if not onset_ok:
            data_quality[i].append("onset_data_gap")
            continue

        onset_ticks = max(0.0, max(last_down_ticks[i] if visible_hit else 0.0,
                                   _cand_depth(i, last_down_ticks, vwap_down_ticks, interval_hit))
                           - max(0.0, pre_depth))
        sigma = float(out.at[i, "execution_depth_robust_sigma"]) if np.isfinite(out.at[i, "execution_depth_robust_sigma"]) else 1.0
        onset_threshold = max(ONSET_MIN_TICKS, ONSET_K_SIGMA * sigma)
        if onset_ticks < onset_threshold:
            continue

        candidate_mask[i] = True
        out.at[i, "candidate_execution_depth"] = max(
            last_down_ticks[i] if visible_hit else -math.inf,
            (combined_vwap_down_ticks[i] if np.isfinite(combined_vwap_down_ticks[i]) else vwap_down_ticks[i]) if interval_hit else -math.inf,
        )
        out.at[i, "onset_ticks"] = onset_ticks
        out.at[i, "combined_vwap_1s"] = combined_vwap_1s[i]
        out.at[i, "combined_vwap_down_ticks"] = combined_vwap_down_ticks[i]
        out.at[i, "interval_confirmation_end_time"] = confirmation_end[i]
        if visible_hit:
            trigger_reasons[i].append("visible_execution_drop")
        if interval_hit:
            trigger_reasons[i].append("interval_execution_drop")

    # 写回 data_quality_flags
    out["data_quality_flags"] = [",".join(set(f for f in flags if f)) for flags in data_quality]
    out["trigger_reasons"] = [",".join(r) for r in trigger_reasons]
    out.loc[~candidate_mask, "candidate_execution_depth"] = np.nan

    if return_marked:
        return out
    return out.loc[candidate_mask].copy()


# ---------------------------------------------------------------------------
# 1 秒合并 helper（Task 3 检测 + Task 4 恢复共用）
# ---------------------------------------------------------------------------


def _compute_1s_confirmation(
    df: pd.DataFrame,
    start_idx: int,
    tick_size: float,
    multiplier: int,
) -> dict[str, object]:
    """从 start_idx 起，取同 session 内完整 1s 窗口的合并 vwap。

    完整性要求（设计文档 §7.1）：
      - 窗口覆盖到 t+1s
      - 中间相邻时间键 gap 不超过 MAX_CONFIRMATION_GAP_SECONDS
      - 所有累计增量有效且未跨 session
      - 仅累计有效正增量
    """
    keys = df["market_time_key"].to_numpy()
    start_mk = int(keys[start_idx])
    end_mk = start_mk + 1000  # t+1s
    n = len(df)

    total_dv = 0.0
    total_dt = 0.0
    window_complete = False
    confirmation_end_key = np.nan
    block_reason = ""
    last_mk = start_mk

    for j in range(start_idx, n):
        mk = int(keys[j])
        if mk > end_mk:
            break
        if j > start_idx:
            gap_ms = mk - last_mk
            if gap_ms > MAX_CONFIRMATION_GAP_SECONDS * 1000:
                block_reason = "counter_sync_unconfirmed"
                return {"combined_vwap": np.nan, "window_complete": False,
                        "confirmation_end_key": np.nan, "block_reason": block_reason}
        # 跨 session 检查
        if not bool(df.at[j, "is_tradable_session"]):
            block_reason = "counter_sync_unconfirmed"
            return {"combined_vwap": np.nan, "window_complete": False,
                    "confirmation_end_key": np.nan, "block_reason": block_reason}
        dv = df.at[j, "delta_volume"]
        dt = df.at[j, "delta_turnover"]
        if np.isfinite(dv) and dv > 0 and np.isfinite(dt) and dt > 0:
            total_dv += dv
            total_dt += dt
        elif np.isfinite(dv) and dv < 0:
            block_reason = "counter_sync_unconfirmed"
            return {"combined_vwap": np.nan, "window_complete": False,
                    "confirmation_end_key": np.nan, "block_reason": block_reason}
        last_mk = mk
        if mk >= end_mk:
            window_complete = True
            confirmation_end_key = mk
            break

    if not window_complete or total_dv <= 0:
        return {"combined_vwap": np.nan, "window_complete": False,
                "confirmation_end_key": np.nan,
                "block_reason": block_reason or "confirmation_incomplete"}
    combined_vwap = total_dt / total_dv / multiplier
    return {"combined_vwap": combined_vwap, "window_complete": True,
            "confirmation_end_key": confirmation_end_key, "block_reason": ""}


# ---------------------------------------------------------------------------
# onset
# ---------------------------------------------------------------------------


def _check_onset(
    df: pd.DataFrame,
    i: int,
    last_down_ticks: np.ndarray,
    vwap_down_ticks: np.ndarray,
    tick_size: float,
) -> tuple[float, bool, float]:
    """过去 3 秒深度的中位数；数据中断时 pre_depth 未知 -> onset_ok=False。"""
    keys = df["market_time_key"].to_numpy()
    mk = int(keys[i])
    ws = mk - ONSET_WINDOW_SECONDS * 1000
    # 先检查候选行与前一行的 gap（即使前一行在 onset 窗口外）
    if i > 0:
        prev_gap = mk - int(keys[i - 1])
        if prev_gap > MAX_DATA_GAP_SECONDS * 1000:
            return (float("nan"), False, 0.0)
    depths: list[float] = []
    for j in range(i - 1, -1, -1):
        jmk = int(keys[j])
        if jmk < ws:
            break
        if j + 1 < len(keys):
            gap_to_prev = int(keys[j + 1]) - jmk
            if gap_to_prev > MAX_DATA_GAP_SECONDS * 1000:
                return (float("nan"), False, 0.0)
        dv = df.at[j, "delta_volume"]
        if not (np.isfinite(dv) and dv > 0):
            continue
        d = max(last_down_ticks[j] if np.isfinite(last_down_ticks[j]) else 0.0,
                vwap_down_ticks[j] if np.isfinite(vwap_down_ticks[j]) else 0.0)
        depths.append(d)
    pre_depth = float(np.median(depths)) if depths else 0.0
    return (pre_depth, True, pre_depth)


def _is_data_eligible(df: pd.DataFrame, i: int) -> bool:
    """§4.4 基础过滤：可交易时段、有效价格。"""
    if not bool(df.at[i, "is_tradable_session"]):
        return False
    last = df.at[i, "LastPrice"]
    lower = df.at[i, "LowerLimitPrice"]
    upper = df.at[i, "UpperLimitPrice"]
    if np.isfinite(lower) and lower > 0 and last <= lower:
        return False
    if np.isfinite(upper) and upper > 0 and last >= upper:
        return False
    return True


def _cand_depth(
    i: int,
    last_down_ticks: np.ndarray,
    vwap_down_ticks: np.ndarray,
    interval_hit: bool,
) -> float:
    if interval_hit and np.isfinite(vwap_down_ticks[i]):
        return float(vwap_down_ticks[i])
    if np.isfinite(last_down_ticks[i]):
        return float(last_down_ticks[i])
    return 0.0
