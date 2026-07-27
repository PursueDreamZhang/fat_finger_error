# 乌龙指程序化双向网格策略与全天状态机回放方案 V2

> 状态：**设计已冻结；V2 核心状态机、合成测试与单日回放入口已实现，尚待真实快照参数校准。**
>
> 本文替代原“手工延迟挂单”方案。V1 的事件条件回放程序保留为历史研究工具，
> 不再作为程序化策略收益结论。V2 先只定义历史全天状态机回放；不接交易终端，
> 不自动下单，不把快照数据包装成逐笔成交或真实排队成交。

## 1. 决策与策略核心

目标不是在检测器已经发现事件后再追价，而是在市场正常时长期维护一组足够远的
双向被动订单：

```text
合理价锚点附近的正常价格带
    之外再留一层深度
        ↓
低侧挂被动买单， 高侧挂被动卖空单
        ↓
只有正常价格体系确认迁移时才撤改单
        ↓
任一侧成交后立即撤另一侧，并程序化对冲
```

用户给出的例子定义了策略直觉：

```text
锚点 = 100
正常价格带 = 90 ~ 110
被动买入 = 80
被动卖空 = 120

若正常价格体系确认下移一格：
锚点 = 90
正常价格带 = 80 ~ 100
被动买入 = 70
被动卖空 = 110
```

这里“正常价格体系确认下移”不能由目标合约单独的 `LastPrice` 决定。
若目标成交价短暂到 89、但同品种参考合约推导的 `fair_price` 仍接近 100，
89 恰恰可能是要抓的乌龙指；程序不得撤掉原来的 80 买单。只有参考价也稳定地
跌破当前价格带，才能把它解释为正常重估并重定锚。

因此 V2 的硬规则是：

```text
目标合约成交价 / 买卖一：只用于判断订单是否成交、如何退出
参考合约推导的 fair_price：只用于判断正常价格体系是否迁移、是否需要撤改单
```

## 2. 数据事实与不可声称的结论

数据源保持为本地聚合快照：

- 原始快照：`data/tick2026/YYYYMM/YYYYMMDD.zip`；
- 检测器候选：`output/20260301_20260331-range/tick_candidate_events_annotated.csv`；
- 每份原始文件的 `UpdateMillisec` 实际只出现 `0`、`500`，最细约为 500ms 快照。

因此 V2 可以回放：订单状态、穿价证据、撤改单竞态、参考腿可执行报价、
500ms 及以上的延迟敏感性。

V2 不能从历史文件证明：

- 本单在盘口队列中的真实位置；
- 同一价格有多少手在本单之前；
- 50ms、100ms 级真实网络/柜台延迟优势；
- 撤单是否先于同一 500ms 桶内的成交；
- 交易终端是否支持某个组合单或组合保证金优惠。

回放报告必须将“快照显示有穿价证据”与“真实订单一定成交”分开。实盘前的
模拟盘或极小仓位观察，才用于校准真实填单率和撤单竞态。

## 3. V1 与 V2 的边界

| 项目 | V1：手工事件条件回放 | V2：程序化全天状态机 |
|---|---|---|
| 起点 | 已知候选事件 | 全部可交易快照 |
| 订单 | 假定事件前已挂出 | 由状态机主动生成、撤销、重挂 |
| 调价 | 固定订单年龄 | fair_price 确认越带后重定锚 |
| 对冲 | 手工 5/10/30/60 秒 | 程序化确认延迟与报撤延迟 |
| 候选事件 CSV | 回放分母 | 事后标签，不参与决策 |
| 核心负样本 | `pre_event_touch` | `normal_move_fill`、`fill_during_replace` |

V1 的 `manual_simulation.py` 不应通过增加几个参数直接变为 V2：它按候选事件
分组运行，无法得出“全天订单何时存在、何时撤掉、正常行情何时已经成交”的分母。
V2 应保持为单独的回放链路，以便保留两种研究结论的可比性。

## 4. V2 最小范围

第一版只覆盖以下场景：

1. 单品种、单目标合约、单个实际对冲合约；`fair_price` 仍至少使用两个**预先冻结**的
   同品种参考合约共同校验；
2. 平仓状态下同时维持低侧 1 手买单与高侧 1 手卖空单；
3. 任一侧成交后，立即撤销另一侧订单；
4. 成交后用 1 个同品种月份做反向对冲；
5. 全账户同一时间最多一组仓位或一组待成交双向单；
6. 使用参考 `fair_price` 重定锚；目标 `LastPrice`、区间成交均价和一档买卖价
   共同提供**进场成交证据**，退出仍只使用可执行买卖一；
7. `LastPrice`、区间成交均价、盘口穿价三条成交证据分别记录，并可按所选成交模型
   合并为策略成交；
8. 所有资金占用按两腿全额保证金；
9. 不实现多品种并发、多档阶梯、多参考篮子、真实下单接口或组合保证金优惠。

这不是做市系统。V2 的目标是验证“低频状态切换的远端双向被动订单”是否具有
统计价值，而不是最大化报撤次数或成交次数。

## 5. 价格坐标与报价公式

V2 内部价格参数统一以 **target tick 数** 表示，报告同时换算成 bps。这样能严格
保证价格合法，并精确复现 100/90/80/120 这类离散网格例子；不在 V2 首版同时支持
tick 网格与 bps 网格两套配置。

定义：

```text
A = 当前网格锚点（合法 tick 价格）
W = band_half_width_ticks，正常价格带半宽
D = outer_quote_offset_ticks，价格带外的乌龙指深度
S = reanchor_step_ticks，单次重定锚步长

band_lower = A - W × tick_size
band_upper = A + W × tick_size
buy_limit  = A - (W + D) × tick_size
sell_limit = A + (W + D) × tick_size
```

例如 `A=100, W=10, D=10, S=10, tick_size=1`：

```text
band = [90, 110]
buy_limit = 80
sell_limit = 120
```

参数统计口径：

- `W`：历史正常状态中，`fair_price` 在确认窗口内迁移的高分位数；
- `D`：目标合约相对 `fair_price` 的正常执行残差高分位数之外，再加费用、点差、
  队列不确定性缓冲；
- `S`：网格状态的离散迁移步长，通常与 `W` 同量级，但必须单独回放；
- 报告展示 `W / fair_price`、`D / fair_price` 的 bps，便于横向比较品种。

`W` 与 `D` 不是同一件事：前者决定“正常价格体系是否换挡”，后者决定“偏离多深
才允许订单成交”。不能将两者都从乌龙指事件偏离分位数直接取值。

## 6. 重定锚规则

### 6.1 信号源

每个时点只使用通过以下条件的 `fair_price` 作为重定锚依据：

- 至少两个有效参考合约；
- 参考价报价年龄不超过 `max_fair_age_ms`；
- fair 不确定性不超过上限；
- 目标和参考均不在涨跌停保护区；
- 不处于开盘、收盘或数据断点保护区。

若 fair 无效、陈旧或参考合约失效，策略进入 `PAUSED`：撤销所有未成交订单，
不使用目标 `LastPrice` 替代 fair 继续报价。

### 6.2 越带确认

仅在以下条件同时满足时发起重定锚：

```text
fair_price < band_lower  或  fair_price > band_upper
并且该方向连续满足 reanchor_confirm_ms
并且距上次重挂至少 min_reprice_interval_ms
并且本分钟订单动作数未超过 max_order_actions_per_minute
```

确认窗口按快照数量实现，不假装拥有 500ms 以下时间精度。

### 6.3 离散迁移

若 `fair_price < band_lower`，向下移动刚好足以让 fair 回到新价格带内的整数步数：

```text
steps = ceil((band_lower - fair_price) / (S × tick_size))
A_new = A - steps × S × tick_size
```

上破时对称上移。

示例：`A=100, W=10, S=10, fair_price=89`：

```text
steps = 1
A_new = 90
new band = [80, 100]
new orders = [70 buy, 110 sell]
```

若 `LastPrice=89`、但 `fair_price≈100`，则 `steps=0`；当前 80/120 订单继续存在。

### 6.4 撤改单不是瞬时的

重定锚发出后，不直接假定旧订单消失：

```text
reanchor signal
    → cancel requested
    → cancel ACK after cancel_ack_latency_ms
    → new order submitted
    → new order ACK after new_order_ack_latency_ms
```

在取消确认之前，旧订单仍可能成交，必须记为 `fill_during_replace`。V2 首版采用
`cancel_then_replace`：撤单确认之前不让新双向单上线，宁可有短暂空窗，也不假设
同一方向有两张订单或自动净额处理。

## 7. 状态机

```text
PAUSED
  └─ fair 恢复且风控通过 → FLAT_QUOTING

FLAT_QUOTING
  ├─ 低侧成交 → LONG_PENDING_HEDGE
  ├─ 高侧成交 → SHORT_PENDING_HEDGE
  ├─ fair 确认越带 → REPLACE_PENDING
  └─ fair 失效/会话保护/动作限额 → PAUSED

REPLACE_PENDING
  ├─ 旧单在 cancel ACK 前成交 → LONG/SHORT_PENDING_HEDGE
  └─ 新单 ACK 后 → FLAT_QUOTING

LONG_PENDING_HEDGE / SHORT_PENDING_HEDGE
  ├─ 对冲成交 → HEDGED_POSITION
  └─ 超过 max_hedge_wait_ms → EMERGENCY_FLATTEN

HEDGED_POSITION
  ├─ 回归止盈 / 时间退出 / 止损 → FLATTENING
  └─ 参考失效或风控触发 → EMERGENCY_FLATTEN

FLATTENING / EMERGENCY_FLATTEN
  └─ 两腿平完 → COOLDOWN → FLAT_QUOTING
```

不允许在 `LONG_PENDING_HEDGE`、`SHORT_PENDING_HEDGE`、`HEDGED_POSITION`、
`FLATTENING` 中继续维护新双向报价。这样避免一笔异常成交后，另一侧旧订单或新订单
把账户变成双向、多腿、超预算仓位。

## 8. 快照回放中的固定事件顺序

每个 500ms 快照必须按以下顺序处理，避免结果依赖实现细节：

1. 生效到时的撤单 ACK、新单 ACK、对冲单或平仓单；
2. 检查当前仍有效的目标订单是否存在穿价成交证据；
3. 若成交，记录成交、发撤另一侧订单、进入待对冲状态；
4. 若未成交且处于 `FLAT_QUOTING`，检查 fair 是否确认越带并发起撤改单；
5. 处理已持仓组合的对冲、回归、止损、超时和紧急退出；
6. 更新动作计数、资金占用和暂停状态。

同一快照既有旧订单穿价又有 fair 越带时，按第 2 步优先：**旧订单已成交**。
不能使用事后 fair 信号把已经发生的成交改写成“成功撤单”。

## 9. 成交、对冲与退出模型

### 9.1 双向被动成交

低侧买单的成交证据是 **OR 关系**，不要求三项同时出现：

| 证据 | 低侧买单条件 | 高侧卖空对称条件 | 处理 |
|---|---|---|---|
| `last_trade_touch` | `LastPrice <= buy_limit` 且 `delta_volume > 0` | `LastPrice >= sell_limit` 且 `delta_volume > 0` | 可见成交触价证据 |
| `interval_vwap_touch` | `interval_vwap <= buy_limit` 且 `delta_volume > 0` | `interval_vwap >= sell_limit` 且 `delta_volume > 0` | 区间成交均价触价证据 |
| `top_of_book_cross` | 新鲜有效的 `AskPrice1 <= buy_limit` | 新鲜有效的 `BidPrice1 >= sell_limit` | 盘口已与被动限价相交的证据 |

因此，低侧买单的默认“可观察成交假设”为：

```text
last_trade_touch
or interval_vwap_touch
or top_of_book_cross
```

高侧卖空完全对称。每笔订单必须记录 `fill_evidence`：`last_trade`、
`interval_vwap`、`top_of_book` 或多个证据的组合，不能只留下笼统的 `filled`。

`top_of_book_cross` 还要求报价未过期；V2 的一手模型默认要求
`AskVolume1 >= target_lots`（卖空侧为 `BidVolume1 >= target_lots`）才标为完整一手成交。
若价格已相交但一档量不足，记为 `quote_cross_partial_unknown`，不能静默当作完整成交。

`strict_cross` 仍保留为敏感性统计：低侧为 `LastPrice < buy_limit`，高侧为
`LastPrice > sell_limit`，且均要求正增量成交。它是较保守的下界；
`observable_cross_assumed` 才是本策略默认的成交模型。由于历史数据不是逐笔和队列数据，
后者仍是“按快照可观察证据假设成交”，不是交易所成交回报。

成交价一律按挂单价，不使用快照中的最低/最高价，也不使用 `interval_vwap` 作为成交价。
`interval_vwap` 只增加进场成交证据；其不能替代退出时真实可执行的买卖一。

### 9.2 程序化对冲

目标订单成交后：

1. 立即撤销另一侧目标订单；
2. 在 `hedge_submit_latency_ms` 后提交参考腿；
3. 参考空头用 `BidPrice1 - hedge_slippage_ticks`，参考多头用
   `AskPrice1 + hedge_slippage_ticks`；
4. 若到 `max_hedge_wait_ms` 仍无可执行参考报价，则目标腿按可执行买卖一紧急平仓；
5. 若参考腿实际可成交价使两腿全额保证金超过账户上限，则拒绝参考腿并紧急平掉目标腿，
   记为 `capital_limit_at_hedge`；
6. 两腿全额保证金、手续费、滑点和未对冲期间 MAE 全部单列。

V2 首版固定 `target_lots=1`、`hedge_lots=1`。滚动 beta、名义金额匹配、
多参考篮子以后再加入；不得在文档中假定它们已经消除了方向风险。

### 9.3 退出

程序化持仓只允许以下退出原因：

- `reversion_exit`：目标相对 fair 的残余偏离回到预设比例；
- `take_profit`：两腿净收益达到费用后的目标；
- `stop_loss`：组合盯市损失达到上限；
- `time_exit`：超过 `max_hold_ms`；
- `reference_invalid_exit`：参考报价失效、断点或涨跌停保护；
- `hedge_failure_exit`：未在允许时间内完成对冲。

禁止使用事后最高价、最低价或“最终总会回归”的信息平仓。
退出成交价始终取当时可执行盘口：目标多头按 `BidPrice1`，目标空头按 `AskPrice1`，
参考腿按反向可执行价；`LastPrice` 与 `interval_vwap` 不用于虚构退出成交价。

## 10. 全天分母与候选事件的角色

V2 的分母是所有满足条件的目标合约快照时段：

```text
连续交易时段
且 fair 有效
且目标与参考买卖一可用
且不处于会话/涨跌停/数据断点保护
且账户可承受两腿全额保证金与压力损失
且状态为 FLAT_QUOTING 或可安全发起新报价
```

候选事件 CSV 不得用于决定何时挂单、撤单或重定锚。它只在事后用于标签：

```text
程序化成交时间靠近已检测事件锚点 → detector_event_fill
程序化成交但附近无候选事件 → normal_move_fill
撤改单窗口内成交 → fill_during_replace
```

策略最关键的负样本是 `normal_move_fill`。如果远端报价仍经常被正常行情打到，
即使候选事件成交后的回归看起来漂亮，也不具备交易价值。

## 11. 无未来数据约束

以下字段必须在报价生效前已经可得：

| 字段 | V2 规则 |
|---|---|
| 参考合约集合 | 前一交易日成交量排名或此前 N 日排名缓存，不用当天最终成交量 |
| fair_price | 当前及此前快照，不读取未来帧 |
| `W`、`D`、`S` | 冻结于前一日或滚动历史校准窗口 |
| 相关性、beta、对冲手数 | 前一日或此前历史窗口 |
| 保证金、手续费、申报限制 | 当日已知的期货公司/终端配置 |
| 日线 High/Low、当日最终量 | 只能做事后审计，不能参与订单决策 |

现有检测器中按**当天最终成交量**选择参考合约的离线便利逻辑不能直接复用到 V2
策略决策。V2 实现时必须先建立前日合约角色/参考集合缓存。

当前原型先采用更保守的过渡方式：`fair_reference_contracts` 与 `hedge_contract` 由
配置显式冻结，且 `hedge_contract` 必须属于前者；这避免了当天最终成交量的未来函数。
自动生成“前日角色缓存”是下一阶段的便利功能，不能替换这一无未来约束。

## 12. V2 配置草案

```json
{
  "account_equity": 100000,
  "max_margin_ratio": 0.30,
  "max_single_trade_loss": 500,
  "target_lots": 1,
  "hedge_lots": 1,

  "target_contract": "NI2605",
  "fair_reference_contracts": ["NI2604", "NI2609", "NI2607"],
  "hedge_contract": "NI2604",

  "band_half_width_ticks": 10,
  "outer_quote_offset_ticks": 10,
  "reanchor_step_ticks": 10,
  "reanchor_confirm_ms": 1000,
  "resume_confirm_ms": 2000,
  "fair_invalid_confirm_ms": 1000,
  "min_reprice_interval_ms": 1000,
  "max_order_actions_per_minute": 20,

  "cancel_ack_latency_ms": 500,
  "new_order_ack_latency_ms": 500,
  "hedge_submit_latency_ms": 500,
  "max_hedge_wait_ms": 2000,

  "max_fair_age_ms": 3000,
  "max_data_gap_ms": 3000,
  "fill_model": "observable_cross_assumed",
  "require_top_of_book_full_lot": true,
  "reversion_exit_ratio": 0.20,
  "take_profit_amount": 0,
  "max_hold_ms": 60000,
  "cooldown_ms": 1000
}
```

以上数值只是配置字段示例，不是实盘推荐值。由于历史数据仅有 500ms 粒度，
首轮延迟网格应至少包含 `500 / 1000 / 2000ms`，不能从该数据推导 50ms 或 100ms
优势。`max_order_actions_per_minute` 也必须在确定交易所、期货公司和终端后按实际规则填写。
`resume_confirm_ms` 用来防止 fair 短暂失效后立刻重挂；它应至少覆盖一个快照间隔，
否则会把数据/参考瞬断放大为高频报撤。
`fair_invalid_confirm_ms` 是反向保护：单个瞬时坏帧不立即撤掉远端报价，但连续失效达到
该窗口后必须暂停；数据断点仍立即暂停，不能用该窗口掩盖缺失区间。

## 13. 最小输出

V2 首版只输出四类结果：

1. `quote_state_transitions.csv`
   - 交易日、时刻、旧锚点、新锚点、旧报价、新报价、重定锚原因、动作计数、暂停原因；
2. `order_lifecycle.csv`
   - 每张目标订单和参考订单的提交、ACK、撤单、穿价、成交、失效及版本号；
3. `programmatic_trades.csv`
   - 方向、目标成交、对冲、退出、候选事件标签、未对冲 MAE、组合 MAE、费用、保证金、净收益；
4. `programmatic_summary.csv` 与 HTML 报告
   - 正常成交率、候选事件成交率、撤改单期间成交率、动作频率、对冲失败率、尾部损失、按日稳定性。

实现还额外输出 `programmatic_skipped_days.csv`，明确记录缺文件、缺合约和数据质量排除，
避免将未回放的日期悄悄从全天分母中删除。

不在首版生成真实下单指令、实时监控面板、复杂资金曲线或多品种组合优化。

## 14. 必须比较的指标

### 14.1 报价质量

- 每日/每合约有效报价时长；
- 每小时双向订单提交数、撤单数、替换数；
- `peak_order_actions_per_minute`，并与配置的 `max_order_actions_per_minute` 对照；
- `reprice_count`、`pause_count`、`stale_quote_seconds`；
- `fill_during_replace_rate`；
- 因动作限额进入暂停的次数。

### 14.2 成交结构

- `strict_cross_fill_rate`；
- `visible_touch_upper_bound_rate`；
- `interval_touch_upper_bound_rate`；
- `detector_event_fill`、`normal_move_fill`、`fill_during_replace` 的数量与比例；
- `last_trade`、`interval_vwap`、`top_of_book` 及多证据组合的成交来源占比；
- 正常成交后继续不利移动的分布；
- 长、短两侧的对称性。

### 14.3 执行和风险

- 对冲完成率、对冲延迟、紧急平仓率；
- `capital_limit_at_hedge`、未对冲止损的次数与损失；
- 目标成交至参考对冲的未对冲 MAE；
- 对冲后组合 MAE、P5/P1、最大单笔损失；
- 两腿全额保证金、单日最大占用、同时风险暴露；
- 扣实际手续费与滑点后的净收益；
- 去掉最高事件日、去掉最高收益日后的稳定性。

V2 只能在 `normal_move_fill` 的损失、频率和尾部风险可接受时，才按
`detector_event_fill` 的收益去讨论机会价值。

## 15. 回放验收样例

实现前必须先用合成快照测试以下不变量：

1. `A=100, W=10, D=10, S=10` 时生成 `[80 buy, 120 sell]`；
2. 目标 `LastPrice=89`、fair 仍为 100 时，不重定锚，80 买单继续有效；
3. fair 连续确认到 89 时，锚点变为 90，订单变为 `[70 buy, 110 sell]`；
4. 旧单在撤单 ACK 前穿价时，必须记录成交，不能被事后重定锚抹掉；
5. 一侧成交后，另一侧订单必定进入撤单流程，期间不得新增报价；
6. 参考腿在 `max_hedge_wait_ms` 内无可执行报价时，必须紧急退出目标腿；
7. `LastPrice = buy_limit`、`interval_vwap = buy_limit`、或 `AskPrice1 = buy_limit`
   时，均按 `observable_cross_assumed` 触发低侧成交；高侧对称；
8. 一档价格已相交但一档量不足一手时，必须标记 `quote_cross_partial_unknown`；
9. 同日最终成交量、候选事件标签、日线 High/Low 改变时，订单决策结果不得改变。
10. 数据断点后恢复的第一帧不得补造旧单成交；平仓中遇到断点必须进入紧急平仓流程。

## 16. 实施顺序

1. 冻结本文的状态、事件顺序和参数语义；
2. 建立前日合约角色/参考集合缓存，消除当天最终成交量未来函数；
3. 先实现单日、单目标合约的纯状态机与合成测试；
4. 接入全天快照和严格穿价模型；
5. 加入撤改单 ACK、程序化对冲和退出；
6. 再把候选事件 CSV 作为事后标签接入；
7. 用 NI、FU 单日验证状态转换，再扩展到 3 月；
8. 只有全天回放稳定后，才讨论终端接口、模拟盘和真实报单。

V2 的完成条件不是“生成了一条看起来赚钱的交易”，而是完整记录了每一笔正常成交、
异常成交、撤改单竞态、未对冲风险和资金占用，并且没有未来函数。

## 17. 参数网格回放

`src/programmatic_grid.py` 对每个品种和交易日只计算一次 fair、目标帧和参考腿，
然后对 `W/D/S` 报价形状与延迟配置做笛卡尔积重放。网格输出分为：

- `programmatic_grid_daily.csv`：每个参数组合的逐日成交、误成交、对冲失败、净收益和报撤峰值；
- `programmatic_grid_summary.csv`：按最小成交数、候选事件成交数、正常成交率、对冲失败率、单日亏损和动作峰值筛选；
- `programmatic_grid_trades.csv`：保留每个组合的逐笔交易，便于复核成交证据和退出原因。

网格的 `eligible` 只表示“满足研究筛选条件”，不表示可直接实盘。没有成交的组合必须保留为
`insufficient_fills`，不能据此宣称策略无风险或有效。
