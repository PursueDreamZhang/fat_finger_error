from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.tick_detector.event_detection import (
    MAX_CONFIRMATION_GAP_SECONDS,
    detect_candidate_ticks,
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
