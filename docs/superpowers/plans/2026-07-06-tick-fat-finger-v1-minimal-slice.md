# Tick 乌龙指 v1 最小回测切片 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已知乌龙指日 AU2606 2026-05-20 上，跑通"读 tick → 双版 expected_price → 事件检测 → 单笔回测"，产出可审计的 2 个 CSV，人工核对 11:08:09 那根孤立向下尖刺信号合理。

**Architecture:** 新包 `src/tick_stats/`（4 模块）+ 根目录 CLI `run_tick_replay.py`，沿用现有 `src/daily_screen/` 的 `from src.<pkg>.<mod> import ...` 导入约定。loader 直接读 `data/tick2026/{YYYYMM}/{YYYYMMDD}.zip`，过滤到日盘连续交易时段，按品种分批跑完整链路。

**Tech Stack:** Python 3 + pandas + numpy（已在 requirements.txt）+ stdlib（zipfile / re / datetime）。测试 pytest，沿用 `tests/` 现有 `tmp_path` 合成 fixture 惯例。

## Global Constraints

- **F1（修大 spec §5.1 bug）**：连续合约识别**先看文件名**——含 `主力连续|当月连续|下月连续|当季连续|下季连续|隔季连续` → `parse_status=continuous_alias` 弃掉；否则才用 InstrumentID 解析。实测主力连续文件 InstrumentID 与真合约相同。
- **夜盘处理**：loader 在全量行上派生 session_state 后，**过滤 `is_tradable_session==true`**（日盘 09:00–10:15 / 10:30–11:30 / 13:30–15:00），丢弃夜盘 21:00–02:30。日盘内按 `(timestamp, snapshot_seq)` 排序。
- **delta_volume**：首条 / 相邻快照 timestamp 差 > 60s / `Volume` 负增量一律 NaN（不参与成交判断）。
- **偏离同口径**：`down_deviation_ticks = (expected_price - target_mid)/tick_size`，不用 mid-vs-LastPrice。
- **时间常数（待标定）**：`lookback_seconds=3`、`max_reference_age_seconds=3`。
- **回测口径（M4 收敛）**：offset `{3,8,21}` × `conservative_fill` × exit `{bid_exit, timeout_exit}`；`target_profit_ticks=5`、`timeout_seconds=60`；`quote_latency_snapshots=1`、`order_latency_snapshots=0`、`reprice_interval_snapshots=2`。
- **导入风格**：`from src.tick_stats.<mod> import <fn>`。
- 每个任务结束 `git commit`。分支 `v1.0.0`。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/tick_stats/__init__.py` | 空包标记 |
| `src/tick_stats/tick_io.py` | 合约解析、CSV 读取、session/delta_volume/mid/spread/tick_size 派生、品种聚合 loader |
| `src/tick_stats/expected_price.py` | asof 对齐工具 + simple/full 双版 expected_price + 偏离 + full_blocked_reason |
| `src/tick_stats/replay.py` | 事件检测/合并/回归标签 + 单笔回测 |
| `run_tick_replay.py` | 根目录 CLI 入口 |
| `tests/test_tick_io.py` | tick_io 测试 |
| `tests/test_expected_price.py` | expected_price 测试 |
| `tests/test_replay.py` | replay 测试 |
| `tests/test_run_tick_replay.py` | 端到端冒烟 |

---

### Task 1: 合约解析 parse_contract（F1 文件名优先）

**Files:**
- Create: `src/tick_stats/__init__.py`
- Create: `src/tick_stats/tick_io.py`
- Test: `tests/test_tick_io.py`

**Interfaces:**
- Produces: `parse_contract(filename: str, instrument_id: str, trading_day: int) -> ContractInfo`，`ContractInfo` 字段 `commodity`、`contract_month`（int YYYYMM）、`parse_status`（`ok|continuous_alias|unknown_suffix|unknown_format`）、`suffix`。

- [ ] **Step 1: 建包 + 写失败测试**

`src/tick_stats/__init__.py`：空文件。

`tests/test_tick_io.py`：
```python
from src.tick_stats.tick_io import parse_contract


def test_continuous_alias_detected_by_filename_not_instrument_id():
    # F1 回归：主力连续文件 InstrumentID 与真合约相同，必须靠文件名识别
    info = parse_contract("AP主力连续_20260105.csv", "AP605", 20260105)
    assert info.parse_status == "continuous_alias"


def test_parse_ok_au2606():
    info = parse_contract("au2606_20260520.csv", "au2606", 20260520)
    assert info.commodity == "AU"
    assert info.contract_month == 202606
    assert info.parse_status == "ok"
    assert info.suffix is None


def test_parse_three_digit_month_ap601():
    info = parse_contract("AP601_20260105.csv", "AP601", 20260105)
    assert info.commodity == "AP"
    assert info.contract_month == 202601
    assert info.parse_status == "ok"


def test_parse_unknown_suffix():
    info = parse_contract("pp2605F_20260520.csv", "pp2605F", 20260520)
    assert info.parse_status == "unknown_suffix"
    assert info.suffix == "F"


def test_parse_unknown_format():
    info = parse_contract("xyz.csv", "!!", 20260520)
    assert info.parse_status == "unknown_format"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py -v`
Expected: FAIL（`ModuleNotFoundError: src.tick_stats.tick_io`）

- [ ] **Step 3: 实现 parse_contract**

`src/tick_stats/tick_io.py`：
```python
from __future__ import annotations

import re
from dataclasses import dataclass

CONTINUOUS_KEYWORDS = ("主力连续", "当月连续", "下月连续", "当季连续", "下季连续", "隔季连续")
_INSTRUMENT_RE = re.compile(r"^([A-Za-z]+)(\d{3,4})([A-Za-z]*)$")


@dataclass
class ContractInfo:
    commodity: str | None
    contract_month: int | None
    parse_status: str
    suffix: str | None


def parse_contract(filename: str, instrument_id: str, trading_day: int) -> ContractInfo:
    stem = filename.rsplit(".", 1)[0]
    name_part = stem.rsplit("_", 1)[0]
    for kw in CONTINUOUS_KEYWORDS:
        if kw in name_part:
            return ContractInfo(None, None, "continuous_alias", None)
    return _parse_instrument_id(instrument_id, trading_day)


def _parse_instrument_id(instrument_id: str, trading_day: int) -> ContractInfo:
    m = _INSTRUMENT_RE.match(instrument_id)
    if not m:
        return ContractInfo(None, None, "unknown_format", None)
    commodity = m.group(1).upper()
    digits = m.group(2)
    suffix = m.group(3) or None
    month = _resolve_month(digits, trading_day)
    if month is None:
        return ContractInfo(commodity, None, "unknown_format", suffix)
    status = "ok" if not suffix else "unknown_suffix"
    return ContractInfo(commodity, month, status, suffix)


def _resolve_month(digits: str, trading_day: int) -> int | None:
    # ponytail: 数据为 2020s，4 位取 20xx；3 位用 trading_day 年份 + 末 2 位月份（v1 近似，AU 用不到）
    if len(digits) == 4:
        year = 2000 + int(digits[:2])
        month = int(digits[2:])
        return year * 100 + month if 1 <= month <= 12 else None
    if len(digits) == 3:
        td_year = int(str(trading_day)[:4])
        month = int(digits[1:])
        return td_year * 100 + month if 1 <= month <= 12 else None
    return None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/__init__.py src/tick_stats/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick_stats): 合约解析 parse_contract（文件名优先 F1）"
```

---

### Task 2: session_state + 单 CSV 读取 + 日盘过滤 + mid/spread

**Files:**
- Modify: `src/tick_stats/tick_io.py`
- Test: `tests/test_tick_io.py`

**Interfaces:**
- Produces: `session_state(update_time_str) -> tuple[str, bool]`、`load_contract_csv(raw: pd.DataFrame, contract: str, trading_day: int) -> pd.DataFrame`。返回 DataFrame 含列：`contract, trading_day, timestamp, snapshot_seq, session_state, is_tradable_session, LastPrice, Volume, Turnover, BidPrice1, AskPrice1, UpperLimitPrice, LowerLimitPrice, mid_price, spread`（已过滤到日盘、已排序）。

- [ ] **Step 1: 追加失败测试**

追加到 `tests/test_tick_io.py`：
```python
import pandas as pd

from src.tick_stats.tick_io import session_state, load_contract_csv


def test_session_state_boundaries():
    assert session_state("08:59:59") == ("pre_open_snapshot", False)
    assert session_state("09:00:00") == ("continuous_trading", True)
    assert session_state("10:14:59") == ("continuous_trading", True)
    assert session_state("10:15:00") == ("intermission", False)
    assert session_state("10:30:00") == ("continuous_trading", True)
    assert session_state("11:30:00") == ("lunch", False)
    assert session_state("13:30:00") == ("continuous_trading", True)
    assert session_state("14:59:59") == ("continuous_trading", True)
    assert session_state("15:00:00") == ("post_close_snapshot", False)


def _raw_row(t, ms, last, vol, bid, ask):
    return {
        "TradingDay": 20260520, "InstrumentID": "au2606", "UpdateTime": t,
        "UpdateMillisec": ms, "LastPrice": last, "Volume": vol, "BidPrice1": bid,
        "BidVolume1": 1, "AskPrice1": ask, "AskVolume1": 1, "AveragePrice": last,
        "Turnover": last * vol, "OpenInterest": 100, "UpperLimitPrice": 1170.7,
        "LowerLimitPrice": 830.48,
    }


def test_load_contract_csv_filters_to_day_session_and_derives_mid():
    raw = pd.DataFrame([
        _raw_row("08:59:59", 0, 990.0, 10, 989.9, 990.1),   # pre_open, dropped
        _raw_row("09:00:00", 0, 991.0, 12, 990.9, 991.1),   # kept
        _raw_row("09:00:01", 0, 992.0, 14, 991.9, 992.1),   # kept
        _raw_row("21:00:00", 0, 995.0, 20, 994.9, 995.1),   # night, dropped
    ])
    df = load_contract_csv(raw, "au2606", 20260520)
    assert len(df) == 2
    assert df["session_state"].eq("continuous_trading").all()
    assert df["snapshot_seq"].tolist() == [0, 1]
    assert df["mid_price"].iloc[0] == 991.0
    assert df["spread"].iloc[0] == pytest.approx(0.2)
```

需要在文件顶部 `import pytest`。

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py::test_session_state_boundaries -v`
Expected: FAIL（`session_state` 未定义）

- [ ] **Step 3: 实现 session_state 与 load_contract_csv**

追加到 `src/tick_stats/tick_io.py`：
```python
import numpy as np
import pandas as pd

_SESSION_TABLE = [
    (0, 9 * 60, "pre_open_snapshot", False),
    (9 * 60, 10 * 60 + 15, "continuous_trading", True),
    (10 * 60 + 15, 10 * 60 + 30, "intermission", False),
    (10 * 60 + 30, 11 * 60 + 30, "continuous_trading", True),
    (11 * 60 + 30, 13 * 60 + 30, "lunch", False),
    (13 * 60 + 30, 15 * 60, "continuous_trading", True),
    (15 * 60, 24 * 60, "post_close_snapshot", False),
]


def session_state(update_time_str: str) -> tuple[str, bool]:
    parts = str(update_time_str).split(":")
    m = int(parts[0]) * 60 + int(parts[1])
    for start, end, state, tradable in _SESSION_TABLE:
        if start <= m < end:
            return state, tradable
    return "unknown", False


def load_contract_csv(raw: pd.DataFrame, contract: str, trading_day: int) -> pd.DataFrame:
    df = raw.copy()
    df["contract"] = contract.upper()
    df["trading_day"] = trading_day
    ts_str = df["TradingDay"].astype(str) + " " + df["UpdateTime"].astype(str) + "." + df["UpdateMillisec"].astype(int).astype(str).str.zfill(3)
    df["timestamp"] = pd.to_datetime(ts_str, format="%Y%m%d %H:%M:%S.%f", errors="coerce")
    df["snapshot_seq"] = np.arange(len(df))
    states = df["UpdateTime"].map(session_state)
    df["session_state"] = [s for s, _ in states]
    df["is_tradable_session"] = [t for _, t in states]
    df = df[df["is_tradable_session"]].copy()
    df = df.sort_values(["timestamp", "snapshot_seq"]).reset_index(drop=True)
    df["snapshot_seq"] = np.arange(len(df))
    df["mid_price"] = (df["BidPrice1"] + df["AskPrice1"]) / 2
    df["spread"] = df["AskPrice1"] - df["BidPrice1"]
    bad = (df["BidPrice1"] <= 0) | (df["AskPrice1"] <= 0)
    df.loc[bad, ["mid_price", "spread"]] = np.nan
    return df
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py -v`
Expected: all passed（含 session/mid 新测）

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick_stats): session_state + 单 CSV 读取（日盘过滤 + mid/spread）"
```

---

### Task 3: delta_volume / delta_turnover 派生（gap > 60s → unknown）

**Files:**
- Modify: `src/tick_stats/tick_io.py`
- Test: `tests/test_tick_io.py`

**Interfaces:**
- Produces: `derive_delta_volume(df: pd.DataFrame) -> pd.DataFrame`，新增列 `delta_volume`、`delta_turnover`（NaN 表示 unknown/不参与）。
- Consumes: Task 2 的 `load_contract_csv` 输出。

- [ ] **Step 1: 追加失败测试**

```python
from src.tick_stats.tick_io import derive_delta_volume


def test_delta_volume_first_row_and_long_gap_unknown():
    raw = pd.DataFrame([
        _raw_row("09:00:00", 0, 990.0, 10, 989.9, 990.1),
        _raw_row("09:00:01", 0, 991.0, 14, 990.9, 991.1),    # delta=4
        _raw_row("11:29:59", 0, 991.0, 100, 990.9, 991.1),   # delta=86
        _raw_row("13:30:00", 0, 991.0, 105, 990.9, 991.1),   # gap>60s → unknown
        _raw_row("13:30:01", 0, 991.0, 110, 990.9, 991.1),   # delta=5
    ])
    df = load_contract_csv(raw, "au2606", 20260520)
    df = derive_delta_volume(df)
    assert pd.isna(df["delta_volume"].iloc[0])               # first row
    assert df["delta_volume"].iloc[1] == 4
    assert df["delta_volume"].iloc[2] == 86
    assert pd.isna(df["delta_volume"].iloc[3])               # lunch gap
    assert df["delta_volume"].iloc[4] == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py::test_delta_volume_first_row_and_long_gap_unknown -v`
Expected: FAIL（`derive_delta_volume` 未定义）

- [ ] **Step 3: 实现 derive_delta_volume**

追加到 `src/tick_stats/tick_io.py`：
```python
def derive_delta_volume(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    gap = df["timestamp"].diff().dt.total_seconds()
    unknown = df["session_state"].ne(df["session_state"].shift(1)) | (gap > 60)
    dv = df["Volume"].diff()
    dv[unknown] = np.nan
    dv[dv < 0] = np.nan
    df["delta_volume"] = dv
    dt_ = df["Turnover"].diff()
    dt_[unknown] = np.nan
    dt_[dt_ < 0] = np.nan
    df["delta_turnover"] = dt_
    return df
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick_stats): delta_volume 派生（首条/切 session/>60s gap → unknown）"
```

---

### Task 4: tick_size 推导 + 品种聚合 loader load_day_snapshots

**Files:**
- Modify: `src/tick_stats/tick_io.py`
- Test: `tests/test_tick_io.py`

**Interfaces:**
- Produces: `infer_tick_size(prices: pd.Series) -> tuple[float|None, str]`、`load_day_snapshots(date: int, symbols: list[str]|None=None, data_dir: str="data/tick2026") -> dict[str, pd.DataFrame]`。每个品种 DataFrame 含 Task 2/3 的列 + `tick_size, tick_size_source, spread_ticks, contract_month`。
- Consumes: Task 1 `parse_contract`、Task 2 `load_contract_csv`、Task 3 `derive_delta_volume`。

- [ ] **Step 1: 追加失败测试**

```python
from src.tick_stats.tick_io import infer_tick_size, load_day_snapshots


def test_infer_tick_size_au_like():
    # AU tick=0.02
    prices = pd.Series([990.00, 990.02, 990.04, 990.02, 990.04, 990.06,
                        990.04, 990.06, 990.08, 990.06, 990.08])
    tick, src = infer_tick_size(prices)
    assert tick == 0.02
    assert src == "inferred"


def test_infer_tick_size_too_few_returns_unknown():
    assert infer_tick_size(pd.Series([100.0, 101.0])) == (None, "unknown")


def test_load_day_snapshots_drops_continuous_and_groups(tmp_path):
    # 造一个 20260105 目录：1 个真合约 + 1 个主力连续（同 InstrumentID）
    day_dir = tmp_path / "202601" / "20260105"
    day_dir.mkdir(parents=True)
    real = pd.DataFrame([
        _raw_row("09:00:00", 0, 990.0, 10, 989.9, 990.1),
        _raw_row("09:00:01", 0, 991.0, 14, 990.9, 991.1),
    ])
    real.to_csv(day_dir / "au2606_20260105.csv", index=False)
    real.to_csv(day_dir / "au主力连续_20260105.csv", index=False)  # F1: 必须被丢
    snaps = load_day_snapshots(20260105, data_dir=str(tmp_path))
    assert "AU" in snaps
    assert snaps["AU"]["contract"].eq("AU2606").all()
    assert len(snaps["AU"]) == 2                                  # 不翻倍
    assert snaps["AU"]["tick_size"].iloc[0] == 0.02
```

`_raw_row` 已在 Task 2 定义。

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py::test_infer_tick_size_au_like -v`
Expected: FAIL（`infer_tick_size` 未定义）

- [ ] **Step 3: 实现 infer_tick_size 与 load_day_snapshots**

追加到 `src/tick_stats/tick_io.py`：
```python
import zipfile
from collections import Counter
from pathlib import Path


def infer_tick_size(prices: pd.Series) -> tuple[float | None, str]:
    diffs: list[float] = []
    prev = None
    for p in prices:
        p = float(p)
        if p > 0:
            if prev is not None:
                d = round(p - prev, 6)
                if d > 0:
                    diffs.append(d)
            prev = p
    if len(diffs) < 5:
        return None, "unknown"
    counts = Counter(diffs)
    candidates = [d for d, c in counts.items() if c >= 5]
    if not candidates:
        return None, "unknown"
    return min(candidates), "inferred"


def _iter_contract_dfs(date: int, data_dir: str):
    ym = str(date)[:6]
    root = Path(data_dir) / ym
    extracted = root / str(date)
    zpath = root / f"{date}.zip"
    if extracted.is_dir():
        for p in sorted(extracted.glob("*.csv")):
            yield p.name, pd.read_csv(p)
    elif zpath.is_file():
        with zipfile.ZipFile(zpath) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith(".csv"):
                    with zf.open(name) as f:
                        yield name, pd.read_csv(f)


def load_day_snapshots(date: int, symbols: list[str] | None = None, data_dir: str = "data/tick2026") -> dict[str, pd.DataFrame]:
    syms = set(symbols) if symbols else None
    by_commodity: dict[str, list[tuple[str, pd.DataFrame, ContractInfo]]] = {}
    for filename, raw in _iter_contract_dfs(date, data_dir):
        if raw.empty:
            continue
        instrument_id = str(raw["InstrumentID"].iloc[0])
        info = parse_contract(filename, instrument_id, date)
        if info.parse_status != "ok":
            continue
        if syms and info.commodity not in syms:
            continue
        contract = instrument_id.upper()
        by_commodity.setdefault(info.commodity, []).append((contract, raw, info))

    result: dict[str, pd.DataFrame] = {}
    for commodity, items in by_commodity.items():
        loaded = []
        for contract, raw, info in items:
            df = derive_delta_volume(load_contract_csv(raw, contract, date))
            if not df.empty:
                loaded.append((contract, df, info))
        if not loaded:
            continue
        # tick_size 从主力（当日最大累计 Volume）推导
        main = max(loaded, key=lambda x: float(x[1]["Volume"].iloc[-1]))
        tick, src = infer_tick_size(main[1]["LastPrice"])
        frames = []
        for contract, df, info in loaded:
            df = df.copy()
            df["tick_size"] = tick
            df["tick_size_source"] = src
            df["spread_ticks"] = df["spread"] / tick if tick else np.nan
            df["contract_month"] = info.contract_month
            frames.append(df)
        result[commodity] = pd.concat(frames, ignore_index=True)
    return result
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_tick_io.py -v`
Expected: all passed（含 F1 不翻倍回归）

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick_stats): tick_size 推导 + load_day_snapshots 品种聚合 loader"
```

---

### Task 5: asof 对齐工具（同秒 tiebreaker + age）

**Files:**
- Create: `src/tick_stats/expected_price.py`
- Test: `tests/test_expected_price.py`

**Interfaces:**
- Produces: `asof_align(query_times: pd.Series, source_df: pd.DataFrame, value_col: str="mid_price") -> tuple[pd.Series, pd.Series]`。返回 `(对齐值, 对齐到的源 timestamp)`，按 `query_times` 索引对齐；源无 <= query 的行时返回 `(NaN, NaT)`。同 timestamp 多行取 `snapshot_seq` 最大者（最新）。

- [ ] **Step 1: 写失败测试**

`tests/test_expected_price.py`：
```python
import numpy as np
import pandas as pd

from src.tick_stats.expected_price import asof_align


def test_asof_basic_backward():
    src = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-05-20 09:00:00", "2026-05-20 09:00:02"]),
        "snapshot_seq": [0, 1],
        "mid_price": [100.0, 101.0],
    })
    q = pd.Series(pd.to_datetime(["2026-05-20 09:00:03"]), index=[10])
    vals, ts = asof_align(q, src)
    assert vals.iloc[0] == 101.0


def test_asof_same_second_tiebreaker_picks_higher_seq():
    # N2: 同秒多条快照，取 snapshot_seq 最大者
    src = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-05-20 11:08:09", "2026-05-20 11:08:09", "2026-05-20 11:08:09"]),
        "snapshot_seq": [0, 1, 2],
        "mid_price": [100.0, 101.0, 102.0],
    })
    q = pd.Series(pd.to_datetime(["2026-05-20 11:08:09"]), index=[0])
    vals, _ = asof_align(q, src)
    assert vals.iloc[0] == 102.0


def test_asof_no_prior_returns_nat():
    src = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-05-20 09:00:05"]),
        "snapshot_seq": [0],
        "mid_price": [100.0],
    })
    q = pd.Series(pd.to_datetime(["2026-05-20 09:00:00"]), index=[0])
    vals, ts = asof_align(q, src)
    assert pd.isna(vals.iloc[0])
    assert pd.isna(ts.iloc[0])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_expected_price.py -v`
Expected: FAIL（模块未定义）

- [ ] **Step 3: 实现 asof_align**

`src/tick_stats/expected_price.py`：
```python
from __future__ import annotations

import numpy as np
import pandas as pd


def asof_align(query_times: pd.Series, source_df: pd.DataFrame, value_col: str = "mid_price") -> tuple[pd.Series, pd.Series]:
    if query_times.empty:
        return pd.Series(dtype=float), pd.Series(dtype="datetime64[ns]")
    left = pd.DataFrame({"_q": query_times.values}, index=query_times.index)
    cols = ["timestamp", "snapshot_seq", value_col]
    right = source_df[cols].dropna(subset=[value_col]).sort_values(["timestamp", "snapshot_seq"]).reset_index(drop=True)
    if right.empty:
        return (
            pd.Series([np.nan] * len(left), index=left.index),
            pd.Series([pd.NaT] * len(left), index=left.index, dtype="datetime64[ns]"),
        )
    merged = pd.merge_asof(left, right, left_on="_q", right_on="timestamp", direction="backward")
    return merged[value_col], merged["timestamp"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_expected_price.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/expected_price.py tests/test_expected_price.py
git commit -m "feat(tick_stats): asof_align（同秒 tiebreaker + 返回源时刻算 age）"
```

---

### Task 6: expected_price simple + full + down_deviation + full_blocked_reason

**Files:**
- Modify: `src/tick_stats/expected_price.py`
- Test: `tests/test_expected_price.py`

**Interfaces:**
- Produces: `compute_expected_prices(commodity_df: pd.DataFrame) -> pd.DataFrame`。输入 Task 4 单品种 DataFrame；输出新增列 `expected_price_simple, down_deviation_simple_ticks, down_deviation_simple_bps, expected_price_full, down_deviation_full_ticks, down_deviation_full_bps, full_blocked_reason`。simple/full 公式相同（`target_mid_asof(t-lookback) * exp(median peer return))`），唯一差别：full 在三类 age（`target_baseline/peer_current/peer_lookback`）任一 > `MAX_REFERENCE_AGE_SECONDS` 时置 NaN 并记 `full_blocked_reason`。
- Consumes: Task 5 `asof_align`。

- [ ] **Step 1: 追加失败测试**

```python
from src.tick_stats.expected_price import compute_expected_prices


def _snap(contract, ts, mid, last, vol, seq, bid=None, ask=None):
    bid = mid if bid is None else bid
    ask = mid if ask is None else ask
    return {
        "contract": contract, "timestamp": pd.Timestamp(ts), "snapshot_seq": seq,
        "mid_price": mid, "LastPrice": last, "Volume": vol, "Turnover": last * vol,
        "BidPrice1": bid, "AskPrice1": ask, "spread": ask - bid, "spread_ticks": (ask - bid) / 0.02,
        "session_state": "continuous_trading", "is_tradable_session": True,
        "delta_volume": vol, "tick_size": 0.02, "tick_size_source": "inferred",
        "UpperLimitPrice": 1170.7, "LowerLimitPrice": 830.48, "contract_month": 202606,
    }


def test_isolated_drop_both_versions_flag_it():
    # target T 在 09:00:03 孤立砸低；peer P 保持
    rows = [
        _snap("T", "2026-05-20 09:00:00", 100.0, 100.0, 10, 0),
        _snap("P", "2026-05-20 09:00:00", 100.0, 100.0, 10, 0),
        _snap("T", "2026-05-20 09:00:03", 100.0, 95.0, 20, 1),   # mid 仍 100 但 LastPrice 砸到 95
        _snap("P", "2026-05-20 09:00:03", 100.0, 100.0, 20, 1),
    ]
    # 注意：偏离用 target_mid，所以让 T 的 mid 也被打低
    rows[2] = _snap("T", "2026-05-20 09:00:03", 95.0, 95.0, 20, 1)
    df = compute_expected_prices(pd.DataFrame(rows))
    t_row = df[(df.contract == "T") & (df.snapshot_seq == 1)].iloc[0]
    assert t_row["down_deviation_simple_ticks"] > 0
    assert t_row["down_deviation_full_ticks"] > 0
    assert t_row["full_blocked_reason"] == "not_blocked"


def test_synchronized_drop_both_versions_small():
    # T 与 P 同步下跌 → peer return 解释了下跌 → 偏离小
    rows = [
        _snap("T", "2026-05-20 09:00:00", 100.0, 100.0, 10, 0),
        _snap("P", "2026-05-20 09:00:00", 100.0, 100.0, 10, 0),
        _snap("Q", "2026-05-20 09:00:00", 100.0, 100.0, 10, 0),
        _snap("T", "2026-05-20 09:00:03", 95.0, 95.0, 20, 1),
        _snap("P", "2026-05-20 09:00:03", 95.0, 95.0, 20, 1),
        _snap("Q", "2026-05-20 09:00:03", 95.0, 95.0, 20, 1),
    ]
    df = compute_expected_prices(pd.DataFrame(rows))
    t_row = df[(df.contract == "T") & (df.snapshot_seq == 1)].iloc[0]
    assert abs(t_row["down_deviation_simple_ticks"]) < 5        # 同步下跌，偏离接近 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_expected_price.py::test_isolated_drop_both_versions_flag_it -v`
Expected: FAIL（`compute_expected_prices` 未定义）

- [ ] **Step 3: 实现 compute_expected_prices**

追加到 `src/tick_stats/expected_price.py`：
```python
LOOKBACK_SECONDS = 3
MAX_REFERENCE_AGE_SECONDS = 3
REFERENCE_LIMIT = 5


def compute_expected_prices(commodity_df: pd.DataFrame) -> pd.DataFrame:
    df = commodity_df.copy().reset_index(drop=True)
    out_cols = ["expected_price_simple", "down_deviation_simple_ticks", "down_deviation_simple_bps",
                "expected_price_full", "down_deviation_full_ticks", "down_deviation_full_bps"]
    for c in out_cols:
        df[c] = np.nan
    df["full_blocked_reason"] = "not_computed"
    if df.empty:
        return df
    tick = float(df["tick_size"].iloc[0])
    if not tick or np.isnan(tick):
        return df

    day_vol = df.groupby("contract")["Volume"].max()
    ranked = day_vol.sort_values(ascending=False).index.tolist()

    for target in ranked:
        tmask = df["contract"] == target
        tidx = df.index[tmask]
        if len(tidx) == 0:
            continue
        peers = [c for c in ranked if c != target][:REFERENCE_LIMIT]
        if len(peers) < 2:
            continue
        qt_now = df.loc[tidx, "timestamp"].reset_index(drop=True)
        qt_prev = (qt_now - pd.Timedelta(seconds=LOOKBACK_SECONDS))

        # 每个目标行预存 peer 对齐结果
        now_mid, now_age = {p: {} for p in peers}, {p: {} for p in peers}
        prev_mid, prev_age = {p: {} for p in peers}, {p: {} for p in peers}
        for p in peers:
            pdf = df[df["contract"] == p]
            nm, nt = asof_align(qt_now, pdf)
            pm, pt = asof_align(qt_prev, pdf)
            for i, idx in enumerate(tidx):
                now_mid[p][idx] = nm.iloc[i]
                now_age[p][idx] = (qt_now.iloc[i] - nt.iloc[i]).total_seconds() if pd.notna(nt.iloc[i]) else np.inf
                prev_mid[p][idx] = pm.iloc[i]
                prev_age[p][idx] = (qt_prev.iloc[i] - pt.iloc[i]).total_seconds() if pd.notna(pt.iloc[i]) else np.inf
        tbm, tbt = asof_align(qt_prev, df[df["contract"] == target])
        for i, idx in enumerate(tidx):
            parts = []
            for p in peers:
                nm = now_mid[p][idx]
                pm = prev_mid[p][idx]
                if pd.notna(nm) and pd.notna(pm) and pm > 0:
                    parts.append((nm, pm, now_age[p][idx], prev_age[p][idx]))
            if len(parts) < 2:
                continue
            base = tbm.iloc[i]
            tgt_mid = df.loc[idx, "mid_price"]
            if pd.isna(base) or base <= 0 or pd.isna(tgt_mid):
                continue
            rets = [np.log(nm / pm) for nm, pm, _, _ in parts]
            exp = base * np.exp(float(np.median(rets)))
            dev_t = (exp - tgt_mid) / tick
            dev_b = (exp - tgt_mid) / exp * 10000
            df.loc[idx, "expected_price_simple"] = exp
            df.loc[idx, "down_deviation_simple_ticks"] = dev_t
            df.loc[idx, "down_deviation_simple_bps"] = dev_b
            now_age_max = max(a[2] for a in parts)
            prev_age_max = max(a[3] for a in parts)
            tbase_age = (qt_now.iloc[i] - tbt.iloc[i]).total_seconds() if pd.notna(tbt.iloc[i]) else np.inf
            reason = "not_blocked"
            if tbase_age > MAX_REFERENCE_AGE_SECONDS:
                reason = "age_target_baseline"
            elif now_age_max > MAX_REFERENCE_AGE_SECONDS:
                reason = "age_peer_current"
            elif prev_age_max > MAX_REFERENCE_AGE_SECONDS:
                reason = "age_peer_lookback"
            df.loc[idx, "full_blocked_reason"] = reason
            if reason == "not_blocked":
                df.loc[idx, "expected_price_full"] = exp
                df.loc[idx, "down_deviation_full_ticks"] = dev_t
                df.loc[idx, "down_deviation_full_bps"] = dev_b
    return df
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_expected_price.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/expected_price.py tests/test_expected_price.py
git commit -m "feat(tick_stats): compute_expected_prices（simple/full 双版 + age 门 + full_blocked_reason）"
```

---

### Task 7: 事件检测 + 合并 + 回归标签（窗口 session 截断）

**Files:**
- Create: `src/tick_stats/replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Produces: `detect_events(commodity_df: pd.DataFrame) -> pd.DataFrame`（每行一段事件，列见 spec §5.1）。
- Consumes: Task 6 的输出列（`down_deviation_full_*`、`full_blocked_reason`、`expected_price_full_at_event` 等）。

- [ ] **Step 1: 写失败测试**

`tests/test_replay.py`：
```python
import pandas as pd

from src.tick_stats.replay import detect_events


def test_detect_events_picks_deep_iso_drop():
    # 构造：T 在 11:08:09 深度孤立下跌（已带 expected_price 列），peer 不动
    base = {
        "contract": "T", "session_state": "continuous_trading", "is_tradable_session": True,
        "tick_size": 0.02, "spread_ticks": 1.0, "delta_volume": 5.0,
        "UpperLimitPrice": 1170.7, "LowerLimitPrice": 830.48, "Volume": 100,
        "BidPrice1": 100.0, "AskPrice1": 100.02,
    }
    rows = [
        dict(base, timestamp=pd.Timestamp("2026-05-20 11:08:09"), snapshot_seq=0,
             LastPrice=95.0, mid_price=95.0,
             expected_price_simple=100.0, expected_price_full=100.0,
             down_deviation_simple_ticks=250.0, down_deviation_simple_bps=500.0,
             down_deviation_full_ticks=250.0, down_deviation_full_bps=500.0,
             full_blocked_reason="not_blocked"),
    ]
    df = pd.DataFrame(rows)
    ev = detect_events(df)
    assert len(ev) == 1
    assert ev["event_depth_ticks"].iloc[0] == 250.0
    assert ev["recovery_label"].iloc[0] != "truncated"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_replay.py -v`
Expected: FAIL（模块未定义）

- [ ] **Step 3: 实现 detect_events**

`src/tick_stats/replay.py`：
```python
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_DEVIATION_BPS = 30
MIN_DEVIATION_TICKS_FLOOR = 3
MAX_SPREAD_TICKS = 3
LIMIT_BUFFER_TICKS = 2
MERGE_WINDOW_SECONDS = 10
RECOVERY_WINDOWS = [30, 60, 180, 300]


def detect_events(commodity_df: pd.DataFrame) -> pd.DataFrame:
    df = commodity_df.reset_index(drop=True)
    out_cols = ["expected_price_simple", "down_deviation_simple_ticks", "down_deviation_simple_bps",
                "expected_price_full", "down_deviation_full_ticks", "down_deviation_full_bps",
                "full_blocked_reason"]
    for c in out_cols:
        df[c] = df.get(c, np.nan)
    need = df["is_tradable_session"] & (df["delta_volume"] > 0) & df["down_deviation_full_bps"].notna()
    cand = df[need].copy()
    if cand.empty:
        return pd.DataFrame(columns=_event_columns())
    tick = float(df["tick_size"].iloc[0])
    upper_ok = cand["LastPrice"] < cand["UpperLimitPrice"] - LIMIT_BUFFER_TICKS * tick
    lower_ok = cand["LastPrice"] > cand["LowerLimitPrice"] + LIMIT_BUFFER_TICKS * tick
    cand = cand[
        (cand["down_deviation_full_bps"] >= MIN_DEVIATION_BPS) &
        (cand["down_deviation_full_ticks"] >= MIN_DEVIATION_TICKS_FLOOR) &
        (cand["spread_ticks"] <= MAX_SPREAD_TICKS) & upper_ok & lower_ok
    ]
    if cand.empty:
        return pd.DataFrame(columns=_event_columns())
    cand = cand.sort_values("timestamp").reset_index(drop=True)
    events = []
    i = 0
    n = len(cand)
    while i < n:
        j = i
        while j + 1 < n and (cand["timestamp"].iloc[j + 1] - cand["timestamp"].iloc[i]).total_seconds() <= MERGE_WINDOW_SECONDS:
            j += 1
        grp = cand.iloc[i:j + 1]
        deep = grp.loc[grp["down_deviation_full_ticks"].idxmax()]
        events.append(_build_event(grp, deep, df, tick))
        i = j + 1
    return pd.DataFrame(events, columns=_event_columns())


def _event_columns() -> list[str]:
    return ["trade_date", "commodity", "contract", "event_time", "event_low_price", "tick_size",
            "tick_size_source", "parse_status", "session_state", "event_volume", "spread_ticks",
            "expected_price_simple_at_event", "down_deviation_simple_ticks", "down_deviation_simple_bps",
            "expected_price_full_at_event", "down_deviation_full_ticks", "down_deviation_full_bps",
            "full_blocked_reason", "event_depth_ticks",
            "quote_recovery_30s_ticks", "quote_recovery_60s_ticks", "quote_recovery_180s_ticks", "quote_recovery_300s_ticks",
            "trade_recovery_30s_ticks", "trade_recovery_60s_ticks", "trade_recovery_180s_ticks", "trade_recovery_300s_ticks",
            "recovery_label", "night_session_coverage"]


def _build_event(grp: pd.DataFrame, deep: pd.Series, full_df: pd.DataFrame, tick: float) -> dict:
    contract = deep["contract"]
    t0 = deep["timestamp"]
    low_price = float(grp["LastPrice"].min())
    event_volume = float(grp["delta_volume"].sum())
    # recovery：从 full_df 取该合约 t0 之后窗口内的最高价
    after = full_df[(full_df["contract"] == contract) & (full_df["timestamp"] >= t0)]
    quote_rec, trade_rec, label = {}, {}, "no_recovery"
    truncated = False
    for w in RECOVERY_WINDOWS:
        upper = t0 + pd.Timedelta(seconds=w)
        win = after[after["timestamp"] <= upper]
        if win.empty or (win["timestamp"].iloc[0] - t0).total_seconds() > MERGE_WINDOW_SECONDS:
            # 窗口起点就断（跨 session）→ 截断
            truncated = True
            quote_rec[w] = np.nan
            trade_rec[w] = np.nan
            continue
        q = win["BidPrice1"].max()
        tr_rows = win[win["delta_volume"] > 0]
        tr = tr_rows["LastPrice"].max() if not tr_rows.empty else np.nan
        quote_rec[w] = (q - low_price) / tick if pd.notna(q) else np.nan
        trade_rec[w] = (tr - low_price) / tick if pd.notna(tr) else np.nan
    # recovery_label
    depth = float(deep["down_deviation_full_ticks"])
    if depth > 0 and not truncated:
        best = max([trade_rec.get(w, np.nan) for w in RECOVERY_WINDOWS] + [0.0])
        ratio = best / depth if depth else 0
        label = "trade_full_recovery" if ratio >= 0.8 else (
            "trade_partial_recovery" if ratio >= 0.5 else "no_recovery")
    elif truncated:
        label = "truncated"
    row = {
        "trade_date": int(deep["trading_day"]) if "trading_day" in deep else 0,
        "commodity": deep.get("commodity", ""),
        "contract": contract,
        "event_time": t0,
        "event_low_price": low_price,
        "tick_size": tick,
        "tick_size_source": deep.get("tick_size_source", ""),
        "parse_status": "ok",
        "session_state": deep["session_state"],
        "event_volume": event_volume,
        "spread_ticks": deep["spread_ticks"],
        "expected_price_simple_at_event": deep.get("expected_price_simple"),
        "down_deviation_simple_ticks": deep.get("down_deviation_simple_ticks"),
        "down_deviation_simple_bps": deep.get("down_deviation_simple_bps"),
        "expected_price_full_at_event": deep.get("expected_price_full"),
        "down_deviation_full_ticks": deep.get("down_deviation_full_ticks"),
        "down_deviation_full_bps": deep.get("down_deviation_full_bps"),
        "full_blocked_reason": deep.get("full_blocked_reason", "not_blocked"),
        "event_depth_ticks": depth,
        "recovery_label": label,
        "night_session_coverage": False,
    }
    for w in RECOVERY_WINDOWS:
        row[f"quote_recovery_{w}s_ticks"] = quote_rec[w]
        row[f"trade_recovery_{w}s_ticks"] = trade_rec[w]
    return row
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_replay.py -v`
Expected: 1 passed

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/replay.py tests/test_replay.py
git commit -m "feat(tick_stats): detect_events（候选筛选 + 10s 合并 + recovery 窗口 session 截断）"
```

---

### Task 8: 单笔回测 simulate_trades（conservative × {bid, timeout}）

**Files:**
- Modify: `src/tick_stats/replay.py`
- Test: `tests/test_replay.py`

**Interfaces:**
- Produces: `simulate_trades(events: pd.DataFrame, commodity_df: pd.DataFrame) -> pd.DataFrame`（每行一笔模拟成交，列见 spec §5.2）。
- Consumes: Task 7 `detect_events` 输出 + Task 6 带价 DataFrame。
- 回测口径：offset `{3,8,21}` × `conservative_fill` × exit `{bid_exit, timeout_exit}`；`target_profit_ticks=5`、`timeout_seconds=60`；订单生命周期 `quote_latency=1, order_latency=0, reprice_interval=2`。

- [ ] **Step 1: 追加失败测试**

```python
from src.tick_stats.replay import simulate_trades


def test_simulate_trades_fills_and_exits_on_synthetic_spike():
    # 构造：target 在 09:00:03 砸到 95，09:00:10 回到 100；事件触发后挂单 100-8*0.02=99.84
    # conservative_fill 要求 LastPrice <= order_price - tick → 95<=99.82 ✓
    base = {"contract": "T", "session_state": "continuous_trading", "is_tradable_session": True,
            "tick_size": 0.02, "spread_ticks": 1.0, "BidPrice1": 100.0, "AskPrice1": 100.02,
            "UpperLimitPrice": 1170.7, "LowerLimitPrice": 830.48, "Volume": 100,
            "trading_day": 20260520, "commodity": "T"}
    snaps = pd.DataFrame([
        dict(base, timestamp=pd.Timestamp("2026-05-20 09:00:03"), snapshot_seq=0,
             LastPrice=95.0, mid_price=95.0, delta_volume=5.0,
             expected_price_full=100.0, down_deviation_full_ticks=250.0,
             down_deviation_full_bps=500.0, full_blocked_reason="not_blocked"),
        dict(base, timestamp=pd.Timestamp("2026-05-20 09:00:10"), snapshot_seq=1,
             LastPrice=100.0, mid_price=100.0, delta_volume=2.0,
             expected_price_full=100.0, down_deviation_full_ticks=0.0,
             down_deviation_full_bps=0.0, full_blocked_reason="not_blocked"),
    ])
    events = pd.DataFrame([{
        "contract": "T", "event_time": pd.Timestamp("2026-05-20 09:00:03"),
        "event_low_price": 95.0, "expected_price_full_at_event": 100.0,
        "down_deviation_full_ticks": 250.0, "down_deviation_full_bps": 500.0,
    }])
    trades = simulate_trades(events, snaps)
    assert len(trades) > 0
    # offset=8 → order_price = floor(100 - 8*0.02)=99.84
    t = trades[(trades.offset_ticks == 8)].iloc[0]
    assert t["fill_price"] == 99.84
    assert t["fill_model"] == "conservative"
    assert t["exit_rule"] in ("bid", "timeout")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `./venv/bin/python -m pytest tests/test_replay.py::test_simulate_trades_fills_and_exits_on_synthetic_spike -v`
Expected: FAIL（`simulate_trades` 未定义）

- [ ] **Step 3: 实现 simulate_trades**

追加到 `src/tick_stats/replay.py`：
```python
OFFSET_TICKS = [3, 8, 21]
TARGET_PROFIT_TICKS = 5
TIMEOUT_SECONDS = 60
QUOTE_LATENCY = 1
ORDER_LATENCY = 0
REPRICE_INTERVAL = 2
MIN_FILL_VOLUME = 1


def _floor_tick(price: float, tick: float) -> float:
    import math
    return math.floor(price / tick) * tick


def _ceil_tick(price: float, tick: float) -> float:
    import math
    return math.ceil(price / tick) * tick


def simulate_trades(events: pd.DataFrame, commodity_df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if events.empty:
        return pd.DataFrame(columns=_trade_columns())
    df = commodity_df.sort_values(["contract", "timestamp"]).reset_index(drop=True)
    for _, ev in events.iterrows():
        contract = ev["contract"]
        cdf = df[df["contract"] == contract].reset_index(drop=True)
        if cdf.empty:
            continue
        tick = float(cdf["tick_size"].iloc[0])
        # 决策快照 = 事件时刻对应的快照
        match = cdf[cdf["timestamp"] == ev["event_time"]]
        if match.empty:
            continue
        decision_seq = int(match["snapshot_seq"].iloc[0])
        # quote_snapshot_seq = decision_seq - quote_latency（订单基于更早一帧的 expected）
        quote_seq = max(decision_seq - QUOTE_LATENCY, 0)
        if quote_seq >= len(cdf):
            continue
        exp_at_quote = cdf.iloc[quote_seq]["expected_price_full"]
        if pd.isna(exp_at_quote):
            continue
        order_submit = quote_seq + QUOTE_LATENCY
        order_live_from = order_submit + ORDER_LATENCY
        for offset in OFFSET_TICKS:
            raw = float(exp_at_quote) - offset * tick
            order_price = _floor_tick(raw, tick)
            # 找成交：conservative_fill
            fill = None
            for k in range(order_live_from, len(cdf)):
                row = cdf.iloc[k]
                if (row["snapshot_seq"] - order_live_from) % REPRICE_INTERVAL == 0 or k == order_live_from:
                    pass  # ponytail: 不实现高频撤改单，固定节奏仅记录
                if not row.get("is_tradable_session", True):
                    continue
                if row["delta_volume"] > MIN_FILL_VOLUME - 1 and row["LastPrice"] <= order_price - tick:
                    fill = row
                    break
            if fill is None:
                continue
            fill_seq = int(fill["snapshot_seq"])
            exit_target = _ceil_tick(order_price + TARGET_PROFIT_TICKS * tick, tick)
            exit_row, exit_rule, exit_price = None, "timeout", None
            for k in range(fill_seq + 1, len(cdf)):
                row = cdf.iloc[k]
                if row["BidPrice1"] >= exit_target:
                    exit_row, exit_rule, exit_price = row, "bid", exit_target
                    break
                if (row["timestamp"] - fill["timestamp"]).total_seconds() >= TIMEOUT_SECONDS:
                    exit_row, exit_rule, exit_price = row, "timeout", row["BidPrice1"]
                    break
            if exit_row is None:
                continue
            hold = (exit_row["timestamp"] - fill["timestamp"]).total_seconds()
            gross = (exit_price - order_price) / tick
            adverse = (cdf.iloc[fill_seq: int(exit_row["snapshot_seq"]) + 1]["LastPrice"].min() - order_price) / tick
            favorable = (cdf.iloc[fill_seq: int(exit_row["snapshot_seq"]) + 1]["LastPrice"].max() - order_price) / tick
            rows.append({
                "trade_id": f"{contract}-{ev['event_time']}-{offset}",
                "matched_event_id": f"{contract}-{ev['event_time']}",
                "trade_date": int(cdf.iloc[0]["trading_day"]) if "trading_day" in cdf.columns else 0,
                "commodity": ev.get("commodity", ""),
                "contract": contract,
                "offset_ticks": offset,
                "raw_order_price": raw,
                "order_price": order_price,
                "quote_snapshot_seq": quote_seq,
                "order_submit_snapshot": order_submit,
                "order_live_from_snapshot": order_live_from,
                "decision_snapshot": decision_seq,
                "fill_snapshot_seq": fill_seq,
                "fill_time": fill["timestamp"],
                "fill_price": order_price,
                "last_price_at_fill": fill["LastPrice"],
                "penetration_ticks": (order_price - fill["LastPrice"]) / tick,
                "delta_volume_at_fill": fill["delta_volume"],
                "fill_model": "conservative",
                "exit_rule": exit_rule,
                "exit_target_price": exit_target,
                "target_profit_ticks": TARGET_PROFIT_TICKS,
                "timeout_seconds": TIMEOUT_SECONDS,
                "exit_time": exit_row["timestamp"],
                "exit_price": exit_price,
                "hold_seconds": hold,
                "gross_profit_ticks": gross,
                "max_adverse_ticks": adverse,
                "max_favorable_ticks": favorable,
                "exit_reason": exit_rule,
                "expected_price_at_fill": fill.get("expected_price_full"),
                "expected_price_at_exit": exit_row.get("expected_price_full"),
                "down_deviation_simple_ticks_at_fill": fill.get("down_deviation_simple_ticks"),
            })
    return pd.DataFrame(rows, columns=_trade_columns())


def _trade_columns() -> list[str]:
    return ["trade_id", "matched_event_id", "trade_date", "commodity", "contract", "offset_ticks",
            "raw_order_price", "order_price", "quote_snapshot_seq", "order_submit_snapshot",
            "order_live_from_snapshot", "decision_snapshot", "fill_snapshot_seq", "fill_time",
            "fill_price", "last_price_at_fill", "penetration_ticks", "delta_volume_at_fill",
            "fill_model", "exit_rule", "exit_target_price", "target_profit_ticks", "timeout_seconds",
            "exit_time", "exit_price", "hold_seconds", "gross_profit_ticks",
            "max_adverse_ticks", "max_favorable_ticks", "exit_reason",
            "expected_price_at_fill", "expected_price_at_exit", "down_deviation_simple_ticks_at_fill"]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_replay.py -v`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add src/tick_stats/replay.py tests/test_replay.py
git commit -m "feat(tick_stats): simulate_trades（offset 3 档 × conservative × bid/timeout）"
```

---

### Task 9: 入口 CLI + 端到端冒烟

**Files:**
- Create: `run_tick_replay.py`
- Create: `tests/test_run_tick_replay.py`

**Interfaces:**
- Produces: CLI `run_tick_replay.py --date 20260520 [--symbols AU] [--output-dir DIR]`，写 `tick_event_replay.csv` + `simulated_trades.csv`。
- Consumes: Task 4 `load_day_snapshots`、Task 6 `compute_expected_prices`、Task 7/8 `detect_events`/`simulate_trades`。

- [ ] **Step 1: 写失败冒烟测试**

`tests/test_run_tick_replay.py`：
```python
import pandas as pd
from src.tick_stats.tick_io import load_day_snapshots
from src.tick_stats.expected_price import compute_expected_prices
from src.tick_stats.replay import detect_events, simulate_trades


def _raw_row(t, ms, last, vol, bid, ask):
    return {"TradingDay": 20260520, "InstrumentID": "T", "UpdateTime": t, "UpdateMillisec": ms,
            "LastPrice": last, "Volume": vol, "BidPrice1": bid, "BidVolume1": 1, "AskPrice1": ask,
            "AskVolume1": 1, "AveragePrice": last, "Turnover": last * vol, "OpenInterest": 100,
            "UpperLimitPrice": 1170.7, "LowerLimitPrice": 830.48}


def test_end_to_end_pipeline_runs(tmp_path):
    day_dir = tmp_path / "202605" / "20260520"
    day_dir.mkdir(parents=True)
    # T + P，T 在 09:00:05 孤立砸低后回归
    rows = [_raw_row("09:00:00", 0, 100.0, 10, 99.99, 100.01)]
    for sec in range(1, 30):
        last = 95.0 if sec == 5 else 100.0
        rows.append(_raw_row(f"09:00:{sec:02d}", 0, last, 10 + sec, last - 0.01, last + 0.01))
    pd.DataFrame(rows).to_csv(day_dir / "t2606_20260520.csv", index=False)
    prows = [_raw_row(f"09:00:{sec:02d}", 0, 100.0, 10 + sec, 99.99, 100.01) for sec in range(0, 30)]
    prows = [dict(r, InstrumentID="P") for r in prows]
    pd.DataFrame(prows).to_csv(day_dir / "p2606_20260520.csv", index=False)

    snaps = load_day_snapshots(20260520, data_dir=str(tmp_path))
    assert "T" in snaps
    snaps["T"] = compute_expected_prices(snaps["T"])
    events = detect_events(snaps["T"])
    trades = simulate_trades(events, snaps["T"])
    # 至少链路跑通（事件/成交数允许 0，只要不抛异常）
    assert isinstance(events, pd.DataFrame)
    assert isinstance(trades, pd.DataFrame)
```

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `./venv/bin/python -m pytest tests/test_run_tick_replay.py -v`
Expected: PASS（链路已可跑；若 `detect_events` 因 expected_price NaN 返回空，也属通过——只要无异常）

- [ ] **Step 3: 实现 CLI**

`run_tick_replay.py`：
```python
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from src.tick_stats.expected_price import compute_expected_prices
from src.tick_stats.replay import detect_events, simulate_trades
from src.tick_stats.tick_io import load_day_snapshots


def main() -> None:
    ap = argparse.ArgumentParser(description="Tick 乌龙指 v1 单日回测")
    ap.add_argument("--date", required=True, help="YYYYMMDD")
    ap.add_argument("--symbols", default=None, help="品种逗号分隔，如 AU")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--data-dir", default="data/tick2026")
    args = ap.parse_args()

    syms = args.symbols.split(",") if args.symbols else None
    out = Path(args.output_dir or f"output/tick_{args.date}")
    out.mkdir(parents=True, exist_ok=True)

    snaps = load_day_snapshots(int(args.date), symbols=syms, data_dir=args.data_dir)
    events_all, trades_all = [], []
    for commodity, df in snaps.items():
        df = compute_expected_prices(df)
        events_all.append(detect_events(df))
        trades_all.append(simulate_trades(detect_events(df), df))

    events = pd.concat([e for e in events_all if not e.empty], ignore_index=True) if events_all else pd.DataFrame()
    trades = pd.concat([t for t in trades_all if not t.empty], ignore_index=True) if trades_all else pd.DataFrame()
    events.to_csv(out / "tick_event_replay.csv", index=False)
    trades.to_csv(out / "simulated_trades.csv", index=False)
    print(f"events={len(events)} trades={len(trades)} -> {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑冒烟测试确认通过**

Run: `./venv/bin/python -m pytest tests/test_run_tick_replay.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add run_tick_replay.py tests/test_run_tick_replay.py
git commit -m "feat(tick_stats): run_tick_replay CLI + 端到端冒烟"
```

---

### Task 10: 真数据验收 AU2606 20260520

**Files:** 无新增（运行 + 人工核对）

- [ ] **Step 1: 跑 AU2606 真数据**

Run: `./venv/bin/python run_tick_replay.py --date 20260520 --symbols AU`
Expected: 控制台打印 `events=N trades=M -> output/tick_20260520/`，N≥1。

- [ ] **Step 2: 核对 11:08:09 事件**

Run（查事件表）:
```bash
./venv/bin/python -c "import pandas as pd; df=pd.read_csv('output/tick_20260520/tick_event_replay.csv'); print(df[df.contract=='AU2606'][['event_time','event_low_price','expected_price_full_at_event','down_deviation_full_ticks','down_deviation_full_bps','full_blocked_reason','recovery_label']])"
```
Expected: 存在 `event_time` ≈ 11:08:09 的行，`event_low_price` ≈ 979.66，`down_deviation_full_bps` 显著（>30，对照 peer ~982 → 几百 bps），`full_blocked_reason = not_blocked`，`recovery_label` 非 truncated。

- [ ] **Step 3: 核对成交时序合法**

Run:
```bash
./venv/bin/python -c "import pandas as pd; df=pd.read_csv('output/tick_20260520/simulated_trades.csv'); print(df[['matched_event_id','offset_ticks','quote_snapshot_seq','order_live_from_snapshot','fill_snapshot_seq','fill_price','exit_rule','gross_profit_ticks']])"
```
Expected: `order_live_from_snapshot < fill_snapshot_seq` 全部成立；`fill_price = order_price`；`fill_model = conservative`。

- [ ] **Step 4: 对照 simple vs full**

Run:
```bash
./venv/bin/python -c "import pandas as pd; df=pd.read_csv('output/tick_20260520/tick_event_replay.csv'); print(df[['event_time','down_deviation_simple_ticks','down_deviation_full_ticks','full_blocked_reason']])"
```
Expected: AU2606 事件行 simple/full ticks 接近（流动性好，age 不挡）；其余事件若有 `full_blocked_reason != not_blocked`，对照 simple 列查看 age 门是否真起到剔除作用。

- [ ] **Step 5: 全量 pytest 回归**

Run: `./venv/bin/python -m pytest tests/ -v`
Expected: all passed

- [ ] **Step 6: 记录验收结论 + 提交输出样例**

把验收 5 条核对结果写一段备注（可放 `docs/superpowers/specs/2026-07-06-tick-fat-finger-v1-minimal-slice-design.md` 末尾或 commit message），提交：
```bash
git add output/tick_20260520/  # 如需入库；或只提交验收备注
git commit -m "test(tick_stats): AU2606 20260520 真数据验收通过（11:08:09 孤立尖刺）"
```

---

## Self-Review 备注

- **Spec coverage**：F1→Task1/4，F2→Task10（AU2606 真事件），H1→Task6（秒制 age），H2→Task6（full_blocked_reason），H3→Task6（同口径 mid）+ Task4（spread_ticks 流动性过滤尚未显式剔除 target 候选——Task4 已派生 spread_ticks，事件筛选用 `spread_ticks<=3` 在 Task7 隐式过滤），H4→Task6（peer 质量过滤：**实现缺口**，见下），H5→Task2（日盘过滤），H6/M1→Task2 session 表 + Task3 gap>60s，M2→Task7 recovery 截断，M3→Task4 tick_size 主力推导，M4→Task8 收敛口径，N2→Task5 同秒 tiebreaker + 测试。
- **已知实现缺口（实现时补）**：① H4 peer 质量过滤（peer 自身 spread/涨跌停）在 Task6 `compute_expected_prices` 中未显式实现——实现时在取 peer 对齐值后加一条 `peer spread_ticks<=3 且 peer LastPrice 距涨跌停 ≥ buffer` 的 per-row 过滤；② H3 流动性过滤"中位 spread_ticks>5 剔出 target/reference"在 Task4 loader 中需显式加；这两点在对应任务实现时按 spec §4.1/§4.2 补，不另起任务。
- **Type 一致性**：`detect_events`/`simulate_trades` 的列名与 spec §5.1/§5.2 对齐；`compute_expected_prices` 产出的列名被 Task7/8 消费时大小写一致。
- **待标定参数**：`LOOKBACK_SECONDS=3`、`MAX_REFERENCE_AGE_SECONDS=3`、`MIN_DEVIATION_BPS=30`、`TARGET_PROFIT_TICKS=5`、`TIMEOUT_SECONDS=60`——Task10 验收后按 `full_blocked_reason` 分布与回归标签再调。
