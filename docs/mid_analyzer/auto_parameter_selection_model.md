# 乌龙指策略自动选参模型

## 1. 目标

本模型把前面两套模型串起来，最终自动输出每个合约、每个方向的：

```text
SafeDistance
TargetDistance
MinDistance
MaxDistance
```

对应含义：

- `SafeDistance`：正常行情波动模型给出的安全下限。
- `TargetDistance`：满足安全性和机会条件后，选择的最小合格挂单距离。
- `MinDistance` / `MaxDistance`：实盘每秒检查时使用的价值带。

最终示例：

```text
SA BUY
SafeDistance   = 0.82%
TargetDistance = 1.25%
MinDistance    = 0.875%
MaxDistance    = 1.625%

SA SELL
SafeDistance   = 1.03%
TargetDistance = 1.50%
MinDistance    = 1.05%
MaxDistance    = 1.95%
```

---

# 2. 三层参数链

## 第一层：正常波动模型

使用 `volatility_quantiles.csv`。

核心指标：

### BUY

\[
E^-_H(t)=
\frac{Mid_t-\min(Mid_{t:t+H})}{Mid_t}
\]

### SELL

\[
E^+_H(t)=
\frac{\max(Mid_{t:t+H})-Mid_t}{Mid_t}
\]

第一版建议：

```text
H = 5 秒
Percentile = Q99.9
SafetyFactor = 1.2
```

计算：

\[
SafeDistance=Q_{99.9}(E_H)\times SafetyFactor
\]

例如：

```text
Q99.9 = 0.68%
SafetyFactor = 1.2
```

得到：

```text
SafeDistance = 0.816%
```

它表示：

> 小于这个距离的候选挂单直接淘汰。

---

# 3. 第二层：异常成交机会模型

读取前一套 `Mid + LastPrice` 模型输出的 `event_details.csv`。

只研究：

```text
ΔVolume > 0
```

时出现的独立异常成交事件。

候选距离：

```text
0.25%
0.50%
0.75%
1.00%
1.25%
...
```

默认步长：

```text
0.25%
```

对于每个候选距离 `d`：

```text
abs(LastPrice - Mid) / Mid >= d
```

则认为该异常事件能够触及这个挂单距离。

BUY 和 SELL 分开统计。

---

# 4. FollowRatio

希望判断：

> Last 瞬间走出去以后，整个市场的 Mid 有没有跟着走。

理论定义：

\[
FollowRatio=
\frac{
未来H秒Mid最大同方向移动
}{
Last相对事件Mid的异常偏离
}
\]

第一版：

```text
H = 5 秒
```

定义：

```text
FollowRatio <= 25%
```

为：

```text
IsolatedEvent = True
```

也就是：

> Last 走得很远，但 Mid 最多只跟了异常距离的 25%。

---

## 4.1 当前 event_details.csv 的兼容处理

旧版 `event_details.csv` 已经有：

```text
event_mid
mid_after_5s
abs_deviation_pct
```

但没有直接保存：

```text
5 秒窗口内 Mid 最大同方向移动
```

因此自动选参脚本支持两种模式。

### 优先模式

如果输入里存在：

```text
follow_ratio_5s
```

直接使用。

这是推荐口径。

### 兼容模式

如果没有该字段，则使用：

```text
event_mid -> mid_after_5s
```

计算终点近似 FollowRatio。

BUY/down：

\[
FollowMove=
\max\left(
0,
\frac{eventMid-midAfter5s}{eventMid}
\right)
\]

SELL/up：

\[
FollowMove=
\max\left(
0,
\frac{midAfter5s-eventMid}{eventMid}
\right)
\]

然后：

\[
FollowRatio=
\frac{FollowMove}{LastDeviation}
\]

输出会把这种情况标记为：

```text
endpoint_approx
```

注意：

> 终点近似可能漏掉“5 秒中途曾经大幅跟随、随后又恢复”的情况。

因此后续建议把前面的异常事件脚本升级，直接输出真正的 `follow_ratio_5s`。

---

# 5. 自动选参硬条件

对每个候选距离 `d`，依次判断：

## 条件 1：超过安全下限

\[
d\ge SafeDistance
\]

## 条件 2：异常事件样本足够

默认：

```text
EventCount >= 30
```

## 条件 3：孤立事件比例足够高

\[
IsolationRate=
\frac{IsolatedEventCount}{EventCount}
\]

默认：

```text
IsolationRate >= 70%
```

## 条件 4：机会频率足够

\[
MonthlyOpportunity=
\frac{IsolatedEventCount}
{ObservedTradingDays}
\times TradingDaysPerMonth
\]

默认：

```text
TradingDaysPerMonth = 21
MonthlyOpportunity >= 3
```

为了准确计算这个指标，建议显式传入：

```text
--observed-trading-days 120
```

如果不传，脚本只能从事件文件里的 `TradingDay` 推算，这会忽略“完全没有事件的交易日”，从而高估机会频率。

---

# 6. TargetDistance 的选择原则

不是选择最远的，也不是选择 IsolationRate 最高的。

而是：

\[
\boxed{
TargetDistance
=
\min
\left\{
d:
\begin{array}{l}
d\ge SafeDistance\\
EventCount(d)\ge N_{min}\\
IsolationRate(d)\ge I_{min}\\
MonthlyOpportunity(d)\ge O_{min}
\end{array}
\right\}
}
\]

也就是：

> 在所有满足风险和机会条件的候选距离里，选择最近的一档。

原因：

- 更近：异常成交机会更多；
- 太远：虽然看起来安全，但可能一年也成交不了几次；
- 安全性已经由硬条件限制，不需要继续无意义地加远。

---

# 7. 第三层：生成实盘价值带

得到：

```text
TargetDistance
```

以后生成对称价值带：

\[
MinDistance=TargetDistance\times(1-BandRatio)
\]

\[
MaxDistance=TargetDistance\times(1+BandRatio)
\]

第一版：

```text
BandRatio = 30%
```

例如：

```text
TargetDistance = 1.25%
```

得到：

```text
MinDistance = 0.875%
MaxDistance = 1.625%
```

---

# 8. 实盘运行逻辑

## BUY

\[
EffectiveDistance=
\frac{Mid-OrderPrice}{Mid}
\]

每秒检查一次：

```text
如果 MinDistance <= EffectiveDistance <= MaxDistance:
    KEEP

否则:
    NewOrderPrice = Mid * (1 - TargetDistance)
    撤旧单
    挂 NewOrderPrice
```

## SELL

\[
EffectiveDistance=
\frac{OrderPrice-Mid}{Mid}
\]

每秒：

```text
如果 MinDistance <= EffectiveDistance <= MaxDistance:
    KEEP

否则:
    NewOrderPrice = Mid * (1 + TargetDistance)
    撤旧单
    挂 NewOrderPrice
```

按照当前设计：

**不要增加：**

```text
ConfirmTime
MinRepriceTicks
Cooldown
```

因为策略本身已经按 1 秒周期计算，保持状态机简单。

---

# 9. 推荐第一版参数

```text
HorizonSeconds         = 5
Percentile             = 99.9
SafetyFactor           = 1.2

CandidateStep          = 0.25%
MinEvents              = 30

MaxFollowRatio         = 25%
MinIsolationRate       = 70%
MinMonthlyOpportunity  = 3

TradingDaysPerMonth    = 21

BandRatio              = 30%
```

这些参数是第一轮研究参数，不是永久固定值。

---

# 10. 脚本输入

需要：

```text
volatility_quantiles.csv
event_details.csv
```

运行示例：

```bash
python auto_parameter_selector.py \
  --volatility ./result/volatility_quantiles.csv \
  --events ./result/event_details.csv \
  --output ./auto_result \
  --observed-trading-days 120
```

完整参数示例：

```bash
python auto_parameter_selector.py \
  --volatility ./result/volatility_quantiles.csv \
  --events ./result/event_details.csv \
  --output ./auto_result \
  --horizon 5 \
  --percentile 99.9 \
  --safety-factor 1.2 \
  --candidate-step 0.0025 \
  --min-events 30 \
  --max-follow-ratio 0.25 \
  --min-isolation-rate 0.70 \
  --min-monthly-opportunity 3 \
  --band-ratio 0.30 \
  --trading-days-per-month 21 \
  --observed-trading-days 120 \
  --mode strict
```

注意：

```text
--candidate-step 0.0025
```

表示：

```text
0.25%
```

脚本内部距离统一使用小数：

```text
0.01 = 1%
```

---

# 11. 输出

## auto_parameters.csv

最终参数：

```text
InstrumentID
Direction
SafeDistance
TargetDistance
MinDistance
MaxDistance
EventCount
IsolatedEventCount
IsolationRate
MonthlyOpportunity
FollowRatioSource
Status
```

## candidate_evaluation.csv

保留每个候选距离的完整筛选过程：

```text
InstrumentID
Direction
CandidateDistance
SafeDistance
PassSafeDistance
EventCount
PassMinEvents
IsolatedEventCount
IsolationRate
PassIsolationRate
MonthlyOpportunity
PassMonthlyOpportunity
Qualified
```

建议不要只看最终结果。

第一次跑 60～120 天历史数据时，重点检查：

```text
candidate_evaluation.csv
```

确认为什么某个距离被淘汰、为什么下一档被选中。

---

# 12. 最终结构

```text
历史行情
   │
   ├──── 正常 Mid 波动 ────> SafeDistance
   │
   └──── Mid + Last 异常 ──> Event / Isolation / Opportunity
                                 │
                                 ▼
                         候选距离硬条件筛选
                                 │
                                 ▼
                         最小合格 TargetDistance
                                 │
                                 ▼
                   Min / Target / Max 价值带
                                 │
                                 ▼
                         实盘每秒检查一次
```

这就是当前版本完整的自动参数标定流程。
