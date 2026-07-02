from __future__ import annotations

import pandas as pd
import pytest

from src.daily_screen.data_access import load_commodity_data
from src.daily_screen.schemas import REQUIRED_DAILY_COLUMNS, REQUIRED_META_COLUMNS


def _write_day(tmp_path, year: int, date: str, rows: list[dict]) -> None:
    year_dir = tmp_path / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(year_dir / f"{date}.parquet")


_UNSET = object()


def _row(code, date, close, *, pre_close=_UNSET, pre_settle=_UNSET, open_=_UNSET, high=_UNSET, low=_UNSET, vol=1000):
    # 用哨兵 _UNSET 区分"未传 → 默认 close"与"显式 None → 写 NaN(用于测 pre_close 回填)"
    return {
        "code": code,
        "date": date,
        "pre_close": close if pre_close is _UNSET else pre_close,
        "pre_settle": close if pre_settle is _UNSET else pre_settle,
        "open": close if open_ is _UNSET else open_,
        "high": close + 1 if high is _UNSET else high,
        "low": close - 1 if low is _UNSET else low,
        "close": close,
        "vol": vol,
    }


def test_filters_to_requested_symbols_and_drops_continuous(tmp_path):
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 100, vol=1000),
        _row("A.DCE", "20250105", 100, vol=5000),       # 连续合约,须滤除
        _row("SR2501.ZCE", "20250105", 6000, vol=2000),  # 非目标品种
    ])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    daily = result["daily_bar"]
    assert list(daily.columns) == REQUIRED_DAILY_COLUMNS
    assert set(daily["commodity"]) == {"A"}
    assert set(daily["contract"]) == {"A2501"}


def test_date_window_includes_buffer_and_caps_end(tmp_path):
    _write_day(tmp_path, 2024, "20241201", [_row("A2501.DCE", "20241201", 90)])   # 缓冲区(45天内)
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])  # 窗口内
    _write_day(tmp_path, 2025, "20250220", [_row("A2501.DCE", "20250220", 110)])  # 窗口外
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    dates = set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d"))
    assert "20241201" in dates   # 45 天缓冲包含
    assert "20250105" in dates
    assert "20250220" not in dates  # 超过 end_date 截断


def test_contract_meta_listed_date_from_pre_window_history(tmp_path):
    # A2501 真实首日在窗口之前(20241101),窗口从 20250101 起
    _write_day(tmp_path, 2024, "20241101", [_row("A2501.DCE", "20241101", 80)])
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    meta = result["contract_meta"]
    assert list(meta.columns) == REQUIRED_META_COLUMNS
    row = meta.loc[meta["contract"] == "A2501"].iloc[0]
    assert row["listed_date"] == pd.Timestamp("2024-11-01")  # 真实首日,落在窗口外


def test_contract_meta_last_trade_date_from_future_year(tmp_path):
    # A2501 在窗口(2025)出现,真实末日在未来年份(2026)
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])
    _write_day(tmp_path, 2026, "20260315", [_row("A2501.DCE", "20260315", 130)])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    meta = result["contract_meta"]
    row = meta.loc[meta["contract"] == "A2501"].iloc[0]
    assert row["last_trade_date"] == pd.Timestamp("2026-03-15")  # 取自未来年份,不是 end.year


def test_pre_close_falls_back_to_pre_settle(tmp_path):
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 104, pre_close=None, pre_settle=98),
    ])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    assert result["daily_bar"]["pre_close"].iloc[0] == 98


def test_pre_close_prev_close_no_cross_contract_pollution(tmp_path):
    # A2501 20250105 close=100;B2501 20250105 与 20250106,pre_close/pre_settle 都缺
    # 若全局 shift:B2501 20250106 会错拿 A2501 的 100
    # groupby 后:B2501 20250106 pre_close 应=B2501 20250105 的 close=200
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 100, pre_close=None, pre_settle=None),
        _row("B2501.DCE", "20250105", 200, pre_close=None, pre_settle=None),
    ])
    _write_day(tmp_path, 2025, "20250106", [
        _row("B2501.DCE", "20250106", 210, pre_close=None, pre_settle=None),
    ])
    result = load_commodity_data(["A", "B"], "20250101", "20250110", data_dir=tmp_path)
    daily = result["daily_bar"]
    b206 = daily.loc[(daily["contract"] == "B2501") & (daily["trade_date"] == pd.Timestamp("2025-01-06"))]
    assert b206["pre_close"].iloc[0] == 200  # 同合约前一日 close,不是 A 的 100


def test_merges_across_years(tmp_path):
    # expanded_start = 20250101 - 45d = 20241117;20241230 落在缓冲区内,应保留
    _write_day(tmp_path, 2024, "20241230", [_row("A2501.DCE", "20241230", 95)])
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    dates = set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d"))
    assert dates == {"20241230", "20250105"}


def test_misnamed_parquet_raises(tmp_path):
    (tmp_path / "2025").mkdir(parents=True)
    pd.DataFrame([{"code": "A2501.DCE", "date": "20250105"}]).to_parquet(tmp_path / "2025" / "bad_name.parquet")
    with pytest.raises(ValueError, match="非 YYYYMMDD"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)


def test_missing_data_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="data_dir 不存在"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path / "nope")


def test_data_dir_is_file_raises(tmp_path):
    file_path = tmp_path / "not_a_dir.parquet"
    file_path.write_bytes(b"x")
    with pytest.raises(NotADirectoryError, match="不是目录"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=file_path)


def test_invalid_year_dir_raises(tmp_path):
    (tmp_path / "2025").mkdir()
    (tmp_path / "abc").mkdir()
    with pytest.raises(ValueError, match="年份目录名非法"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)


def test_parses_lenient_date_formats(tmp_path):
    # spec 要求 date → to_datetime(无 format);锁死宽松解析,防止退化成只认 YYYYMMDD
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "2025-01-05", 100)])  # ISO 字符串
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    assert set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d")) == {"20250105"}


def test_corrupt_parquet_raises_with_context(tmp_path):
    # 文件名合法但内容非 parquet → 异常必须带 file_path / pass / file_date
    (tmp_path / "2025").mkdir(parents=True)
    (tmp_path / "2025" / "20250105.parquet").write_bytes(b"not a parquet file")
    with pytest.raises(RuntimeError, match=r"parquet 读取失败.*file_path=.*pass=[AB].*file_date=20250105"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)


def test_clips_rows_by_trade_date_not_just_filename(tmp_path):
    # 文件名 20250105 在窗口内,但文件里塞了一行 date=20250220(窗口外)→ 必须按 trade_date 裁掉
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 100),
        _row("A2501.DCE", "20250220", 999),  # 与文件名不一致的脏行
    ])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    dates = set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d"))
    assert dates == {"20250105"}  # 20250220 被裁掉


def test_pass_b_failure_attaches_symbols(tmp_path):
    # 文件只有 code/date 列:Pass A 读 [code,date] 成功,Pass B 读 OHLC 列失败
    # → 异常须带全四字段:file_path / pass=B / file_date / symbols
    (tmp_path / "2025").mkdir(parents=True)
    pd.DataFrame([{"code": "A2501.DCE", "date": "20250105"}]).to_parquet(tmp_path / "2025" / "20250105.parquet")
    with pytest.raises(
        RuntimeError,
        match=r"parquet 读取失败.*file_path=.*pass=B.*file_date=20250105.*symbols=",
    ):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)