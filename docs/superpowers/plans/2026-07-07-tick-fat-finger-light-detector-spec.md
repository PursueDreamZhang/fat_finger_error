# Tick 级乌龙指轻量检测器 实现 Spec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this spec task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 `docs/superpowers/specs/2026-07-03-tick-fat-finger-trading-statistics-design.md` 的标定样本口径落地第一版 tick 级轻量检测器，只做“单日目录/zip → 疑似事件 → 人工复盘 HTML”，不做交易回测、收益统计、推荐排序。

**Architecture:** 新增 `src/tick_detector/` 薄包，拆成 `tick_io.py`、`reference_selection.py`、`event_detection.py`、`report_html.py` 四个模块；根目录提供 `run_tick_detector.py` CLI。保持“按天加载、按品种分组、按合约检测、最后汇总”的直线流程，不引入数据库、任务队列或额外配置系统。

**Tech Stack:** Python 3 + pandas + numpy + stdlib（`zipfile`、`pathlib`、`re`、`dataclasses`）。测试用 pytest，沿用仓库现有 `./venv/bin/python -m pytest`。

参考设计:
- `docs/superpowers/specs/2026-07-03-tick-fat-finger-trading-statistics-design.md`
- `docs/superpowers/specs/2026-07-06-tick-fat-finger-v1-minimal-slice-design.md`

口径优先级：
- 标定样本与验收锚点以 `2026-07-03-tick-fat-finger-trading-statistics-design.md` 为准。
- 若与 `2026-07-06-tick-fat-finger-v1-minimal-slice-design.md` 冲突，涉及标定样本 / 验收锚点 / 是否保留夜盘时，以 07-03 为准。

## Global Constraints

- 只做**向下异常检测**；向上异常、挂单模拟、平仓、收益口径全部跳过。
- `commodity` / `contract` 参数只过滤**检测目标**，不裁掉同品种参考合约加载。
- 连续合约不进检测池也不进参考池；识别优先看文件名中的 `主力连续|当月连续|下月连续|当季连续|下季连续|隔季连续`。
- 输入先支持“已展开日目录”；zip 读取作为同一套 loader 的兼容分支，不另起架构。
- 第一版按单日原始快照运行；标定样本位于夜盘 `21:04:35.500`，因此夜盘快照不能被整体裁掉。
- 事件输出只保留 `tick_events.csv` 和 `event_replay.html` 两个文件。
- `snapshot_seq` 以文件行序为准；不试图还原逐笔成交。
- 标定样本固定为 `au2606_20260520.csv` 的 `2026-05-20 21:04:35.500` 锚点 snapshot；Acceptance 与测试点围绕该唯一锚点定义。
- 所有阈值先做模块内常量；不提前抽象成配置中心。
- 方案文档口径以当前代码为准；若后续研究稿与 `src/tick_detector/reference_selection.py`、`src/tick_detector/event_detection.py` 冲突，以代码已落地口径为准。

## File Structure

| 文件 | 职责 |
|---|---|
| `src/tick_detector/__init__.py` | 空包标记 |
| `src/tick_detector/tick_io.py` | 日目录/zip 读取、文件识别、字段标准化、派生基础列 |
| `src/tick_detector/reference_selection.py` | 同品种参考合约筛选与 asof 对齐 |
| `src/tick_detector/event_detection.py` | 候选触发、事件合并、回归标签 |
| `src/tick_detector/report_html.py` | 生成 `event_replay.html` |
| `run_tick_detector.py` | CLI 入口 |
| `tests/test_tick_io.py` | loader 与基础派生测试 |
| `tests/test_tick_detector_reference_selection.py` | 参考合约与对齐测试 |
| `tests/test_tick_detector_event_detection.py` | 触发、合并、回归测试 |
| `tests/test_run_tick_detector.py` | 端到端冒烟 |

---

### Task 1: 建包并固化 CLI 骨架

**Files:**
- Create: `src/tick_detector/__init__.py`
- Create: `run_tick_detector.py`
- Create: `tests/test_run_tick_detector.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int`
- CLI args: `--tick-day-path`、`--commodity`、`--contract`、`--output-dir`

- [ ] **Step 1: 先写 CLI 冒烟测试**

覆盖点：
- 最小参数 `--tick-day-path` 可解析。
- 未传 `--output-dir` 时默认输出到 `output/<timestamp>-tick-detector/`。
- `--commodity` 与 `--contract` 可同时存在，但只影响目标过滤。

- [ ] **Step 2: 实现最小 CLI**

要求：
- 参数解析后调用单一顶层函数 `run_detection(...)`。
- 错误直接抛 `ValueError` / `FileNotFoundError`，不包一层复杂错误体系。

- [ ] **Step 3: 跑测试**

Run:
```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py -v
```

---

### Task 2: 输入读取与真实合约识别

**Files:**
- Create: `src/tick_detector/tick_io.py`
- Modify: `tests/test_tick_io.py`

**Interfaces:**
- Produces: `iter_day_contract_files(tick_day_path) -> Iterable[ContractFile]`
- Produces: `parse_contract_file(file_name: str, instrument_id: str | None) -> ContractInfo`
- Produces: `load_contract_snapshots(contract_file: ContractFile) -> pd.DataFrame`

- [ ] **Step 1: 写失败测试**

至少覆盖：
- 已展开目录可枚举 `*.csv`。
- zip 根目录平铺文件可枚举 `*.csv`。
- 连续合约按文件名识别为 `continuous_alias`。
- `au2606_20260520.csv` 这类真实合约能解析出 `commodity=AU`、`contract=AU2606`。
- 真实合约文件与 `主力连续` 文件 `InstrumentID` 相同、内容重复时，真实合约只加载一次，连续合约不进入 target/reference 池。
- 不符合 `^[a-zA-Z]+[0-9]{3,4}$` 的文件不进目标池。

- [ ] **Step 2: 实现 loader**

要求：
- 目录与 zip 共用一套 `ContractFile` 抽象，只存最少字段：`file_name`、`open_handle`、`source_type`。
- 首版只读取设计文档要求的字段；缺列直接报错，不做自动补列。
- 不全量解压 zip。

- [ ] **Step 3: 跑测试**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_io.py -v
```

---

### Task 3: 基础派生字段

**Files:**
- Modify: `src/tick_detector/tick_io.py`
- Modify: `tests/test_tick_io.py`

**Interfaces:**
- Produces: `prepare_contract_snapshots(raw_df: pd.DataFrame) -> pd.DataFrame`

返回列至少包含：
- `trade_date`
- `commodity`
- `contract`
- `snapshot_seq`
- `timestamp`
- `session_state`
- `is_tradable_session`
- `night_session_coverage`
- `LastPrice`
- `Volume`
- `Turnover`
- `BidPrice1`
- `AskPrice1`
- `mid_price`
- `spread`
- `delta_volume`
- `delta_turnover`
- `snapshot_avg_trade_price`

- [ ] **Step 1: 先写测试**

覆盖点：
- `snapshot_seq` 从 0 递增。
- 首条记录 `delta_volume` / `delta_turnover` 为缺失。
- 全日快照保留；同秒多快照按 `(timestamp, snapshot_seq)` 稳定排序。
- `session_state` 切换后首条、以及相邻快照 `timestamp` 差 `> 60s` 的首条，`delta_volume` / `delta_turnover` 为缺失。
- `delta_volume <= 0` 时不触发成交均价计算。
- `BidPrice1 <= 0`、`AskPrice1 <= 0`、`AskPrice1 < BidPrice1` 时 `mid_price` 置空。
- `AveragePrice ~= Turnover / Volume` 的 sanity check 失败时，标记“均价触发禁用”。

- [ ] **Step 2: 实现派生逻辑**

要求：
- `timestamp` 用 `TradingDay + UpdateTime + UpdateMillisec` 拼出 pandas 时间戳。
- 先基于全量原始行派生 `session_state` / `night_session_coverage`，不整体裁掉夜盘。
- 全日按 `(timestamp, snapshot_seq)` 排序；首条、`session_state` 切换后首条、以及长断点后首条的增量字段记为缺失。
- `contract_multiplier` 先用模块内最小表：`{"AU": 1000}`；未知品种允许继续，只是 `snapshot_avg_trade_price` 为空。

- [ ] **Step 3: 跑测试**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_io.py -v
```

---

### Task 4: tick_size 与参考合约选择

**Files:**
- Create: `src/tick_detector/reference_selection.py`
- Create: `tests/test_tick_detector_reference_selection.py`

**Interfaces:**
- Produces: `infer_tick_size(df: pd.DataFrame) -> float | None`
- Produces: `select_reference_contracts(day_frames: dict[str, pd.DataFrame], target_contract: str, limit: int = 5) -> list[str]`
- Produces: `attach_reference_metrics(target_df, reference_dfs, tick_size, lookback_seconds=3, max_reference_age_seconds=3) -> pd.DataFrame`

- [ ] **Step 1: 写失败测试**

覆盖点：
- `AU` 价格序列能推导出 `tick_size=0.02`。
- 参考合约取同品种当日成交量前 5，排除目标合约自身。
- `reference_contract_count < 2` 时候选指标为空。
- asof 对齐取“目标时点之前最近一条有效快照”。
- 同秒多快照按 `(timestamp, snapshot_seq)` 决定最近有效快照；`lookback_seconds=3` 与 `max_reference_age_seconds=3` 按秒校验，并验证 `peer_move_limit_ticks` 基于当前 tick 参与计算的 peers 聚合。

- [ ] **Step 2: 实现最小逻辑**

要求：
- 先复用当日真实成交量排序，够用就停，不做跨日活跃度体系。
- 核心输出口径固定为 `expected_price_simple`、`expected_price_full`、`down_deviation_ticks`、`down_deviation_bps`，其中 `down_deviation_ticks = (expected_price - target_mid) / tick_size`。
- `peer_median_move_ticks` 定义为当前 tick 参与计算的参考合约 `peer_move_ticks_i` 的中位数，用于构造 target 相对 peer 的偏离比较；`peer_move_limit_ticks` 定义为同一组 `peer_move_ticks_i` 的最大绝对值，即 `max(abs(peer_move_ticks_i))`，用于过滤“peer 自己也在大幅波动”的时点。
- `target_move_ticks` 必须使用目标合约 `mid_price` 相对 `lookback baseline mid_price` 的位移，不再用 `LastPrice` 位移替代，避免把单笔成交价偏离误算成目标整体位移。
- 参考合约只统计“当前点与 lookback 点都在可交易时段、且两个点都不超过 `3s` 新鲜度”的 peer；不满足条件的 peer 直接剔除，不再参与 `peer_median_move_ticks`、`expected_price_*` 与 `reference_contract_count`。
- `reference_contract_count` 表示“当前 tick 真正参与计算的 fresh peer 数”；若目标 baseline 不可用或 fresh peer 少于 2，相关参考指标直接留空。
- `full_blocked_reason` 当前已落地口径至少包含：`session_target_baseline`、`age_target_baseline`、`missing_target_baseline`、`insufficient_fresh_peers`、`not_blocked`。

- [ ] **Step 3: 跑测试**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -v
```

---

### Task 5: 候选触发与事件合并

**Files:**
- Create: `src/tick_detector/event_detection.py`
- Create: `tests/test_tick_detector_event_detection.py`

**Interfaces:**
- Produces: `detect_candidate_ticks(df: pd.DataFrame) -> pd.DataFrame`
- Produces: `merge_candidates(candidates: pd.DataFrame, merge_window_seconds: int = 10) -> pd.DataFrame`

默认阈值：
- `max_spread_ticks = 20`
- `strong_signal_max_spread_ticks = 25`
- `min_event_delta_volume = 10`
- `min_reference_contract_count = 2`
- `min_last_vs_mid_down_ticks = 20`
- `min_peer_excess_down_ticks = 20`
- `strong_signal_min_delta_volume = 50`
- `strong_signal_min_last_vs_mid_down_ticks = 50`
- `strong_signal_min_peer_excess_down_ticks = 10`
- `min_avg_trade_gap_ticks = 20`
- `limit_buffer_ticks = 2`
- `open_guard_seconds = 60`
- `open_guard_times = 09:00:00 / 09:30:00 / 21:00:00`

- [ ] **Step 1: 写失败测试**

覆盖点：
- `visible_last_drop` 分支在 `last_vs_mid_down_ticks` 与 `peer_excess_down_ticks` 同时达阈值时能触发候选。
- `strong_visible_last_drop` 分支能保留“成交量明显、成交价明显砸穿 mid、但 peer_excess 稍弱 / spread 略宽”的强信号标定样本。
- 任一核心阈值不满足时不会误触发。
- 开盘后首 60 秒内的候选不会误触发。
- 参考 peer 不新鲜或目标 baseline 不可用时不会误触发。
- 同一合约 10 秒内多个候选合并成一个事件。
- `event_time` 取 `event_depth_ticks` 最深的快照。

- [ ] **Step 2: 实现触发与合并**

要求：
- 基础过滤按当前落地口径：`delta_volume > 0`、`delta_volume >= 10`、`reference_contract_count >= 2`、`LastPrice > LowerLimitPrice + 2 * tick_size`，并排除开盘锚点 `09:00:00`、`09:30:00`、`21:00:00` 之后 `60s` 内的快照。
- 异常分支至少命中其一：
  - `visible_last_drop`: `last_vs_mid_down_ticks >= 20` 且 `peer_excess_down_ticks >= 20` 且 `spread_ticks <= 20`
  - `strong_visible_last_drop`: `delta_volume >= 50` 且 `last_vs_mid_down_ticks >= 50` 且 `peer_excess_down_ticks >= 10` 且 `spread_ticks <= 25`
  - `hidden_avg_trade_drop`: `snapshot_avg_trade_gap_ticks >= 20` 且 `snapshot_avg_trade_price` 非空
- `event_depth_ticks` 取合并窗口内最大的 `event_depth_ticks`；`event_low_price` 取合并窗口内最小 `LastPrice`；`event_volume` 取合并窗口内正 `delta_volume` 之和。
- `event_time` 取合并窗口内 `event_depth_ticks` 最深的快照时间；若并列，取 `(timestamp, snapshot_seq)` 最靠前的那条。
- `recovery_denominator_ticks = max(1, (event_reference_price - event_low_price) / tick_size)`，其中 `event_reference_price` 取事件锚点 `mid_price`。
- `trigger_reasons` 只记录命中的展示分支名，逗号拼接即可；不改变主触发判定。

- [ ] **Step 3: 跑测试**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_detector_event_detection.py -v
```

---

### Task 6: 回归标签与 HTML 复盘

**Files:**
- Modify: `src/tick_detector/event_detection.py`
- Create: `src/tick_detector/report_html.py`
- Modify: `tests/test_tick_detector_event_detection.py`

**Interfaces:**
- Produces: `attach_recovery_metrics(events_df, contract_df, tick_size) -> pd.DataFrame`
- Produces: `render_event_replay_html(events_df, replay_payload) -> str`

- [ ] **Step 1: 先写测试**

覆盖点：
- 10 秒 / 30 秒窗口的报价回归与成交回归都能算。
- `fast_trade_recovery`、`fast_quote_recovery`、`slow_recovery`、`no_recovery` 四类标签互斥。
- 回归窗口不跨 session 断点；跨断点事件标记为 `truncated`。
- HTML 至少包含事件基础表和事件前后窗口明细。

- [ ] **Step 2: 实现最小版本**

要求：
- HTML 直接输出单文件，不接模板引擎。
- 复盘窗口按事件前后各 30 秒截取，够人工核对即可。
- 不加图表库；表格 + 少量内联样式就够。
- 回归窗口命中午休或收盘断点时立即截断；被截断事件保留 `truncated` 标签，不继续跨 session 取样。

- [ ] **Step 3: 跑测试**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_detector_event_detection.py -v
```

---

### Task 7: 端到端串联与输出落盘

**Files:**
- Modify: `run_tick_detector.py`
- Modify: `tests/test_run_tick_detector.py`

**Interfaces:**
- Produces: `tick_events.csv`
- Produces: `event_replay.html`

- [ ] **Step 1: 先写端到端测试**

覆盖点：
- 指定 `--commodity AU` 时，仍会加载 AU 其他真实合约做参考。
- 输出目录中存在 `tick_events.csv` 与 `event_replay.html`。
- `tick_events.csv` 至少包含 `trade_date`、`commodity`、`contract`、`event_time`、`event_low_price`、`event_volume`、`event_depth_ticks`、`recovery_denominator_ticks`、`reference_contract_count`、`recovery_label`。

- [ ] **Step 2: 串联主流程**

顺序固定：
1. 枚举输入文件
2. 读取并预处理所有真实合约
3. 按目标合约构建参考指标
4. 检测并合并事件
5. 计算回归标签
6. 落 CSV 和 HTML

- [ ] **Step 3: 全量回归**

Run:
```bash
./venv/bin/python -m pytest tests/test_tick_io.py tests/test_tick_detector_reference_selection.py tests/test_tick_detector_event_detection.py tests/test_run_tick_detector.py -v
```

---

## Acceptance

- 能读取某个单日目录或 zip。
- 能按 `commodity` / `contract` 限定检测目标，但不误删参考合约。
- 能输出 `tick_events.csv`，每行一个合并后的疑似事件。
- 能输出 `event_replay.html`，展示事件前后窗口、盘口、成交增量与回归标签。
- 在标定样本 `au2606_20260520.csv` 上，能抓到锚点 `2026-05-20 21:04:35.500` 对应事件。
- 上述命中至少满足其一：`event_time` 距 `2026-05-20 21:04:35.500` 不超过 1 秒，或该事件合并窗口包含该锚点 snapshot。
- 当前实现已通过真数据验收：`output/review_AU/tick_events.csv` 与 `output/tick_detector_20260520_all/tick_events.csv` 中都保留 `AU2606 @ 2026-05-20 21:04:35.500` 事件，`trigger_reasons=strong_visible_last_drop`，`recovery_label=fast_trade_recovery`。
- 当前实现的对抗式整改结论是：`LC` 等此前明显误报样本已被压掉；按 `2026-05-20` 全天全品种全量重跑，当前只保留 1 笔事件，即上述 `AU2606` 标定样本。

## Non-Goals

- 不做跨多日批量扫描。
- 不做收益统计、成交模拟、手续费、胜率。
- 不做品种排序、月份桶、机会推荐。
- 不做逐笔成交复原。
