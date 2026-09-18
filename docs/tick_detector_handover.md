# 聚合 Tick 乌龙指候选检测器 — 代码交接文档

> 本文档面向接手继续开发/跑批的人（或新会话），描述**当前实际生效的实现**：代码在哪、怎么跑、输出什么、当前进度到哪。
> 若只需理解模型、判定与日常使用，请先读 [主 Tick 乌龙指候选检测器：模型与使用说明](main_tick_detector_model_and_usage.md)。
> 算法口径（fair_price、噪声阈值、双通道、恢复规则等）的推导见设计文档，本文不重复：
> - [docs/superpowers/specs/2026-07-10-aggregated-tick-fat-finger-detector-design.md](docs/superpowers/specs/2026-07-10-aggregated-tick-fat-finger-detector-design.md)（主设计）
> - [docs/superpowers/plans/2026-07-15-tick-detector-performance-optimization-reviewed.md](docs/superpowers/plans/2026-07-15-tick-detector-performance-optimization-reviewed.md)（性能优化计划）

这是 **tick 级**初筛器，和仓库里另一套**日线级**初筛器（`run_daily_screen.py` + `src/daily_screen/`，交接文档见 [docs/daily_screen_code_handover.md](docs/daily_screen_code_handover.md)）是两套独立东西，别搞混。

---

## 1. 一句话定位

输入某交易日全市场 tick 快照，逐合约筛查「疑似乌龙指」候选事件，输出每个事件的复盘 HTML + 候选事件 CSV。**输出是初筛候选，不是已确认的交易所错单**，需人工结合分时/逐笔/盘口复核。

---

## 2. 代码地图

入口在仓库根目录，核心在 `src/tick_detector/`：

| 文件 | 职责 | 关键函数 |
|---|---|---|
| [run_tick_detector.py](run_tick_detector.py) | 入口、编排、事件字段收尾、HTML payload 构建 | `run_detection()`、`_detect_contract()`、`_build_replay_payload()`、`_build_target_detail()`、`_build_peer_raw_windows()`、`_finalize_event_fields()` |
| [src/tick_detector/tick_io.py](src/tick_detector/tick_io.py) | 读 CSV / 归一化交易时钟 / 合并同时间键 / 差分 / interval_vwap / 时段 / 开盘保护 | `iter_day_contract_files()`、`load_contract_snapshots()`、`prepare_contract_snapshots()`；`COMMODITY_PROFILES`（tick_size、multiplier、validation_status） |
| [src/tick_detector/reference_selection.py](src/tick_detector/reference_selection.py) | 选参考合约、fair_price（Pass 1）、噪声历史与阈值（Pass 2）、peer asof 对齐 | `select_reference_contracts()`、`attach_fair_price_metrics()`、`_build_peer_aligned()`、`_attach_noise_history()` |
| [src/tick_detector/event_detection.py](src/tick_detector/event_detection.py) | 候选检测（visible/interval 双通道 + onset）、事件合并、恢复判定 | `detect_candidate_ticks()`、`merge_candidates()`、`attach_recovery_metrics()`、`_compute_1s_confirmation()` |
| [src/tick_detector/report_html.py](src/tick_detector/report_html.py) | 中文 HTML 渲染（诊断表 / 事件总览 / 每事件复盘弹窗） | `render_event_replay_html()`；列定义 `EVENT_SUMMARY_COLUMNS` / `TARGET_DETAIL_COLUMNS` / `PEER_RAW_COLUMNS` |

脚本与测试：

| 文件 | 用途 |
|---|---|
| [scripts/run_day_parallel.sh](scripts/run_day_parallel.sh) | **全品种并行跑批（B 方案，日常跑全量用这个）** |
| [scripts/profile_tick_detector.py](scripts/profile_tick_detector.py) | 各阶段耗时 profiling（需 `PYTHONPATH=.`） |
| [tests/test_tick_detector_perf_golden.py](tests/test_tick_detector_perf_golden.py) | 合成 + JD 真数据 golden 等价测试（正确性基线） |
| [tests/test_run_tick_detector.py](tests/test_run_tick_detector.py) | 含 AU2606 设计标定锚点慢测试 |
| [tests/test_tick_io.py](tests/test_tick_io.py) / [tests/test_tick_detector_reference_selection.py](tests/test_tick_detector_reference_selection.py) / [tests/test_tick_detector_event_detection.py](tests/test_tick_detector_event_detection.py) | 各模块单测 |

---

## 3. 核心调用链（单个目标合约）

```
run_detection()                              # run_tick_detector.py，按品种→按目标合约循环
  ├─ prepare_contract_snapshots(raw)         # tick_io：生成 market_time_key、合并同键、delta_volume/turnover、interval_vwap、session、开盘保护
  ├─ 目标资格门槛                          # 目标外少于 3 个真实同品种合约 → 直接跳过
  ├─ select_reference_contracts(day_frames)  # 同品种成交量前 5 候选，前三用于有效性门槛
  ├─ attach_fair_price_metrics(target, peers)# reference_selection：
  │     ├─ Pass 1：逐行 fair_price（peer asof mid + basis 滑动中位数）
  │     │          至少 2 个有效 peer，且至少命中成交量前三中的 1 个
  │     └─ Pass 2：[t-300s, t-10s] 噪声历史 → last/vwap 阈值
  ├─ detect_candidate_ticks(enriched)        # event_detection：visible/interval 双通道 + onset 突发性
  ├─ merge_candidates(candidates, enriched)  # 10s 内同合约候选合并，取深度最大者为锚点
  ├─ attach_recovery_metrics(events, ...)    # 冻结 basis，看 3s/10s 内各通道是否回归
  ├─ _finalize_event_fields(events, ...)     # 补展示字段（偏离基点、名义缺口等）
  ├─ _build_replay_payload(...)              # 锚点±10s 窗口 → 目标明细 + 参考合约原始快照
  └─ render_event_replay_html(...)           # report_html：渲染诊断表 + 总览 + 每事件弹窗
```

> **时间契约**：全链路只用 `market_time_key`（毫秒，交易日周期内单调、正确跨午夜 21:xx→00:xx）。自然日 `timestamp`/`UpdateTime` 只用于生成展示文本，不参与排序/窗口/asof。

---

## 4. 数据

- 路径：`data/tick2026/{YYYYMM}/{YYYYMMDD}/`（解压后的目录，每天全市场合约）或同名 `.zip`（loader 都支持）。
- 每个文件 = 一个合约×一天的 tick 快照 CSV。字段：`TradingDay, InstrumentID, UpdateTime, UpdateMillisec, LastPrice, Volume, BidPrice1, BidVolume1, AskPrice1, AskVolume1, AveragePrice, Turnover, OpenInterest, UpperLimitPrice, LowerLimitPrice, ...`。
- `COMMODITY_PROFILES`（tick_io.py）给每个品种的 `tick_size`、`contract_multiplier`、`validation_status`。**只有 `validated` 品种会真正检测**，其余只加载不检测（不报错）。
- 目前 81 个 validated 品种。一个交易日约 980 个合约、1.7GB。

---

## 5. 怎么跑（回测/跑批）

> 所有命令在仓库根目录执行。`run_tick_detector.py` 在根目录，直接跑 `from src...` 没问题；`scripts/` 下的脚本 import 根目录模块，需 `PYTHONPATH=.`。

### 5.1 单品种 / 单合约（调试、看某个事件）

```bash
# 单合约（最快，只检测 AU2606，但会加载 AU 全部合约做 peer）
PYTHONPATH=. ./venv/bin/python run_tick_detector.py \
  --tick-day-path data/tick2026/202605/20260520 \
  --commodity AU --contract AU2606 \
  --output-dir output/au2606-test

# 单品种（检测该品种全部合约）
PYTHONPATH=. ./venv/bin/python run_tick_detector.py \
  --tick-day-path data/tick2026/202605/20260520 \
  --commodity JD \
  --output-dir output/jd-test
```

CLI 参数：`--tick-day-path`（必填）、`--commodity` / `--commodities`（逗号分隔）/ `--contract`、`--output-dir`、`--only-with-events`（无事件品种不输出 HTML/CSV）。

### 5.2 全品种并行跑批（B 方案，跑整天用这个）

```bash
# 用法：scripts/run_day_parallel.sh <tick_day_path> [总进程预算=10] [品种内进程数=2]
#      [--commodities AU,AG] [--output-root output]
bash scripts/run_day_parallel.sh data/tick2026/202605/20260520 10 2

# 只跑指定品种，并把结果写到独立目录
bash scripts/run_day_parallel.sh \
  data/tick2026/202605/20260520 10 2 \
  --commodities AU,AG --output-root output
```

- 自动算出该目录下 validated∩present 的品种；总进程预算由 `xargs` 控制，品种内目标合约由 `target_workers` 控制。
- 默认带 `--only-with-events`：**只有命中事件的品种才出 HTML/CSV**，空品种目录跑完自动清掉。
- 输出在 `<output-root>/<day>-par/`：每个有事件的品种一个子目录（`event_replay_{品种}.html` + `tick_candidate_events.csv`），根目录还有合并 CSV、`batch_status.json`、`batch_summary.html` 和各品种日志。
- `--tick-day-path` 支持解压目录或同名 `.zip` 文件。

### 5.3 测试

```bash
# 常规套件（快，含合成 golden；~3 秒）
./venv/bin/python -m pytest tests/ -q -k 'not slow_'

# JD 真数据 golden（慢，~85 秒，逐列等价基线）
./venv/bin/python -m pytest tests/test_tick_detector_perf_golden.py::test_slow_jd_enriched_and_event_outputs_match_golden

# AU2606 设计标定锚点（慢，~25 秒）
./venv/bin/python -m pytest tests/test_run_tick_detector.py::test_slow_au2606_real_anchor_210435_hits_two_reasons_and_matches_design
```

> `slow_` 是按测试名前缀过滤（不是 pytest marker）。改了检测逻辑后，至少跑前两个确认没回归。

### 5.4 性能 profiling

```bash
PYTHONPATH=. ./venv/bin/python scripts/profile_tick_detector.py \
  data/tick2026/202605/20260520 --commodity JD --output-dir output/prof-jd
# 看 [timing] totals 段：各阶段累计耗时
```

---

## 6. 输出结构

单次 `run_detection`（可多品种）：
- **HTML 增量写**：每品种跑完即写 `event_replay_{品种}.html`。
- **CSV 末尾写一次**：全部品种跑完后合并写 `tick_candidate_events.csv`（单品种进程时 = 该品种自己的事件）。
- 单品种时额外复制一份 `event_replay.html`（冗余，和 `event_replay_{品种}.html` 相同）。
- `--only-with-events`：品种 0 事件 → 跳过其 HTML；全部 0 事件 → 跳过 CSV。
- 若某品种当天有效真实合约少于 4 个（目标自身加至少 3 个其他合约），该品种所有目标直接跳过；这不是“无事件”，而是未满足参考合约前置条件。

CSV 列定义在 [run_tick_detector.py](run_tick_detector.py) 的 `CSV_COLUMNS`（中英文映射）。HTML 列定义在 [report_html.py](src/tick_detector/report_html.py) 的几个 `*_COLUMNS` 常量。

**最近改过的 HTML 口径（2026-07-17）**：
- 复盘窗口 = **锚点前 10 秒 ~ 后 10 秒**（原来是前 60 秒）。
- 目标检测明细现在能显示 `合理价 / 末笔向下偏离_跳 / 区间均价向下偏离_跳`（原先空白：复盘窗口原先取 prepared 帧没有这三列，改成取检测后全行帧 `detect_candidate_ticks(enriched, return_marked=True)`）。
- 参考合约原始快照加了 `区间增量成交量 / 区间增量成交额 / 区间成交均价` 三列。

---

## 7. 性能特性 & 重要注意事项

- **瓶颈在 `attach_fair_price_metrics`（约占 95%）**：Pass 1 逐行调 `np.percentile`/`np.median`/`_mad_sigma`（小数组、GIL-bound）。`prepare` 已向量很快。单合约耗时与 tick 行数强相关，前几个大合约占大头。
- **并行必须用进程，不能用线程**：Pass 1/2 是逐行 Python 循环，线程会被 GIL 串起来几乎不加速。B 方案用 `xargs -P`（多进程）才有 ~7×。
- **`scripts/` 下脚本要 `PYTHONPATH=.`**（它们 import 根目录的 `run_tick_detector` / `src`）。
- **未审核品种**：`--commodity` 指定未 validated 品种会 `raise ValueError`；B 脚本已自动过滤成 validated∩present，直接给整目录即可。
- **参考合约门槛**：先按目标之外的全天成交量取前 5 个候选；每个时点最终有效 peer 必须至少 2 个，且至少有 1 个属于候选中的成交量前三，否则该行标记 `insufficient_peers`，不生成 `fair_price` 或候选事件。
- **参考价新鲜度**：peer 的 as-of 快照默认必须在目标时刻前 `3s` 内；该限制同时影响合理价和恢复计算。
- **数值口径**：「N 跳」= N 个最小变动价位（tick_size）。`末笔向下偏离_跳 = (fair_price − LastPrice)/tick_size`，`区间均价向下偏离_跳 = (fair_price − interval_vwap)/tick_size`。阈值（`*_threshold_ticks`）也是按跳算，与偏离同单位直接可比。

---

## 8. 正确性基线（改代码前必看）

- golden 测试用 **rtol=1e-6, atol=1e-9, equal_nan=True** 逐列比 enriched frame + 事件 CSV。通过 = 输出与基线逐条一致。
- golden fixtures（`tests/fixtures/golden_*.json/csv`）是**性能优化前（phase 0）**录的基线。**只有检测口径有意改变时才重录**；纯重构/性能优化必须保持 fixtures 不变并通过测试。
- AU2606 锚点测试锁死设计文档 §10 的标定值（fair_price≈995.33、区间均价≈940.52、回归=trade_recovered_3s）。

---

## 9. 当前进度（截至 2026-08-03）

**性能优化计划**（[plan](docs/superpowers/plans/2026-07-15-tick-detector-performance-optimization-reviewed.md)）：
- 阶段 1–4 已完成：`tick_io`（market_time_key / 开盘保护 / 同键合并 / 差分数组化）+ `reference_selection`（peer asof searchsorted、Pass 2 噪声向量化、Pass 1 输出预分配）。**JD 端到端 3.8×（~146s→~38s），零输出差异**。
- 阶段 5（排序/缓存复用）：profiling 显示排序仅占 0.27%，**按门控不做**。
- 阶段 6 验收通过（fast 133 passed + JD golden + AU2606 锚点）。
- 阶段 7（主力合约范围实验，`--target-limit N`）：**未做**，属业务覆盖实验，独立任务。

**已落地的工程改动**：
- B 方案并行跑批脚本 [scripts/run_day_parallel.sh](scripts/run_day_parallel.sh)（日常跑全天用这个）。
- `--only-with-events` flag（无事件品种不输出）。
- 参考合约门槛：目标外至少 3 个真实合约、至少 2 个有效 peer，且有效 peer 命中成交量前三。
- HTML 口径调整（见第 6 节）。

**已有结果**：20260515 / 20260518 / 20260519 / 20260520 四个交易日有完整 81 品种检测结果（`output/<day>-par/` 或 `output/full-<day>-experimental-81*/`）。每天约 200–270 个候选事件、命中 21–23 个品种。

---

## 10. 接手后的典型动作

- **跑某天全量**：`bash scripts/run_day_parallel.sh data/tick2026/202605/<day> 8`，等 ~25 分钟，看 `output/<day>-par/tick_candidate_events.csv`。
- **调检测参数**：阈值/窗口常量在 `reference_selection.py` 和 `event_detection.py` 顶部（`BASELINE_WINDOW_SECONDS=300`、`NOISE_K=8`、`MAX_REFERENCE_AGE_SECONDS=3`、`MERGE_WINDOW_SECONDS=10` 等）。改完**必须**跑 golden 测试确认是否需要重录 fixtures。
- **加 HTML 列**：同时改 `report_html.py` 的 `*_COLUMNS` 和 `run_tick_detector.py` 里对应 `_build_*` 的字段列表（两边要对齐）。
- **新一个品种上线**：在 `COMMODITY_PROFILES`（tick_io.py）补 `tick_size` / `contract_multiplier` / `validation_status="validated"`，并用真数据跑 golden 比对。

## 11. 交给其他 AI 的最短执行流程

1. 确认输入是某一个交易日的全市场 tick 目录或 `.zip`，不要只提供目标合约文件；参考合约必须同时加载。
2. 先跑单合约验证：

   ```bash
   PYTHONPATH=. ./venv/bin/python run_tick_detector.py \
     --tick-day-path data/tick2026/YYYYMM/YYYYMMDD \
     --commodity AU --contract AU2606 \
     --output-dir output/ai-check-AU2606
   ```

3. 先查看 `event_replay.html` 的“合约运行诊断”和“候选事件总览”，再打开事件详情；没有事件时先区分“参考合约不足 / 历史噪声不足 / 确实无候选”。
4. 批量任务使用 `scripts/run_day_parallel.sh`，必须给新的 `--output-root`，不要覆盖已有复盘结果。
5. 任何结果都只能称为“疑似候选事件”；不能直接表述为交易所已经确认的乌龙指。

## 12. 当前检测方向与下游边界（v2）

- 当前主检测器版本为 `aggregated-tick-v2`：`detect_candidate_ticks(..., return_marked=True)` 先生成完整双向标记帧，`extract_candidate_ticks()` 再按 `event_direction=down/up` 展开候选，随后按方向独立合并与恢复。
- CSV、单品种复盘 HTML 和全日批量汇总同时统计 `down` 与 `up`。向下偏离沿用旧字段；新增向上末笔/区间/一秒合并偏离、卖一恢复秒数和名义成交额超额。机器方向值固定为 `down`、`up`。
- 同一时间键若末笔和区间均价分别命中相反方向，会输出两个事件；不能因相同时间键互相覆盖。
- 恢复判断中向下检查 `BidPrice1`，向上检查 `AskPrice1`；合理价、peer 新鲜度、冻结 basis 和 3 秒/10 秒窗口不变。
- 低侧的参数生成、手工回放、程序化单次/网格回放、旧确认深度迁移会校验方向，只处理 `down`。旧 CSV 缺少 `异常方向` 时按历史兼容为 `down`；非法非空方向会报错，手工回放的 `up` 会写入排除清单。
- Mid + LastPrice 分析器和日线初筛不属于这条主检测链，本次没有修改。
