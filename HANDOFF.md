# 全品种 Tick 乌龙指检测交接

更新日期：2026-07-15

## 1. 当前在做什么

本项目是期货品种乌龙指的日线初筛与 Tick 级候选复盘工具。本轮工作的目标是：

1. 为可从 `20260520` Tick 数据自动推导参数的品种建立实验参数档。
2. 对 81 个已审核的实验品种，在 `data/tick2026/202605/20260520` 与 `20260519` 两个目录分别执行全品种 Tick 候选检测。
3. 每次按 10 个品种分批执行，保留进度日志和单品种 HTML 复盘报告。

这是“疑似候选”筛选，不是交易所错单确认。后续人工复盘必须结合逐笔、盘口和交易所数据。

## 2. 已完成事项

### 2.1 参数推导与品种覆盖

- 新增 `scripts/derive_commodity_profiles.py`：从指定 Tick 日自动推导品种的最小变动价位和合约乘数。
- 在 `20260520` 数据上得到 81 个可用实验参数，已写入 `src/tick_detector/tick_io.py` 的 `COMMODITY_PROFILES`，档位为 `AUTO_INFERRED_V1`、状态为 `validated`。
- 未能自动补齐的 6 个品种：`BB、JR、PM、RI、WH、ZC`。它们在 `20260520` 的代表合约没有可用成交量/成交额增量；检查主连及 `20260519` 后也无法形成可靠参数。因此本轮全量运行明确排除这 6 个品种。

### 2.2 检测器与批处理能力

- `run_tick_detector.py` 支持 `--commodities A,AG,...`，可一次按多个品种运行；会按品种分别生成 `event_replay_<品种>.html`，并输出合并候选 CSV。
- 新增 `scripts/run_all_commodities_batched.sh`：81 个品种分为 9 批（前 8 批各 10 个，最后一批 ZN），输出 `progress.log`，支持 `START_BATCH=<n>` 从某批继续。
- 修复 `_collapse_same_time_key()`：前面出现重复时间键后，后续单行的 `snapshot_seq_start/end` 曾会变成 `NaN`，触发 `ValueError: cannot convert float NaN to integer`。现在对已有列以 `snapshot_seq` 回填缺失值。
- 相关回归测试已通过：

```bash
./venv/bin/python -m pytest tests/test_tick_io.py tests/test_tick_detector_event_detection.py tests/test_run_tick_detector.py -q -k 'not slow_'
# 结果：60 passed, 1 deselected
```

### 2.3 两个日期目录的全量运行

两个目录都已生成 81 份单品种 HTML 报告，均为完整完成状态：

| Tick 目录 | 输出目录 | 完成情况 |
|---|---|---|
| `data/tick2026/202605/20260520` | `output/full-20260520-experimental-81-run2/` | 81/81，日志显示 2026-07-14 15:34:02 全部 9 批完成 |
| `data/tick2026/202605/20260519` | `output/full-20260519-experimental-81/` | 81/81，日志显示 2026-07-15 11:01:07 全部 9 批完成 |

### 2.4 已做的结果汇总

HTML 报告是全量统计的权威来源（见“踩坑”）。解析 81 份 HTML 得到：

| 指标 | 20260519 目录 | 20260520 目录 |
|---|---:|---:|
| 候选事件数 | 273 | 242 |
| 涉及品种数 | 25 | 27 |
| 3 秒内成交恢复 | 217 | 205 |
| 仅行情恢复 | 39 | 34 |
| 10 秒仍未恢复 | 16 | 0 |

重点结论：

- `20260519`：`PX` 56、`TA` 53、`SH` 25、`MA` 18、`PF` 17、`BU` 16。16 个 `persistent_10s` 全集中在 `BU2612` 的 `21:09–21:11`，应视为一个连续异常区间，而不是 16 个独立事件。
- `20260520`：`SR` 30、`OI` 26、`PX` 22、`TA` 22、`CF` 20、`SH` 19、`AP` 14。
- 两日均值得优先人工复核的成交恢复且大区间偏离事件：`OI609`、`SR609`、`AP610`、`PF607`、`PX607`。
- 夜盘会使报告内 `交易日` 早于输入目录日期：例如 `20260520` 目录包含 `20260519/20260520` 事件。这是正常的交易时段归属，不是错读数据。
- 不能仅按“偏离跳数”跨品种比较严重性，因为不同品种的 tick 和合约乘数不同；先在同一品种内排序，再结合事件成交量和恢复标签。

## 3. 当前是否卡住

没有运行阻塞：两日 81 品种批处理都已完成。

当前真正待做的是人工分析，而不是继续跑批：

1. 复核 `BU2612` 在 20260519 目录中的连续 `persistent_10s` 区间。
2. 对 `OI609/SR609/AP610/PF607/PX607` 的高优先级事件查看对应 HTML 详情，并结合逐笔或盘口验证。
3. 视人工复核结果决定是否调整阈值、事件合并规则，或为 6 个缺失品种补充权威参数。

## 4. 推荐下一步

### 若要人工复盘

从以下报告开始：

- `output/full-20260519-experimental-81/batch-2/event_replay_BU.html`
- `output/full-20260519-experimental-81/batch-5/event_replay_OI.html`
- `output/full-20260520-experimental-81-run2/batch-7/event_replay_SR.html`
- `output/full-20260520-experimental-81-run2/batch-1-resume/event_replay_AP.html`
- `output/full-20260520-experimental-81-run2/batch-5-resume/event_replay_PF.html`
- `output/full-20260520-experimental-81-run2/batch-6/event_replay_PX.html`

人工复核优先顺序：

1. `trade_recovered_3s` 且同品种内区间偏离较大的事件。
2. `persistent_10s` 连续区间（特别是 BU2612）。
3. `quote_only_recovered_3s`：优先级降低，因为窗口内没有观察到成交恢复。
4. `truncated`：需先补后续窗口数据，暂不能定性。

### 若要再跑一个日期

沿用同一 81 品种和独立输出目录：

```bash
bash scripts/run_all_commodities_batched.sh \
  data/tick2026/YYYYMM/YYYYMMDD \
  output/full-YYYYMMDD-experimental-81
```

如某批已完成，从后续批次恢复：

```bash
START_BATCH=4 bash scripts/run_all_commodities_batched.sh \
  data/tick2026/YYYYMM/YYYYMMDD \
  output/full-YYYYMMDD-experimental-81
```

如果在某一批中途停止，**不要**直接用 `START_BATCH` 重跑整个批次；先保留已经生成的 `event_replay_<品种>.html`，只用 `run_tick_detector.py --commodities <剩余品种>` 补跑剩余品种，再从下一完整批次继续。

## 5. 绝对不要再踩的坑

1. **不能把“81 个参数档”理解为所有品种都有可靠人工验证。** 当前 81 个是由 20260520 自动推导、用户同意用于实验的参数；6 个缺失品种没有可靠参数，不能默默补默认值或标记为 validated。
2. **不要把旧版脚本当主链路。** 当前入口是 `run_tick_detector.py`，核心实现在 `src/tick_detector/`。
3. **不要用 `./venv/bin/pytest`。** 此仓库该脚本 shebang 指向旧路径；统一用 `./venv/bin/python -m pytest`。
4. **不要以批次 CSV 作为全量事实来源。** 20260520 的批次 1 与批次 5 都发生过中断后恢复：
   - `batch-1` 只保留 A/AD/AG/AL/AO 的 HTML，`batch-1-resume` 有 AP/AU/B/BC/BR 的 CSV；
   - `batch-5` 只保留 NI/NR/OI/OP 的 HTML，`batch-5-resume` 有 P/PB/PD/PF/PG/PK 的 CSV。
   因此需要全量统计时，解析所有 `event_replay_*.html` 中的“候选事件总览”；本轮 273/242 统计就是这样得到的。
5. **不要在中断后盲目从批次开头重跑。** 会浪费大量时间，也会覆盖或混淆产物。按“已完成 HTML + 剩余 commodities”做精确恢复。
6. **不要误判运行已完成。** 必须同时检查 `progress.log` 中的 `全部 9 批完成` 和 `event_replay_*.html` 数量为 81；日志可能不含恢复批次，所以要以实际报告数交叉验证。
7. **不要把候选当作已确认乌龙指。** 报告本身明确说明聚合 Tick 无法还原逐笔最低价；候选仅用于缩小人工复核范围。
8. **不要跨品种直接比较“跳数”。** 同一数值在不同品种的经济意义不同；比较时需结合 tick、乘数、成交量和恢复状态。
9. **不要依赖一次性子代理来做长期监控。** 它不能稳定跨会话等待。若还需定时检查，应使用 Codex heartbeat/automation；任务完成后应删除自动化，避免持续打扰。

## 6. 关键文件

- `run_tick_detector.py`：Tick 检测 CLI，支持 `--commodity`、`--commodities`。
- `src/tick_detector/tick_io.py`：81 个实验参数档，以及同时间键合并逻辑。
- `scripts/derive_commodity_profiles.py`：参数推导。
- `scripts/run_all_commodities_batched.sh`：9 批运行与 `START_BATCH` 恢复。
- `tests/test_derive_commodity_profiles.py`
- `tests/test_tick_io.py`
- `tests/test_run_tick_detector.py`
- `docs/superpowers/plans/2026-07-13-full-day-all-commodities-batch-run.md`：执行计划与审查记录。
