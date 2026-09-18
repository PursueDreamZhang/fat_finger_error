# 聚合 Tick 乌龙指候选检测方案

> 本文只定义离线检测器。回测、挂单模拟、收益统计和品种推荐全部后置。
>
> 第一版只检测向下异常；所有输出均为“疑似候选”，不宣称已确认交易所错单。

## 1. 检测定义

第一版只检测一件可由现有数据支持的事：

> **某个合约在有新增成交的快照区间内，成交价格突然显著低于同品种其他活跃合约所支持的合理价格，且不是全品种同步下跌。**

检测链只保留三步：

1. 用事件前正常跨月价差和参考合约当前价格重建目标合约 `fair_price`。
2. 分别检查末笔价 `LastPrice`、区间增量成交均价 `interval_vwap` 是否向下偏离 `fair_price`。
3. 要求偏离突然出现；快速回归只作为事后证据标签，不作为候选硬门槛。

```text
candidate = data_eligible
            and fair_price_reliable
            and noise_history_reliable
            and sudden_onset
            and (visible_execution_drop or interval_execution_drop)
```

不引入总分、机器学习、复杂 movement 模型或任何回测条件。

## 2. 现有方案中需要纠正的地方

| 旧口径 | 新口径 | 原因 |
|---|---|---|
| 当前目标 `mid_price` 作为主要参考价 | 事件前目标-参考价差 + 参考合约当前价格 | 当前目标盘口也可能失真或已恢复 |
| `abs(interval_vwap - LastPrice)` | 两者分别有向比较 `fair_price` | 绝对差无方向，正常快速行情也可能很大 |
| 两个成交信号必须同时满足 | 两个独立 OR 分支 | 快照可能只留下异常末笔，也可能末笔已恢复 |
| 快速回归是硬门 | 回归只打标签 | 持续错价、低流动性和窗口截断不应被漏掉 |
| 固定 `delta_volume >= 10/50` | 仅要求 `delta_volume > 0` | 小成交也可能是真错价，量级留作证据 |
| 单个 AU 日内极值直接变通用阈值 | ticks + bps + 本合约历史噪声 | 单正样本不足以定义跨品种标准 |
| 自动推导乘数后直接使用 | 权威配置为主，自动推导只校验 | 乘数错误会把区间均价整体算错 |
| 从区间均价推断最低成交约 830 | 不推断精确最低价 | 聚合均价无法还原逐笔极值和成交分布 |

特别说明：若 `interval_vwap=940.52`，只能证明该区间至少存在价格不高于 `940.52` 的成交；不能仅凭均价和末笔价推出精确最低成交价。

## 3. 数据边界

当前数据是约 2Hz 的市场快照，不是逐笔成交明细。

可以使用：

- `Volume`、`Turnover` 差分得到区间成交量和区间成交均价。
- `LastPrice` 判断快照落盘时的末笔成交。
- 一档盘口与同品种其他月份判断当时市场合理价。
- 后续快照判断报价或新增成交是否回归。

不能得到：

- 区间内逐笔成交价格、顺序和方向。
- 精确最低价、被扫盘口档位和各档成交量。
- 挂单队列位置、必然成交结论和错单原因。

因此输出名称统一使用 `candidate_event`，不能使用 `confirmed_fat_finger`。

## 4. 数据准备

### 4.1 必需字段与元数据

快照字段：

```text
TradingDay, InstrumentID, UpdateTime, UpdateMillisec,
LastPrice, Volume, Turnover, AveragePrice,
BidPrice1, BidVolume1, AskPrice1, AskVolume1,
OpenInterest, UpperLimitPrice, LowerLimitPrice
```

每个支持品种必须配置：

```text
commodity, tick_size, contract_multiplier,
trading_session_profile, parameter_profile, validation_status
```

- `tick_size`、`contract_multiplier` 优先使用权威配置。
- 自动推导只做 sanity check，不能静默替代缺失配置。
- `trading_session_profile` 用于识别夜盘、日盘、休市和每段开盘保护期。
- 缺少元数据的品种记为 `unsupported_metadata`，不进入正式候选。
- 第一版只有 `AU` 的 `validation_status=validated`，使用 `AU_V1` 参数档；其他品种即使元数据齐全，也只记为 `unvalidated_commodity`，不输出正式候选。
- `AU tick_size=0.02`，`contract_multiplier=1000`。

### 4.2 正确时间轴

夜盘文件中的 `TradingDay=20260520 / UpdateTime=21:04:35.500` 不等于自然日 `2026-05-20 21:04:35.500`。对外分开保留：

```text
trade_date, update_time, snapshot_seq
```

内部使用交易日周期内单调递增的 `market_time_key`。不能直接按 `TradingDay + UpdateTime` 排序，否则午夜后 `00:xx` 会排到同一夜盘 `21:xx` 前面。

```text
if UpdateTime >= 21:00:00:
    cycle_millis = millis(UpdateTime, UpdateMillisec) - millis(21:00:00.000)
else:
    cycle_millis = millis(UpdateTime, UpdateMillisec) + millis(03:00:00.000)

market_time_key = (TradingDay, cycle_millis)
```

这个键只用于排序、asof 和时间差，不宣称为自然日时间。

### 4.3 同一时间键先合并

郑商所等数据中可能出现大量 `UpdateMillisec=0` 且同一秒多行。`snapshot_seq` 只能说明单个文件内顺序，不能说明不同合约在同一秒的先后关系，因此不做跨合约伪排序。

每个合约先按 `(market_time_key, snapshot_seq)` 排序，再把相同 `market_time_key` 合成一个可观测区间：

```text
snapshot_seq_start = 该时间键第一条原始行号
snapshot_seq_end = 该时间键最后一条原始行号

LastPrice / BidPrice1 / AskPrice1 = 最后一条原始行
Volume / Turnover / AveragePrice = 最后一条原始行
```

完成同键合并后，才对相邻的不同时间键做累计字段差分。这样既保留该秒最终可见状态，也不会虚构跨合约微观顺序。

### 4.4 基础派生与过滤

```text
delta_volume = Volume(t) - Volume(t-1)
delta_turnover = Turnover(t) - Turnover(t-1)
mid_price = (BidPrice1 + AskPrice1) / 2
spread_ticks = (AskPrice1 - BidPrice1) / tick_size
interval_vwap = delta_turnover / delta_volume / contract_multiplier
```

以下快照不触发：

- 非连续交易时段、每个 session 第一条、每段开盘后 `60s` 保护期内快照。
- 累计量/额回退或缺失，`delta_volume <= 0` 或 `delta_turnover <= 0`。
- 价格超出涨跌停有效范围，或买一大于卖一。
- 参考合约报价无效、过旧或数量不足。

`AveragePrice` 只用于校验数据源单位。以 AU 为例，应验证：

```text
Turnover / Volume ≈ AveragePrice
(Turnover / Volume) / contract_multiplier ≈ 累计成交均价的价格单位
```

不能拿已经除过 `contract_multiplier` 的值直接与原始 `AveragePrice` 比较。

这项校验只能确认数据单位，不能证明 `Volume` 与 `Turnover` 在每个快照同步更新；区间均价分支还必须执行 §7 的计数器错位确认。

## 5. 重建目标合理价

### 5.1 参考合约

对目标 T：

1. 只取同品种真实月份合约，排除连续合约和目标本身。
2. 目标之外不足 3 个同品种真实合约时，目标合约直接丢弃，不进入检测。
3. 离线阶段按当日总成交量取前 5 个候选参考合约；其中成交量前三记为 `top_volume_peers`。
4. 时刻 t 只保留报价有效、处于交易时段、age 不超过 `3s` 的合约。
5. 至少需要 2 个有效参考合约，且有效集合必须与 `top_volume_peers` 至少有一个合约重合；否则标记 `insufficient_peers`，不生成合理价或候选事件。

不先加 `daily_volume >= 5000`、`snapshot_count >= 55000` 等跨品种固定地板；活跃度排序、新鲜度和中位数已经能处理多数稀疏参考。若多日验证仍有污染，再增加相对流动性条件。

当天总成交量使用了未来信息，只适用于本阶段离线检测。以后回测必须改为前一交易日或当时可见数据。

“报价有效”对目标和 peer 使用同一规则：

```text
BidPrice1 > 0
AskPrice1 >= BidPrice1
不贴近涨跌停
spread_ticks <= baseline_spread_p95_ticks + 1
```

目标的每个 `target_mid(s)` 和 peer 的每个 `peer_mid_asof(s)` 都必须来自有效报价。`p95 + 1 tick` 给正常 spread 波动留一档余量，同时避免跨品种固定 spread 阈值。

### 5.2 正常跨月价差与 fair price

```text
baseline_window = [t - 300s, t - 10s]
basis_i(t) = median(target_mid(s) - peer_i_mid_asof(s))
fair_i(t) = peer_i_mid_asof(t) + basis_i(t)
fair_price(t) = median_i(fair_i(t))
fair_uncertainty_ticks = 1.4826 * MAD_i(fair_i(t)) / tick_size
```

约束：

- 最近 `10s` 不进基线，防异常萌芽污染正常价差。
- 基线不跨 session；每个 peer 至少覆盖 `60s` 且有 20 个有效配对点。
- 当前 peer 报价 age 均不超过 `3s`。
- `valid_peer_count >= 2` 且 `fair_uncertainty_ticks` 不超过上限。

这样保留了月份间正常升贴水。若全品种同步下跌，peer 当前价格会一起下移，`fair_price` 也跟随，不会误判为目标独有错价。

## 6. 两个有向成交信号

第一版只做向下异常。

```text
last_down_ticks = (fair_price - LastPrice) / tick_size
last_down_bps = (fair_price - LastPrice) / fair_price * 10000

vwap_down_ticks = (fair_price - interval_vwap) / tick_size
vwap_down_bps = (fair_price - interval_vwap) / fair_price * 10000
```

- `visible_execution_drop`：末笔成交明显低于合理价。
- `interval_execution_drop`：本快照区间成交均价明显低于合理价，用于发现末笔已恢复的隐藏事件。
- 两个分支是 OR；若同时命中，证据更完整，但不额外变成分数。
- `LastPrice` 只在 `delta_volume > 0` 时作为成交证据，避免使用陈旧末笔。

### 6.1 动态阈值

从事件前基线窗分别估计正常的末笔偏离和区间均价偏离。对每个历史时刻 s，都按 §5.2 使用它自己的 `[s-300s, s-10s]` 前置窗口重新得到 `fair_price(s)`：

```text
noise_rows = s ∈ [t - 300s, t - 10s]
             and data_eligible(s)
             and fair_price_reliable(s)
             and delta_volume(s) > 0

last_noise(s) = (fair_price(s) - LastPrice(s)) / tick_size
vwap_noise(s) = (fair_price(s) - interval_vwap(s)) / tick_size
execution_depth_noise(s) = max(last_noise(s), vwap_noise(s))

noise_sample_count >= 100
noise_time_span_seconds >= 120
noise_history_reliable = 上述两项同时满足

last_robust_sigma = 1.4826 * MAD(last_noise)
vwap_robust_sigma = 1.4826 * MAD(vwap_noise)
execution_depth_robust_sigma = 1.4826 * MAD(execution_depth_noise)

min_depth_bps_ticks = fair_price(t) * min_depth_bps / 10000 / tick_size

last_threshold_ticks = max(
    min_last_ticks,
    min_depth_bps_ticks,
    median(last_noise) + noise_k * last_robust_sigma
)

vwap_threshold_ticks = max(
    min_vwap_ticks,
    min_depth_bps_ticks,
    median(vwap_noise) + noise_k * vwap_robust_sigma
)
```

样本数或覆盖时长不足时，不退化到固定阈值，直接记 `insufficient_noise_history` 并禁止触发。开盘早期因此可能比 `60s` 保护期更长，这是为了避免用几条样本临时标定阈值。

第一轮 AU 起始值，仅用于启动验证：

```text
min_last_ticks = 20
min_vwap_ticks = 20
min_depth_bps = 5
noise_k = 8
fair_uncertainty_limit_ticks = 10
```

## 7. 突发性与候选触发

```text
candidate_execution_depth(t) = max(
    last_down_ticks(t) if last_down_ticks(t) >= last_threshold_ticks else -infinity,
    vwap_down_ticks(t)
        if vwap_down_ticks(t) >= vwap_threshold_ticks
        and interval counter confirmed
        else -infinity
)

historical_execution_depth(s) = max(
    last_down_ticks(s),
    vwap_down_ticks(s)
        if vwap_down_ticks(s) < vwap_threshold_ticks(s)
        or interval counter confirmed at s
        else -infinity
)
pre_depth = median(historical_execution_depth(s)), s ∈ [t - 3s, t)
onset_ticks = max(0, candidate_execution_depth(t) - max(0, pre_depth))
onset_threshold_ticks = max(8, 5 * execution_depth_robust_sigma)
```

如果前 3 秒没有新增成交，但报价与参考价连续有效，`pre_depth=0`；如果数据中断，`pre_depth` 记未知并禁止触发。

### 7.1 区间均价计数器确认

`interval_vwap` 单帧异常还不能排除 `Volume`、`Turnover` 更新错位。对初步命中 `vwap_threshold_ticks` 的快照，利用本阶段“离线检测”的边界，向后取同一 session 内完整 `1s` 窗口：

```text
combined_delta_volume_1s = sum(delta_volume(u)), u ∈ [t, t + 1s]
combined_delta_turnover_1s = sum(delta_turnover(u)), u ∈ [t, t + 1s]
combined_vwap_1s = combined_delta_turnover_1s
                   / combined_delta_volume_1s
                   / contract_multiplier
combined_vwap_down_ticks = (fair_price(t) - combined_vwap_1s) / tick_size

interval_execution_drop =
    vwap_down_ticks >= vwap_threshold_ticks
    and combined_vwap_down_ticks >= vwap_threshold_ticks
```

- `1s` 窗不完整、跨 session 或出现累计字段回退：标 `counter_sync_unconfirmed`，区间均价分支不触发。
- 单帧异常但合并后不异常，或下一帧出现反方向不可能均价：标 `counter_lag_suspect`，区间均价分支不触发。
- `visible_execution_drop` 独立判断；即使区间均价未确认，可见末笔分支仍可保留候选。
- `interval_confirmation_end_time` 记录完整确认窗的最后时间；以后任何回测不得假定区间均价证据在事件时刻 t 已经可见。

```text
visible_execution_drop = last_down_ticks >= last_threshold_ticks

is_candidate_snapshot =
    data_eligible
    and fair_price_reliable
    and noise_history_reliable
    and delta_volume > 0
    and onset_ticks >= onset_threshold_ticks
    and (visible_execution_drop or interval_execution_drop)
```

触发原因只允许：

```text
visible_execution_drop
interval_execution_drop
visible_execution_drop,interval_execution_drop
```

不再保留 `strong_visible_last_drop` 这类单样本救援分支。若动态阈值抓不到已知样本，应先检查时间轴、单位、合理价和噪声估计。

## 8. 事件合并与回归

同一合约候选间隔不超过 `10s` 时合并，跨 session 或数据断点不得合并：

```text
event_anchor = candidate_execution_depth 最大的候选
event_volume = event_start 至 event_end 内全部正 delta_volume 合计
event_depth_ticks = anchor.candidate_execution_depth
```

从锚点后观察 `3s` 和 `10s`。恢复阶段冻结锚点事件前的 peer 集合与 `basis_i(anchor)`，只更新 peer 当前价格，避免事件本身进入新的基线：

```text
recovery_fair_i(u) = peer_i_mid_asof(u) + basis_i(anchor)
recovery_fair_price(u) = median_i(recovery_fair_i(u))

last_remaining_ticks(u) = (recovery_fair_price(u) - LastPrice(u)) / tick_size
vwap_remaining_ticks(u) = (recovery_fair_price(u) - interval_vwap(u)) / tick_size
combined_vwap_remaining_ticks(u) =
    (recovery_fair_price(u) - combined_vwap_1s(u)) / tick_size
quote_remaining_ticks(u) = (recovery_fair_price(u) - BidPrice1(u)) / tick_size
```

不再用统一 `event_depth_ticks` 作为三个通道的恢复分母。每个锚点实际触发的成交通道分别判断：

```text
visible_channel_recovered(u) =
    anchor 触发 visible_execution_drop
    and u 有新增成交
    and last_remaining_ticks(u) < last_threshold_ticks(anchor)

interval_channel_recovered(u) =
    anchor 触发 interval_execution_drop
    and u 的完整 1s 合并窗口有效
    and combined_vwap_remaining_ticks(u) < vwap_threshold_ticks(anchor)

trade_recovered(u) = 锚点触发的全部成交通道均已 recovered
quote_recovered(u) = quote_remaining_ticks(u) < last_threshold_ticks(anchor)
```

回归标签：

- `trade_recovered_3s`：3 秒内全部触发成交通道回到各自锚点阈值以内。
- `quote_only_recovered_3s`：3 秒内报价回到阈值以内，但成交通道没有完整确认。
- `trade_recovered_10s`：3 秒未恢复、10 秒内成交通道恢复。
- `quote_only_recovered_10s`：10 秒内只有报价恢复。
- `persistent_10s`：10 秒仍未恢复。
- `truncated`：窗口跨 session、数据断点或参考价失效。

同时输出每个通道首次恢复的**确认可用时间**；未触发的通道记空值：

- 可见末笔和报价通道：`u - anchor_time`。
- 区间均价通道：完整 1 秒确认窗结束时间减去 `anchor_time`，即 `(u + 1s) - anchor_time`，不能把窗口起点误写成确认时间。

回归只增强人工判断，不改变候选是否输出。

## 9. 输出

第一版只输出：

1. `tick_candidate_events.csv`
2. `event_replay.html`

CSV 至少保留：

```text
检测器版本, 参数档, 验证状态,
事件编号, 交易日, 事件时间, 区间均价确认结束时间,
品种, 合约,
事件开始序号, 事件锚点开始序号, 事件锚点结束序号, 事件结束序号,
触发原因,
合理价, 合理价不确定性_跳, 有效参考合约数, 参考合约列表,
价差基线最少样本数, 噪声样本数, 噪声覆盖秒数,
最新成交价, 区间成交均价, 末笔向下偏离_跳, 末笔向下偏离_基点,
区间均价向下偏离_跳, 区间均价向下偏离_基点,
一秒合并成交均价, 一秒合并均价向下偏离_跳,
末笔触发阈值_跳, 区间均价触发阈值_跳,
突发偏离_跳, 区间增量成交量, 区间增量成交额, 事件成交量,
名义成交额缺口, 回归标签,
可见末笔恢复确认秒数, 区间均价恢复确认秒数, 买一恢复确认秒数,
数据质量标记
```

```text
notional_shortfall = max(0, fair_price - interval_vwap)
                     * delta_volume
                     * contract_multiplier
```

它只是相对合理价的成交额缺口证据，不是损失金额或可实现利润。

即使没有候选，CSV 也必须输出完整表头。

HTML 的所有表格表头都使用中文释义；内部计算字段名可以继续使用英文，但不得直接作为用户可见表头。

HTML 复盘窗口统一为：

```text
[事件锚点前 60 秒, 事件锚点后 10 秒]
```

目标合约展示两部分：

1. **目标合约检测明细表**：展示原始快照字段，以及区间增量成交量、区间成交均价、合理价、两类向下偏离、触发原因等检测字段。表头使用 `交易日、合约代码、更新时间、更新毫秒、最新成交价、累计成交量、累计成交额、买一价、买一量、卖一价、卖一量、原始平均价、持仓量、涨停价、跌停价、区间增量成交量、区间增量成交额、区间成交均价、合理价、末笔向下偏离_跳、区间均价向下偏离_跳、是否候选锚点`。
2. **目标合约事件摘要表**：使用上方 `tick_candidate_events.csv` 的中文表头，集中展示事件级指标。

每个参考合约在同一窗口内单独展示一张**原始快照表**。参考合约表只允许出现原始行情字段，不展示合理价、价差基线、偏离、阈值、噪声或回归等计算指标。表头固定为：

```text
交易日, 合约代码, 更新时间, 更新毫秒,
最新成交价, 累计成交量, 累计成交额,
买一价, 买一量, 卖一价, 卖一量,
原始平均价, 持仓量, 涨停价, 跌停价
```

参考合约原始表与目标合约使用完全相同的 `[事件锚点前 60 秒, 事件锚点后 10 秒]` 窗口，并遵守同一 session 截断规则；没有快照时显示“该窗口无原始快照”。

固定提示“聚合快照无法还原逐笔最低价”。

HTML 顶部必须包含每个合约的运行诊断，至少统计：

```text
原始行数
同时间键合并后行数
可检测行数
元数据阻断行数
交易时段阻断行数
参考合约阻断行数
历史噪声不足行数
计数器同步未确认行数
计数器错位嫌疑行数
候选事件数
```

这样空候选可以区分“确实无事件”和“整条检测链没有有效运行”，不新增第三个输出文件。

## 10. AU2606 标定锚点

```text
file = data/tick2026/202605/20260520/au2606_20260520.csv
TradingDay = 20260520
UpdateTime = 21:04:35.500
```

用 `au2608 / au2610 / au2612` 和事件前 `[t-300s, t-10s]` 基线只读复算：

```text
fair_price ≈ 995.33
fair_uncertainty_ticks ≈ 0.74
LastPrice = 993.92
interval_vwap ≈ 940.52
combined_vwap_1s ≈ 962.18
delta_volume = 370
noise_sample_count = 354
noise_time_span_seconds = 195
last_threshold_ticks ≈ 24.88
vwap_threshold_ticks ≈ 24.88
last_down_ticks ≈ 70.5
vwap_down_ticks ≈ 2740.3
combined_vwap_down_ticks ≈ 1657.7
onset_ticks ≈ 2740.1
visible_recovered_seconds = 0.5
interval_recovered_seconds = 1.5
recovery_label = trade_recovered_3s
```

两个成交分支均能命中，且区间均价异常在完整 `1s` 合并后仍然成立，不需要救援条件。严格可说的结论是该 500ms 区间量价加权均价约 `940.52`；不能断言精确最低成交价约为 `830`。

该样本只校验公式和数据单位，不证明起始阈值已适合所有品种。

## 11. 验收标准

### 11.1 必须通过

- 夜盘、午夜、日盘按交易日周期排列，累计字段只在同 session 内差分。
- `AP610` 这类同秒多行数据必须先合并；相同 `market_time_key` 不得直接参与跨合约 asof。
- AU 的 `interval_vwap` 与原始 `Volume / Turnover / AveragePrice` 单位一致。
- 合成“单帧低均价、下一帧反向补偿、1 秒合并后正常”的计数器错位样本，必须标 `counter_lag_suspect` 且不能靠区间均价分支触发。
- 少于 2 个 fresh peer 或 fair 分歧过大时不触发。
- `noise_sample_count < 100` 或 `noise_time_span_seconds < 120` 时必须标 `insufficient_noise_history`，不得退回固定阈值触发。
- 必须输出 `AU2606 / TradingDay=20260520 / 21:04:35.500`，且两个成交原因均命中。
- 该锚点应标 `trade_recovered_3s`，并分别给出可见末笔与区间均价通道的恢复时间。
- 无候选日仍输出 CSV 表头和 HTML 合约级阻断统计。
- 非 AU 品种在完成单独标定前只能记 `unvalidated_commodity`，不能输出正式候选。

### 11.2 必须挡住

- `delta_volume=0` 的盘口毛刺。
- 开盘、午间重开和收盘后结算快照。
- 全品种同步移动但跨月价差仍正常的行情。
- `AU2606 / TradingDay=20260520 / 11:08:09.000` 缓跌反例：`LastPrice=979.66`，实算 `last_down_ticks≈2.0`、`vwap_down_ticks≈0.75`，不得触发。
- 只有 `abs(interval_vwap - LastPrice)` 较大，但二者都没有向下偏离 `fair_price` 的快照。

### 11.3 冻结参数前

至少再扫描：

1. AU 正样本日前后各 5 个交易日。
2. 一个高流动性、价格单位不同的品种。
3. 一个远月较多、流动性分化明显的品种。

所有候选人工标注为 `保留 / 正常行情 / 数据问题 / 无法判断`。误报若无法由现有证据字段解释，优先修数据口径或合理价，不增加不透明评分。

## 12. 后置范围与实施顺序

后置：向上检测、挂单价、成交模拟、退出规则、收益、资金曲线和品种推荐。

实施顺序：

1. 修正时间轴、同时间键合并、session 差分和 AU 单位校验。
2. 只实现 `fair_price`、noise history 与参考质量诊断。
3. 实现两个有向成交分支、1 秒计数器确认和 onset，验证锚点与反例。
4. 增加事件合并、冻结 basis 的 3s/10s 同通道回归和 HTML 诊断。
5. 完成最小多日、多品种人工复核后冻结参数。
6. 检测稳定后另写回测方案，不在本文追加。

## 13. v2 双向扩展（2026-09-18）

第 12 节的“后置：向上检测”是 v1 的历史边界；主检测器现已升级为 `aggregated-tick-v2`，默认同时输出向下与向上疑似候选。

- 向下仍使用 `(fair_price - LastPrice) / tick_size` 与 `(fair_price - interval_vwap) / tick_size`；向上镜像使用 `(LastPrice - fair_price) / tick_size` 与 `(interval_vwap - fair_price) / tick_size`。
- 两个方向复用同一行的末笔、区间和 onset 阈值。区间通道仍必须通过完整 1 秒合并均价确认。
- 同一时间键可保留一个 `down` 和一个 `up` 候选；10 秒事件合并和锚点深度选择按方向隔离。
- 向下恢复继续检查买一，向上恢复检查卖一；CSV 保留旧向下字段，并新增 `异常方向`、向上偏离、卖一恢复和名义成交额超额字段。
- 参数生成、手工回放、程序化单次/网格回放以及旧深度迁移仍是低侧模型：读取新 CSV 时只消费 `down`，历史无方向 CSV 兼容视为 `down`。

该扩展只增加疑似候选的方向统计，不改变参考合约、合理价、噪声阈值或日线初筛，也不把任何结果表述为已确认错单。
