# Tick 级乌龙指轻量检测器 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依据 `docs/superpowers/plans/2026-07-07-tick-fat-finger-light-detector-spec.md`，在当前分支 `v1.0.0` 上完成第一版 tick 级轻量检测器实现，跑通“单日 tick → 参考对齐 → 候选事件 → 回归标签 → HTML 复盘”，并在 `au2606_20260520.csv` 的 `2026-05-20 21:04:35.500` 标定样本锚点上完成真数据验收。

**Architecture:** 新增 `src/tick_detector/` 四模块：`tick_io.py`、`reference_selection.py`、`event_detection.py`、`report_html.py`，根目录新增 `run_tick_detector.py`。实现顺序严格按“CLI 骨架 → loader → 基础派生 → 参考对齐 → 事件合并 → 回归/HTML → 端到端/真数据验收”推进，不并行开大坑。

**Tech Stack:** Python 3 + pandas + numpy + stdlib（`zipfile`、`pathlib`、`dataclasses`、`re`）。测试统一走 `./venv/bin/python -m pytest`。

**Source of Truth:** 只以 `docs/superpowers/plans/2026-07-07-tick-fat-finger-light-detector-spec.md` 为实现口径；若与更早文档冲突，以该 spec 为准。

## Execution Rules

- **不改 scope**：只做 detector，不写交易统计、收益回测、推荐排序。
- **保留标定样本时段**：不能整体裁掉夜盘；`2026-05-20 21:04:35.500` 必须留在检测路径内。
- **同口径偏离**：主触发只认 `expected_price - target_mid`。
- **秒制 asof**：`lookback_seconds=3`、`max_reference_age_seconds=3`。
- **F1 回归必须锁死**：`主力连续` 文件与真实合约 `InstrumentID` 相同也不能双加载。
- **验收锚点固定**：`au2606_20260520.csv @ 2026-05-20 21:04:35.500`。
- 仓库当前已有未跟踪文档，**不要清理，不要回退**。
- 每个任务结束先跑对应测试，再 `git add` / `git commit`。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/tick_detector/__init__.py` | 空包标记 |
| `src/tick_detector/tick_io.py` | 枚举日目录/zip、合约识别、全日快照保留、基础派生 |
| `src/tick_detector/reference_selection.py` | tick_size、参考合约选择、simple/full expected price、age 校验 |
| `src/tick_detector/event_detection.py` | 候选过滤、事件合并、回归标签 |
| `src/tick_detector/report_html.py` | 单文件 HTML 复盘输出 |
| `run_tick_detector.py` | CLI 入口 |
| `tests/test_tick_io.py` | loader / session / F1 / 基础派生 |
| `tests/test_tick_detector_reference_selection.py` | 秒制 asof / full_blocked_reason / peer 指标 |
| `tests/test_tick_detector_event_detection.py` | 候选 / 合并 / truncated / recovery |
| `tests/test_run_tick_detector.py` | 端到端冒烟 |

---

### Task 0: 建立执行骨架

**Files:**
- Create: `src/tick_detector/__init__.py`
- Create: `run_tick_detector.py`
- Create: `tests/test_run_tick_detector.py`

- [ ] **Step 1: 先写 CLI 红灯测试**

覆盖：
- `--tick-day-path` 必填。
- `--commodity` / `--contract` 只控制目标过滤。
- `--output-dir` 缺省时能生成默认目录名。

- [ ] **Step 2: 实现最小 CLI**

约束：
- `main(argv=None) -> int`
- 参数解析后只调用一个顶层 `run_detection(...)`
- Task 0 只锁 CLI 壳与参数解析；测试必须限定为 argparse/默认值校验，不触发真实检测主流程，也不允许以 `NotImplementedError` 作为通过条件。

- [ ] **Step 3: 跑测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py -v
```

Commit:
```bash
git add src/tick_detector/__init__.py run_tick_detector.py tests/test_run_tick_detector.py
git commit -m "feat(tick-detector): add cli skeleton"
```

---

### Task 1: loader 与 F1 回归

**Files:**
- Create: `src/tick_detector/tick_io.py`
- Modify: `tests/test_tick_io.py`

**Interfaces:**
- `iter_day_contract_files(tick_day_path) -> Iterable[ContractFile]`
- `parse_contract_file(file_name, instrument_id) -> ContractInfo`
- `load_contract_snapshots(contract_file) -> pd.DataFrame`

- [ ] **Step 1: 先写失败测试**

必须覆盖：
- 已展开目录枚举 `*.csv`
- zip 根目录平铺枚举 `*.csv`
- `continuous_alias` 文件名优先识别
- 真实合约与 `主力连续` `InstrumentID` 相同、内容重复时，真实合约只加载一次
- 非 `^[a-zA-Z]+[0-9]{3,4}$` 文件不进 target/reference 池

- [ ] **Step 2: 实现最小 loader**

约束：
- 不全量解压 zip
- 目录和 zip 复用同一 `ContractFile` 抽象
- 首版只读 spec 需要的列；缺列直接报错

- [ ] **Step 3: 跑测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_io.py -v
```

Commit:
```bash
git add src/tick_detector/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick-detector): implement day file loader and F1 guard"
```

---

### Task 2: session 派生与基础派生

**Files:**
- Modify: `src/tick_detector/tick_io.py`
- Modify: `tests/test_tick_io.py`

**Interfaces:**
- `prepare_contract_snapshots(raw_df) -> pd.DataFrame`

- [ ] **Step 1: 先写失败测试**

必须覆盖：
- `session_state` / `is_tradable_session`
- 全日快照保留
- 同秒多快照按 `(timestamp, snapshot_seq)` 稳定排序
- `09:00` 首条 / session 切换后首条 / `timestamp` 差 `>60s` 首条的 `delta_*` 为缺失
- `mid_price` / `spread` / `snapshot_avg_trade_price`
- `AveragePrice ~= Turnover / Volume` sanity check 失败时只禁用均价触发

- [ ] **Step 2: 实现派生**

约束：
- 先全量派生 `session_state`、`night_session_coverage`，不整体裁掉夜盘
- `contract_multiplier` 先只内置 `AU: 1000`
- 未知 multiplier 不中断，只让 `snapshot_avg_trade_price` 为空

- [ ] **Step 3: 跑测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_io.py -v
```

Commit:
```bash
git add src/tick_detector/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick-detector): add full-day snapshots and derived fields"
```

---

### Task 3: 参考合约与秒制 asof

**Files:**
- Create: `src/tick_detector/reference_selection.py`
- Create: `tests/test_tick_detector_reference_selection.py`

**Interfaces:**
- `infer_tick_size(df) -> float | None`
- `select_reference_contracts(day_frames, target_contract, limit=5) -> list[str]`
- `attach_reference_metrics(target_df, reference_dfs, tick_size, lookback_seconds=3, max_reference_age_seconds=3) -> pd.DataFrame`

- [ ] **Step 1: 先写失败测试**

必须覆盖：
- `tick_size=0.02` 推导
- 同品种当日成交量前 5 参考合约
- `reference_contract_count < 2` 时核心指标为空
- asof 只取目标时点之前最近有效快照
- 同秒多快照 tiebreaker
- `peer_median_move_ticks` 与 `peer_move_limit_ticks=max(abs(peer_move_ticks_i))`
- `full_blocked_reason` 四种分支

- [ ] **Step 2: 实现 simple/full**

约束：
- `expected_price_simple` / `expected_price_full` 共用对齐机制
- full 版只多 age 校验，不额外发明权重或分支
- `down_deviation_ticks = (expected_price - target_mid) / tick_size`

- [ ] **Step 3: 跑测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -v
```

Commit:
```bash
git add src/tick_detector/reference_selection.py tests/test_tick_detector_reference_selection.py
git commit -m "feat(tick-detector): add reference alignment and expected price"
```

---

### Task 4: 候选过滤与事件合并

**Files:**
- Create: `src/tick_detector/event_detection.py`
- Create: `tests/test_tick_detector_event_detection.py`

**Interfaces:**
- `detect_candidate_ticks(df) -> pd.DataFrame`
- `merge_candidates(candidates, merge_window_seconds=10) -> pd.DataFrame`

- [ ] **Step 1: 先写失败测试**

必须覆盖：
- `visible_last_drop` 触发
- `last_vs_mid_down_ticks` 或 `peer_excess_down_ticks` 任一不足时不触发
- 10 秒窗口合并
- `event_time` 取最深 `event_depth_ticks`；并列时取最早 `(timestamp, snapshot_seq)`

- [ ] **Step 2: 实现事件逻辑**

约束：
- 主触发按 07-03 设计稿：
  - `delta_volume > 0`
  - `delta_volume >= 5`
  - `spread_ticks <= 30`
  - `reference_contract_count >= 2`
  - `LastPrice > LowerLimitPrice + 2 * tick_size`
  - `visible_last_drop` 或 `hidden_avg_trade_drop` 至少命中其一
- `last_vs_mid_down_ticks` / `peer_excess_down_ticks` / `snapshot_avg_trade_gap_ticks` 都进入触发判定
- `event_start_time` / `event_end_time` 分别取合并窗口首尾候选快照时间，供真数据验收判断锚点是否落在事件窗口内
- `event_low_price` / `event_volume` / `recovery_denominator_ticks` 按 spec 固定口径出

- [ ] **Step 3: 跑测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_detector_event_detection.py -v
```

Commit:
```bash
git add src/tick_detector/event_detection.py tests/test_tick_detector_event_detection.py
git commit -m "feat(tick-detector): add candidate detection and event merge"
```

---

### Task 5: 回归标签与 HTML 复盘

**Files:**
- Modify: `src/tick_detector/event_detection.py`
- Create: `src/tick_detector/report_html.py`
- Modify: `tests/test_tick_detector_event_detection.py`

**Interfaces:**
- `attach_recovery_metrics(events_df, contract_df, tick_size) -> pd.DataFrame`
- `render_event_replay_html(events_df, replay_payload) -> str`

- [ ] **Step 1: 先写失败测试**

必须覆盖：
- 10s / 30s quote/trade recovery
- `fast_trade_recovery` / `fast_quote_recovery` / `slow_recovery` / `no_recovery`
- recovery 不跨 session
- 命中断点标 `truncated`
- HTML 至少有事件表和前后 30 秒窗口明细

- [ ] **Step 2: 实现最小版本**

约束：
- 单文件 HTML
- 不用模板引擎，不加图表库
- 复盘窗口只做表格和少量内联样式

- [ ] **Step 3: 跑测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_detector_event_detection.py -v
```

Commit:
```bash
git add src/tick_detector/event_detection.py src/tick_detector/report_html.py tests/test_tick_detector_event_detection.py
git commit -m "feat(tick-detector): add recovery metrics and replay html"
```

---

### Task 6: 串联主流程与端到端冒烟

**Files:**
- Modify: `run_tick_detector.py`
- Modify: `tests/test_run_tick_detector.py`

- [ ] **Step 1: 先写端到端测试**

必须覆盖：
- `--commodity AU` 只过滤目标，不误删 AU peer
- 落出 `tick_events.csv` 与 `event_replay.html`
- `tick_events.csv` 至少包含：
  - `trade_date`
  - `commodity`
  - `contract`
  - `event_time`
  - `event_start_time`
  - `event_end_time`
  - `event_low_price`
  - `event_volume`
  - `event_depth_ticks`
  - `recovery_denominator_ticks`
  - `reference_contract_count`
  - `recovery_label`

- [ ] **Step 2: 串联完整流程**

顺序固定：
1. 枚举输入文件
2. 读取并预处理所有真实合约
3. 针对目标合约构造 reference metrics
4. 检测候选并合并事件
5. 计算 recovery
6. 输出 CSV / HTML

- [ ] **Step 3: 全量测试并提交**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_io.py tests/test_tick_detector_reference_selection.py tests/test_tick_detector_event_detection.py tests/test_run_tick_detector.py -v
```

Commit:
```bash
git add run_tick_detector.py src/tick_detector tests/test_run_tick_detector.py
git commit -m "feat(tick-detector): wire end-to-end detector pipeline"
```

---

### Task 7: 真数据验收 au2606_20260520

**Files:**
- No code changes required unless验收失败

- [ ] **Step 1: 跑真数据**

Run:
```bash
./venv/bin/python run_tick_detector.py --tick-day-path data/tick2026/202605/20260520.zip --commodity AU --output-dir output/tick_detector_20260520_au
```

- [ ] **Step 2: 核对锚点事件**

最小核对：
- 存在 `contract == AU2606` 的事件
- 至少一条事件满足：
  - `event_time` 距 `2026-05-20 21:04:35.500` 不超过 1 秒，或
  - `event_start_time <= 2026-05-20 21:04:35.500 <= event_end_time`
- `recovery_label != truncated`

建议核对命令：
```bash
./venv/bin/python - <<'PY'
import pandas as pd
df = pd.read_csv('output/tick_detector_20260520_au/tick_events.csv')
au = df[df['contract'] == 'AU2606'].copy()
print(au[['event_time','event_low_price','event_depth_ticks','reference_contract_count','recovery_label']])
PY
```

若要直接核对窗口覆盖锚点，使用：
```bash
./venv/bin/python - <<'PY'
import pandas as pd
anchor = pd.Timestamp("2026-05-20 21:04:35.500")
df = pd.read_csv('output/tick_detector_20260520_au/tick_events.csv', parse_dates=['event_time', 'event_start_time', 'event_end_time'])
au = df[df['contract'] == 'AU2606'].copy()
hit = au[
    ((au['event_time'] - anchor).abs() <= pd.Timedelta(seconds=1))
    | ((au['event_start_time'] <= anchor) & (au['event_end_time'] >= anchor))
]
print(hit[['event_time','event_start_time','event_end_time','event_low_price','event_depth_ticks','recovery_label']])
PY
```

- [ ] **Step 3: 验收失败处理规则**

若失败，按下面顺序排：
1. 先看 F1 是否双加载
2. 再看 session / 夜盘处理是否把锚点裁掉
3. 再看 age 校验是否过严导致 `full_blocked_reason` 全挡
4. 再看 `peer_move_limit_ticks` 是否把锚点误杀
5. 最后才调阈值

- [ ] **Step 4: 验收通过后提交**

```bash
git add src/tick_detector run_tick_detector.py tests
git commit -m "test(tick-detector): pass au2606 20260520 calibration anchor validation"
```

---

## Done Criteria

- `tests/test_tick_io.py`
- `tests/test_tick_detector_reference_selection.py`
- `tests/test_tick_detector_event_detection.py`
- `tests/test_run_tick_detector.py`

以上测试全部通过。

- 真数据 `au2606_20260520.csv @ 2026-05-20 21:04:35.500` 锚点事件命中。
- 输出目录包含 `tick_events.csv` 和 `event_replay.html`。
- 当前已验到的结果：`AU2606 @ 2026-05-20 21:04:35.500` 命中，`trigger_reasons=visible_last_drop`，`recovery_label=fast_trade_recovery`。
- 文档口径未被实现过程偷改；若需要改口径，先回改 spec，再改代码。

## Non-Goals

- 不实现收益回测
- 不实现成交模拟
- 不实现品种排序/推荐
- 不实现跨多日批量扫描
- 不引入数据库、配置中心、模板系统、前端图表库
