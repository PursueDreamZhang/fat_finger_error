from __future__ import annotations

import math

import numpy as np
import pandas as pd

# AU_V1 起始参数（设计文档 §6.1）
MIN_LAST_TICKS = 20
MIN_VWAP_TICKS = 20
MIN_DEPTH_BPS = 5
NOISE_K = 8
FAIR_UNCERTAINTY_LIMIT_TICKS = 10

BASELINE_WINDOW_SECONDS = 300
BASELINE_EXCLUDE_RECENT_SECONDS = 10
MAX_REFERENCE_AGE_SECONDS = 3
MIN_VALID_PEERS = 2
BASELINE_MIN_PAIRS_PER_PEER = 20
BASELINE_MIN_SPAN_PER_PEER_SECONDS = 60
NOISE_MIN_SAMPLE_COUNT = 100
NOISE_MIN_SPAN_SECONDS = 120
LIMIT_BUFFER_TICKS = 2


def select_reference_contracts(day_frames: dict[str, pd.DataFrame], target_contract: str, limit: int = 5) -> list[str]:
    """离线阶段按当日总成交量取同品种前 5 个真实参考合约。"""
    target_df = day_frames[target_contract]
    target_commodity = str(target_df["commodity"].iloc[0])
    candidates: list[tuple[str, float]] = []
    for contract, frame in day_frames.items():
        if contract == target_contract:
            continue
        if frame.empty or str(frame["commodity"].iloc[0]) != target_commodity:
            continue
        volume = float(frame["Volume"].max()) if "Volume" in frame.columns else 0.0
        candidates.append((contract, volume))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return [contract for contract, _ in candidates[:limit]]


def attach_fair_price_metrics(
    target_df: pd.DataFrame,
    reference_frames: dict[str, pd.DataFrame],
    tick_size: float,
) -> pd.DataFrame:
    """为 enriched target frame 写入 fair_price、noise history、阈值与参考质量。

    设计文档 §5.1（参考合约）、§5.2（基线与 fair_price）、§6.1（noise 阈值）的离线实现。
    时间契约统一使用 market_time_key（毫秒），不使用自然日 timestamp。

    两遍算法：
      Pass 1 — 对每个目标行 t 计算 basis_i(t)、fair_price(t)、fair_uncertainty_ticks。
      Pass 2 — noise history 复用 Pass 1 已算好的 fair_price(s)，不再重复重估。
    """
    out = target_df.sort_values("market_time_key", kind="stable").reset_index(drop=True).copy()

    n = len(out)
    keys = out["market_time_key"].to_numpy()

    # 初始化输出列
    out["fair_price"] = np.nan
    out["fair_uncertainty_ticks"] = np.nan
    out["fair_price_reliable"] = False
    out["valid_peer_count"] = 0
    out["peer_contracts"] = ""
    out["__valid_peer_contracts"] = [list() for _ in range(n)]
    out["__peer_bases"] = [dict() for _ in range(n)]
    out["last_threshold_ticks"] = np.nan
    out["vwap_threshold_ticks"] = np.nan
    out["noise_sample_count"] = 0
    out["noise_time_span_seconds"] = 0.0
    out["noise_history_reliable"] = False
    out["last_noise_median"] = np.nan
    out["vwap_noise_median"] = np.nan
    out["last_noise_robust_sigma"] = np.nan
    out["vwap_noise_robust_sigma"] = np.nan
    out["execution_depth_robust_sigma"] = np.nan
    out["reference_blocked_reason"] = ""

    # 为每个 peer 预计算与 target 行对齐的 asof mid / spread / 有效性
    target_mids = out["mid_price"].to_numpy(dtype=float)
    peer_aligned = _build_peer_aligned(out, reference_frames, tick_size)

    # Pass 1：逐行计算 fair_price
    for i in range(n):
        mk = int(keys[i])
        ws = mk - BASELINE_WINDOW_SECONDS * 1000
        we = mk - BASELINE_EXCLUDE_RECENT_SECONDS * 1000
        lo = int(np.searchsorted(keys, ws, side="left"))
        hi = int(np.searchsorted(keys, we, side="right"))

        fair_i_values: list[float] = []
        valid_peers: list[str] = []
        peer_bases: dict[str, float] = {}

        for code, pa in peer_aligned.items():
            cur_mid = pa["asof_mid"][i]
            if not np.isfinite(cur_mid) or cur_mid <= 0:
                continue
            if not pa["asof_valid"][i]:
                continue
            if hi - lo < BASELINE_MIN_PAIRS_PER_PEER:
                continue
            diffs = pa["diff"][lo:hi]
            valid_mask = np.isfinite(diffs)
            if valid_mask.sum() < BASELINE_MIN_PAIRS_PER_PEER:
                continue
            basis = float(np.median(diffs[valid_mask]))
            peer_bases[code] = basis
            fair_i_values.append(cur_mid + basis)
            valid_peers.append(code)

        out.at[i, "valid_peer_count"] = len(valid_peers)
        out.at[i, "peer_contracts"] = ",".join(valid_peers)
        out.at[i, "__valid_peer_contracts"] = valid_peers
        out.at[i, "__peer_bases"] = peer_bases

        if len(valid_peers) < MIN_VALID_PEERS:
            out.at[i, "reference_blocked_reason"] = "insufficient_peers"
            continue

        fair_arr = np.array(fair_i_values, dtype=float)
        fair_price = float(np.median(fair_arr))
        uncertainty = _mad_sigma(fair_arr) / tick_size
        out.at[i, "fair_price"] = fair_price
        out.at[i, "fair_uncertainty_ticks"] = uncertainty
        reliable = uncertainty <= FAIR_UNCERTAINTY_LIMIT_TICKS
        out.at[i, "fair_price_reliable"] = reliable
        if not reliable:
            out.at[i, "reference_blocked_reason"] = "fair_uncertainty_exceeded"

    # Pass 2：noise history，复用 Pass 1 的 fair_price(s)
    _attach_noise_history(out, tick_size)
    return out


# ---------------------------------------------------------------------------
# peer asof 对齐（向量化）
# ---------------------------------------------------------------------------


def _build_peer_aligned(
    target_df: pd.DataFrame,
    reference_frames: dict[str, pd.DataFrame],
    tick_size: float,
) -> dict[str, dict[str, np.ndarray]]:
    """为每个 peer 预计算与 target 行索引对齐的 asof mid / diff / 有效性。

    asof_mid[i]  = peer 在 target_keys[i] 时刻的 asof mid（age<=3s 且有效报价，否则 nan）
    diff[i]      = target_mid[i] - asof_mid[i]（供 basis 滑动中位数使用）
    asof_valid[i]= 该时刻 peer 报价是否有效
    """
    keys = target_df["market_time_key"].to_numpy()
    target_mids = target_df["mid_price"].to_numpy(dtype=float)
    result: dict[str, dict[str, np.ndarray]] = {}
    for code, frame in reference_frames.items():
        if frame.empty:
            continue
        fr = frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
        p_keys = fr["market_time_key"].to_numpy()
        p_mids = fr["mid_price"].to_numpy(dtype=float)
        p_bids = fr["BidPrice1"].to_numpy(dtype=float)
        p_asks = fr["AskPrice1"].to_numpy(dtype=float)
        p_tradable = fr["is_tradable_session"].fillna(False).to_numpy(dtype=bool)
        p_upper = fr.get("UpperLimitPrice", pd.Series([np.inf] * len(fr))).to_numpy(dtype=float)
        p_lower = fr.get("LowerLimitPrice", pd.Series([-np.inf] * len(fr))).to_numpy(dtype=float)

        asof_mid = np.full(len(keys), np.nan)
        asof_valid = np.zeros(len(keys), dtype=bool)
        for i, mk in enumerate(keys):
            pos = int(np.searchsorted(p_keys, mk, side="right") - 1)
            if pos < 0:
                continue
            age_ms = mk - int(p_keys[pos])
            if age_ms > MAX_REFERENCE_AGE_SECONDS * 1000:
                continue
            mid = p_mids[pos]
            bid = p_bids[pos]
            ask = p_asks[pos]
            if not p_tradable[pos]:
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            if not np.isfinite(mid) or mid <= 0:
                continue
            ul = p_upper[pos]
            ll = p_lower[pos]
            if ul > 0 and mid >= ul - LIMIT_BUFFER_TICKS * tick_size:
                continue
            if ll > 0 and mid <= ll + LIMIT_BUFFER_TICKS * tick_size:
                continue
            asof_mid[i] = mid
            asof_valid[i] = True

        diff = target_mids - asof_mid
        result[code] = {"asof_mid": asof_mid, "diff": diff, "asof_valid": asof_valid}
    return result


# ---------------------------------------------------------------------------
# noise history（Pass 2，复用已算好的 fair_price）
# ---------------------------------------------------------------------------


def _attach_noise_history(out: pd.DataFrame, tick_size: float) -> None:
    """每个目标行 t 用 [t-300s, t-10s] 窗口的已算 fair_price(s) 生成噪声与阈值。"""
    keys = out["market_time_key"].to_numpy()
    n = len(out)
    target_mids = out["mid_price"].to_numpy(dtype=float)
    target_last = out["LastPrice"].to_numpy(dtype=float)
    target_vwap = out["interval_vwap"].to_numpy(dtype=float)
    target_dv = out["delta_volume"].to_numpy(dtype=float)
    fair_prices = out["fair_price"].to_numpy(dtype=float)
    fair_reliable = out["fair_price_reliable"].to_numpy(dtype=bool)

    for i in range(n):
        mk = int(keys[i])
        ws = mk - BASELINE_WINDOW_SECONDS * 1000
        we = mk - BASELINE_EXCLUDE_RECENT_SECONDS * 1000
        lo = int(np.searchsorted(keys, ws, side="left"))
        hi = int(np.searchsorted(keys, we, side="right"))
        if lo >= hi:
            if not out.at[i, "reference_blocked_reason"]:
                out.at[i, "reference_blocked_reason"] = "insufficient_noise_history"
            continue

        last_noise: list[float] = []
        vwap_noise: list[float] = []
        for s in range(lo, hi):
            if not (np.isfinite(target_dv[s]) and target_dv[s] > 0):
                continue
            if not fair_reliable[s]:
                continue
            fp = fair_prices[s]
            if not np.isfinite(fp) or fp <= 0:
                continue
            last = target_last[s]
            if np.isfinite(last) and last > 0:
                last_noise.append((fp - last) / tick_size)
            vwap = target_vwap[s]
            if np.isfinite(vwap) and vwap > 0:
                vwap_noise.append((fp - vwap) / tick_size)

        span = (int(keys[hi - 1]) - int(keys[lo])) / 1000.0 if hi > lo else 0.0
        out.at[i, "noise_sample_count"] = len(last_noise)
        out.at[i, "noise_time_span_seconds"] = span
        reliable = len(last_noise) >= NOISE_MIN_SAMPLE_COUNT and span >= NOISE_MIN_SPAN_SECONDS
        out.at[i, "noise_history_reliable"] = reliable
        if not reliable:
            if not out.at[i, "reference_blocked_reason"]:
                out.at[i, "reference_blocked_reason"] = "insufficient_noise_history"
            continue

        last_arr = np.array(last_noise, dtype=float)
        vwap_arr = np.array(vwap_noise, dtype=float)

        last_med = float(np.median(last_arr)) if len(last_arr) else 0.0
        vwap_med = float(np.median(vwap_arr)) if len(vwap_arr) else 0.0
        last_sigma = _mad_sigma(last_arr) if len(last_arr) else 0.0
        vwap_sigma = _mad_sigma(vwap_arr) if len(vwap_arr) else 0.0
        # execution_depth_robust_sigma: 取两通道各自深度噪声的并集
        depth_all = np.concatenate([last_arr, vwap_arr]) if len(last_arr) and len(vwap_arr) else (last_arr if len(last_arr) else vwap_arr)
        depth_sigma = _mad_sigma(depth_all) if len(depth_all) else 0.0

        out.at[i, "last_noise_median"] = last_med
        out.at[i, "vwap_noise_median"] = vwap_med
        out.at[i, "last_noise_robust_sigma"] = last_sigma
        out.at[i, "vwap_noise_robust_sigma"] = vwap_sigma
        out.at[i, "execution_depth_robust_sigma"] = depth_sigma

        fp_t = fair_prices[i]
        min_depth_ticks = fp_t * MIN_DEPTH_BPS / 10000 / tick_size if np.isfinite(fp_t) and fp_t > 0 else 0.0
        out.at[i, "last_threshold_ticks"] = max(MIN_LAST_TICKS, min_depth_ticks, last_med + NOISE_K * last_sigma)
        out.at[i, "vwap_threshold_ticks"] = max(MIN_VWAP_TICKS, min_depth_ticks, vwap_med + NOISE_K * vwap_sigma)


def _mad_sigma(arr: np.ndarray) -> float:
    if len(arr) == 0:
        return float("nan")
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    return 1.4826 * mad
