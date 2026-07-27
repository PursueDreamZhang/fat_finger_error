# Tick 级乌龙指轻量检测方案

> 本文档只定义第一版 tick 级“疑似乌龙指检测器”。交易回测、挂单收益、品种推荐先不进入第一版实现。

## 1. 目标

现有日线系统只能筛出“某合约某天可疑”。tick 数据加入后，第一版目标收缩为：

1. 从单日 tick 目录或 zip 中找出疑似低价砸穿事件。
2. 输出事件发生前后盘口、成交增量、同品种参考合约对照和回归情况。
3. 用已知样本 `au2606_20260520.csv` 的 `21:04:35.500` 作为第一条标定样本。

第一版只做向下异常检测。向上异常、提前挂单、平仓、收益统计都后置。

### 1.1 边界

当前 CSV 是快照数据，不是逐笔成交数据。

因此第一版能做：

- 判断某个快照区间里是否出现异常低价成交或异常低价 `LastPrice`。
- 用 `Volume` / `Turnover` 差分估算快照区间成交均价。
- 判断异常后报价或成交价是否快速回归。
- 对比同品种其他合约，判断它是全品种同步波动，还是目标合约孤立偏离。

第一版不能做：

- 还原逐笔成交明细。
- 证明某个挂单在真实队列中一定成交。
- 输出实盘收益、手续费后收益或自动下单建议。
- 复现交易所错单认定规则。

## 2. 数据形态

数据目录：

- `data/tick2026/{YYYYMM}/{YYYYMMDD}.zip`
- `data/tick2026/{YYYYMM}/{YYYYMMDD}/`

CSV 字段：

- `TradingDay`
- `InstrumentID`
- `UpdateTime`
- `UpdateMillisec`
- `LastPrice`
- `Volume`
- `BidPrice1`
- `BidVolume1`
- `AskPrice1`
- `AskVolume1`
- `AveragePrice`
- `Turnover`
- `OpenInterest`
- `UpperLimitPrice`
- `LowerLimitPrice`

关键约束：

- `Volume` 和 `Turnover` 是当日累计值，只能差分，不能当逐笔成交。
- `UpdateMillisec` 不同交易所/品种粒度不一致，第一版使用文件行序作为 `snapshot_seq`。
- 同一天既有真实合约文件，也有 `主力连续` 等连续合约文件。连续合约不作为检测目标，也不进入参考合约池。

## 3. 第一版输入输出

### 3.1 输入

第一版命令只需要支持：

```text
tick_day_path = 某个已展开日目录，或某个日 zip
commodity = 可选，只处理某个品种
contract = 可选，只处理某个合约
```

当前实现已支持已展开目录和单日 zip，两者共用同一套 loader。

注意：`commodity` / `contract` 只过滤检测目标，不过滤参考合约加载。即使只检测 `au2606`，也必须读取同目录下其他 AU 真实合约作为参考，否则 `reference_contract_count` 不足会把标定样本过滤掉。

### 3.2 输出

第一版只输出两个文件：

1. `tick_events.csv`
2. `event_replay.html`

`tick_events.csv` 每行一个合并后的疑似事件，字段：

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
- `trigger_reasons`
- `quote_recovery_10s_ticks`
- `trade_recovery_10s_ticks`
- `quote_recovery_30s_ticks`
- `trade_recovery_30s_ticks`
- `recovery_label`

`event_replay.html` 只用于人工复盘，展示事件前后窗口：

- 目标合约 `LastPrice`、买一、卖一、成交增量。
- 同品种参考合约在同一时间窗口的价格变化。
- 事件触发原因和回归标签。

## 4. 基础派生字段

每个真实合约 CSV 读取后派生：

```text
snapshot_seq = 文件内从 0 开始的行号
timestamp = TradingDay + UpdateTime + UpdateMillisec
mid_price = (BidPrice1 + AskPrice1) / 2
spread = AskPrice1 - BidPrice1
delta_volume = Volume - previous(Volume)
delta_turnover = Turnover - previous(Turnover)
snapshot_avg_trade_price = delta_turnover / delta_volume / contract_multiplier
```

修正规则：

- 首条记录的 `delta_volume` / `delta_turnover` 记为未知，不触发事件。
- `delta_volume <= 0` 不触发事件。
- `delta_turnover <= 0` 不计算 `snapshot_avg_trade_price`。
- 若累计字段回退，视为数据重置或坏数据，该快照不触发事件。
- 用 `AveragePrice ~= Turnover / Volume` 做倍率 sanity check；倍率缺失或校验明显不一致时，禁用均价砸穿触发，只保留展示字段。
- `BidPrice1 <= 0` 或 `AskPrice1 <= 0` 不计算 `mid_price`。
- `AskPrice1 < BidPrice1` 视为坏盘口，不触发事件。

第一版 `contract_multiplier` 只需要覆盖标定样本用到的品种。没有配置时，可以先不计算 `snapshot_avg_trade_price`，但事件仍可用 `LastPrice` 和盘口偏离触发。

## 5. Tick size

优先使用最小品种配置：

```text
commodity,tick_size,contract_multiplier
AU,0.02,1000
```

没有配置时，从价格序列兜底推导：

```text
valid_prices = LastPrice / BidPrice1 / AskPrice1 中大于 0 的价格
price_diff = 相邻不同价格的正差值
tick_size = 出现频率最高的最小合理差值
```

兜底推导只用于观察结果，不能进入后续交易统计。

## 6. 同品种参考合约

乌龙指检测不只看目标合约自身跳价，还要看同品种其他合约是否同步下跌。

真实合约的最小识别规则：

```text
^[a-zA-Z]+[0-9]{3,4}$
```

不符合该规则的文件默认不进入检测目标和参考合约池，例如连续合约、无月份合约、带未知后缀的厂商特殊文件。

对目标合约，参考合约取同品种真实合约，不含：

- 目标合约本身。
- `主力连续`、`当月连续`、`下月连续` 等连续合约。
- 当天成交量过低或快照太少的合约。

第一版简单做法：

```text
reference_contracts = 同品种内当天成交量前 5，排除目标合约
```

这使用了当天成交量，所以只适合检测复盘，不用于交易回测。第一版目标是先把事件抓准，这个取法够用。

对每个目标快照，取参考合约在同一时间之前最近一条有效快照，并同时取目标合约自己的 `3s` lookback 基线：

```text
peer_mid_asof(t) = 参考合约 <= t 的最近有效 mid_price
lookback_time = t - 3s
peer_move_ticks = (peer_mid_asof(t) - peer_mid_asof(lookback_time)) / tick_size
peer_median_move_ticks = median(peer_move_ticks)
```

默认参数：

```text
lookback_seconds = 3
max_reference_age_seconds = 3
reference_contract_limit = 5
min_reference_contract_count = 2
```

当前代码对参考可用性还有两条硬约束：

- 目标 baseline 必须位于可交易时段，且 age 不超过 `3s`。
- 参考合约只有在“当前点”和 “lookback 点”都位于可交易时段、且两个点 age 都不超过 `3s` 时，才计入 `reference_contract_count` 与 `peer_median_move_ticks`。

## 7. 事件触发

单个快照成为候选事件，需要先满足基础过滤：

```text
delta_volume > 0
delta_volume >= min_event_delta_volume
reference_contract_count >= min_reference_contract_count
LastPrice > LowerLimitPrice + limit_buffer_ticks * tick_size
not in open_guard_window
```

其中：

```text
open_guard_times = 09:00:00 / 09:30:00 / 21:00:00
open_guard_seconds = 60
```

然后至少命中一个向下异常分支：

```text
visible_last_drop:
    last_vs_mid_down_ticks >= min_last_vs_mid_down_ticks
    and peer_excess_down_ticks >= min_peer_excess_down_ticks
    and spread_ticks <= max_spread_ticks

strong_visible_last_drop:
    delta_volume >= strong_signal_min_delta_volume
    and last_vs_mid_down_ticks >= strong_signal_min_last_vs_mid_down_ticks
    and peer_excess_down_ticks >= strong_signal_min_peer_excess_down_ticks
    and spread_ticks <= strong_signal_max_spread_ticks

hidden_avg_trade_drop:
    snapshot_avg_trade_gap_ticks >= min_avg_trade_gap_ticks
    and snapshot_avg_trade_price is not null
```

派生口径：

```text
last_vs_mid_down_ticks = (mid_price - LastPrice) / tick_size
previous_last_price_at_lookback = 目标合约 lookback 基线的 LastPrice
target_move_ticks = (mid_price - baseline_mid_price) / tick_size
peer_excess_down_ticks = max(0, peer_median_move_ticks - target_move_ticks)
event_reference_price = mid_price
snapshot_avg_trade_gap_ticks = (mid_price - snapshot_avg_trade_price) / tick_size
event_depth_ticks = max(last_vs_mid_down_ticks, peer_excess_down_ticks, snapshot_avg_trade_gap_ticks)
event_depth_bps = event_depth_ticks * tick_size / event_reference_price * 10000
```

如果 `snapshot_avg_trade_price` 不可用，`event_depth_ticks` 的 `max(...)` 忽略 `snapshot_avg_trade_gap_ticks`。

`target_move_ticks` 和 `peer_median_move_ticks` 下跌时为负数。`peer_excess_down_ticks` 只计算“目标合约比同品种参考合约跌得更多”的部分，不能用绝对值。

当前落地参数按“压掉已知误报，同时保留标定样本”调定：

```text
max_spread_ticks = 20
strong_signal_max_spread_ticks = 25
min_event_delta_volume = 10
min_last_vs_mid_down_ticks = 20
min_peer_excess_down_ticks = 20
strong_signal_min_delta_volume = 50
strong_signal_min_last_vs_mid_down_ticks = 50
strong_signal_min_peer_excess_down_ticks = 10
min_avg_trade_gap_ticks = 20
limit_buffer_ticks = 2
open_guard_seconds = 60
```

说明：

- `last_vs_mid_down_ticks` 抓可见 `LastPrice` 低于盘口的异常。
- `snapshot_avg_trade_gap_ticks` 抓快照内隐藏低价成交。只有倍率配置和累计字段校验通过时才启用该触发分支。
- `peer_excess_down_ticks` 防止把全品种同步下跌误判成单合约乌龙指。

## 8. 事件合并

同一合约短时间内多个候选快照合并成一个事件：

```text
merge_window_seconds = 10
event_time = event_depth_ticks 最大的候选快照时间
event_low_price = 候选窗口内最低 LastPrice
event_volume = 候选窗口内 delta_volume 合计
event_reference_price = 触发最强快照的 mid_price
recovery_denominator_ticks = max(1, (event_reference_price - event_low_price) / tick_size)
```

合并后保留触发最强快照的盘口和参考合约指标。

## 9. 回归判定

事件发生后观察两个窗口即可：

```text
recovery_windows = 10s, 30s
```

分别计算报价回归和成交回归：

```text
quote_recovery_price = 窗口内最高 BidPrice1
trade_recovery_price = 窗口内最高 LastPrice 且 delta_volume > 0
quote_recovery_ticks = (quote_recovery_price - event_low_price) / tick_size
trade_recovery_ticks = (trade_recovery_price - event_low_price) / tick_size
quote_recovery_ratio = quote_recovery_ticks / recovery_denominator_ticks
trade_recovery_ratio = trade_recovery_ticks / recovery_denominator_ticks
```

回归标签：

- `fast_trade_recovery`：10 秒内成交回归比例 >= 50%。
- `fast_quote_recovery`：10 秒内报价回归比例 >= 50%。
- `slow_recovery`：30 秒内报价或成交回归比例 >= 50%。
- `no_recovery`：30 秒内仍未达到 50%。
- `truncated`：30 秒观察窗内遇到 `> 60s` 时间断点，无法继续跨 session / 跨断点评估。

回归标签是事后复盘标签，不能用于触发实盘成交或平仓。

## 10. 标定样本

第一版检测器必须抓出：

```text
file = data/tick2026/202605/20260520/au2606_20260520.csv
event_time = 21:04:35.500
```

已确认现象：

- `LastPrice` 从 `995.60` 跳到 `993.92`，半秒下跌 `1.68`，约 `84 tick`。
- 该快照 `delta_volume = 370`。
- 买一/卖一为 `994.84 / 995.28`，`LastPrice` 比盘口中间价低约 `57 tick`。
- `Turnover` 差分反推该快照区间均价约 `940.52`，明显低于可见盘口。
- 到 `21:04:37.000`，`LastPrice` 回到 `995.20`。
- 同品种其他黄金合约也有波动，但 `au2606` 的偏离和成交增量明显更强。

这条样本是第一版验收标准：如果检测器抓不出它，先修检测口径，不做交易回测。

当前代码最终保留这条样本，靠的是 `strong_visible_last_drop` 分支，而不是普通 `visible_last_drop`：

- `delta_volume = 370`，满足强信号成交量门槛。
- `last_vs_mid_down_ticks ≈ 57`，满足“成交价明显砸穿盘口中价”门槛。
- `peer_excess_down_ticks` 仍为正，但低于普通分支的 `20 tick` 阈值，因此需要强信号兜底路径保留。

## 11. 第一版验收

最小验收：

1. 能读取 `20260520` 已展开目录或对应单日 zip。
2. 能只跑 `AU` 或 `au2606`。
3. 能输出 `tick_events.csv`。
4. `tick_events.csv` 包含 `AU2606 2026-05-20 21:04:35.500` 事件，或其合并窗口覆盖该锚点。
5. `event_replay.html` 能展示事件前后至少 30 秒窗口。

按当前代码与真数据复核，`2026-05-20` 全天全品种重跑后的结果是：

- `output/tick_detector_20260520_all/tick_events.csv` 只保留 1 笔事件。
- 该事件就是 `AU2606 @ 2026-05-20 21:04:35.500`。
- `trigger_reasons = strong_visible_last_drop`
- `recovery_label = fast_trade_recovery`

第一版不要求：

- 跨月份全量扫描。
- 收益统计。
- 挂单模拟。
- 品种/月份推荐。

## 12. 后续附录：交易统计再做什么

检测器稳定后，再单独设计交易统计。不要把下面内容塞回第一版检测器。

当前独立统计设计见：

- `docs/superpowers/specs/2026-07-23-fat-finger-passive-order-hedge-statistics-design.md`

后续可以新增：

- `eligible_contract_days.csv`：统计分母和排除原因。
- `simulated_trades.csv`：模拟挂单、成交、退出的明细。
- `offset_curve.csv`：不同挂单距离的成交率和回归表现。
- `commodity_rank.csv`：品种机会排序。
- `bucket_rank.csv`：主力、次主力、远月合约对比。

后续交易统计必须继续遵守：

- 成交模拟只能使用快照当时可见信息。
- 不能用事件结束后才知道的最低价或最高回归价设置订单。
- `touch_fill` 只能代表触达上限。
- 没有逐笔成交和队列数据时，所有收益都只能标为研究上限。

## 13. 实施顺序

1. 读取 `20260520` 日目录，只把检测目标过滤为 `au2606`，参考合约仍加载同目录 AU 真实合约。
2. 扩到同品种 AU 全部真实合约，加入参考合约对照。
3. 扩到 `20260520` 全日目录，输出当天全部疑似事件。
4. 人工审查误报后，再决定是否扩到多日扫描。
5. 多日检测稳定后，再写交易统计附录里的回测模块。
