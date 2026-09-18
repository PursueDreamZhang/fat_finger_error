# 极简乌龙指历史统计模型（Mid + LastPrice）

## 1. 目标

这套模型只解决一个问题：

> 在历史行情中，寻找“最新成交价 `LastPrice` 明显偏离当前盘口中心 `Mid`，但盘口本身并没有同步移动”的孤立异常成交，并统计不同偏离阈值（例如 0.5%、1%、1.5%、2%）出现频率与后续恢复情况，从而为实际挂单距离提供数据依据。

模型刻意保持简单，不依赖：

- `Turnover`
- `AveragePrice`
- `ΔTurnover / ΔVolume`
- 五档盘口
- 复杂盘口冲击模型

第一版核心只使用：

- `BidPrice1`
- `AskPrice1`
- `LastPrice`
- `Volume`

其中 `BidVolume1 / AskVolume1` 可以保留，但不是第一版必需条件。

---

## 2. 为什么使用 Mid + LastPrice

### 2.1 Mid：市场当前盘口中心

定义：

\[
Mid_t = \frac{BidPrice1_t + AskPrice1_t}{2}
\]

它代表当前盘口的中心位置，用来回答：

> 这一刻市场大体认可的价格在哪里？

### 2.2 LastPrice：最新真实成交价

`LastPrice` 用来回答：

> 最近一笔真实成交发生在哪里？

真正值得研究的事件通常长这样：

```text
正常时：
Bid1 = 1199
Ask1 = 1200
Mid  = 1199.5

突然：
LastPrice = 1175

但当前盘口仍然：
Bid1 = 1198
Ask1 = 1199
Mid  = 1198.5
```

成交价明显下刺，但盘口中心几乎没动，这比“LastPrice 自己跌了多少”更接近孤立异常成交。

---

## 3. 第一个必要过滤：必须有新成交

历史快照里 `LastPrice` 可能长时间不更新，而 Bid/Ask 已经变化。

因此只有：

\[
\Delta Volume_t = Volume_t - Volume_{t-1} > 0
\]

时才检查 `LastPrice`。

规则：

```text
ΔVolume <= 0：
    不进行 Last-Mid 异常判断

ΔVolume > 0：
    才计算 Deviation
```

这一步可以过滤冷门合约中大量“旧 LastPrice + 新盘口”造成的假异常。

---

## 4. 数据有效性过滤

只有满足以下条件的记录才计算 Mid：

```text
BidPrice1 > 0
AskPrice1 > 0
BidPrice1 <= AskPrice1
LastPrice > 0
Volume >= 0
```

如果成交量出现回退（例如换交易日后 Volume 重置），该条记录的 `ΔVolume` 不用于新成交判断，并重新建立后续基准。

---

## 5. 核心指标：Last-Mid 偏离

定义有方向偏离：

\[
Deviation_t = \frac{LastPrice_t - Mid_t}{Mid_t}
\]

绝对偏离：

\[
AbsDeviation_t = |Deviation_t|
\]

方向解释：

```text
Deviation < 0：LastPrice 位于 Mid 下方，关注向下异常成交
Deviation > 0：LastPrice 位于 Mid 上方，关注向上异常成交
```

例如：

```text
Mid = 1200
LastPrice = 1176
```

则：

\[
Deviation = -2\%
\]

---

## 6. 候选阈值

建议第一轮不要只测 2% 和 4%，而是一次性测试多个距离：

```text
0.25%
0.50%
0.75%
1.00%
1.25%
1.50%
2.00%
2.50%
3.00%
```

如果样本中极端行情更多，还可以继续增加：

```text
3.50%
4.00%
5.00%
```

### 向下候选

当：

\[
Deviation_t \le -Threshold
\]

则标记为向下候选。

### 向上候选

当：

\[
Deviation_t \ge Threshold
\]

则标记为向上候选。

---

## 7. Raw 候选与 Strict 候选

只看 `Last-Mid` 有一个典型假信号：冷门合约价差很宽。

例如：

```text
Bid1 = 1112
Ask1 = 1124
Mid  = 1118
Last = 1124
```

Last 相对 Mid 偏离约 0.54%，但 Last 只是正常成交在 Ask1，并没有脱离当前盘口。

因此脚本同时输出两种口径。

### Raw

只要求：

```text
ΔVolume > 0
并且 |Last-Mid| / Mid >= Threshold
```

适合研究偏离分布。

### Strict

额外要求最新成交已经跑出当前一档盘口：

向下：

```text
LastPrice < BidPrice1
```

向上：

```text
LastPrice > AskPrice1
```

Strict 更适合作为“疑似孤立异常成交”样本。

说明：Strict 是保守过滤，会漏掉一部分盘口在异常成交后尚未来得及恢复的事件，所以 Raw 和 Strict 都应保留。

---

## 8. 不要把连续快照重复算成很多次事件

假设连续数据：

```text
10:00:00.0  Deviation = -2.1%
10:00:00.5  Deviation = -2.4%
10:00:01.0  Deviation = -2.2%
10:00:01.5  Deviation = -1.8%
```

不能简单算成 4 次独立异常。

脚本按 `event_merge_seconds` 合并相邻候选。

默认建议：

```text
event_merge_seconds = 5
```

同一合约、同一方向、同一阈值下，如果两个候选间隔不超过该值，则视为同一个事件，并保留该事件中 `|Deviation|` 最大的那条作为代表点。

---

## 9. Anchor Mid

对于每个独立事件：

- `event_mid`：事件代表点当前 Mid
- `anchor_mid`：事件之前最近一个有效 Mid
- `event_last`：异常事件 LastPrice

同时记录：

\[
MidMove = \frac{event\_mid - anchor\_mid}{anchor\_mid}
\]

如果：

```text
Last 偏离很大
但 MidMove 很小
```

通常比“Last 和 Mid 一起移动”更值得关注。

脚本默认不强制用 `MidMove` 过滤，而是输出该指标，便于后续根据真实分布决定阈值。

---

## 10. 模拟挂单价

如果某个阈值为 `D`，以事件 Mid 为锚点：

### 向下异常（模拟买单）

\[
FillPrice = Mid \times (1-D)
\]

### 向上异常（模拟卖单）

\[
FillPrice = Mid \times (1+D)
\]

注意：历史快照只能证明 LastPrice 曾经达到/越过阈值，不能证明实盘排队中的限价单一定成交。因此这里得到的是“候选触发”，不是精确成交回测。

---

## 11. 后续恢复统计

对每个独立事件观察：

```text
1 秒
3 秒
5 秒
10 秒
30 秒
60 秒
```

之后的 Mid。

### 向下事件

假设：

```text
Anchor Mid = 1200
模拟 Fill = 1176
5 秒后 Mid = 1195
```

恢复比例：

\[
RecoveryRatio = \frac{Mid_{future}-FillPrice}{AnchorMid-FillPrice}
\]

### 向上事件

\[
RecoveryRatio = \frac{FillPrice-Mid_{future}}{FillPrice-AnchorMid}
\]

解释：

```text
0%    ：还在模拟成交价附近
50%   ：恢复了一半
80%   ：基本恢复
100%  ：回到异常前锚点
>100% ：出现反向超调
```

脚本统计：

```text
Recovery50
Recovery80
Recovery100
```

分别在 1s / 3s / 5s / 10s / 30s / 60s 的命中率。

---

## 12. MFE / MAE

默认观察事件后 60 秒。

### 向下候选（模拟买入）

MFE（最大有利波动）：

\[
MFE = \frac{MaxFutureMid-FillPrice}{FillPrice}
\]

MAE（最大不利波动）：

\[
MAE = \frac{FillPrice-MinFutureMid}{FillPrice}
\]

### 向上候选（模拟卖出）

MFE：

\[
MFE = \frac{FillPrice-MinFutureMid}{FillPrice}
\]

MAE：

\[
MAE = \frac{MaxFutureMid-FillPrice}{FillPrice}
\]

MAE 尤其重要：它能告诉你“如果这次不是孤立异常，而是真趋势继续走，成交后最多还会逆向走多少”。

---

## 13. 时间段与休市处理

不要让 10:15 的事件拿 10:30 之后的数据做“15 秒后恢复”。

脚本通过 `session_gap_seconds` 自动分段。

默认：

```text
session_gap_seconds = 300
```

即相邻两条数据超过 5 分钟，则认为进入新时间段，恢复统计不能跨段。

如果你的数据非常稀疏，可以适当调大；如果分析主力合约，300 秒通常较保守。

---

## 14. 为什么第一版不使用 Turnover / AveragePrice

当前已检查的数据中，存在：

```text
Turnover = AveragePrice × Volume
```

且 Turnover 会出现负增量。

因此不能把：

\[
\Delta Turnover / \Delta Volume
\]

可靠解释为区间成交均价。

这套极简模型完全不需要 Turnover，因此不会受到这一问题影响。

---

## 15. 输出结果应该怎么看

脚本会重点输出：

### deviation_distribution.csv

统计新成交记录中 `|Last-Mid|/Mid` 的分位数：

```text
Q90
Q95
Q99
Q99.5
Q99.9
Q99.99
MAX
```

用于了解正常偏离量级。

### threshold_summary.csv

每个：

```text
合约 × 方向 × 阈值 × Raw/Strict
```

统计：

- 有效交易日数
- 候选行数
- 独立事件数
- 平均每天事件数
- Recovery50/80/100 @ 1/3/5/10/30/60s
- MFE 平均/中位数
- MAE 平均/中位数/P95

### event_details.csv

保存每个独立事件明细，方便人工回放真正极端的事件。

---

## 16. 如何根据结果选择 0.5%、1%、1.5%、2% 或更远

不要只看“距离越远胜率越高”。距离越远，样本越少，很容易被小样本骗到。

建议按以下顺序：

### 第一步：排除正常噪声区

如果某阈值每天都有大量 Raw/Strict 事件，则太近。

### 第二步：要求足够独立事件

例如：

```text
2.0%：120 个事件
2.5%：60 个事件
3.0%：8 个事件
4.0%：1 个事件
```

不能因为 4% 的 Recovery100=100% 就说 4% 最优。

### 第三步：看快速恢复

乌龙指/流动性瞬时失衡重点看：

```text
Recovery80 @ 1s
Recovery80 @ 3s
Recovery80 @ 5s
```

恢复越快，越符合策略假设。

### 第四步：看尾部风险

重点看：

```text
P95 MAE
```

如果某阈值虽然恢复率高，但 P95 MAE 很大，说明一旦误接正常趋势会很危险。

### 第五步：优先选择“最小的满足条件的距离”

如果：

```text
1.5%：假信号多
2.0%：样本充足、5s Recovery80 高、P95 MAE 可接受
2.5%：效果只略好，但机会少一半
3.0%：样本太少
```

则更应该先研究 2.0%，而不是盲目选择更远的 3% 或 4%。

---

## 17. 买卖方向必须分开

不要把向上和向下异常混在一起。

最终参数可能是：

```text
SA BuyDistance  = 1.75%
SA SellDistance = 2.00%
```

这是正常现象，因为商品上涨和下跌的尾部结构可能不对称。

---

## 18. 合约也必须分开

不要直接把：

```text
SA605
SA606
SA607
SA608
SA609
SA610
```

全部混成一个“SA”。

主力与远月的：

- Spread
- 流动性
- Last 更新频率
- 极端偏离

差异很大。

推荐先分别统计每个 `InstrumentID`，再根据主力/次主力角色比较。

---

## 19. 推荐历史长度

单日数据只能验证算法，不能确定实盘参数。

最低建议：

```text
30 个交易日
```

更推荐：

```text
60～120 个交易日
```

最好包含：

- 平静日
- 趋势日
- 暴涨暴跌日
- 夜盘
- 主力换月附近
- 消息冲击日

---

## 20. 推荐工作流

```text
大量历史 CSV
    ↓
计算有效 Mid
    ↓
计算 ΔVolume
    ↓
只保留 ΔVolume > 0 的新成交记录
    ↓
计算 Last-Mid Deviation
    ↓
分别测试：
0.25 / 0.5 / 0.75 / 1 / 1.25 / 1.5 / 2 / 2.5 / 3%
    ↓
Raw + Strict 两种候选
    ↓
合并连续记录为独立事件
    ↓
统计 1/3/5/10/30/60s Recovery
    ↓
统计 MFE / MAE
    ↓
按：合约 + 方向 + 阈值 比较
    ↓
选择机会频率 / 快速恢复 / 尾部风险的甜蜜点
```

---

## 21. 重要限制

这套模型是“历史候选事件统计”，不是精确撮合回测。

你的 500ms / 1s 或不规则快照无法知道：

- 每一笔逐笔成交顺序
- 限价单排队位置
- 你的订单是否真的成交
- 100ms 内盘口如何变化

因此最终确定实盘距离后，还应该使用 CTP 实时行情进行一段时间仿真验证。
