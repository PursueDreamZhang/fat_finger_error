from __future__ import annotations

import math

import numpy as np
import pandas as pd

from src.tick_detector.reference_selection import MAX_REFERENCE_AGE_SECONDS
from src.tick_detector.tick_io import MAX_CONFIRMATION_GAP_SECONDS, MAX_DATA_GAP_SECONDS

# onset 窗口
ONSET_WINDOW_SECONDS = 3
ONSET_MIN_TICKS = 8
LIMIT_BUFFER_TICKS = 2
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
        # 设计文档 §7.1：确认窗内所有累计增量必须有效。
        # NaN 或零增量行不可静默跳过，视为计数器同步未确认。
        if not np.isfinite(dv) or not np.isfinite(dt):
            block_reason = "counter_sync_unconfirmed"
            return {"combined_vwap": np.nan, "window_complete": False,
                    "confirmation_end_key": np.nan, "block_reason": block_reason}
        if dv <= 0 or dt <= 0:
            block_reason = "counter_sync_unconfirmed"
            return {"combined_vwap": np.nan, "window_complete": False,
                    "confirmation_end_key": np.nan, "block_reason": block_reason}
        total_dv += dv
        total_dt += dt
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


# ===========================================================================
# Task 4: 事件合并
# ===========================================================================

REASON_ORDER = ("visible_execution_drop", "interval_execution_drop")


def merge_candidates(
    candidates: pd.DataFrame,
    enriched_target_frame: pd.DataFrame,
    max_data_gap_seconds: int = MAX_DATA_GAP_SECONDS,
) -> pd.DataFrame:
    """同合约候选在 MERGE_WINDOW_SECONDS(10s) 内合并；跨数据断点(gap>max)不得合并。

    设计文档 §8：锚点取 candidate_execution_depth 最大者；事件成交量从
    enriched_target_frame 的 [event_start_key, event_end_key] 内所有有效正 delta_volume 回填。
    """
    if candidates.empty:
        return candidates.copy()
    out = candidates.sort_values("market_time_key", kind="stable").reset_index(drop=True)
    frame = enriched_target_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
    keys = out["market_time_key"].to_numpy()
    n = len(out)

    events: list[dict[str, object]] = []
    group_start = 0
    for idx in range(1, n + 1):
        should_flush = idx == n
        if not should_flush:
            gap = int(keys[idx]) - int(keys[idx - 1])
            if gap > MERGE_WINDOW_SECONDS * 1000 or gap > max_data_gap_seconds * 1000:
                should_flush = True
        if not should_flush:
            continue
        window = out.iloc[group_start:idx]
        # 锚点取 candidate_execution_depth 最大者
        anchor_idx = window["candidate_execution_depth"].idxmax()
        anchor = out.loc[anchor_idx]
        start_key = int(window["market_time_key"].min())
        end_key = int(window["market_time_key"].max())
        anchor_key = int(anchor["market_time_key"])
        # 事件成交量：从 enriched frame 的 [start_key, end_key] 内所有有效正 delta_volume
        event_volume = _sum_positive_delta_volume(frame, start_key, end_key)
        # 触发原因去重 + 固定顺序
        reasons = _dedup_reasons(window["trigger_reasons"].tolist())
        event = {
            "trade_date": anchor["trade_date"],
            "commodity": anchor["commodity"],
            "contract": anchor["contract"],
            "display_trade_date": anchor.get("display_trade_date", anchor["trade_date"]),
            "event_anchor_time": anchor.get("display_time"),
            "event_anchor_key": anchor_key,
            "event_anchor_start_key": int(anchor.get("snapshot_seq_start", anchor_key)),
            "event_anchor_end_key": int(anchor.get("snapshot_seq_end", anchor_key)),
            "event_start_key": start_key,
            "event_end_key": end_key,
            "event_volume": event_volume,
            "event_depth_ticks": float(anchor["candidate_execution_depth"]),
            "trigger_reasons": reasons,
            "fair_price": float(anchor["fair_price"]),
            "fair_uncertainty_ticks": float(anchor.get("fair_uncertainty_ticks", np.nan)),
            "valid_peer_count": int(anchor.get("valid_peer_count", 0)),
            "peer_contracts": str(anchor.get("peer_contracts", "")),
            "last_threshold_ticks": float(anchor["last_threshold_ticks"]),
            "vwap_threshold_ticks": float(anchor["vwap_threshold_ticks"]),
            "last_down_ticks": float(anchor.get("last_down_ticks", np.nan)),
            "vwap_down_ticks": float(anchor.get("vwap_down_ticks", np.nan)),
            "combined_vwap_1s": float(anchor.get("combined_vwap_1s", np.nan)),
            "combined_vwap_down_ticks": float(anchor.get("combined_vwap_down_ticks", np.nan)),
            "interval_confirmation_end_time": anchor.get("interval_confirmation_end_time", np.nan),
            "onset_ticks": float(anchor.get("onset_ticks", np.nan)),
            "noise_sample_count": int(anchor.get("noise_sample_count", 0)),
            "noise_time_span_seconds": float(anchor.get("noise_time_span_seconds", 0)),
            "__valid_peer_contracts": anchor.get("__valid_peer_contracts", list()),
            "__peer_bases": anchor.get("__peer_bases", dict()),
            "last_price_anchor": float(anchor.get("LastPrice", np.nan)),
            "interval_vwap_anchor": float(anchor.get("interval_vwap", np.nan)),
        }
        events.append(event)
        group_start = idx
    return pd.DataFrame(events)


def _sum_positive_delta_volume(frame: pd.DataFrame, start_key: int, end_key: int) -> float:
    fkeys = frame["market_time_key"].to_numpy()
    lo = int(np.searchsorted(fkeys, start_key, side="left"))
    hi = int(np.searchsorted(fkeys, end_key, side="right"))
    if lo >= hi:
        return 0.0
    dv = frame["delta_volume"].iloc[lo:hi]
    valid = dv[np.isfinite(dv) & (dv > 0)]
    return float(valid.sum()) if not valid.empty else 0.0


def _dedup_reasons(reason_lists: list[str]) -> str:
    parts: set[str] = set()
    for r in reason_lists:
        if not r or (isinstance(r, float) and math.isnan(r)):
            continue
        for p in str(r).split(","):
            p = p.strip()
            if p:
                parts.add(p)
    ordered = [r for r in REASON_ORDER if r in parts]
    return ",".join(ordered)


# ===========================================================================
# Task 4: 冻结 basis 同通道回归
# ===========================================================================

RECOVERY_WINDOWS = (3, 10)


def attach_recovery_metrics(
    events: pd.DataFrame,
    enriched_target_frame: pd.DataFrame,
    reference_frames: dict[str, pd.DataFrame],
    profile: dict[str, object],
) -> pd.DataFrame:
    """冻结锚点 basis 与 peer 集合，观察 3s/10s 内各触发通道是否回归。

    设计文档 §8：恢复阶段冻结 basis_i(anchor)，只更新 peer 当前价格；
    每个锚点实际触发的成交通道分别判断恢复；复用 _compute_1s_confirmation helper。
    函数不得重新估计 noise、阈值或 basis。
    """
    if events.empty:
        return events.copy()
    out = events.copy()
    out["recovery_label"] = ""
    out["visible_recovered_seconds"] = np.nan
    out["interval_recovered_seconds"] = np.nan
    out["quote_recovered_seconds"] = np.nan
    out["recovery_truncated"] = False

    tick_size = float(profile["tick_size"])
    multiplier = int(profile["contract_multiplier"])
    frame = enriched_target_frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
    fkeys = frame["market_time_key"].to_numpy()
    # peer 索引
    peer_aligned = _build_recovery_peer_index(reference_frames)

    for ei in range(len(out)):
        event = out.iloc[ei]
        anchor_key = int(event["event_anchor_key"])
        reasons = str(event.get("trigger_reasons", "")).split(",")
        anchor_thr_last = float(event["last_threshold_ticks"])
        anchor_thr_vwap = float(event["vwap_threshold_ticks"])
        fair_price = float(event["fair_price"])
        peer_bases = event.get("__peer_bases", {})
        peer_contracts = event.get("__valid_peer_contracts", [])

        anchor_pos = int(np.searchsorted(fkeys, anchor_key, side="left"))
        if anchor_pos >= len(fkeys) or int(fkeys[anchor_pos]) != anchor_key:
            out.at[ei, "recovery_label"] = "truncated"
            out.at[ei, "recovery_truncated"] = True
            continue

        visible_triggered = "visible_execution_drop" in reasons
        interval_triggered = "interval_execution_drop" in reasons

        # 收集恢复窗口内的 target 帧（不跨数据断点）
        window_rows = _collect_recovery_window(frame, anchor_pos, tick_size)
        if window_rows is None:
            out.at[ei, "recovery_label"] = "truncated"
            out.at[ei, "recovery_truncated"] = True
            continue

        visible_recovered_sec = np.nan
        interval_recovered_sec = np.nan
        quote_recovered_sec = np.nan
        any_valid_recovery_fp = False

        for wpos in window_rows:
            mk = int(fkeys[wpos])
            elapsed = (mk - anchor_key) / 1000.0
            # 锚点本身不计入恢复（elapsed==0）
            if wpos == anchor_pos:
                continue
            # 逐通道判断恢复（使用冻结 basis 的 recovery_fair_price）
            rec_fp = _recovery_fair_price(mk, peer_aligned, peer_contracts, peer_bases, tick_size)
            if rec_fp is None:
                continue
            any_valid_recovery_fp = True
            last = frame.at[wpos, "LastPrice"]
            dv = frame.at[wpos, "delta_volume"]
            # 可见通道
            if visible_triggered and not np.isfinite(visible_recovered_sec):
                if np.isfinite(dv) and dv > 0 and np.isfinite(last) and last > 0:
                    last_rem = (rec_fp - last) / tick_size
                    if last_rem < anchor_thr_last:
                        visible_recovered_sec = elapsed
            # 区间均价通道：需完整 1s 确认窗
            if interval_triggered and not np.isfinite(interval_recovered_sec):
                conf = _compute_1s_confirmation(frame, wpos, tick_size, multiplier)
                if conf["window_complete"]:
                    cv = (rec_fp - conf["combined_vwap"]) / tick_size
                    if cv < anchor_thr_vwap:
                        interval_recovered_sec = (int(conf["confirmation_end_key"]) - anchor_key) / 1000.0
            # 报价通道
            if not np.isfinite(quote_recovered_sec):
                bid = frame.at[wpos, "BidPrice1"]
                if np.isfinite(bid) and bid > 0:
                    quote_rem = (rec_fp - bid) / tick_size
                    if quote_rem < anchor_thr_last:
                        quote_recovered_sec = elapsed

        # 设计文档 §8：参考价失效（peer 报价无效或不足）时输出 truncated
        if not any_valid_recovery_fp:
            out.at[ei, "recovery_label"] = "truncated"
            out.at[ei, "recovery_truncated"] = True
            continue

        out.at[ei, "visible_recovered_seconds"] = visible_recovered_sec
        out.at[ei, "interval_recovered_seconds"] = interval_recovered_sec
        out.at[ei, "quote_recovered_seconds"] = quote_recovered_sec

        out.at[ei, "recovery_label"] = _classify_recovery(
            visible_triggered, interval_triggered,
            visible_recovered_sec, interval_recovered_sec, quote_recovered_sec,
        )
    return out


def _classify_recovery(
    visible_triggered: bool,
    interval_triggered: bool,
    visible_sec: float,
    interval_sec: float,
    quote_sec: float,
) -> str:
    trade_channels_recovered_3s = True
    trade_channels_recovered_10s = True
    if visible_triggered and not np.isfinite(visible_sec):
        trade_channels_recovered_3s = False
        trade_channels_recovered_10s = False
    if interval_triggered and not np.isfinite(interval_sec):
        trade_channels_recovered_3s = False
        trade_channels_recovered_10s = False
    if visible_triggered and np.isfinite(visible_sec) and visible_sec > 3:
        trade_channels_recovered_3s = False
    if interval_triggered and np.isfinite(interval_sec) and interval_sec > 3:
        trade_channels_recovered_3s = False
    if visible_triggered and np.isfinite(visible_sec) and visible_sec > 10:
        trade_channels_recovered_10s = False
    if interval_triggered and np.isfinite(interval_sec) and interval_sec > 10:
        trade_channels_recovered_10s = False

    if trade_channels_recovered_3s:
        return "trade_recovered_3s"
    quote_3s = np.isfinite(quote_sec) and quote_sec <= 3
    if quote_3s and not trade_channels_recovered_3s:
        return "quote_only_recovered_3s"
    if trade_channels_recovered_10s:
        return "trade_recovered_10s"
    quote_10s = np.isfinite(quote_sec) and quote_sec <= 10
    if quote_10s:
        return "quote_only_recovered_10s"
    return "persistent_10s"


def _collect_recovery_window(
    frame: pd.DataFrame,
    anchor_pos: int,
    tick_size: float,
) -> list[int] | None:
    """收集锚点后 10s 内、不跨数据断点的 target 帧位置。"""
    fkeys = frame["market_time_key"].to_numpy()
    anchor_key = int(fkeys[anchor_pos])
    end_key = anchor_key + 10 * 1000
    positions: list[int] = []
    last_mk = anchor_key
    for j in range(anchor_pos, len(fkeys)):
        mk = int(fkeys[j])
        if j > anchor_pos:
            gap = mk - last_mk
            if gap > MAX_DATA_GAP_SECONDS * 1000:
                # 数据断点：如果 10s 窗未满，标 truncated
                if mk <= end_key:
                    return None
                break
        if mk > end_key:
            break
        positions.append(j)
        last_mk = mk
    return positions


def _build_recovery_peer_index(
    reference_frames: dict[str, pd.DataFrame],
) -> dict[str, dict[str, np.ndarray]]:
    """为恢复阶段构建 peer 索引，保留报价有效性所需的全部字段。"""
    result: dict[str, dict[str, np.ndarray]] = {}
    for code, frame in reference_frames.items():
        if frame.empty:
            continue
        fr = frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        result[code] = {
            "keys": fr["market_time_key"].to_numpy(),
            "mids": fr["mid_price"].to_numpy(dtype=float),
            "bids": fr["BidPrice1"].to_numpy(dtype=float),
            "asks": fr["AskPrice1"].to_numpy(dtype=float),
            "tradable": fr["is_tradable_session"].fillna(False).to_numpy(dtype=bool) if "is_tradable_session" in fr.columns else np.ones(len(fr), dtype=bool),
            "upper": fr.get("UpperLimitPrice", pd.Series([np.inf] * len(fr))).to_numpy(dtype=float),
            "lower": fr.get("LowerLimitPrice", pd.Series([-np.inf] * len(fr))).to_numpy(dtype=float),
        }
    return result


MAX_RECOVERY_PEER_AGE_MS = MAX_REFERENCE_AGE_SECONDS * 1000


def _recovery_fair_price(
    mk: int,
    peer_idx: dict[str, dict[str, np.ndarray]],
    peer_contracts: list[str],
    peer_bases: dict[str, float],
    tick_size: float,
) -> float | None:
    """recovery_fair_price(u) = median_i(peer_i_mid_asof(u) + basis_i(anchor))。

    严格使用冻结的 basis_i(anchor)，不从未来帧重新估计。
    peer asof 必须满足与检测阶段相同的新鲜度/交易时段/报价有效性/涨跌停校验。
    """
    fair_i_values: list[float] = []
    for code in peer_contracts:
        if code not in peer_idx or code not in peer_bases:
            continue
        pidx = peer_idx[code]
        keys = pidx["keys"]
        pos = int(np.searchsorted(keys, mk, side="right") - 1)
        if pos < 0:
            continue
        # 新鲜度校验（age <= 3s）
        age_ms = mk - int(keys[pos])
        if age_ms > MAX_RECOVERY_PEER_AGE_MS:
            continue
        mid = float(pidx["mids"][pos])
        bid = float(pidx["bids"][pos])
        ask = float(pidx["asks"][pos])
        # 交易时段校验
        if not bool(pidx["tradable"][pos]):
            continue
        # 报价有效性
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        if not np.isfinite(mid) or mid <= 0:
            continue
        # 涨跌停距离校验
        ul = float(pidx["upper"][pos])
        ll = float(pidx["lower"][pos])
        if ul > 0 and mid >= ul - LIMIT_BUFFER_TICKS * tick_size:
            continue
        if ll > 0 and mid <= ll + LIMIT_BUFFER_TICKS * tick_size:
            continue
        fair_i_values.append(mid + peer_bases[code])
    if len(fair_i_values) < MIN_VALID_PEERS_RECOVERY:
        return None
    return float(np.median(fair_i_values))


MIN_VALID_PEERS_RECOVERY = 2
