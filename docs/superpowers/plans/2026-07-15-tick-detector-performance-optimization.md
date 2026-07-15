# Tick 检测器性能优化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把单日全品种（81 个）Tick 候选检测的端到端运行时间从 ~17.5 小时降到 ~3-4 小时（纯技术优化，4-5× 提速），且候选事件与现状逐条一致。

**Architecture:** 这是一次**保行为不变的性能重构**。瓶颈由 cProfile 定位（见下"性能证据"）：82% 的时间花在 `attach_fair_price_metrics`（fair_price 计算），其中过半在 `_attach_noise_history`（Pass 2）。根因是逐行 Python 循环 + 逐元素 `df.at[i,…]=` 赋值 + 在循环里反复调用 numpy 标量聚合（median/percentile/MAD）。优化手段分三档：P0 向量化 fair_price 的 Pass 1/Pass 2 与 peer asof 对齐（收益最大）；P1 向量化 tick 帧归一化（`_collapse_same_time_key`、两个 `apply(axis=1)`、`_invalidate_blocked_diffs`，低风险）；P2 去掉冗余排序。每个 Task 先靠"黄金基线测试"锁住当前输出，再重构，最后验证输出不变 + 提速。

**Tech Stack:** Python 3.13, pandas, numpy, pytest。无新依赖。

## 性能证据（cProfile，JD 品种 12 合约，单跑 267s）

| 函数 | cum 耗时 | 占比 |
|---|---:|---:|
| `attach_fair_price_metrics` | 220s | 82% |
| └─ `_attach_noise_history`（Pass 2） | 137s | 51% |
| └─ Pass 1 主体 | ~83s | 31% |
| `prepare_contract_snapshots` | 39s | 15% |
| └─ `_collapse_same_time_key` | 34s | 13% |
| `detect_candidate_ticks` | 8s | 3%（非瓶颈） |

热点调用：`list.append` 1.16 亿次（Pass 2 双层循环）、`np.median` 186 万次、`np.percentile` 83 万次、pandas `__setitem__`（即 `out.at[i,…]=`）212 万次。

真实计时佐证：`output/full-20260520-experimental-81-run2/progress.log` 显示批次 2-9（~80 品种）2026-07-13 22:06 → 2026-07-14 15:34 ≈ 17.5 小时，单品种均 ~13 分钟。

## Global Constraints

- **数值一致性是生命线。** 这是检测算法重构，不是改算法。每个 Task 完成后，候选事件必须与重构前逐条一致。连续数值列用容差比对；离散结果（是否成为候选、触发原因、回归标签）必须精确一致。任何向量化导致的边界点翻转必须人工复核并记录，不得静默放过。
- **浮点容差（连续列比对统一用这个）：** `np.allclose(actual, golden, rtol=1e-6, atol=1e-9, equal_nan=True)`。事件级数值（合理价、区间均价等）用 `pytest.approx(rel=1e-6, abs=1e-6)`。向量化改变浮点求值顺序，1e-12 级抖动正常；若抖动经多步传播使某候选点的 `last_down_ticks >= last_threshold_ticks` 翻转，必须查清是否可接受。
- **测试惯例（沿用项目既有模式）：** 函数级测试用 `_mk`/`_contract_frame` 构造 prepared frame（见 `tests/test_tick_detector_reference_selection.py`）；真数据测试用 `slow_` 前缀 + `@pytest.mark.skipif(not _real_data_available(), reason=…)`，常规套件用 `./venv/bin/python -m pytest tests/ -q -k "not slow_"` 跳过。
- **不要用 `./venv/bin/pytest`**（shebang 指向旧路径），统一 `./venv/bin/python -m pytest`。
- **不改检测算法逻辑、不改阈值常量、不改 CSV/HTML 字段定义。** 只改"同样的输入如何更快算出同样的输出"。
- **prepare 产出的 frame 已按 `["market_time_key","snapshot_seq"]` 升序稳定排序**（`tick_io.py:256`）。所有依赖排序的向量化（`np.searchsorted`、`drop_duplicates(keep="last")`、`np.diff`）都建立在此前提上。
- **不引入多进程/多线程、不引入新依赖。** 单进程向量化即可达成目标；并发是本计划范围外的后续选项。

---

## 0. 文件范围

| 文件 | 改动 |
|---|---|
| `tests/test_tick_detector_perf_golden.py`（新建） | 黄金基线 characterization 测试（合成快基线 + 真数据 JD 全链线） |
| `tests/fixtures/golden_jd_candidates.csv`（新建，二进制不可读，由测试生成后提交） | JD 真数据全链路候选事件黄金快照 |
| `src/tick_detector/tick_io.py` | `_collapse_same_time_key` 向量化；`prepare_contract_snapshots` 两个 `apply(axis=1)` 与 `_invalidate_blocked_diffs` 向量化 |
| `src/tick_detector/reference_selection.py` | `_build_peer_aligned` 的 asof 向量化；`_attach_noise_history`（Pass 2）向量化；`attach_fair_price_metrics`（Pass 1）预分配去 `out.at` |
| `tests/test_tick_io.py` | 补 collapse/prepare 行为不变断言 |
| `tests/test_tick_detector_reference_selection.py` | 既有 12 个 fair_price 测试是无变更安全网；视需要补向量化等价断言 |

不新增配置、不新增模块、不改 `run_tick_detector.py` 的流程（除非 Task 7 的 sort 缓存需要）。

---

## 1. Task 1：建立黄金基线安全网（characterization tests）

**目的：** 在动任何算法代码之前，先把"当前实现的输出"固化成可比对的黄金快照。后续每个 Task 重构后，都靠它证明行为不变。这是整个计划的安全网，**必须最先做**。

**Files:**
- Create: `tests/test_tick_detector_perf_golden.py`
- Create（由测试首次生成后提交）: `tests/fixtures/golden_jd_candidates.csv`

**Interfaces:**
- Consumes: `run_tick_detector.run_detection`、`src.tick_detector.reference_selection.attach_fair_price_metrics`
- Produces: 黄金快照文件 + 两个 characterization 测试函数，供 Task 2-7 反复运行验证

### 1a. 合成快基线（秒级跑，TDD 循环用）

合成一份足够触发 fair_price + noise 完整计算的多合约数据，跑全链路 `run_detection`，把候选 CSV 的关键字段哈希/快照存为黄金。重构后比对。

- [ ] **Step 1：写 characterization 测试（合成）**

在 `tests/test_tick_detector_perf_golden.py` 写：

```python
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from run_tick_detector import run_detection

_TICK_COLUMNS = [
    "TradingDay", "InstrumentID", "UpdateTime", "UpdateMillisec", "LastPrice",
    "Volume", "BidPrice1", "BidVolume1", "AskPrice1", "AskVolume1",
    "AveragePrice", "Turnover", "OpenInterest", "UpperLimitPrice", "LowerLimitPrice",
]


def _tick_row(instrument_id, update_time, millisec, *, last_price=100.0, volume=10,
              bid=99.98, ask=100.0, turnover=1000000.0):
    return {
        "TradingDay": 20260520, "InstrumentID": instrument_id,
        "UpdateTime": update_time, "UpdateMillisec": millisec,
        "LastPrice": last_price, "Volume": volume,
        "BidPrice1": bid, "BidVolume1": 1, "AskPrice1": ask, "AskVolume1": 1,
        "AveragePrice": last_price, "Turnover": turnover, "OpenInterest": 100,
        "UpperLimitPrice": 200.0, "LowerLimitPrice": 50.0,
    }


def _write_csv(path: Path, rows):
    pd.DataFrame(rows, columns=_TICK_COLUMNS).to_csv(path, index=False)


def _build_synthetic_day(day_dir: Path):
    """3 个 AU 合约 × 320 秒：前 300 秒价格稳定 100，第 301 秒 target 突跌到 95。

    数据量足以让 fair_price 与 noise history 进入 reliable，从而完整走到候选检测。
    """
    target_rows, peer_a_rows, peer_b_rows = [], [], []
    for sec in range(320):
        h, m, s = 9, (90 + sec) // 60, (90 + sec) % 60
        ut = f"{h:02d}:{m % 60:02d}:{s:02d}"
        if sec < 300:
            lp, vol, to = 100.0, 10 + sec, (10 + sec) * 1000000.0
        else:
            lp, vol, to = 95.0, 400, 38000000.0
        target_rows.append(_tick_row("au2606", ut, 0, last_price=lp, volume=vol,
                                     turnover=to, bid=lp - 0.02, ask=lp))
        peer_a_rows.append(_tick_row("au2608", ut, 0, last_price=100.0, volume=20 + sec,
                                     turnover=(20 + sec) * 1000000.0, bid=99.98, ask=100.0))
        peer_b_rows.append(_tick_row("au2610", ut, 0, last_price=100.0, volume=18 + sec,
                                     turnover=(18 + sec) * 1000000.0, bid=99.98, ask=100.0))
    day_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(day_dir / "au2606_20260520.csv", target_rows)
    _write_csv(day_dir / "au2608_20260520.csv", peer_a_rows)
    _write_csv(day_dir / "au2610_20260520.csv", peer_b_rows)


def _candidate_signature(events_csv_path: Path) -> str:
    """把候选 CSV 的离散判定列拼成稳定签名（触发原因/标签必须精确一致）。"""
    df = pd.read_csv(events_csv_path)
    if df.empty:
        return "EMPTY"
    cols = [c for c in ("合约", "事件时间", "触发原因", "回归标签") if c in df.columns]
    df = df[cols].astype(str).sort_values(cols).reset_index(drop=True)
    return df.to_csv(index=False)


def test_synthetic_candidate_signature_is_stable(tmp_path):
    """合成数据全链路：候选的合约/时间/触发原因/标签签名必须与重构前一致。

    这是 TDD 循环用的秒级安全网。首次运行在未优化代码上记录签名值（见 Step 2）。
    """
    day_dir = tmp_path / "20260520"
    _build_synthetic_day(day_dir)
    out_dir = tmp_path / "out"
    run_detection(tick_day_path=str(day_dir), commodity="AU", contract="AU2606",
                  output_dir=str(out_dir))
    sig = _candidate_signature(out_dir / "tick_candidate_events.csv")
    # 黄金签名：Task 1 首次在未优化代码上跑出后填入。后续 Task 不得修改此值——
    # 若失败说明行为已变，必须查因并修复，禁止直接改黄金值绕过。
    assert sig == "GOLDEN_SYNTHETIC_SIGNATURE_PLACEHOLDER", (
        f"合成候选签名变化：\n{sig}"
    )
```

- [ ] **Step 2：在未优化代码上生成合成黄金签名**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from tests.test_tick_detector_perf_golden import _build_synthetic_day, _candidate_signature
from run_tick_detector import run_detection
import tempfile, pathlib
d = pathlib.Path(tempfile.mkdtemp())/'20260520'
_build_synthetic_day(d)
o = pathlib.Path(tempfile.mkdtemp())/'out'
run_detection(tick_day_path=str(d), commodity='AU', contract='AU2606', output_dir=str(o))
print(_candidate_signature(o/'tick_candidate_events.csv'))
"
```

把打印出的签名（一整段 CSV 文本）原样粘贴替换测试里的 `"GOLDEN_SYNTHETIC_SIGNATURE_PLACEHOLDER"`。再次运行测试确认 PASS：

```bash
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py::test_synthetic_candidate_signature_is_stable -v
```

预期：PASS。此签名即后续所有 Task 必须保持的黄金值。

### 1b. 真数据 JD 全链路黄金（slow_，最终验收用）

- [ ] **Step 3：写真数据 characterization 测试**

在同一个文件追加：

```python
JD_REAL_DATA_PATH = "data/tick2026/202605/20260520"
FIXTURES = Path(__file__).parent / "fixtures"


def _jd_real_data_available() -> bool:
    return (Path(JD_REAL_DATA_PATH) / "jd2606_20260520.csv").exists()


@pytest.mark.skipif(not _jd_real_data_available(), reason="JD 真数据不可用")
def test_slow_jd_full_run_matches_golden(tmp_path):
    """JD 单品种真数据全链路：候选事件必须与重构前黄金逐条一致。

    慢测试（~4 分钟）。常规套件用 -k 'not slow_' 跳过；性能验收时必须跑。
    """
    out_dir = tmp_path / "jd-20260520"
    run_detection(tick_day_path=JD_REAL_DATA_PATH, commodities="JD",
                  output_dir=str(out_dir))
    actual = pd.read_csv(out_dir / "tick_candidate_events.csv")
    golden = pd.read_csv(FIXTURES / "golden_jd_candidates.csv")
    # 行数必须一致
    assert len(actual) == len(golden), (
        f"候选事件数变化：golden={len(golden)} actual={len(actual)}")
    # 离散判定列精确一致
    key_cols = [c for c in ("合约", "事件时间", "触发原因", "回归标签") if c in actual.columns]
    a = actual[key_cols].astype(str).sort_values(key_cols).reset_index(drop=True)
    g = golden[key_cols].astype(str).sort_values(key_cols).reset_index(drop=True)
    pd.testing.assert_series_equal(a["触发原因"], g["触发原因"], check_names=False)
    pd.testing.assert_series_equal(a["回归标签"], g["回归标签"], check_names=False)
    # 连续数值列容差一致
    for col in ("合理价", "区间成交均价", "末笔向下偏离_跳", "区间均价向下偏离_跳"):
        if col in actual.columns:
            assert np.allclose(actual[col].to_numpy(dtype=float),
                               golden[col].to_numpy(dtype=float),
                               rtol=1e-6, atol=1e-9, equal_nan=True), f"{col} 数值漂移"
```

- [ ] **Step 4：在未优化代码上生成 JD 黄金 fixture**

```bash
mkdir -p tests/fixtures
./venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from run_tick_detector import run_detection
import tempfile, pathlib, shutil
o = pathlib.Path(tempfile.mkdtemp())
run_detection(tick_day_path='data/tick2026/202605/20260520', commodities='JD', output_dir=str(o))
shutil.copy(o/'tick_candidate_events.csv', 'tests/fixtures/golden_jd_candidates.csv')
print('copied', len(__import__('pandas').read_csv('tests/fixtures/golden_jd_candidates.csv')), 'rows')
"
```

- [ ] **Step 5：跑真数据 characterization 确认 PASS**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py::test_slow_jd_full_run_matches_golden -v
```

预期：PASS（当前未优化代码 vs 自己生成的黄金，必然一致）。

- [ ] **Step 6：回归既有套件，确认没破坏现状**

```bash
./venv/bin/python -m pytest tests/ -q -k "not slow_"
```

预期：全绿（新增测试 PASS，既有测试不受影响）。

- [ ] **Step 7：提交安全网**

```bash
git add tests/test_tick_detector_perf_golden.py tests/fixtures/golden_jd_candidates.csv
git commit -m "test(tick-detector): add golden baseline safety net for perf refactor"
```

---

## 2. Task 2：`_collapse_same_time_key` 向量化（P1，最低风险）

**目的：** 当前用 `groupby(sort=False)` 逐组 `copy()` + `pd.concat`，JD 上 34s。df 进入此函数前已按 `["market_time_key","snapshot_seq"]` 排序，同一 key 连续且末行序号最大——可纯 numpy 取每组首尾位置，避免逐组 copy/concat。预期 34s → ~1s。

**Files:**
- Modify: `src/tick_detector/tick_io.py`（`_collapse_same_time_key`，当前约 333-355 行）
- Test: `tests/test_tick_io.py`、`tests/test_tick_detector_perf_golden.py`

**Interfaces:**
- Consumes: 进入函数的 df 已排序（Global Constraints）
- Produces: 同签名同输出列 `snapshot_seq_start`/`snapshot_seq_end`，行为不变

- [ ] **Step 1：先补一个行为不变失败测试**

在 `tests/test_tick_io.py` 追加（若已有等价测试可跳过）：

```python
def test_collapse_same_time_key_keeps_last_row_and_seq_range():
    import pandas as pd
    from src.tick_detector.tick_io import _collapse_same_time_key
    # 同一 market_time_key 三行 + 单行
    df = pd.DataFrame({
        "market_time_key": [1000, 1000, 1000, 2000],
        "snapshot_seq": [5, 6, 7, 9],
        "LastPrice": [10.0, 11.0, 12.0, 20.0],
    })
    out = _collapse_same_time_key(df)
    assert len(out) == 2
    # key=1000 取最后一行 LastPrice=12，序号范围 5..7
    row0 = out[out["market_time_key"] == 1000].iloc[0]
    assert row0["LastPrice"] == 12.0
    assert int(row0["snapshot_seq_start"]) == 5
    assert int(row0["snapshot_seq_end"]) == 7
    # key=2000 单行：start==end==自身序号
    row1 = out[out["market_time_key"] == 2000].iloc[0]
    assert int(row1["snapshot_seq_start"]) == 9
    assert int(row1["snapshot_seq_end"]) == 9
```

- [ ] **Step 2：运行确认新测试通过（当前实现已满足，作等价基准）**

```bash
./venv/bin/python -m pytest tests/test_tick_io.py::test_collapse_same_time_key_keeps_last_row_and_seq_range -v
```

预期：PASS。

- [ ] **Step 3：向量化重写 `_collapse_same_time_key`**

把 `src/tick_detector/tick_io.py` 里的 `_collapse_same_time_key` 整体替换为：

```python
def _collapse_same_time_key(df: pd.DataFrame) -> pd.DataFrame:
    """同一 market_time_key 多行先合并：最后一行提供盘口/累计值，保留序号范围。

    输入 df 已按 [market_time_key, snapshot_seq] 升序稳定排序（见 prepare_contract_snapshots），
    因此同一 key 的行连续、最后一行 snapshot_seq 最大，可直接用 numpy 定位每组首尾。
    """
    if df.empty:
        return df
    keys = df["market_time_key"].to_numpy()
    n = len(keys)
    if n == 1:
        first_pos = np.array([0])
        last_pos = np.array([0])
    else:
        is_new_group = np.empty(n, dtype=bool)
        is_new_group[0] = True
        is_new_group[1:] = keys[1:] != keys[:-1]
        first_pos = np.flatnonzero(is_new_group)
        last_pos = np.concatenate((first_pos[1:], [n])) - 1
    last = df.iloc[last_pos].copy()
    seq = df["snapshot_seq"].to_numpy()
    last["snapshot_seq_start"] = seq[first_pos]
    last["snapshot_seq_end"] = seq[last_pos]
    return last.reset_index(drop=True)
```

- [ ] **Step 4：跑行为测试 + 黄金安全网确认不变**

```bash
./venv/bin/python -m pytest tests/test_tick_io.py -q
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py::test_synthetic_candidate_signature_is_stable -q
./venv/bin/python -m pytest tests/ -q -k "not slow_"
```

预期：全 PASS。若合成签名变化，说明行为改变，**不得提交**，回查向量化等价性。

- [ ] **Step 5：提交**

```bash
git add src/tick_detector/tick_io.py tests/test_tick_io.py
git commit -m "perf(tick-detector): vectorize _collapse_same_time_key"
```

---

## 3. Task 3：`prepare_contract_snapshots` 的 apply 与逐行差分失效向量化（P1）

**目的：** `df.apply(_compute_market_time_key, axis=1)`、`df.apply(_is_open_protected_row, axis=1)`、`_invalidate_blocked_diffs` 的 `for i` 循环都是逐行 Python。向量化后大品种（au/ag）可省 10-20s，且与 Task 2 叠加进一步降低 prepare 开销。

**Files:**
- Modify: `src/tick_detector/tick_io.py`（`prepare_contract_snapshots` 中两处 `apply` 调用、`_invalidate_blocked_diffs`）
- Test: `tests/test_tick_io.py`、黄金安全网

**Interfaces:** 签名与输出列不变（`market_time_key`、`is_open_protected`、`delta_volume`/`delta_turnover` 的失效规则不变）。

- [ ] **Step 1：补行为不变测试（market_time_key 跨午夜、开盘保护、差分失效）**

在 `tests/test_tick_io.py` 追加：

```python
def test_market_time_key_orders_night_then_post_midnight():
    import pandas as pd
    from src.tick_detector.tick_io import _compute_market_time_key
    # 21:00:00 -> key 0；00:00:00（午夜后）-> 排在 21:xx 之后
    # 直接用 row dict 调用（保持与旧实现相同的单行接口）
    def mk(ut, ms):
        return _compute_market_time_key({"UpdateTime": ut, "UpdateMillisec": ms})
    assert mk("21:00:00", 0) == 0
    assert mk("00:00:00", 0) == 3 * 3600 * 1000
    assert mk("09:00:00", 0) == (9 * 3600) * 1000 + 3 * 3600 * 1000  # 日盘在午夜后分支
    assert mk("21:00:00", 0) < mk("00:00:00", 0) < mk("02:30:00", 0)


def test_invalidate_blocked_diffs_marks_session_first_data_gap_and_nonpositive():
    import numpy as np, pandas as pd
    from src.tick_detector.tick_io import (
        MAX_DATA_GAP_SECONDS, _invalidate_blocked_diffs,
    )
    df = pd.DataFrame({
        "market_time_key": [1000, 2000, 2000 + MAX_DATA_GAP_SECONDS * 1000 + 1, 9000],
        "is_tradable_session": [True, True, True, True],
        "is_open_protected": [False, False, False, False],
        "delta_volume": [1.0, 1.0, 1.0, 1.0],
        "delta_turnover": [1.0, 1.0, 1.0, -1.0],  # 最后一行非正
    })
    _invalidate_blocked_diffs(df)
    dv = df["delta_volume"].to_list()
    # 第0行 session 首条失效；第2行跨数据断点失效；第3行 delta_turnover<=0 失效
    assert np.isnan(dv[0]) and not np.isnan(dv[1]) and np.isnan(dv[2]) and np.isnan(dv[3])
```

- [ ] **Step 2：运行确认通过（当前实现作基准）**

```bash
./venv/bin/python -m pytest tests/test_tick_io.py -q
```

预期：PASS。

- [ ] **Step 3：向量化 `market_time_key` 与 `is_open_protected`，替换两处 `apply`**

原代码里两处 `apply` 位置不同：`market_time_key` 在 `sort_values` **之前**，`is_open_protected` 在 `sort_values` 与 `_collapse_same_time_key` **之后**。因此两处必须各自独立解析时分秒（sort 会重排行序，不能共用中间变量）。分两步替换。

- [ ] **Step 3a：向量化 `market_time_key`（替换第一处 apply）**

把这行：

```python
    df["market_time_key"] = df.apply(_compute_market_time_key, axis=1)
```

替换为：

```python
    _ut = df["UpdateTime"].astype(str)
    _parts = _ut.str.split(":")
    _total_millis = (
        ((_parts.str[0].astype(int).to_numpy() * 60
          + _parts.str[1].astype(int).to_numpy()) * 60
         + _parts.str[2].astype(int).to_numpy()) * 1000
        + df["UpdateMillisec"].astype(int).to_numpy()
    )
    _night_offset = 21 * 3600 * 1000
    df["market_time_key"] = np.where(
        _total_millis >= _night_offset,
        _total_millis - _night_offset,
        _total_millis + 3 * 3600 * 1000,
    )
```

- [ ] **Step 3b：向量化 `is_open_protected`（替换第二处 apply，位于 sort 与 _collapse 之后）**

把这行：

```python
    df["is_open_protected"] = df.apply(_is_open_protected_row, axis=1)
```

替换为（从**已排序的 df** 重新解析时分秒，行序已与 3a 不同）：

```python
    _ut2 = df["UpdateTime"].astype(str)
    _parts2 = _ut2.str.split(":")
    _total_sec = (
        (_parts2.str[0].astype(int).to_numpy() * 60
         + _parts2.str[1].astype(int).to_numpy()) * 60
        + _parts2.str[2].astype(int).to_numpy()
    )
    _protected = np.zeros(len(df), dtype=bool)
    for _session_open in SESSION_OPENS:
        _op = _session_open.split(":")
        _open_sec = (int(_op[0]) * 60 + int(_op[1])) * 60 + int(_op[2])
        _delta = _total_sec - _open_sec
        _protected |= (_delta >= 0) & (_delta < OPEN_GUARD_SECONDS)
    df["is_open_protected"] = _protected
```

- [ ] **Step 4：向量化 `_invalidate_blocked_diffs`**

把 `_invalidate_blocked_diffs` 整体替换为：

```python
def _invalidate_blocked_diffs(df: pd.DataFrame) -> None:
    """session 首条、非可交易行、开盘保护、数据断点(gap>3s)、回退或零增量 -> 差分失效。"""
    n = len(df)
    if n == 0:
        return
    tradable = df["is_tradable_session"].to_numpy(dtype=bool)
    keys = df["market_time_key"].to_numpy()
    invalidate = np.zeros(n, dtype=bool)
    invalidate[0] = True  # session 首条
    if n > 1:
        cross_session = ~(tradable[:-1] & tradable[1:])
        gap = keys[1:].astype(np.int64) - keys[:-1].astype(np.int64)
        data_gap = gap > MAX_DATA_GAP_SECONDS * 1000
        invalidate[1:] = cross_session | data_gap
    invalidate = invalidate | df["is_open_protected"].to_numpy(dtype=bool)
    bad = (
        (df["delta_volume"] <= 0)
        | (df["delta_turnover"] <= 0)
        | df["delta_volume"].isna()
    ).to_numpy(dtype=bool)
    invalidate = invalidate | bad
    df.loc[invalidate, ["delta_volume", "delta_turnover"]] = np.nan
```

- [ ] **Step 5：保留单行版 `_compute_market_time_key` 供测试与潜在复用**

`_compute_market_time_key` 函数体保留不删（单行 row 版），测试仍直接调用它。只是 `prepare_contract_snapshots` 不再通过 `apply` 调它。

- [ ] **Step 6：跑行为测试 + 黄金安全网**

```bash
./venv/bin/python -m pytest tests/test_tick_io.py -q
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py::test_synthetic_candidate_signature_is_stable -q
./venv/bin/python -m pytest tests/ -q -k "not slow_"
```

预期：全 PASS。

- [ ] **Step 7：提交**

```bash
git add src/tick_detector/tick_io.py tests/test_tick_io.py
git commit -m "perf(tick-detector): vectorize market_time_key, open-protect and diff invalidation"
```

---

## 4. Task 4：`_build_peer_aligned` 的 asof 向量化（P0 基础件）

**目的：** 当前对每个 peer 做一次 `for i, mk in enumerate(keys)` 的 Python 循环（`reference_selection.py:229`），P 个 peer 即 P×n。用 `np.searchsorted` 向量化 asof 对齐，把 P×n Python 循环变成 P 次 numpy 向量运算。这是 Pass 1 的基础件，单独抽出来先做、先验证。

**Files:**
- Modify: `src/tick_detector/reference_selection.py`（`_build_peer_aligned`）
- Test: `tests/test_tick_detector_reference_selection.py`、黄金安全网

**Interfaces:** 函数签名与返回的 dict 结构不变（`asof_mid`/`asof_spread_ticks`/`asof_key`/`diff`/`asof_base_valid`，每键为长度 = target 行数的 numpy 数组）。

- [ ] **Step 1：先写等价性失败测试**

在 `tests/test_tick_detector_reference_selection.py` 追加：

```python
def test_build_peer_aligned_asof_matches_step_function():
    """peer asof mid 在 age<=3s 且报价有效时取最近历史值，否则 nan。"""
    import numpy as np
    from src.tick_detector.reference_selection import _build_peer_aligned, MAX_REFERENCE_AGE_SECONDS
    # target 5 个时间点；peer 在 mk=1000 报价 mid=100，mk=3000 报价 mid=101
    target_rows = []
    for mk in (1500, 2500, 3500, 8000, 9000):
        target_rows.append(_mk("AU2606", mk, mid_price=100.0))
    target = _contract_frame("AU2606", "AU", target_rows)
    peer_rows = [
        _mk("AU2608", 1000, mid_price=100.0, bid_price=99.98, ask_price=100.0),
        _mk("AU2608", 3000, mid_price=101.0, bid_price=100.98, ask_price=101.0),
    ]
    peer = _contract_frame("AU2608", "AU", peer_rows)
    pa = _build_peer_aligned(target, {"AU2608": peer}, tick_size=0.02)
    a = pa["AU2608"]
    # mk=1500 -> asof 1000, age=500<=3s -> mid=100
    assert a["asof_mid"][0] == pytest.approx(100.0)
    # mk=2500 -> asof 1000, age=1500<=3s -> mid=100
    assert a["asof_mid"][1] == pytest.approx(100.0)
    # mk=3500 -> asof 3000, age=500<=3s -> mid=101
    assert a["asof_mid"][2] == pytest.approx(101.0)
    # mk=8000 -> asof 3000, age=5000>3s -> nan
    assert np.isnan(a["asof_mid"][3])
```

- [ ] **Step 2：运行确认当前实现通过（基准）**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py::test_build_peer_aligned_asof_matches_step_function -v
```

预期：PASS。

- [ ] **Step 3：向量化重写 `_build_peer_aligned`**

替换 `_build_peer_aligned` 中每个 peer 的逐行循环（`for i, mk in enumerate(keys)` 块）为向量化等价。完整替换函数体为：

```python
def _build_peer_aligned(
    target_df: pd.DataFrame,
    reference_frames: dict[str, pd.DataFrame],
    tick_size: float,
) -> dict[str, dict[str, np.ndarray]]:
    """为每个 peer 预计算与 target 行索引对齐的 asof mid / spread / diff / 有效性。

    asof_mid[i]      = peer 在 target_keys[i] 时刻的 asof mid（age<=3s 且报价基本有效，否则 nan）
    asof_spread_ticks[i] = 该时刻 peer asof 的 spread_ticks
    asof_key[i]      = peer asof 对应的 market_time_key
    diff[i]          = target_mid[i] - asof_mid[i]
    asof_base_valid[i] = 该时刻 peer 是否满足 age/时段/涨跌停等基本有效性（不含 spread p95 过滤）

    peer frame 进入前已按 market_time_key 升序（prepare 保证），故可用 searchsorted。
    """
    keys = target_df["market_time_key"].to_numpy()
    target_mids = target_df["mid_price"].to_numpy(dtype=float)
    n = len(keys)
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

        # asof 位置：每个 target key 在 peer 中 <= 它的最大位置
        pos = np.searchsorted(p_keys, keys, side="right") - 1
        valid_pos = pos >= 0
        pos_clipped = np.clip(pos, 0, len(p_keys) - 1)
        age_ms = keys.astype(np.int64) - p_keys[pos_clipped].astype(np.int64)
        fresh = valid_pos & (age_ms <= MAX_REFERENCE_AGE_SECONDS * 1000)

        mid = p_mids[pos_clipped]
        bid = p_bids[pos_clipped]
        ask = p_asks[pos_clipped]
        trad = p_tradable[pos_clipped]
        upper = p_upper[pos_clipped]
        lower = p_lower[pos_clipped]

        base_valid = (
            fresh
            & trad
            & (bid > 0)
            & (ask > 0)
            & (ask >= bid)
            & np.isfinite(mid)
            & (mid > 0)
            & ~((upper > 0) & (mid >= upper - LIMIT_BUFFER_TICKS * tick_size))
            & ~((lower > 0) & (mid <= lower + LIMIT_BUFFER_TICKS * tick_size))
        )
        asof_mid = np.where(base_valid, mid, np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            asof_spread_ticks = np.where(base_valid, (ask - bid) / tick_size, np.nan)
        asof_key = np.where(base_valid, p_keys[pos_clipped].astype(float), np.nan)
        diff = target_mids - asof_mid
        result[code] = {
            "asof_mid": asof_mid,
            "asof_spread_ticks": asof_spread_ticks,
            "asof_key": asof_key,
            "diff": diff,
            "asof_base_valid": base_valid,
        }
    return result
```

- [ ] **Step 4：跑 fair_price 全套 + 黄金安全网**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -q
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py::test_synthetic_candidate_signature_is_stable -q
./venv/bin/python -m pytest tests/ -q -k "not slow_"
```

预期：全 PASS。

- [ ] **Step 5：提交**

```bash
git add src/tick_detector/reference_selection.py tests/test_tick_detector_reference_selection.py
git commit -m "perf(tick-detector): vectorize peer asof alignment in _build_peer_aligned"
```

---

## 5. Task 5：`_attach_noise_history`（Pass 2）向量化（P0，最大单点 51%）

**目的：** 当前是 `for i in range(n)` 外层 + `for s in range(lo,hi)` 内层的双层 Python 循环，贡献了 1.16 亿次 `list.append`、137s。改为外层 `for i` 保留、内层逐点循环换成 numpy 切片聚合（窗口内 valid 点用 `np.median`/`_mad_sigma` 批量算），并把 `out.at[i,…]=` 全部换预分配数组。预期 137s → ~10-20s。

> **ceiling（`ponytail:`）：** 外层 n 次循环保留——滑动窗口的 median/MAD 对非等间隔 tick 无 O(n) 向量化。若 Task 8 验收时此处仍是热点，升级路径为单调队列/平衡树维护窗口中位数（O(n log w)），本计划不做。

**Files:**
- Modify: `src/tick_detector/reference_selection.py`（`_attach_noise_history`）
- Test: `tests/test_tick_detector_reference_selection.py`、黄金安全网

**Interfaces:** 同签名；写入的列与语义不变（`noise_sample_count`/`noise_time_span_seconds`/`noise_history_reliable`/`last_noise_median`/`vwap_noise_median`/`last_noise_robust_sigma`/`vwap_noise_robust_sigma`/`execution_depth_robust_sigma`/`last_threshold_ticks`/`vwap_threshold_ticks`/`reference_blocked_reason`）。

- [ ] **Step 1：先写浮点一致失败测试（已有合成快基线，此处补函数级数值点）**

在 `tests/test_tick_detector_reference_selection.py` 追加（`_two_peers_stable` 已存在）：

```python
def test_noise_history_threshold_values_match_baseline_formula():
    """reliable 帧的 last/vwap 阈值 = max(MIN, min_depth, median + K*sigma)。"""
    target, peer_a, peer_b = _two_peers_stable(rows=300)
    out = attach_fair_price_metrics(
        target, {"AU2608": peer_a, "AU2610": peer_b}, tick_size=0.02
    )
    row = out.iloc[-1]
    assert row["noise_history_reliable"] == True or row["noise_history_reliable"] is True  # noqa: E712
    # 数值落在合理区间且与字段自洽
    assert row["last_threshold_ticks"] >= 20  # MIN_LAST_TICKS
    assert row["vwap_threshold_ticks"] >= 20
    assert row["noise_sample_count"] >= 100
```

- [ ] **Step 2：运行确认通过（基准）**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -q
```

预期：PASS。

- [ ] **Step 3：向量化重写 `_attach_noise_history`**

整体替换为：

```python
def _attach_noise_history(out: pd.DataFrame, tick_size: float) -> None:
    """每个目标行 t 用 [t-300s, t-10s] 窗口的已算 fair_price(s) 生成噪声与阈值。

    外层逐行循环保留（滑动窗口 median/MAD 对非等间隔 tick 无 O(n) 向量化），
    内层从逐点 Python 循环改为 numpy 切片聚合；输出写预分配数组，末尾一次性赋列。
    """
    keys = out["market_time_key"].to_numpy()
    n = len(out)
    if n == 0:
        return
    target_last = out["LastPrice"].to_numpy(dtype=float)
    target_vwap = out["interval_vwap"].to_numpy(dtype=float)
    target_dv = out["delta_volume"].to_numpy(dtype=float)
    fair_prices = out["fair_price"].to_numpy(dtype=float)
    fair_reliable = out["fair_price_reliable"].to_numpy(dtype=bool)

    with np.errstate(invalid="ignore", divide="ignore"):
        last_noise_all = np.where(
            np.isfinite(target_last) & (target_last > 0)
            & np.isfinite(fair_prices) & (fair_prices > 0),
            (fair_prices - target_last) / tick_size, np.nan,
        )
        vwap_noise_all = np.where(
            np.isfinite(target_vwap) & (target_vwap > 0)
            & np.isfinite(fair_prices) & (fair_prices > 0),
            (fair_prices - target_vwap) / tick_size, np.nan,
        )
    contrib = fair_reliable & np.isfinite(target_dv) & (target_dv > 0)
    ln_valid = np.isfinite(last_noise_all)
    vn_valid = np.isfinite(vwap_noise_all)

    noise_count = np.zeros(n, dtype=np.int64)
    noise_span = np.zeros(n, dtype=float)
    noise_reliable = np.zeros(n, dtype=bool)
    last_med_arr = np.full(n, np.nan)
    vwap_med_arr = np.full(n, np.nan)
    last_sigma_arr = np.full(n, np.nan)
    vwap_sigma_arr = np.full(n, np.nan)
    depth_sigma_arr = np.full(n, np.nan)
    last_thr_arr = np.full(n, np.nan)
    vwap_thr_arr = np.full(n, np.nan)
    blocked = (
        out["reference_blocked_reason"].to_numpy().astype(object).copy()
        if "reference_blocked_reason" in out.columns
        else np.array([""] * n, dtype=object)
    )

    ws_offset = BASELINE_WINDOW_SECONDS * 1000
    we_offset = BASELINE_EXCLUDE_RECENT_SECONDS * 1000

    for i in range(n):
        mk = int(keys[i])
        lo = int(np.searchsorted(keys, mk - ws_offset, side="left"))
        hi = int(np.searchsorted(keys, mk - we_offset, side="right"))
        if lo >= hi:
            if not blocked[i]:
                blocked[i] = "insufficient_noise_history"
            continue
        win_contrib = contrib[lo:hi]
        win_ln_valid = ln_valid[lo:hi]
        win_vn_valid = vn_valid[lo:hi]
        ln = last_noise_all[lo:hi][win_contrib & win_ln_valid]
        vn = vwap_noise_all[lo:hi][win_contrib & win_vn_valid]
        cnt = int(ln.shape[0])
        span = (int(keys[hi - 1]) - int(keys[lo])) / 1000.0
        noise_count[i] = cnt
        noise_span[i] = span
        reliable = cnt >= NOISE_MIN_SAMPLE_COUNT and span >= NOISE_MIN_SPAN_SECONDS
        noise_reliable[i] = reliable
        if not reliable:
            if not blocked[i]:
                blocked[i] = "insufficient_noise_history"
            continue

        last_med = float(np.median(ln)) if ln.size else 0.0
        vwap_med = float(np.median(vn)) if vn.size else 0.0
        last_sigma = _mad_sigma(ln) if ln.size else 0.0
        vwap_sigma = _mad_sigma(vn) if vn.size else 0.0

        both = win_contrib & win_ln_valid & win_vn_valid
        only_ln = win_contrib & win_ln_valid & ~win_vn_valid
        only_vn = win_contrib & win_vn_valid & ~win_ln_valid
        if both.any() or only_ln.any() or only_vn.any():
            depth_vals = np.concatenate([
                np.maximum(last_noise_all[lo:hi][both], vwap_noise_all[lo:hi][both]),
                last_noise_all[lo:hi][only_ln],
                vwap_noise_all[lo:hi][only_vn],
            ])
        else:
            depth_vals = np.array([], dtype=float)
        depth_sigma = _mad_sigma(depth_vals) if depth_vals.size else 0.0

        last_med_arr[i] = last_med
        vwap_med_arr[i] = vwap_med
        last_sigma_arr[i] = last_sigma
        vwap_sigma_arr[i] = vwap_sigma
        depth_sigma_arr[i] = depth_sigma

        fp_t = fair_prices[i]
        min_depth_ticks = (
            fp_t * MIN_DEPTH_BPS / 10000 / tick_size
            if np.isfinite(fp_t) and fp_t > 0 else 0.0
        )
        last_thr_arr[i] = max(MIN_LAST_TICKS, min_depth_ticks, last_med + NOISE_K * last_sigma)
        vwap_thr_arr[i] = max(MIN_VWAP_TICKS, min_depth_ticks, vwap_med + NOISE_K * vwap_sigma)

    out["noise_sample_count"] = noise_count
    out["noise_time_span_seconds"] = noise_span
    out["noise_history_reliable"] = noise_reliable
    out["last_noise_median"] = last_med_arr
    out["vwap_noise_median"] = vwap_med_arr
    out["last_noise_robust_sigma"] = last_sigma_arr
    out["vwap_noise_robust_sigma"] = vwap_sigma_arr
    out["execution_depth_robust_sigma"] = depth_sigma_arr
    out["last_threshold_ticks"] = last_thr_arr
    out["vwap_threshold_ticks"] = vwap_thr_arr
    out["reference_blocked_reason"] = blocked
```

- [ ] **Step 4：跑 fair_price 全套 + 黄金安全网（合成 + 真数据 JD）**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -q
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py -q   # 含 JD slow_
```

预期：全 PASS。若 JD 真数据签名/数值漂移，**不得提交**——逐行回查窗口聚合等价性（重点：unreliable 点的 median/sigma/threshold 必须保持 nan，count/span 仍写实际值）。

- [ ] **Step 5：提交**

```bash
git add src/tick_detector/reference_selection.py tests/test_tick_detector_reference_selection.py
git commit -m "perf(tick-detector): vectorize Pass 2 _attach_noise_history inner loop"
```

---

## 6. Task 6：`attach_fair_price_metrics`（Pass 1）预分配去 `out.at`（P0）

**目的：** Pass 1 主体 `for i in range(n)` 内层对每个 peer 已用切片聚合（非逐点），主要开销在 **`out.at[i, …] =` 逐元素赋值**（profile 显示 `__setitem__` 35s + `_set_value` 20s）与每行每 peer 的 `np.percentile`。本 Task 把所有逐元素赋值换成预分配数组、末尾一次性赋列；滑动窗口的 percentile/median 逻辑保持不变（属算法需要）。预期 ~83s → ~35-45s。

**Files:**
- Modify: `src/tick_detector/reference_selection.py`（`attach_fair_price_metrics`）
- Test: `tests/test_tick_detector_reference_selection.py`、黄金安全网

**Interfaces:** 签名与输出列不变（含内部字段 `__valid_peer_contracts`/`__peer_bases`）。

- [ ] **Step 1：先写等价基准测试（已有 12 个 fair_price 测试作安全网，确认它们仍绿）**

无需新增——既有 `test_fair_price_*`、`test_basis_requires_at_least_60s_coverage`、`test_peer_spread_uses_its_own_baseline_p95_not_target_p95` 等已覆盖 Pass 1 的关键分支。本 Step 仅确认它们在动手前全绿：

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -q
```

预期：全 PASS。

- [ ] **Step 2：重写 `attach_fair_price_metrics`，预分配所有输出列**

把 `for i in range(n):` 循环体内所有 `out.at[i, …] = …` 改为写预分配数组，循环结束后一次性 `out[col] = arr`。完整替换函数体中"初始化输出列"与"Pass 1 循环"两段为：

```python
    out = target_df.sort_values("market_time_key", kind="stable").reset_index(drop=True).copy()
    n = len(out)
    keys = out["market_time_key"].to_numpy()

    # 预分配所有输出为 numpy 数组 / list，循环中不再用 out.at
    fair_price = np.full(n, np.nan)
    fair_uncertainty = np.full(n, np.nan)
    fair_reliable = np.zeros(n, dtype=bool)
    valid_peer_count = np.zeros(n, dtype=np.int64)
    peer_contracts = np.array([""] * n, dtype=object)
    valid_peer_lists: list[list[str]] = [[] for _ in range(n)]
    peer_bases_list: list[dict[str, float]] = [dict() for _ in range(n)]
    last_threshold = np.full(n, np.nan)   # 由 Pass 2 填，此处仅占位
    vwap_threshold = np.full(n, np.nan)   # 由 Pass 2 填
    reference_blocked = np.array([""] * n, dtype=object)

    peer_aligned = _build_peer_aligned(out, reference_frames, tick_size)
    target_spread_ticks = (
        out["spread_ticks"].to_numpy(dtype=float)
        if "spread_ticks" in out.columns else np.full(n, np.nan)
    )
    target_base_valid = _quote_base_valid(out, tick_size)

    for i in range(n):
        mk = int(keys[i])
        lo = int(np.searchsorted(keys, mk - BASELINE_WINDOW_SECONDS * 1000, side="left"))
        hi = int(np.searchsorted(keys, mk - BASELINE_EXCLUDE_RECENT_SECONDS * 1000, side="right"))

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
                not np.isfinite(cur_mid) or cur_mid <= 0
                or not pa["asof_base_valid"][i]
                or not np.isfinite(cur_spread) or cur_spread > peer_spread_limit_ticks
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

        valid_peer_count[i] = len(valid_peers)
        peer_contracts[i] = ",".join(valid_peers)
        valid_peer_lists[i] = valid_peers
        peer_bases_list[i] = peer_bases

        if len(valid_peers) < MIN_VALID_PEERS:
            reference_blocked[i] = "insufficient_peers"
            continue

        fair_arr = np.array(fair_i_values, dtype=float)
        fair_price[i] = float(np.median(fair_arr))
        uncertainty = _mad_sigma(fair_arr) / tick_size
        fair_uncertainty[i] = uncertainty
        reliable = uncertainty <= FAIR_UNCERTAINTY_LIMIT_TICKS
        fair_reliable[i] = reliable
        if not reliable:
            reference_blocked[i] = "fair_uncertainty_exceeded"

    # 一次性赋列
    out["fair_price"] = fair_price
    out["fair_uncertainty_ticks"] = fair_uncertainty
    out["fair_price_reliable"] = fair_reliable
    out["valid_peer_count"] = valid_peer_count
    out["peer_contracts"] = peer_contracts
    out["__valid_peer_contracts"] = valid_peer_lists
    out["__peer_bases"] = peer_bases_list
    out["last_threshold_ticks"] = last_threshold
    out["vwap_threshold_ticks"] = vwap_threshold
    out["noise_sample_count"] = np.zeros(n, dtype=np.int64)
    out["noise_time_span_seconds"] = np.zeros(n, dtype=float)
    out["noise_history_reliable"] = np.zeros(n, dtype=bool)
    out["last_noise_median"] = np.full(n, np.nan)
    out["vwap_noise_median"] = np.full(n, np.nan)
    out["last_noise_robust_sigma"] = np.full(n, np.nan)
    out["vwap_noise_robust_sigma"] = np.full(n, np.nan)
    out["execution_depth_robust_sigma"] = np.full(n, np.nan)
    out["reference_blocked_reason"] = reference_blocked

    # Pass 2：noise history，复用 Pass 1 的 fair_price(s)
    _attach_noise_history(out, tick_size)
    return out
```

注意：`last_threshold_ticks`/`vwap_threshold_ticks` 的最终值由 `_attach_noise_history` 写入（它在 reliable 帧上覆盖）；此处先以 nan 占位，与原实现初始化一致。Pass 2 会覆写 `reference_blocked_reason` 的 `insufficient_noise_history` 分支——为保持原行为（Pass 1 写的 `insufficient_peers`/`fair_uncertainty_exceeded` 不被 Pass 2 覆盖），`_attach_noise_history` 已用 `if not blocked[i]` 守卫，Task 5 的实现保持该守卫。

- [ ] **Step 3：跑 fair_price 全套 + 黄金安全网（合成 + 真数据 JD）**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -q
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py -q
```

预期：全 PASS。JD 真数据数值必须落在容差内（rtol=1e-6）。

- [ ] **Step 4：提交**

```bash
git add src/tick_detector/reference_selection.py
git commit -m "perf(tick-detector): preallocate Pass 1 outputs, drop per-row out.at writes"
```

---

## 7. Task 7：去掉 peer frame 的冗余排序（P2）

**目的：** `_build_peer_aligned`（Task 4 后）与 `_build_recovery_peer_index`（`event_detection.py:578`）仍对每个 peer `sort_values + reset_index`，而进入它们的 frame 已由 `prepare_contract_snapshots` 排好序。同品种多 target × 多 peer 时这是重复 O(n) 扫描 + copy。去掉冗余排序。预期单品种省 5-10s。

**Files:**
- Modify: `src/tick_detector/reference_selection.py`（`_build_peer_aligned`）、`src/tick_detector/event_detection.py`（`_build_recovery_peer_index`、`attach_recovery_metrics` 中对 `enriched_target_frame` 的排序）
- Test: 黄金安全网

**Interfaces:** 不变。

- [ ] **Step 1：确认进入这两个函数的 frame 已排序**

读 `prepare_contract_snapshots`（`tick_io.py:256`）确认 `sort_values(["market_time_key","snapshot_seq"], kind="stable")`，且 `run_detection` 存入 `day_frames` 的都是 prepared frame、`attach_fair_price_metrics` 开头对 target 也做了稳定排序。结论：peer frame 升序前提成立。

- [ ] **Step 2：去掉冗余 `sort_values`，保留 `reset_index`（位置访问需要干净 RangeIndex）**

在 `_build_peer_aligned` 里把：

```python
        fr = frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
```

改为：

```python
        # ponytail: frame 已由 prepare_contract_snapshots 按 market_time_key 升序排序，跳过冗余 sort
        fr = frame.reset_index(drop=True)
```

在 `_build_recovery_peer_index`（`event_detection.py`）里同理，把：

```python
        fr = frame.sort_values("market_time_key", kind="stable").reset_index(drop=True)
```

改为：

```python
        # ponytail: frame 已由 prepare_contract_snapshots 按 market_time_key 升序排序，跳过冗余 sort
        fr = frame.reset_index(drop=True)
```

- [ ] **Step 3：跑全量套件 + 黄金安全网**

```bash
./venv/bin/python -m pytest tests/ -q -k "not slow_"
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py -q
```

预期：全 PASS。若任一失败，说明某条路径传入了未排序 frame——回退本 Task 并排查（不得放宽排序假设）。

- [ ] **Step 4：提交**

```bash
git add src/tick_detector/reference_selection.py src/tick_detector/event_detection.py
git commit -m "perf(tick-detector): drop redundant peer frame sorts (already sorted by prepare)"
```

---

## 8. Task 8：性能验收 + 数值一致性终检

**目的：** 确认累计提速达标、所有真数据行为不变。无新代码，只跑验证。

**Files:** 无。

- [ ] **Step 1：全量合成套件**

```bash
./venv/bin/python -m pytest tests/ -q -k "not slow_"
```

预期：全 PASS。

- [ ] **Step 2：黄金安全网（合成 + JD 真数据）**

```bash
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py -v
```

预期：全 PASS（JD 候选事件数、触发原因、回归标签精确一致；数值列容差内一致）。

- [ ] **Step 3：AU2606 §10 设计锚点回归**

```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py::test_slow_au2606_real_anchor_210435_hits_two_reasons_and_matches_design -v
```

预期：PASS，合理价≈995.33、区间均价≈940.52、一秒合并均价≈962.18、回归标签=trade_recovered_3s 全部不变。

- [ ] **Step 4：cProfile 复测 JD，对比基线 267s**

```bash
PYTHONPATH=. ./venv/bin/python /tmp/prof_tick.py 2>&1 | tail -20
```

（`/tmp/prof_tick.py` 为本计划调研阶段所用 JD cProfile 脚本；若已删，按 Task 1 调研记录重建：cProfile `run_detection(commodities="JD")`，按 cumulative 与 tottime 各输出 top 25。）

验收门槛：
- JD 总耗时 ≤ 90s（基线 267s，≥3× 提速）。
- `_attach_noise_history` 退出 tottime top 3。
- `attach_fair_price_metrics` cum 占比从 82% 降到 < 55%。

- [ ] **Step 5：大品种真数据 smoke（可选，确认无内存/正确性回归）**

```bash
./venv/bin/python run_tick_detector.py \
  --tick-day-path data/tick2026/202605/20260520 \
  --commodities AU --output-dir /tmp/perf-au-smoke
```

预期：完成、生成 `event_replay_AU.html` 与 CSV、AU2606 候选仍在（与 HANDOFF 记录的 20260520 候选一致）。

- [ ] **Step 6：记录验收结果**

把 JD 优化前后耗时、cProfile top 5、AU2606 锚点值写入提交信息：

```bash
git add -A
git commit -m "test(tick-detector): verify perf optimization (JD 267s-><Ns, golden+AU2606 green)" || echo "无改动"
```

---

## 6. 完成定义

以下条件同时满足才结束本阶段：

1. Task 1 的黄金安全网（合成 + JD 真数据）在最终代码上全 PASS。
2. AU2606 §10 设计锚点测试 PASS（数值不变）。
3. 全量 `tests/ -k "not slow_"` 全 PASS。
4. JD cProfile 总耗时 ≤ 90s（≥3× 提速）；`_attach_noise_history` 不再是头号热点。
5. 至少一个大品种（AU）真数据 smoke 跑通，候选与既有记录一致。

**不做的事（本计划范围外，需另立项）：**
- 不改检测算法、阈值常量、CSV/HTML 字段。
- 不做"只检测主力 N 合约"的业务级裁剪（会改变检测范围，需业务确认）。
- 不引入多进程/多线程、不引入 numba/cython 等新依赖。
- 不做 Pass 2 的 O(n log w) 滑动中位数结构（ceiling 已标注，留给后续若仍不够快）。
- 不做多日批量或批次输出自动合并（沿用 HANDOFF 的分批方案）。

**后续（不在本计划范围）：**
- 若业务确认可接受，做"主力 N 合约"裁剪，再叠加 2-3× 提速。
- 若 Pass 2 残余开销仍不可接受，引入滑动中位数结构。
- 跨 2-3 个交易日复核提速后的数值稳定性。
