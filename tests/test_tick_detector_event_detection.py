from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.tick_detector.event_detection import (
    MAX_CONFIRMATION_GAP_SECONDS,
    attach_recovery_metrics,
    detect_candidate_ticks,
    extract_candidate_ticks,
    merge_candidates,
)
from src.tick_detector.tick_io import MAX_DATA_GAP_SECONDS


def _row(
    mk: int,
    *,
    last_price: float = 100.0,
    interval_vwap: float = 100.0,
    delta_volume: float = 10.0,
    delta_turnover: float = 10000.0,
    fair_price: float = 100.0,
    fair_price_reliable: bool = True,
    last_threshold_ticks: float = 20.0,
    vwap_threshold_ticks: float = 20.0,
    tick_size: float = 0.02,
    multiplier: int = 1000,
    validation_status: str = "validated",
    is_open_protected: bool = False,
    is_tradable_session: bool = True,
    noise_history_reliable: bool = True,
    display_trade_date: str = "20260520",
    display_time: str | None = None,
    volume: float = 100.0,
    turnover: float = 1000000.0,
    bid_price: float = 99.98,
    ask_price: float = 100.0,
    delta_volume_nan: bool = False,
) -> dict[str, object]:
    return {
        "market_time_key": mk,
        "display_trade_date": display_trade_date,
        "display_time": display_time or f"mk{mk}",
        "snapshot_seq": mk,
        "snapshot_seq_start": mk,
        "snapshot_seq_end": mk,
        "trade_date": display_trade_date,
        "commodity": "AU",
        "contract": "AU2606",
        "LastPrice": last_price,
        "interval_vwap": interval_vwap,
        "delta_volume": float("nan") if delta_volume_nan else delta_volume,
        "delta_turnover": delta_turnover,
        "Volume": volume,
        "Turnover": turnover,
        "BidPrice1": bid_price,
        "AskPrice1": ask_price,
        "mid_price": (bid_price + ask_price) / 2,
        "spread": ask_price - bid_price,
        "spread_ticks": (ask_price - bid_price) / tick_size,
        "UpperLimitPrice": 200.0,
        "LowerLimitPrice": 50.0,
        "fair_price": fair_price,
        "fair_price_reliable": fair_price_reliable,
        "fair_uncertainty_ticks": 0.5,
        "last_threshold_ticks": last_threshold_ticks,
        "vwap_threshold_ticks": vwap_threshold_ticks,
        "noise_history_reliable": noise_history_reliable,
        "noise_sample_count": 200,
        "noise_time_span_seconds": 200.0,
        "last_noise_median": 0.0,
        "vwap_noise_median": 0.0,
        "last_noise_robust_sigma": 1.0,
        "vwap_noise_robust_sigma": 1.0,
        "execution_depth_robust_sigma": 1.0,
        "tick_size": tick_size,
        "contract_multiplier": multiplier,
        "parameter_profile": "AU_V1",
        "validation_status": validation_status,
        "is_open_protected": is_open_protected,
        "is_tradable_session": is_tradable_session,
        "session_state": "continuous_trading" if is_tradable_session else "off_session",
        "data_quality_flags": "",
        "valid_peer_count": 3,
        "peer_contracts": "AU2608,AU2610,AU2612",
        "__valid_peer_contracts": ["AU2608", "AU2610", "AU2612"],
        "__peer_bases": {"AU2608": 0.0, "AU2610": 0.0, "AU2612": 0.0},
        "reference_blocked_reason": "",
    }


def _df(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows).sort_values("market_time_key", kind="stable").reset_index(drop=True)


# ---------------------------------------------------------------------------
# visible_execution_drop（可见末笔分支可独立触发）
# ---------------------------------------------------------------------------


def test_visible_execution_drop_triggers_independently():
    # fair_price=100, last=99.0 -> last_down_ticks=(100-99)/0.02=50 >= 20
    # interval_vwap 正常,不触发区间分支;可见分支应独立命中
    df = _df([
        _row(10000, last_price=99.0, interval_vwap=100.0),
    ])
    out = detect_candidate_ticks(df)
    assert len(out) == 1
    assert "visible_execution_drop" in str(out["trigger_reasons"].iloc[0])


def test_visible_drop_blocked_when_last_below_threshold():
    # last=99.7 -> down_ticks=15 < 20
    df = _df([
        _row(10000, last_price=99.7, interval_vwap=100.0),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_visible_execution_spike_triggers_independently():
    marked = detect_candidate_ticks(_df([_row(10000, last_price=101.0)]), return_marked=True)
    out = extract_candidate_ticks(marked)
    assert out["event_direction"].tolist() == ["up"]
    assert "visible_execution_spike" in out["trigger_reasons"].iloc[0]
    assert out["last_up_ticks"].iloc[0] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# interval_execution_drop（区间均价分支需 1 秒确认）
# ---------------------------------------------------------------------------


def test_interval_execution_drop_needs_1s_confirmation():
    # fair=100, vwap=99.0 -> vwap_down=50>=20; t+1s 合并 vwap 仍异常才触发
    # 第一帧 mk=10000, 第二帧 mk=11000(gap=1s)
    df = _df([
        _row(10000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        _row(11000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
    ])
    out = detect_candidate_ticks(df)
    assert len(out) == 1
    assert "interval_execution_drop" in str(out["trigger_reasons"].iloc[0])
    # 应记录确认结束时间
    assert pd.notna(out["interval_confirmation_end_time"].iloc[0])


def test_interval_drop_blocked_when_1s_combined_normal():
    """单帧低均价、下一帧反向补偿、1 秒合并正常 -> counter_lag_suspect 不触发"""
    # mk=10000: vwap=99.0(down=50), mk=11000: vwap=101.0(反向)
    # 合并 vwap = (990000+1010000)/(10+10)/1000 = 100.0 -> 正常
    df = _df([
        _row(10000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        _row(11000, last_price=100.0, interval_vwap=101.0, delta_volume=10, delta_turnover=1010000),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty
    # 数据质量标记应反映 counter_lag_suspect（通过 data_quality_flags）
    df_marked = detect_candidate_ticks(df, return_marked=True)
    flags = str(df_marked["data_quality_flags"].iloc[0])
    assert "counter_lag_suspect" in flags


def test_interval_execution_spike_needs_and_passes_1s_confirmation():
    out = detect_candidate_ticks(_df([
        _row(10000, last_price=100.0, interval_vwap=101.0, delta_volume=10, delta_turnover=1010000),
        _row(11000, last_price=100.0, interval_vwap=101.0, delta_volume=10, delta_turnover=1010000),
    ]))
    assert out["event_direction"].tolist() == ["up"]
    assert "interval_execution_spike" in out["trigger_reasons"].iloc[0]


def test_same_time_key_keeps_opposite_direction_candidates_separate():
    out = detect_candidate_ticks(_df([
        _row(10000, last_price=99.0, interval_vwap=101.0, delta_volume=10, delta_turnover=1010000),
        _row(11000, last_price=100.0, interval_vwap=101.0, delta_volume=10, delta_turnover=1010000),
    ]))
    same_key = out.loc[out["market_time_key"] == 10000]
    assert same_key["event_direction"].tolist() == ["down", "up"]


def test_interval_drop_blocked_when_confirmation_window_incomplete():
    """确认窗末端没有覆盖到 t+1s 或 gap>1s 时为不完整，区间分支不触发"""
    # 只有 mk=10000 一帧,无 t+1s 数据 -> 窗口不完整
    df = _df([
        _row(10000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
    ])
    out = detect_candidate_ticks(df)
    # 可见分支不命中(last=100),区间分支因窗口不完整不命中
    assert out.empty


def test_interval_drop_blocked_on_gap_over_confirmation_limit():
    """相邻时间键 gap 超过 1s 时确认窗不完整"""
    # mk=10000 vwap异常, mk=12000 gap=2s > MAX_CONFIRMATION_GAP_SECONDS=1
    df = _df([
        _row(10000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        _row(12000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_interval_drop_blocked_when_window_has_nan_or_zero_delta():
    """确认窗内出现 NaN 或零增量行时视为不完整，区间分支不触发。

    设计文档 §7.1：确认窗内所有累计增量必须有效。
    mk=10000 vwap异常, mk=11000 delta_volume=NaN -> 计数器不同步未确认。
    """
    df = _df([
        _row(10000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        _row(11000, last_price=100.0, interval_vwap=99.0, delta_volume_nan=True),  # delta_volume=NaN
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_interval_drop_blocked_on_cross_session():
    """跨 session 的 1s 窗口不可确认"""
    df = _df([
        _row(10000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        _row(11000, last_price=100.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000, is_tradable_session=False),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


# ---------------------------------------------------------------------------
# 阻断条件
# ---------------------------------------------------------------------------


def test_blocked_when_delta_volume_zero():
    df = _df([
        _row(10000, last_price=99.0, delta_volume=0.0, delta_volume_nan=False),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_blocked_when_open_protected():
    df = _df([
        _row(10000, last_price=99.0, is_open_protected=True),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_blocked_when_noise_history_insufficient():
    df = _df([
        _row(10000, last_price=99.0, noise_history_reliable=False),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_blocked_when_fair_price_unreliable():
    df = _df([
        _row(10000, last_price=99.0, fair_price_reliable=False),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_blocked_when_non_au_commodity():
    df = _df([
        _row(10000, last_price=99.0, validation_status="unvalidated_commodity"),
    ])
    out = detect_candidate_ticks(df)
    assert out.empty


def test_blocked_when_data_gap_in_onset_window():
    """候选行与前一有效行之间 gap > MAX_DATA_GAP_SECONDS=3s 时 onset 禁止触发"""
    # 前面有正常历史，最后一条候选与上一条 gap=4s > 3s -> 数据断点
    df = _df([
        _row(3000, last_price=100.0),
        _row(5000, last_price=100.0),
        _row(6000, last_price=100.0),     # 最后连续点
        # gap 4s > 3s -> 数据断点，candidate(10000) 前一行即 6000
        _row(10000, last_price=99.0),
    ])
    out = detect_candidate_ticks(df)
    # 前三行正常不触发(last=100)，最后一行因数据断点不触发
    cand_rows = out[out["market_time_key"] == 10000]
    assert cand_rows.empty


# ---------------------------------------------------------------------------
# onset
# ---------------------------------------------------------------------------


def test_onset_blocks_when_depth_not_sudden():
    """前3秒已有同等深度下跌 -> onset 不够突发，不触发。

    前5秒价格正常(last=100)，之后持续跌到 99.0。
    第一笔 99.0（mk=10000）突发触发；但 mk=10000 之后的行因前3秒已有同等深度，
    onset≈0 不应再触发。这里验证 mk=13000/14000（已持续下跌）不触发。
    """
    df = _df([
        _row(5000, last_price=100.0, delta_volume=10),
        _row(6000, last_price=100.0, delta_volume=10),
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _row(10000, last_price=99.0, delta_volume=10),   # 突发下跌首笔
        _row(11000, last_price=99.0, delta_volume=10),
        _row(12000, last_price=99.0, delta_volume=10),
        _row(13000, last_price=99.0, delta_volume=10),   # 前3秒已有 depth=50
        _row(14000, last_price=99.0, delta_volume=10),   # 前3秒已有 depth=50
    ])
    out = detect_candidate_ticks(df)
    # mk=10000 突发首笔可能触发；mk=13000/14000 不应触发（持续非突发）
    later = out[out["market_time_key"].isin([13000, 14000])]
    assert later.empty


def test_onset_passes_when_depth_is_sudden():
    """前3秒正常，t 时刻突然大跌 -> 触发"""
    df = _df([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _row(10000, last_price=99.0, delta_volume=10),
    ])
    out = detect_candidate_ticks(df)
    assert len(out) == 1
    assert out["onset_ticks"].iloc[0] >= 8  # onset_threshold = max(8, 5*sigma)


def test_candidate_writes_depth_and_down_tick_fields():
    df = _df([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _row(10000, last_price=99.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        _row(11000, last_price=99.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
    ])
    out = detect_candidate_ticks(df)
    row = out.iloc[0]
    assert pd.notna(row["last_down_ticks"])
    assert pd.notna(row["vwap_down_ticks"])
    assert pd.notna(row["candidate_execution_depth"])
    assert pd.notna(row["onset_ticks"])
    # 两个原因都命中
    assert "visible_execution_drop" in str(row["trigger_reasons"])
    assert "interval_execution_drop" in str(row["trigger_reasons"])


# ===========================================================================
# Task 4: 事件合并与冻结 basis 同通道回归
# ===========================================================================


def _enriched_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    """构造带候选标记的 enriched target frame（模拟 detect_candidate_ticks 输出上下文）。"""
    return _df(rows)


def _candidate(
    mk: int,
    *,
    depth: float = 50.0,
    delta_volume: float = 10.0,
    delta_turnover: float = 10000.0,
    last_price: float = 99.0,
    trigger_reasons: str = "visible_execution_drop",
    last_threshold_ticks: float = 20.0,
    vwap_threshold_ticks: float = 20.0,
    fair_price: float = 100.0,
    interval_vwap: float = 100.0,
) -> dict[str, object]:
    """带候选字段的行，用于 merge_candidates 测试。"""
    base = _row(mk, last_price=last_price, interval_vwap=interval_vwap,
                delta_volume=delta_volume, delta_turnover=delta_turnover,
                fair_price=fair_price,
                last_threshold_ticks=last_threshold_ticks,
                vwap_threshold_ticks=vwap_threshold_ticks)
    base["candidate_execution_depth"] = depth
    base["onset_ticks"] = 50.0
    base["trigger_reasons"] = trigger_reasons
    base["last_down_ticks"] = (fair_price - last_price) / 0.02
    base["vwap_down_ticks"] = (fair_price - interval_vwap) / 0.02
    base["combined_vwap_1s"] = np.nan
    base["combined_vwap_down_ticks"] = np.nan
    base["interval_confirmation_end_time"] = np.nan
    return base


def test_merge_candidates_merges_within_10s_and_picks_max_depth_anchor():
    # 三个候选在 10s 内，depth 不同
    enriched = _enriched_frame([
        _candidate(10000, depth=50.0, delta_volume=10),
        _candidate(12000, depth=80.0, delta_volume=10),
        _candidate(15000, depth=60.0, delta_volume=10),
    ])
    candidates = enriched.copy()
    events = merge_candidates(candidates, enriched)
    assert len(events) == 1
    event = events.iloc[0]
    # 锚点取 depth 最大者
    assert event["event_depth_ticks"] == 80.0
    assert event["event_anchor_key"] == 12000
    # 事件成交量 = 开始至结束所有正增量之和
    assert event["event_volume"] == 30.0


def test_merge_candidates_does_not_merge_across_data_gap():
    # gap > MAX_DATA_GAP_SECONDS=3s 时不可合并
    enriched = _enriched_frame([
        _candidate(10000, depth=50.0, delta_volume=10),
        _candidate(20000, depth=80.0, delta_volume=10),  # gap=10s > 3s
    ])
    candidates = enriched.copy()
    events = merge_candidates(candidates, enriched)
    # 10s 合并窗内但数据 gap>3s -> 两个独立事件
    assert len(events) == 2


def test_merge_candidates_includes_intermediate_non_candidate_rows_in_volume():
    """两个候选间插入未命中但 delta_volume>0 的快照，事件成交量仍包含该行"""
    enriched = _enriched_frame([
        _candidate(10000, depth=50.0, delta_volume=10),
        _row(11000, last_price=100.0, delta_volume=15),   # 非候选但有成交
        _candidate(12000, depth=80.0, delta_volume=10),
    ])
    candidates = enriched[enriched["market_time_key"].isin([10000, 12000])].copy()
    events = merge_candidates(candidates, enriched)
    assert len(events) == 1
    # event_volume = 10 + 15 + 10 = 35
    assert events["event_volume"].iloc[0] == 35.0


def test_merge_candidates_copies_anchor_fields_and_dedups_reasons():
    enriched = _enriched_frame([
        _candidate(10000, depth=50.0, trigger_reasons="visible_execution_drop"),
        _candidate(12000, depth=80.0, trigger_reasons="visible_execution_drop,interval_execution_drop"),
    ])
    candidates = enriched.copy()
    events = merge_candidates(candidates, enriched)
    event = events.iloc[0]
    # 原子原因去重，固定顺序 visible,interval
    assert event["trigger_reasons"] == "visible_execution_drop,interval_execution_drop"
    # 锚点 fair_price 被复制
    assert event["fair_price"] == 100.0
    # 内部 peer 字段被复制
    assert isinstance(event["__peer_bases"], dict)


def test_merge_candidates_does_not_merge_opposite_directions():
    down = _candidate(10000, depth=50.0)
    up = _candidate(10000, depth=60.0, last_price=101.0, trigger_reasons="visible_execution_spike")
    up.update({
        "event_direction": "up", "last_up_ticks": 50.0, "vwap_up_ticks": 0.0,
        "combined_vwap_up_ticks": float("nan"),
    })
    candidates = _df([down, up])
    events = merge_candidates(candidates, candidates)
    assert events["event_direction"].tolist() == ["down", "up"]
    assert events["event_depth_ticks"].tolist() == [50.0, 60.0]


# ---------------------------------------------------------------------------
# 恢复：冻结 basis 的同通道回归
# ---------------------------------------------------------------------------


def _peer_frame(code: str, rows: list[dict[str, object]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["contract"] = code
    df["commodity"] = "AU"
    df["parse_status"] = "ok"
    df["trade_date"] = "20260520"
    df["validation_status"] = "validated"
    df["tick_size"] = 0.02
    df["contract_multiplier"] = 1000
    df["parameter_profile"] = "AU_V1"
    df["session_state"] = "continuous_trading"
    df["is_tradable_session"] = True
    df["avg_trade_price_enabled"] = True
    df["UpperLimitPrice"] = 200.0
    df["LowerLimitPrice"] = 50.0
    return df


def _peer_row(mk: int, mid_price: float = 100.0) -> dict[str, object]:
    return {
        "market_time_key": mk,
        "display_trade_date": "20260520",
        "display_time": f"mk{mk}",
        "LastPrice": mid_price,
        "mid_price": mid_price,
        "BidPrice1": mid_price - 0.01,
        "AskPrice1": mid_price + 0.01,
        "Volume": 100.0,
        "Turnover": 100000.0,
        "delta_volume": 10.0,
        "delta_turnover": 10000.0,
        "interval_vwap": mid_price,
        "is_tradable_session": True,
    }


def test_recovery_marks_trade_recovered_3s_when_channels_return():
    """锚点两通道均触发，3s 内成交回到阈值内 -> trade_recovered_3s。

    区间均价通道恢复需要完整 1s 确认窗，故恢复点必须有 t+1s 数据覆盖。
    """
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        _row(10500, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),  # 可见恢复
        _row(11000, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),  # 1s确认覆盖到11500
        _row(11500, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
    ])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    # peer frames：锚点后 peer 价格回到 100
    peer_keys = [10000, 10500, 11000, 11500]
    peer_a = _peer_frame("AU2608", [_peer_row(mk, 100.0) for mk in peer_keys])
    peer_b = _peer_frame("AU2610", [_peer_row(mk, 100.0) for mk in peer_keys])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    row = recovered.iloc[0]
    assert row["recovery_label"] == "trade_recovered_3s"
    assert pd.notna(row["visible_recovered_seconds"])
    assert pd.notna(row["interval_recovered_seconds"])


def test_up_recovery_uses_ask_and_keeps_bid_recovery_empty():
    anchor = _candidate(10000, depth=50.0, last_price=101.0, trigger_reasons="visible_execution_spike")
    anchor.update({"event_direction": "up", "last_up_ticks": 50.0, "vwap_up_ticks": 0.0})
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        anchor,
    ] + [
        _row(mk, last_price=100.0, delta_volume=10, bid_price=99.0, ask_price=100.0)
        for mk in range(11000, 21000, 1000)
    ])
    candidates = enriched.loc[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    peer_keys = list(range(10000, 21000, 1000))
    peers = {code: _peer_frame(code, [_peer_row(mk, 100.0) for mk in peer_keys]) for code in ("AU2608", "AU2610")}
    recovered = attach_recovery_metrics(events, enriched, peers, {"tick_size": 0.02, "contract_multiplier": 1000})
    row = recovered.iloc[0]
    assert pd.isna(row["quote_recovered_seconds"])
    assert row["ask_recovered_seconds"] == pytest.approx(1.0)


def test_recovery_marks_truncated_on_data_gap():
    """锚点后 10s 恢复窗内出现数据断点（gap>3s）-> truncated"""
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        _row(11000, last_price=99.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
        # gap=5s > 3s 数据断点，落在 10s 恢复窗 [10000,20000] 内
        _row(16000, last_price=99.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000),
    ])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    peer_a = _peer_frame("AU2608", [_peer_row(10000, 100.0)])
    peer_b = _peer_frame("AU2610", [_peer_row(10000, 100.0)])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    assert recovered["recovery_label"].iloc[0] == "truncated"


def test_recovery_persistent_when_not_recovered_in_10s():
    """10s 内连续数据但未恢复 -> persistent_10s"""
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        # 持续 99.0 不恢复，连续数据覆盖 10s 窗
    ] + [_row(mk, last_price=99.0, interval_vwap=99.0, delta_volume=10, delta_turnover=990000,
              bid_price=99.0)
         for mk in range(11000, 21000, 1000)])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    peer_keys = list(range(10000, 21000, 1000))
    peer_a = _peer_frame("AU2608", [_peer_row(mk, 100.0) for mk in peer_keys])
    peer_b = _peer_frame("AU2610", [_peer_row(mk, 100.0) for mk in peer_keys])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    assert recovered["recovery_label"].iloc[0] == "persistent_10s"


def test_recovery_interval_seconds_uses_confirmation_window_end():
    """区间均价恢复秒数取完整 1s 确认窗结束，而非窗口起点。

    恢复点 mk=10500，但 1s 确认窗覆盖到 mk=11500 才完整，
    故 interval_recovered_seconds = (11500-10000)/1000 = 1.5s，而非 0.5s。
    """
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        # mk=10500 成交正常，但 1s 合并窗要到 mk=11500 才完整
        _row(10500, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
        _row(11000, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
        _row(11500, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
    ])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    peer_keys = [10000, 10500, 11000, 11500]
    peer_a = _peer_frame("AU2608", [_peer_row(mk, 100.0) for mk in peer_keys])
    peer_b = _peer_frame("AU2610", [_peer_row(mk, 100.0) for mk in peer_keys])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    row = recovered.iloc[0]
    # 区间恢复确认秒数 = 确认窗结束(mk=11500) - 锚点(10000) = 1.5s
    assert row["interval_recovered_seconds"] == pytest.approx(1.5, abs=0.6)


def test_recovery_does_not_re_estimate_threshold_from_future():
    """恢复严格使用锚点的两类阈值，不从未来帧重新估计"""
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        _row(10500, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
        _row(11000, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
        _row(11500, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
    ])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    # 锚点阈值 last_threshold_ticks=20, vwap_threshold_ticks=20
    anchor_thr = float(candidates["last_threshold_ticks"].iloc[0])
    peer_keys = [10000, 10500, 11000, 11500]
    peer_a = _peer_frame("AU2608", [_peer_row(mk, 100.0) for mk in peer_keys])
    peer_b = _peer_frame("AU2610", [_peer_row(mk, 100.0) for mk in peer_keys])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    # 恢复判断使用锚点阈值 anchor_thr=20，不应被未来帧的阈值覆盖
    assert recovered["last_threshold_ticks"].iloc[0] == pytest.approx(anchor_thr)


def test_recovery_marks_truncated_when_peer_quote_invalid():
    """恢复阶段参考价失效（peer 报价无效或不足）时标 truncated。

    peer 报价在锚点后全部失效（mid<=0），recovery_fair_price 无法计算 -> truncated。
    """
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        _row(11000, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
    ])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    # peer 报价无效（mid=0）
    peer_a = _peer_frame("AU2608", [_peer_row(10000, 100.0), _peer_row(11000, 0.0)])
    peer_b = _peer_frame("AU2610", [_peer_row(10000, 100.0), _peer_row(11000, 0.0)])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    assert recovered["recovery_label"].iloc[0] == "truncated"


def test_recovery_marks_truncated_when_peer_stale():
    """恢复阶段 peer 报价 age > 3s（陈旧）时参考价失效 -> truncated。"""
    enriched = _enriched_frame([
        _row(7000, last_price=100.0, delta_volume=10),
        _row(8000, last_price=100.0, delta_volume=10),
        _row(9000, last_price=100.0, delta_volume=10),
        _candidate(10000, depth=50.0, last_price=99.0, interval_vwap=99.0,
                   delta_volume=10, delta_turnover=990000,
                   trigger_reasons="visible_execution_drop,interval_execution_drop"),
        _row(14000, last_price=100.0, interval_vwap=100.0, delta_volume=10, delta_turnover=1000000),
    ])
    candidates = enriched[enriched["market_time_key"] == 10000].copy()
    events = merge_candidates(candidates, enriched)
    # peer 在 14000 时刻只有 10000 的报价（age=4s > 3s -> 陈旧）
    peer_a = _peer_frame("AU2608", [_peer_row(10000, 100.0)])
    peer_b = _peer_frame("AU2610", [_peer_row(10000, 100.0)])
    profile = {"tick_size": 0.02, "contract_multiplier": 1000}
    recovered = attach_recovery_metrics(events, enriched, {"AU2608": peer_a, "AU2610": peer_b}, profile)
    assert recovered["recovery_label"].iloc[0] == "truncated"
