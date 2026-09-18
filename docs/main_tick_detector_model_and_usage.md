# 主 Tick 乌龙指候选检测器：模型与使用说明

> 当前实现版本：`aggregated-tick-v2`。本文是日常查阅入口；它描述的是**逐 Tick 的疑似候选初筛**，不把事件表述为已经确认的交易所错单。

## 1. 它回答什么问题

主检测器从同一品种的其他活跃合约推导目标合约在当前时刻的合理价格，再检查目标合约的新增成交是否突然、显著偏离该价格结构。

它回答的是：**这笔成交相对同品种整体价格结构，是否异常？**

它并不回答“是否已经成交”“是否必然是错单”，也不能替代逐笔成交、委托簿和交易所规则复核。若要判断一笔成交是否脱离它自己的当时一档盘口，应使用独立的 [Mid + LastPrice 检测器说明](mid_analyzer/model_and_usage.md)。两套检测器的事件数量和命中结果不能直接横向等同。

## 2. 运行方式与输入

在仓库根目录运行。`--data-dir` 指向一个交易日的全市场 Tick 目录或同名 ZIP；一个文件对应一个合约当天的快照。标准单品种复盘示例：

```bash
./venv/bin/python run_tick_detector.py \
  --data-dir data/tick2026/202605/20260520 \
  --symbols AU \
  --contracts AU2606 \
  --output-dir output/20260520-au2606-tick-v2
```

常用场景如下：

| 目的 | 建议范围 | 原因 |
|---|---|---|
| 复盘一个已知时点 | `--symbols` + `--contracts` | 便于打开该合约 HTML，核对参考合约和锚点前后行情 |
| 跑某品种一个交易日 | 只指定 `--symbols` | 同品种全部合约仍会读取；参考价格需要它们 |
| 跑全市场一个交易日 | 用 `scripts/run_day_parallel.sh` | 按品种并行，避免一次性串行处理所有品种 |

输入至少应含 `TradingDay`、`InstrumentID`、`UpdateTime`、`LastPrice`、`Volume`、`Turnover`、`BidPrice1`、`AskPrice1`、涨跌停价；`UpdateMillisec`、一档量等字段会提升时间与盘口诊断质量。只有 `src/tick_detector/tick_io.py` 的 `COMMODITY_PROFILES` 中标为 `validated` 的品种会真正触发检测。

## 3. 从原始 Tick 到事件的计算链

```text
原始全市场 Tick
  → 统一交易时钟、同时间键合并、成交差分和区间 VWAP
  → 选择同品种参考合约
  → 参考合约 as-of 对齐 + 历史 basis，得到 fair_price
  → 历史噪声生成动态阈值
  → 向下/向上候选、1 秒成交确认和 onset 突发性检查
  → 同方向候选合并，取最大深度锚点
  → 冻结锚点 basis，计算 3 秒 / 10 秒恢复
  → CSV、单合约 HTML、单日批量汇总
```

### 3.1 时间、成交量与区间成交价

检测使用 `market_time_key`，而不是自然日字符串。它以交易日的夜盘 21:00 为起点保持单调，因此可正确连接 `21:xx → 00:xx`。相同时间键的多条快照先合并，再计算：

```text
delta_volume  = Volume(t) - Volume(t-1)
delta_turnover = Turnover(t) - Turnover(t-1)
interval_vwap = delta_turnover / delta_volume / contract_multiplier
mid_price = (BidPrice1 + AskPrice1) / 2
```

只有正且可信的成交量/成交额增量才用于 `interval_vwap`。数据会经过交易时段、开盘保护、价格上下界、无效盘口和数据断点等保护；开盘前 60 秒不做候选检测。

### 3.2 合理价格 `fair_price`

对目标合约 `T`，先从同品种其他合约中按日成交量挑选前 5 个候选参考合约。每个时刻至少需要 2 个有效参考合约，并且其中至少 1 个来自成交量前三；参考行情距目标时刻不得超过 3 秒。

对每个可用参考合约 `i`，先在目标时刻前的稳定历史窗口估计价差基准：

```text
diff_i(s)  = target_mid(s) - peer_mid_asof_i(s)
basis_i(t) = median(diff_i(s)), s ∈ [t - 300 秒, t - 10 秒]
fair_i(t)  = peer_mid_asof_i(t) + basis_i(t)
fair_price(t) = median_i(fair_i(t))
```

每个 `basis_i` 至少需要 20 个配对样本，且覆盖至少 60 秒。参考合约本身也要通过盘口有效性、点差与新鲜度门槛。不同 `fair_i` 的稳健离散度（`1.4826 × MAD`）必须不超过 10 个跳动，才认为该时刻的合理价格可靠。

### 3.3 动态阈值：只和过去正常噪声比较

每行阈值来自过去 `[t - 300 秒, t - 10 秒]` 的可靠历史，故当前异常不会抬高自己的门槛。末笔与区间成交价分别计算，历史样本数至少 100 且时间覆盖至少 120 秒：

```text
min_depth_ticks = fair_price × 5 bp / 10,000 / tick_size
robust_sigma    = 1.4826 × MAD(历史偏离)
threshold       = max(20 跳, min_depth_ticks, 历史中位数 + 8 × robust_sigma)
```

这得到 `last_threshold_ticks` 与 `vwap_threshold_ticks`。阈值完全镜像复用于两个方向；不是简单使用一个固定价格差。

## 4. 双向候选判定

每个有效 Tick 同时计算下面四个正数深度（单位为跳）：

```text
last_down_ticks = (fair_price - LastPrice) / tick_size
last_up_ticks   = (LastPrice - fair_price) / tick_size

vwap_down_ticks = (fair_price - interval_vwap) / tick_size
vwap_up_ticks   = (interval_vwap - fair_price) / tick_size
```

| 方向 | 可见末笔通道 | 区间成交通道 | 原因机器值 |
|---|---|---|---|
| `down` | `last_down_ticks ≥ last_threshold_ticks` | `vwap_down_ticks` 与 1 秒合并深度均 `≥ vwap_threshold_ticks` | `visible_execution_drop` / `interval_execution_drop` |
| `up` | `last_up_ticks ≥ last_threshold_ticks` | `vwap_up_ticks` 与 1 秒合并深度均 `≥ vwap_threshold_ticks` | `visible_execution_spike` / `interval_execution_spike` |

区间通道不是只看一行累计字段：从当前 Tick 起完整覆盖 1 秒，要求期间没有超过 1 秒的相邻数据缺口，并将全部可靠成交增量重新合并为 `combined_vwap`。这能过滤“本行 `interval_vwap` 异常、但整秒累计成交实际正常”的计数器滞后假象。

此外，候选必须有突然性（onset）。将当前方向的执行深度和过去 3 秒中位深度比较：

```text
onset_ticks = max(0, 当前候选深度 - max(0,过去 3 秒深度中位数))
onset_ticks ≥ max(8 跳, 5 × 该执行深度的稳健 sigma)
```

过去窗口存在超过 3 秒的数据断点时不通过 onset。一个时间键若同时存在向下和向上通道命中，会保留为两个方向事件，不会互相覆盖。

## 5. 合并、锚点与恢复

候选只在**同一方向**内合并；相反方向即使同秒发生也独立。连续候选受 10 秒合并窗口与数据连续性限制，事件锚点取 `candidate_execution_depth` 最大的一行，事件量为事件范围内可靠正成交增量之和。

恢复阶段冻结锚点时刻的参考合约和各自 `basis`，在事件后 3 秒及 10 秒重新计算合理价格，避免恢复期重新选参考合约改变口径。两个已触发成交通道都需回到各自阈值内，才算成交恢复。

| 方向 | 末笔/区间剩余偏离 | 盘口确认 | 对外盘口恢复字段 |
|---|---|---|---|
| `down` | `(fair - observed_price) / tick_size` | `BidPrice1` 回到阈值内 | `quote_recovered_seconds` |
| `up` | `(observed_price - fair) / tick_size` | `AskPrice1` 回到阈值内 | `ask_recovered_seconds` |

恢复标签为 `trade_recovered_3s`、`quote_only_recovered_3s`、`trade_recovered_10s`、`quote_only_recovered_10s`、`persistent_10s` 或 `truncated`。恢复窗口缺少足够连续数据时为 `truncated`，不可把它读成“持续未恢复”。

## 6. 输出怎么读

标准输出目录包含：

| 文件 | 用途 |
|---|---|
| `tick_candidate_events.csv` | 机器可读的候选事件主表；`异常方向` 固定为 `down` 或 `up` |
| `{合约}_event_replay.html` | 单合约事件摘要、锚点前后 Tick、参考合约原始快照，适合人工复盘 |
| `batch_summary.html` / 汇总 CSV | 单日按品种汇总上下方向数量与最大偏离 |

主表中，`event_depth_ticks` / `event_depth_bps` 对两个方向都为正数；`last_*` 是末笔深度，`vwap_*` 是本区间成交均价深度，`combined_vwap_*` 是完整 1 秒确认深度。向下金额使用 `notional_shortfall`，向上金额使用 `notional_excess`；它们是相对合理价的成交金额偏离估计，不是实际损益。

事件编号兼容旧向下格式：向下为 `合约|事件时间`，向上为 `合约|事件时间|up`。因此同一时刻的双向事件不会冲突。

## 7. 使用时必须保留的边界

- 新输出虽然双向统计，但低侧参数生成、手工回放、程序化单次/网格回放和旧深度迁移仍只消费 `异常方向=down`；`up` 行被明确过滤或审计，不会悄悄混入低侧策略。
- 应先看 `fair_reliable`、`noise_reliable`、参考合约数、阈值、1 秒确认、onset、恢复标签，再对照 HTML 中目标与参考合约窗口。
- 涨跌停附近、开盘保护期、参考合约不足、报价陈旧、成交增量不可信或数据断点的行本来就会被排除；“未命中”不等于“确认正常”。
- 该模型不读取交易所逐笔委托/撤单队列，也没有成交回报，故只能称“疑似候选”。

## 8. 代码与验证索引

| 事实来源 | 位置 |
|---|---|
| 命令编排、CSV/HTML 字段、事件编号 | `run_tick_detector.py` |
| 时间归一、差分、`interval_vwap` | `src/tick_detector/tick_io.py` |
| 参考合约、`fair_price`、动态噪声阈值 | `src/tick_detector/reference_selection.py` |
| 双向候选、onset、合并、恢复 | `src/tick_detector/event_detection.py` |
| 页面中文显示 | `src/tick_detector/report_html.py` |
| 重点回归测试 | `tests/test_tick_detector_event_detection.py`、`tests/test_run_tick_detector.py`、`tests/test_tick_detector_perf_golden.py` |

需要排查实现细节、跑批和性能时，再读 [tick 检测器代码交接文档](tick_detector_handover.md)；历史设计决策保留在 `docs/superpowers/specs/2026-07-10-aggregated-tick-fat-finger-detector-design.md`。
