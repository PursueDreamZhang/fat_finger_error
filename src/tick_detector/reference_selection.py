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

    # 初始化 Pass 2 输出列（由 _attach_noise_history 整列覆写）
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

    # 为每个 peer 预计算与 target 行对齐的 asof mid / spread / 有效性
    peer_aligned = _build_peer_aligned(out, reference_frames, tick_size)

    # Pass 1 输出预分配：循环内只写数组，结束后一次性写回 DataFrame。
    # valid_peer_contracts_col / peer_bases_col 保留逐行 list/dict 的对象语义。
    fair_prices = np.full(n, np.nan)
    fair_uncertainties = np.full(n, np.nan)
    fair_reliable = np.zeros(n, dtype=bool)
    valid_peer_counts = np.zeros(n, dtype=np.int64)
    peer_contracts_arr = np.empty(n, dtype=object)
    peer_contracts_arr[:] = ""
    valid_peer_contracts_col: list[list[str]] = [[] for _ in range(n)]
    peer_bases_col: list[dict[str, float]] = [{} for _ in range(n)]
    pass1_blocked = np.empty(n, dtype=object)
    pass1_blocked[:] = ""

    # Pass 1：逐行计算 fair_price
    target_spread_ticks = out["spread_ticks"].to_numpy(dtype=float) if "spread_ticks" in out.columns else np.full(n, np.nan)
    target_base_valid = _quote_base_valid(out, tick_size)
    for i in range(n):
        mk = int(keys[i])
        ws = mk - BASELINE_WINDOW_SECONDS * 1000
        we = mk - BASELINE_EXCLUDE_RECENT_SECONDS * 1000
        lo = int(np.searchsorted(keys, ws, side="left"))
        hi = int(np.searchsorted(keys, we, side="right"))

        # 目标与 peer 分别用各自基线的 spread p95 + 1 tick 判定报价有效。
        target_window_valid = target_base_valid[lo:hi]
        target_window_spreads = target_spread_ticks[lo:hi]
        valid_target_spreads = target_window_spreads[target_window_valid & np.isfinite(target_window_spreads)]
        if len(valid_target_spreads) < BASELINE_MIN_PAIRS_PER_PEER:
            continue
        target_spread_limit_ticks = float(np.percentile(valid_target_spreads, 95)) + 1

        fair_i_values: list[float] = []
        valid_peers: list[str] = []
        peer_bases: dict[str, float] = {}

        for code, pa in peer_aligned.items():
            if hi - lo < BASELINE_MIN_PAIRS_PER_PEER:
                continue
            diffs = pa["diff"][lo:hi]
            peer_window_valid = pa["asof_base_valid"][lo:hi]
            peer_window_spreads = pa["asof_spread_ticks"][lo:hi]
            valid_peer_spreads = peer_window_spreads[peer_window_valid & np.isfinite(peer_window_spreads)]
            if len(valid_peer_spreads) < BASELINE_MIN_PAIRS_PER_PEER:
                continue
            peer_spread_limit_ticks = float(np.percentile(valid_peer_spreads, 95)) + 1
            cur_mid = pa["asof_mid"][i]
            cur_spread = pa["asof_spread_ticks"][i]
            if (
                not np.isfinite(cur_mid)
                or cur_mid <= 0
                or not pa["asof_base_valid"][i]
                or not np.isfinite(cur_spread)
                or cur_spread > peer_spread_limit_ticks
            ):
                continue
            valid_mask = (
                np.isfinite(diffs)
                & target_window_valid
                & (target_window_spreads <= target_spread_limit_ticks)
                & peer_window_valid
                & (peer_window_spreads <= peer_spread_limit_ticks)
            )
            if valid_mask.sum() < BASELINE_MIN_PAIRS_PER_PEER:
                continue
            # 基线覆盖时长 >= 60s（设计文档 §5.2）
            peer_keys_window = pa["asof_key"][lo:hi]
            peer_keys_valid = peer_keys_window[valid_mask & np.isfinite(peer_keys_window)]
            if len(peer_keys_valid) < 2:
                continue
            span_ms = int(peer_keys_valid.max()) - int(peer_keys_valid.min())
            if span_ms < BASELINE_MIN_SPAN_PER_PEER_SECONDS * 1000:
                continue
            basis = float(np.median(diffs[valid_mask]))
            peer_bases[code] = basis
            fair_i_values.append(cur_mid + basis)
            valid_peers.append(code)

        valid_peer_counts[i] = len(valid_peers)
        peer_contracts_arr[i] = ",".join(valid_peers)
        valid_peer_contracts_col[i] = valid_peers
        peer_bases_col[i] = peer_bases

        if len(valid_peers) < MIN_VALID_PEERS:
            pass1_blocked[i] = "insufficient_peers"
            continue

        fair_arr = np.array(fair_i_values, dtype=float)
        fair_price = float(np.median(fair_arr))
        uncertainty = _mad_sigma(fair_arr) / tick_size
        fair_prices[i] = fair_price
        fair_uncertainties[i] = uncertainty
        reliable = uncertainty <= FAIR_UNCERTAINTY_LIMIT_TICKS
        fair_reliable[i] = reliable
        if not reliable:
            pass1_blocked[i] = "fair_uncertainty_exceeded"

    # Pass 1 输出一次性写回 DataFrame
    out["fair_price"] = fair_prices
    out["fair_uncertainty_ticks"] = fair_uncertainties
    out["fair_price_reliable"] = fair_reliable
    out["valid_peer_count"] = valid_peer_counts
    out["peer_contracts"] = peer_contracts_arr
    out["__valid_peer_contracts"] = valid_peer_contracts_col
    out["__peer_bases"] = peer_bases_col
    out["reference_blocked_reason"] = pass1_blocked

    # Pass 2：noise history，复用 Pass 1 的 fair_price(s)
    _attach_noise_history(out, tick_size, pass1_blocked)
    return out


def _quote_base_valid(frame: pd.DataFrame, tick_size: float) -> np.ndarray:
    """不含动态 spread 门槛的报价有效性，供目标基线配对复用。"""
    bid = frame["BidPrice1"].to_numpy(dtype=float)
    ask = frame["AskPrice1"].to_numpy(dtype=float)
    mid = frame["mid_price"].to_numpy(dtype=float)
    tradable = frame["is_tradable_session"].fillna(False).to_numpy(dtype=bool)
    upper = frame.get("UpperLimitPrice", pd.Series([np.inf] * len(frame))).to_numpy(dtype=float)
    lower = frame.get("LowerLimitPrice", pd.Series([-np.inf] * len(frame))).to_numpy(dtype=float)
    return (
        tradable
        & (bid > 0)
        & (ask > 0)
        & (ask >= bid)
        & np.isfinite(mid)
        & (mid > 0)
        & ~((upper > 0) & (mid >= upper - LIMIT_BUFFER_TICKS * tick_size))
        & ~((lower > 0) & (mid <= lower + LIMIT_BUFFER_TICKS * tick_size))
    )


# ---------------------------------------------------------------------------
# peer asof 对齐（向量化）
# ---------------------------------------------------------------------------


def _build_peer_aligned(
    target_df: pd.DataFrame,
    reference_frames: dict[str, pd.DataFrame],
    tick_size: float,
) -> dict[str, dict[str, np.ndarray]]:
    """为每个 peer 预计算与 target 行索引对齐的 asof mid / spread / diff / 有效性。

    asof_mid[i]      = peer 在 target_keys[i] 时刻的 asof mid（age<=3s 且报价基本有效，否则 nan）
    asof_spread_ticks[i] = 该时刻 peer asof 的 spread_ticks
    asof_key[i]      = peer asof 对应的 market_time_key（供基线跨度检查）
    diff[i]          = target_mid[i] - asof_mid[i]（供 basis 滑动中位数使用）
    asof_base_valid[i] = 该时刻 peer 报价是否满足 age/时段/涨跌停等基本有效性（不含 spread p95 过滤）
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

        positions = np.searchsorted(p_keys, keys, side="right") - 1
        has_position = positions >= 0
        safe_positions = np.clip(positions, 0, len(p_keys) - 1)
        matched_keys = p_keys[safe_positions]
        matched_mids = p_mids[safe_positions]
        matched_bids = p_bids[safe_positions]
        matched_asks = p_asks[safe_positions]
        matched_upper = p_upper[safe_positions]
        matched_lower = p_lower[safe_positions]
        age_ms = keys.astype(np.int64) - matched_keys.astype(np.int64)
        asof_base_valid = (
            has_position
            & (age_ms <= MAX_REFERENCE_AGE_SECONDS * 1000)
            & p_tradable[safe_positions]
            & (matched_bids > 0)
            & (matched_asks > 0)
            & (matched_asks >= matched_bids)
            & np.isfinite(matched_mids)
            & (matched_mids > 0)
            & ~((matched_upper > 0) & (matched_mids >= matched_upper - LIMIT_BUFFER_TICKS * tick_size))
            & ~((matched_lower > 0) & (matched_mids <= matched_lower + LIMIT_BUFFER_TICKS * tick_size))
        )
        asof_mid = np.where(asof_base_valid, matched_mids, np.nan)
        asof_spread_ticks = np.where(
            asof_base_valid,
            (matched_asks - matched_bids) / tick_size,
            np.nan,
        )
        asof_key = np.where(asof_base_valid, matched_keys.astype(float), np.nan)

        diff = target_mids - asof_mid
        result[code] = {
            "asof_mid": asof_mid,
            "asof_spread_ticks": asof_spread_ticks,
            "asof_key": asof_key,
            "diff": diff,
            "asof_base_valid": asof_base_valid,
        }
    return result


# ---------------------------------------------------------------------------
# noise history（Pass 2，复用已算好的 fair_price）
# ---------------------------------------------------------------------------


def _attach_noise_history(out: pd.DataFrame, tick_size: float, blocked_reasons: np.ndarray) -> None:
    """每个目标行 t 用 [t-300s, t-10s] 窗口的已算 fair_price(s) 生成噪声与阈值。

    blocked_reasons 为 Pass 1 已填入的对象数组（引用语义），本函数就地补写
    insufficient_noise_history，结束后整列写回 DataFrame。
    """
    keys = out["market_time_key"].to_numpy()
    n = len(out)
    target_mids = out["mid_price"].to_numpy(dtype=float)
    target_last = out["LastPrice"].to_numpy(dtype=float)
    target_vwap = out["interval_vwap"].to_numpy(dtype=float)
    target_dv = out["delta_volume"].to_numpy(dtype=float)
    fair_prices = out["fair_price"].to_numpy(dtype=float)
    fair_reliable = out["fair_price_reliable"].to_numpy(dtype=bool)
    noise_sample_counts = np.zeros(n, dtype=int)
    noise_spans = np.zeros(n, dtype=float)
    noise_history_reliable = np.zeros(n, dtype=bool)
    last_noise_medians = np.full(n, np.nan)
    vwap_noise_medians = np.full(n, np.nan)
    last_noise_sigmas = np.full(n, np.nan)
    vwap_noise_sigmas = np.full(n, np.nan)
    depth_noise_sigmas = np.full(n, np.nan)
    last_thresholds = np.full(n, np.nan)
    vwap_thresholds = np.full(n, np.nan)

    for i in range(n):
        mk = int(keys[i])
        ws = mk - BASELINE_WINDOW_SECONDS * 1000
        we = mk - BASELINE_EXCLUDE_RECENT_SECONDS * 1000
        lo = int(np.searchsorted(keys, ws, side="left"))
        hi = int(np.searchsorted(keys, we, side="right"))
        if lo >= hi:
            if not blocked_reasons[i]:
                blocked_reasons[i] = "insufficient_noise_history"
            continue

        window_dv = target_dv[lo:hi]
        window_fp = fair_prices[lo:hi]
        valid_base = (
            np.isfinite(window_dv)
            & (window_dv > 0)
            & fair_reliable[lo:hi]
            & np.isfinite(window_fp)
            & (window_fp > 0)
        )
        window_last = target_last[lo:hi]
        window_vwap = target_vwap[lo:hi]
        valid_last = valid_base & np.isfinite(window_last) & (window_last > 0)
        valid_vwap = valid_base & np.isfinite(window_vwap) & (window_vwap > 0)
        last_noise = (window_fp[valid_last] - window_last[valid_last]) / tick_size
        vwap_noise = (window_fp[valid_vwap] - window_vwap[valid_vwap]) / tick_size
        depth_valid = valid_last | valid_vwap
        last_depth = np.where(valid_last, (window_fp - window_last) / tick_size, -np.inf)
        vwap_depth = np.where(valid_vwap, (window_fp - window_vwap) / tick_size, -np.inf)
        depth_noise = np.maximum(last_depth, vwap_depth)[depth_valid]

        span = (int(keys[hi - 1]) - int(keys[lo])) / 1000.0
        noise_sample_counts[i] = len(last_noise)
        noise_spans[i] = span
        reliable = len(last_noise) >= NOISE_MIN_SAMPLE_COUNT and span >= NOISE_MIN_SPAN_SECONDS
        noise_history_reliable[i] = reliable
        if not reliable:
            if not blocked_reasons[i]:
                blocked_reasons[i] = "insufficient_noise_history"
            continue

        last_med = float(np.median(last_noise)) if len(last_noise) else 0.0
        vwap_med = float(np.median(vwap_noise)) if len(vwap_noise) else 0.0
        last_sigma = _mad_sigma(last_noise) if len(last_noise) else 0.0
        vwap_sigma = _mad_sigma(vwap_noise) if len(vwap_noise) else 0.0
        # 设计文档 §6.1: execution_depth_robust_sigma = 1.4826 * MAD(max(last_noise, vwap_noise))
        depth_sigma = _mad_sigma(depth_noise) if len(depth_noise) else 0.0

        last_noise_medians[i] = last_med
        vwap_noise_medians[i] = vwap_med
        last_noise_sigmas[i] = last_sigma
        vwap_noise_sigmas[i] = vwap_sigma
        depth_noise_sigmas[i] = depth_sigma

        fp_t = fair_prices[i]
        min_depth_ticks = fp_t * MIN_DEPTH_BPS / 10000 / tick_size if np.isfinite(fp_t) and fp_t > 0 else 0.0
        last_thresholds[i] = max(MIN_LAST_TICKS, min_depth_ticks, last_med + NOISE_K * last_sigma)
        vwap_thresholds[i] = max(MIN_VWAP_TICKS, min_depth_ticks, vwap_med + NOISE_K * vwap_sigma)

    out["noise_sample_count"] = noise_sample_counts
    out["noise_time_span_seconds"] = noise_spans
    out["noise_history_reliable"] = noise_history_reliable
    out["last_noise_median"] = last_noise_medians
    out["vwap_noise_median"] = vwap_noise_medians
    out["last_noise_robust_sigma"] = last_noise_sigmas
    out["vwap_noise_robust_sigma"] = vwap_noise_sigmas
    out["execution_depth_robust_sigma"] = depth_noise_sigmas
    out["last_threshold_ticks"] = last_thresholds
    out["vwap_threshold_ticks"] = vwap_thresholds
    out["reference_blocked_reason"] = blocked_reasons


def _mad_sigma(arr: np.ndarray) -> float:
    if len(arr) == 0:
        return float("nan")
    med = float(np.median(arr))
    mad = float(np.median(np.abs(arr - med)))
    return 1.4826 * mad
