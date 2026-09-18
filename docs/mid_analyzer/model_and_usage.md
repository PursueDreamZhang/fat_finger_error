# Mid + LastPrice 检测器：模型与使用说明

> 当前实现版本：`mid-last-v1`。本文描述独立的 Tick 异常成交初筛器；输出的是疑似事件，不是成交确认、成交概率或策略收益结论。

## 1. 它回答什么问题

此检测器只看**目标合约自身**的新增成交与当时一档盘口，不依赖其他合约，也不构建跨合约合理价：

```text
Mid = (BidPrice1 + AskPrice1) / 2
deviation = (LastPrice - Mid) / Mid
```

它回答的是：**这笔新增成交是否脱离了它自己当时的一档盘口，像一笔孤立的异常成交？**

与主 Tick 检测器的分工不同：主检测器用同品种其他活跃合约建立 `fair_price`，用于发现相对整个品种价格结构的异常；本检测器发现的是相对本合约当时盘口的异常。两者可以交叉验证，但不能将其中一方的事件当成另一方的漏斗、成交或收益结果。主检测器说明见 [主 Tick 乌龙指候选检测器：模型与使用说明](../main_tick_detector_model_and_usage.md)。

## 2. 运行方式与输入

默认复盘输出未来 **1、3、5 秒**的恢复信息：

```bash
./venv/bin/python run_mid_analyzer.py \
  --input data/tick2026 \
  --symbols SA,FG,PX \
  --start-date 20260301 \
  --end-date 20260331 \
  --output-dir output/mid-march
```

可以用 `--contracts` 收窄合约，或显式用 `--thresholds`、`--horizons` 做研究性覆盖；若覆盖 horizons，`run_manifest.json` 中记录的值才是该次结果的真实口径。默认偏离阈值为 **0.25%、0.5%、0.75%、1%、1.25%、1.5%、2%、2.5%、3%**，默认同方向候选合并窗口 5 秒，默认数据断点阈值 300 秒，默认允许未来采样比目标时刻迟到最多 1 秒。

输入可为单个 CSV、ZIP、单日目录，或按月/日组织的数据根目录。每个原始来源至少需要：

```text
TradingDay, InstrumentID, UpdateTime, LastPrice, Volume, BidPrice1, AskPrice1
```

`UpdateMillisec`、`BidVolume1`、`AskVolume1` 是可选字段。对于同一合约/交易日的重复来源，加载器按“直接 CSV → 日 ZIP → 其他 ZIP”选择一份，并把选择与重复情况记录到质量输出中。

## 3. 数据有效性与时间段

本检测器刻意保留原始行，不把相同时间键聚合；它要判断的是单条新增成交相对于当时盘口的位置。按每个合约交易日排序后，使用以下定义：

```text
delta_volume = Volume(i) - Volume(i - 1)
has_new_trade = delta_volume > 0
valid_bbo = BidPrice1、AskPrice1 均为有限正数，且 BidPrice1 ≤ AskPrice1
valid_last = LastPrice 为有限正数
valid_dev = valid_bbo and valid_last and has_new_trade
```

首行、无效时间、时间倒退、相邻时间超过 300 秒、成交量无效或成交量回退都会切开一个 `segment`。事件、恢复和 MFE/MAE 都不能跨 `segment`，所以夜盘/午间/坏数据断点不会被误读为连续行情。

## 4. 双方向模型与 Raw / Strict 两个口径

在 `valid_dev` 行上计算：

```text
mid       = (BidPrice1 + AskPrice1) / 2
deviation = (LastPrice - mid) / mid
```

对每个阈值 `p`，下列规则分别产生向下和向上候选：

| 方向 | Raw | Strict（在 Raw 基础上增加） |
|---|---|---|
| `down` | `deviation ≤ -p` | `LastPrice < BidPrice1` |
| `up` | `deviation ≥ p` | `LastPrice > AskPrice1` |

`Raw` 回答“Last 与中价的偏离是否足够大”；`Strict` 进一步要求成交已经穿过同侧一档盘口，因此通常更保守。`outside_bbo_down` / `outside_bbo_up` 会写入事件明细，方便复核。

同一阈值、方向、模式下，只在同一 `segment` 且相邻候选间隔不超过 5 秒时合并为一个事件。代表行选绝对偏离最大的候选；若并列，选更早的原始行。不同方向、不同阈值或不同模式是独立的事件统计口径。

## 5. 恢复率、MFE/MAE 的真正含义

每个事件先根据阈值构造一个**标准化阈值边界价格**，不是模拟的真实可成交价格：

```text
down: sim_fill_price = mid × (1 - threshold)
up:   sim_fill_price = mid × (1 + threshold)
```

`anchor_mid` 是事件行之前最近的有效中价。事件后在同一连续段中，寻找达到未来 1、3、5 秒时刻（可迟至 1 秒）的第一条有效 Mid。恢复率以阈值边界价格到事前中价的距离为分母：

```text
down: recovery_ratio = (future_mid - sim_fill_price) / (anchor_mid - sim_fill_price)
up:   recovery_ratio = (sim_fill_price - future_mid) / (sim_fill_price - anchor_mid)
```

分母非正或未来样本不存在时，恢复率为空；空观察不会进入该 horizon 的恢复率分母。输出同时给出是否达到 50%、80%、100% 回归。这里的“恢复”是**中价相对标准化边界的回归**，不是订单成交回报。

默认 5 秒 MFE/MAE 也基于同一连续段内的未来 Mid 路径：

| 方向 | MFE | MAE |
|---|---|---|
| `down` | 未来 Mid 相对 `sim_fill_price` 的最大有利上移 | 最大不利下移 |
| `up` | 未来 Mid 相对 `sim_fill_price` 的最大有利下移 | 最大不利上移 |

若 5 秒未来窗口不完整，该事件不进入“完整窗口”的 MFE/MAE 汇总。报告中的事件窗口用于人工查看，默认展示事件前 30 秒到后 5 秒的原始行。

## 6. 输出怎么读

| 文件 | 用途 |
|---|---|
| `analysis_report.html` | 人工复盘：汇总、筛选、事件窗口图及原始行 |
| `threshold_summary.csv` | 按阈值/方向/Raw-Strict 汇总事件数、恢复和 MFE/MAE |
| `threshold_summary_by_day.csv` | 同一口径按交易日拆分 |
| `event_details.csv` | 每个候选事件、代表锚点、BBO、恢复和 MFE/MAE 明细 |
| `deviation_distribution.csv` | 合约日与合约全区间的偏离分布 |
| `data_quality.csv` | 字段有效性、断点、重复来源与回退诊断 |
| `run_manifest.json` | 本次阈值、horizons、MFE/MAE 窗口、输入来源与完整性；复现时优先看它 |

阅读事件时建议按此顺序核对：`direction` → `mode`（Raw/Strict）→ `threshold` → `LastPrice / BidPrice1 / AskPrice1 / mid` → 是否 `outside_bbo` → 1/3/5 秒恢复与数据质量。先确认 `segment` 连续和盘口有效，再讨论异常强度。

## 7. 适用范围与明确限制

- 适合 `Turnover`、`AveragePrice`、区间 VWAP 不可信或不可用时，以本合约 BBO 做快速筛查。
- 它不使用 `Turnover`、`AveragePrice`、`interval_vwap`，也不建立跨合约 `fair_price`；这是有意保持的独立模型边界，不是主检测器的简化版。
- 没有订单簿逐档、委托队列、交易所逐笔回报和真实撮合结果，不能据此推导实际成交率、填单价格、策略收益或交易所错单结论。
- `sim_fill_price` 只用于让不同事件的恢复/MFE/MAE 可比，不能当作可执行报价。
- 数据不完整、盘口倒挂、成交量回退或断点会减少可检测样本；未命中并不证明没有异常。

## 8. 代码与验证索引

| 事实来源 | 位置 |
|---|---|
| CLI、默认参数与输出编排 | `run_mid_analyzer.py` |
| 行准备、分段、候选、合并、恢复、MFE/MAE | `src/mid_analyzer.py` |
| HTML 与 CSV 展示 | `src/mid_analyzer_report.py` |
| 回归测试 | `tests/test_mid_analyzer.py` |

简短运行入口和文件清单可见 [Mid 检测器 README](README.md)。`docs/mid_analyzer/` 中的历史模型材料可作背景参考，但当前实际运行以本页所列入口和 `src/mid_analyzer.py` 为准。
