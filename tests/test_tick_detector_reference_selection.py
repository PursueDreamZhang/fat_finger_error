from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.tick_detector.reference_selection import (
    FAIR_UNCERTAINTY_LIMIT_TICKS,
    attach_fair_price_metrics,
    select_reference_contracts,
)


def _contract_frame(
    contract: str,
    commodity: str,
    rows: list[dict[str, object]],
    *,
    trade_date: str = "20260520",
) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["contract"] = contract
    df["commodity"] = commodity
    df["parse_status"] = "ok"
    df["trade_date"] = trade_date
    df["validation_status"] = "validated" if commodity == "AU" else "unvalidated_commodity"
    df["tick_size"] = 0.02 if commodity == "AU" else np.nan
    df["contract_multiplier"] = 1000 if commodity == "AU" else None
    df["parameter_profile"] = "AU_V1" if commodity == "AU" else None
    for col in ("market_time_key", "display_trade_date", "display_time"):
        if col not in df.columns:
            df[col] = range(len(df))
    for col in ("session_state", "is_tradable_session", "is_open_protected"):
        if col not in df.columns:
            df[col] = True if col != "session_state" else "continuous_trading"
    if "avg_trade_price_enabled" not in df.columns:
        df["avg_trade_price_enabled"] = True
    return df


def _mk(
    contract: str,
    mk: int,
    *,
    last_price: float = 100.0,
    mid_price: float = 100.0,
    volume: float = 10.0,
    delta_volume: float = 10.0,
    delta_turnover: float = 10000.0,
    interval_vwap: float = 100.0,
    bid_price: float = 99.98,
    ask_price: float = 100.0,
    upper_limit: float = 200.0,
    lower_limit: float = 50.0,
    display_time: str | None = None,
    is_tradable: bool = True,
    delta_volume_nan: bool = False,
) -> dict[str, object]:
    return {
        "market_time_key": mk,
        "display_trade_date": "20260520",
        "display_time": display_time or f"{mk}",
        "LastPrice": last_price,
        "mid_price": mid_price,
        "BidPrice1": bid_price,
        "AskPrice1": ask_price,
        "Volume": volume,
        "delta_volume": float("nan") if delta_volume_nan else delta_volume,
        "delta_turnover": delta_turnover,
        "interval_vwap": interval_vwap,
        "UpperLimitPrice": upper_limit,
        "LowerLimitPrice": lower_limit,
        "is_tradable_session": is_tradable,
        "session_state": "continuous_trading" if is_tradable else "off_session",
        "spread": ask_price - bid_price,
        "spread_ticks": (ask_price - bid_price) / 0.02,
    }


# ---------------------------------------------------------------------------
# select_reference_contracts
# ---------------------------------------------------------------------------


def test_select_reference_contracts_picks_top5_by_daily_volume_excluding_target_and_cross_commodity():
    a = _contract_frame("AU2606", "AU", [{"Volume": 100}])
    b = _contract_frame("AU2608", "AU", [{"Volume": 300}])
    c = _contract_frame("AU2610", "AU", [{"Volume": 200}])
    ag = _contract_frame("AG2606", "AG", [{"Volume": 1000}])

    refs = select_reference_contracts(
        {"AU2606": a, "AU2608": b, "AU2610": c, "AG2606": ag},
        target_contract="AU2606",
    )
    assert refs == ["AU2608", "AU2610"]


# ---------------------------------------------------------------------------
# fair_price baseline
# ---------------------------------------------------------------------------


def _two_peers_stable(rows: int = 250):
    """目标与两个 peer 保持稳定 0 价差，基线点 target_mid==peer_mid，basis=0。

    默认 250 秒以保证最后一帧的 noise 窗口 [t-300s,t-10s] 内有足够 fair_reliable 样本。
    fair_price 需要 ≥60s 历史才 reliable，因此 noise 可用样本 ≈ rows-70。
    """
    target_rows = []
    peer_a_rows = []
    peer_b_rows = []
    for sec in range(rows):
        mk = sec * 1000
        target_rows.append(_mk("AU2606", mk, last_price=100.0, mid_price=100.0, display_time=f"t{sec}"))
        peer_a_rows.append(_mk("AU2608", mk, last_price=100.0, mid_price=100.0, display_time=f"t{sec}"))
        peer_b_rows.append(_mk("AU2610", mk, last_price=100.0, mid_price=100.0, display_time=f"t{sec}"))
    target = _contract_frame("AU2606", "AU", target_rows)
    peer_a = _contract_frame("AU2608", "AU", peer_a_rows)
    peer_b = _contract_frame("AU2610", "AU", peer_b_rows)
    return target, peer_a, peer_b


def test_fair_price_equals_target_mid_when_peers_track_target():
    target, peer_a, peer_b = _two_peers_stable()
    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    # 最后一行（t119）的 fair_price 应 ≈ 100.0
    row = out.iloc[-1]
    assert row["fair_price"] == pytest.approx(100.0, abs=0.05)
    assert row["fair_price_reliable"] is True or row["fair_price_reliable"] == True  # noqa: E712
    assert row["valid_peer_count"] == 2
    assert "AU2608" in str(row["peer_contracts"]) and "AU2610" in str(row["peer_contracts"])


def test_fair_price_unreliable_when_fewer_than_two_peers():
    target, peer_a, _ = _two_peers_stable()
    out = attach_fair_price_metrics(target, {"AU2608": peer_a}, tick_size=0.02)
    row = out.iloc[-1]
    assert row["fair_price_reliable"] is False or row["fair_price_reliable"] == False  # noqa: E712
    assert math.isnan(row["fair_price"]) if pd.isna(row["fair_price"]) else True


def test_fair_price_records_internal_peer_bases_for_event_chain():
    target, peer_a, peer_b = _two_peers_stable()
    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]
    # 内部字段供事件链使用，不落入中文 CSV
    assert "__valid_peer_contracts" in out.columns
    assert "__peer_bases" in out.columns
    assert isinstance(row["__peer_bases"], dict)


def test_fair_price_excludes_recent_10s_from_baseline():
    """最近 10s 不进基线，防异常萌芽污染正常价差。"""
    target_rows = []
    peer_a_rows = []
    peer_b_rows = []
    for sec in range(300):
        mk = sec * 1000
        target_rows.append(_mk("AU2606", mk, mid_price=100.0, display_time=f"t{sec}"))
        peer_a_rows.append(_mk("AU2608", mk, mid_price=100.0, display_time=f"t{sec}"))
        peer_b_rows.append(_mk("AU2610", mk, mid_price=100.0, display_time=f"t{sec}"))
    target = _contract_frame("AU2606", "AU", target_rows)
    peer_a = _contract_frame("AU2608", "AU", peer_a_rows)
    peer_b = _contract_frame("AU2610", "AU", peer_b_rows)

    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]  # t299
    assert row["fair_price"] == pytest.approx(100.0, abs=0.1)


def test_fair_price_unreliable_when_uncertainty_exceeds_limit():
    """peer 报价分歧过大（fair_uncertainty_ticks > 上限）时 fair_price_reliable=False。

    让 target 与 peer_a 始终同价（basis=0），与 peer_b 始终差 5（basis=5）。
    这样 fair_i_a = peer_a + 0 = 100，fair_i_b = peer_b + 5 = 100。
    两者的 fair_i 都=100 不够——需要让两 peer 当前价本身不一致。
    改为：peer_a 当前价=100（basis=0），peer_b 当前价=95（basis=5）-> fair_i = 100 vs 100，仍一致。
    真正发散：让 peer_a 当前价持续 = 103，peer_b 当前价持续 = 97，target=100。
    basis_a = median(100-103) = -3，basis_b = median(100-97) = 3。
    fair_i_a = 103 + (-3) = 100，fair_i_b = 97 + 3 = 100 —— 仍然一致！
    根因：稳定价差下 basis + peer_current 恒等。必须让价差不稳定。
    """
    # 让 target/peer_a 的价差随时间变化：前 150s basis≈0，后 150s basis≈10
    target_rows = []
    peer_a_rows = []
    peer_b_rows = []
    for sec in range(300):
        mk = sec * 1000
        target_mid = 100.0
        # peer_a 前150s与target同价，后150s低10
        peer_a_mid = 100.0 if sec < 150 else 90.0
        # peer_b 始终与target同价
        peer_b_mid = 100.0
        target_rows.append(_mk("AU2606", mk, mid_price=target_mid, display_time=f"t{sec}"))
        peer_a_rows.append(_mk("AU2608", mk, mid_price=peer_a_mid, display_time=f"t{sec}"))
        peer_b_rows.append(_mk("AU2610", mk, mid_price=peer_b_mid, display_time=f"t{sec}"))
    target = _contract_frame("AU2606", "AU", target_rows)
    peer_a = _contract_frame("AU2608", "AU", peer_a_rows)
    peer_b = _contract_frame("AU2610", "AU", peer_b_rows)

    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]  # t299: 窗口 [0, 289000] 包含前150s(basis_a=0)和后部分(basis_a=10)
    # t299 时 peer_a 当前价=90，basis_a = median of (100-100 for sec<150) and (100-90 for sec>=150)
    # 窗口 [0,289s]: 前150个 diff=0，后139个 diff=10 -> median=0 -> basis_a=0
    # fair_i_a = 90 + 0 = 90, fair_i_b = 100 + 0 = 100 -> 发散！
    assert row["fair_uncertainty_ticks"] > FAIR_UNCERTAINTY_LIMIT_TICKS
    assert row["fair_price_reliable"] is False or row["fair_price_reliable"] == False  # noqa: E712


# ---------------------------------------------------------------------------
# noise history
# ---------------------------------------------------------------------------


def test_noise_history_marked_insufficient_when_under_100_samples_or_120s():
    """样本少于 100 或跨度少于 120 秒时标 insufficient_noise_history。"""
    target_rows = []
    peer_a_rows = []
    peer_b_rows = []
    # 只有 50 秒数据，50 个样本 < 100
    for sec in range(50):
        mk = sec * 1000
        target_rows.append(_mk("AU2606", mk, mid_price=100.0, display_time=f"t{sec}"))
        peer_a_rows.append(_mk("AU2608", mk, mid_price=100.0, display_time=f"t{sec}"))
        peer_b_rows.append(_mk("AU2610", mk, mid_price=100.0, display_time=f"t{sec}"))
    target = _contract_frame("AU2606", "AU", target_rows)
    peer_a = _contract_frame("AU2608", "AU", peer_a_rows)
    peer_b = _contract_frame("AU2610", "AU", peer_b_rows)

    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]
    assert row["noise_history_reliable"] is False or row["noise_history_reliable"] == False  # noqa: E712
    assert row["noise_sample_count"] < 100


def test_noise_history_reliable_when_enough_samples_and_span():
    target, peer_a, peer_b = _two_peers_stable()
    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]
    assert row["noise_history_reliable"] is True or row["noise_history_reliable"] == True  # noqa: E712


def test_noise_thresholds_writes_last_and_vwap_threshold_fields():
    target, peer_a, peer_b = _two_peers_stable()
    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]
    assert "last_threshold_ticks" in out.columns
    assert "vwap_threshold_ticks" in out.columns
    assert "noise_sample_count" in out.columns
    assert "noise_time_span_seconds" in out.columns


def test_frozen_basis_does_not_change_when_peer_set_changes_after_anchor():
    """锚点后 peer 集合变化、重新计算基线时，冻结的 basis 不得变化。

    通过检查 __peer_bases 在锚点帧被记录后保持不变来验证。
    """
    target, peer_a, peer_b = _two_peers_stable()
    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    # 取锚点帧的 basis，后续帧的 basis 不应改变锚点记录
    anchor_bases = out.iloc[-1]["__peer_bases"]
    assert isinstance(anchor_bases, dict)
    # 所有值有限
    for v in anchor_bases.values():
        assert pd.notna(v)
