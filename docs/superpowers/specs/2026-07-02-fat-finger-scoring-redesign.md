# 期货乌龙指日线初筛器 评分标准设计（redesign 草案）

> 本文档是三轮代码 + 数据对抗审查之后，按当前理解重写的评分设计草案，不是增量重构计划。它描述"评分体系应该长什么样"，作为对 `2026-06-02-fat-finger-scoring-design.md` 的整体替代提案。结构性的设计决策在此定死；只有真正依赖实测的参数（k 倍数、等级线）标"待标定"。
>
> 三轮审查的关键教训已内化为 §2 的设计原则。最深的几条：① 命中泛滥的机械根因是 `min_periods=10` 让短历史合约拿到偏低抖动的分位阈值；② C 的绝对阈值 fallback 是 P0 堵不住的漏点（A 无 fallback、C 有，这个不对称是病根）；③ 单档 ratio 外推不能预测最终命中率，参数必须用真实 before/after 标定；④ 日线度量的是"异常日"，不是"乌龙指"。

## 1. 文档目标

约束日线初筛器的评分对象、公式、阈值规则与边界。不负责数据访问层、HTML 结构、用户入口。

## 2. 核心设计原则（三轮审查沉淀）

1. **诚实命名**：评分对象是"单合约-交易日的异常程度"。日线 OHLC 无法直接观测瞬时乌龙指，本系统是异常日初筛器，不是乌龙指判定器。命名、等级标签、下游期望都要反映这点。
2. **历史门槛是硬约束，不是副效应**：所有分位阈值必须建立在足够长、足够稳定的有效历史上。`min_periods` 是一号旋钮——历史不足就判不出有意义的极值，而不是"硬给个分"。
3. **A 与 C 对称**：历史不足时分项一律不得给分。不存在"某一路绝对阈值兜底直接给满分"的特权通道。C 的原 fallback 正是这种特权通道，是命中泛滥的主要漏点。
4. **满分必须稀有**：任何分项给满分都要"显著超出历史极值"，不是"刚好碰到分位线"。靠倍数（k）+ 长窗口共同保证稀有，不靠堆档位。
5. **参数必须实测**：所有影响命中率的参数（窗口、min_periods、k、等级线）一律以真实 full_run 的 before/after 为准；单档 ratio 占比的外推不算数（曾因此把命中率估错一个数量级）。
6. **日线层面，量的维度排除**：日线成交量无法反映瞬时错单（被全天稀释）。volume 仅作流动性过滤，不作异常确认；"是否尖刺"只能靠价格形态。
7. **改 A/C/E，不改 level**：可信度问题在 E 里扣分，不在等级层补提分（沿用原设计 §7.3）。

## 3. 评分对象与历史窗口

- 评分对象：单 `品种-合约-交易日`。
- 历史窗口：每个分项统一向前 **60 个有效交易日**（约一季度），不含当日，最少 **30** 个有效样本（`ROLLING_WINDOW=60`，`ROLLING_MIN_PERIODS=30`）。
  - 一号旋钮是 min_periods：它决定"分位阈值是否可信"。历史不足 30 → 该分项无阈值 → 不给分（与 §2.3 对称原则一致）。
- 数据缓冲：`TRADING_LOOKBACK_BUFFER_DAYS = 100` 自然日（实测 60 交易日的自然日跨度 max=98、p95=94，100 才不赌假期）。
- 历史样本口径：A 用 `sample_status=="valid"`；C 用 `valid 且 active_peer_count>=2`（结构残差在 peer<2 时无意义）。

## 4. 总分公式

```
candidate_score = clip(A + C + E, 0, 100)
```

- A 自身极值异常分（0~20）
- C 同品种结构异常分（0~60）
- E 可信度惩罚分（-20~0）

无效样本（`sample_status != valid` 或 `active_peer_count == 0`）统一 `A=0, C=0, E=-20, candidate_score=0`，保留在结果中用于解释。

## 5. A 分（自身极值异常，0~20）

### 5.1 单信号设计（撤掉 A2）

原设计的 A1（range_pct）+ A2（extreme_pct）高度共线：实测 `extreme_pct ∈ [range_pct/2, range_pct]`，A1≥8 的样本里 75% 同时 A2≥5。A2 不提供独立证据。**A 收缩为单信号**：

```
range_pct = (high - low) / close
```

"是不是单边尖刺"由 C 维度回答（结构残差天然分上下半：upper/lower residual），不由 A 重复表达。

### 5.2 分母仍用 close

`A` 用 `close` 而非 `pre_close`，沿用原设计 §3.2.1 的有意识取舍：A 衡量"相对当天最终价格尺度，盘中极值有多极端"，不混入前一日价格锚。

已知边界（接受，不主动改）：异常发生在临近收盘、close 被污染时，分母被抬高，A 可能漏报"未修复的强异常"。**仅在真实 before/after 出现已知未修复尖刺被明显低估（A 分比同款已修复尖刺低 ≥3 分）时**，才引入 `pre_close` 作为 scoped guard。无证据前不动。

### 5.3 评分

拿 `range_pct` 与该合约过去 60 个有效交易日的分布比较，触发统一加 **k 倍显著超出**（k 待标定，起始 1.5）：

- `< k × range_q95` → 0
- `[k × range_q95, k × range_q99)` → 8
- `>= k × range_q99` → 20

`range_q99` 在真实数据无 0 值，A 分支不需要 fallback；`range_q99` 缺失（历史不足 30）→ A=0。

## 6. C 分（同品种结构异常，0~60）——核心外科

### 6.1 同日结构基线（不变）

对目标合约，取同日活跃可比合约（`valid 且 volume>0`，按 volume 排序前 5），记 `active_peer_count = 该集合去掉目标后的数量`。

```
peer_high_median, peer_low_median = 活跃可比合约 high/low 的中位数
peer_range_median = median((peer.high - peer.low) / peer.close)
upper_residual = max(0, (high - peer_high_median) / close)
lower_residual = max(0, (peer_low_median - low) / close)
raw_structure_residual = max(upper_residual, lower_residual)
excess_structure_residual = max(0, raw_structure_residual - peer_range_median)
normalized_structure_residual = excess_structure_residual / peer_range_median
```

`uniqueness_gap`：目标 `normalized_structure_residual` 减同日其他活跃合约同量的中位数，只保留正值。

### 6.2 关键修正一：堵 fallback 漏点（对称原则）

原 `_meets_threshold_with_fallback` 在 `structure_q99 <= 0 或 NaN` 时走绝对阈值 fallback（0.25/0.5/1.0），其中 0.25/0.5 极易命中、95.6% 直接拿 C1 满分 40。这是命中泛滥的主要漏点，且 **k 倍数对它无效**（绝对阈值不乘 k）。

修正：**按 q99 的状态分三种情况，缺历史的样本不再走兜底**：

| structure_q99 状态 | 含义 | C1/C2 处理 |
|---|---|---|
| `NaN` | 历史不足（min_periods 内未攒够 peer≥2 的有效日） | **C1=0, C2=0**（对齐 A，无阈值不给分） |
| `== 0` | 历史充足但该合约长期零结构残差（真·calm） | 走**收紧后**的绝对阈值（见 6.3） |
| `> 0` | 正常 | 走 k 倍分位（见 6.4） |

> 这条直接切掉 round-3 Z2 的漏点：min_periods 提到 30 后，一批短历史合约的 q99 由有值变 NaN，原设计把它们推进 fallback 满分通道；新设计让它们 C=0。calm（q99==0，历史充足）仍保留 fallback 意图，但收紧阈值。

### 6.3 关键修正二：calm 兜底阈值收紧

q99==0 分支（真 calm，历史充足）继续用绝对阈值（保留原设计 §5.4 C1 fallback 的初衷：避免 calm 合约的孤立尖刺拿不到 C），但阈值从过松的 0.25/0.5/1.0 收紧：

- `normalized < 2.0` → 0
- `[2.0, 3.0)` → 12
- `>= 3.0` → 40

（"excess 超出 peer 正常波动带 2 倍"才算显著异常。）

C2 同理：`uniqueness_gap < 1.0 → 0; [1.0,2.0) → 5; >= 2.0 → 20`。

### 6.4 关键修正三：正常分支加 k 倍显著超出

q99>0 分支，触发统一加 k 倍（与 A 一致）：

**C1（0~40）**：
- `< k × structure_q95` → 0
- `[k × structure_q95, k × structure_q99)` → 12
- `>= k × structure_q99` → 40

**C2（0~20）**：
- `< k × uniqueness_q95` → 0
- `[k × uniqueness_q95, k × uniqueness_q99)` → 5
- `>= k × uniqueness_q99` → 20

> 不再在 P99 之上堆更多档位（round-3 实测中间档几乎空转、无区分度）。饱和靠"让满分稀有"解决（k 倍 + 堵漏 + 长 min_periods 三者合力），不靠档位细分。若实测 high 群体仍过大且全压在满分，再加档，由真实分布决定切点，不预固定。

### 6.5 关键修正四：peer 数衰减（C 上限）

单/少 peer 的 median 是点估计，不可靠。C 上限随 peer 数衰减，规则统一为"**active_peer_count < 4 不可达 high**"：

```
C_cap = {1: 24, 2: 44, 3: 44, >=4: 60}[active_peer_count]
C_score = min(C1 + C2, C_cap)
```

peer==1 → 最高 24（必 low）；peer∈{2,3} → 最高 44（A=20+C=44+E=0=64 < 65，必 medium 及以下）；peer≥4 → 不受限。

> 诚实定位：此 cap 只修 peer<4 的不可靠对照（当前约占 high 的 6/71）。high 主体（peer≥4）的压缩由 §6.2~6.4 的阈值收紧承担，不由 cap 解决。

## 7. E 分（可信度，-20~0）

E 取所有适用惩罚中最重的一档（不累加）：

| E | 条件 |
|---|---|
| 0 | valid 且 active_peer_count≥3 且无下述任何弱旗标 |
| -5 | valid 且 active_peer_count==2 且无弱旗标；**或换月日**（`main_reference_changed_today==True`，基线跳变） |
| -10 | valid 且 active_peer_count==1；或 `peer_comparability_weak_flag`；或 `target_liquidity_weak_flag` |
| -20 | `sample_status != valid`；或 `active_peer_count == 0` |

弱旗标定义（沿用原设计 §3.4）：
- `peer_comparability_weak_flag`：`active_peer_count < max(2, rolling_median_active_peer_count - 1)`
- `target_liquidity_weak_flag`：`volume < 0.2 × rolling_median_volume 且 volume < 0.3 × peer_volume_median`（volume 仅作流动性过滤，见 §2.6）

**换月日（新增）**：`main_reference_changed_today` 已在 `reference_selection.py` 算好并 merge 进 df，scoring 直接读列。落地前先统计换月日 vs 非换月日的命中率，确认换月日确实偏高再扣 5。

## 8. 候选等级

### 8.1 分数→等级（阈值待标定）

P0+min_periods+k+堵漏 落地后，绝对分整体下移，30/50/65 不再适用。**等级线必须用真实 full_run 的分数分布重新标定**，目标 medium+high 命中率落在 0.3%~1%。标定方法：落地后跑 before/after，按分数分位反推等级线；起始建议 28/47/62，由实测定。

### 8.2 结构约束（等级层，沿用原 §7.2）

- `C < 20` → 最高 low
- `C < 35` → 不能 high

（这是基于核心信号强度的约束，不违反 §2.7。peer 数的不可靠已由 §6.5 在分值层 C_cap 处理，不再在 level 层封顶。）

## 9. 参数标定状态

| 参数 | 值 | 状态 |
|---|---|---|
| ROLLING_WINDOW | 60 | 已定（实验支撑：20→60 单档命中率 6.4%→3.85%） |
| ROLLING_MIN_PERIODS | 30 | 已定（一号旋钮，治理短历史 q99 不稳） |
| TRADING_LOOKBACK_BUFFER_DAYS | 100 | 已定（实测 max 98） |
| k（显著超出倍数） | 1.5 起始 | **待标定**（需真改 `_meets_threshold` 跑 before/after；外推无效） |
| 等级线 | 28/47/62 起始 | **待标定**（依赖 k 与分数分布的实测） |
| C calm 兜底阈值 | 2.0/3.0 | 起始建议，**待复核**（calm 占比小，M5 监控） |
| C_cap 表 | {1:24, 2:44, 3:44, ≥4:60} | 已定 |

待标定的参数必须一次性、用同一份 full_run before/after 联合标定，不能各调各的。

## 10. 已知边界与未覆盖

**本设计不解决的（诚实声明）**：

1. **日线无法识别瞬时乌龙指**。本系统输出"异常日候选"，映射到"疑似乌龙指"是未经验证的先验，需人工结合分时/逐笔/盘口确认。
2. **涨跌停未处理**。单合约锁板、同品种其他月份未锁（临近交割常见）会被当结构异常，是确定性误报源。`pre_settle` 在数据里有但 scoring 未用。**未覆盖，待真实 before/after 看涨跌停类误报占比再决定是否立项**（需品种板幅数据）。
3. **A 分母 close 的污染盲区**（§5.2），接受，挂证据触发。
4. **`range_pct==0`（high==low，锁板/脏数据）样本**：当前约 285 valid 条，其中 26 条靠结构残差拿 medium。本设计未单独处理，依赖数据层 `high < low` 校验和 §6 的结构判断自然过滤；落地后核对这批样本的归属。

**本轮确定不做的**：不引入 tick/分时；不引入成交量作异常确认；不动数据访问层；不做跨品种绝对阈值库；不改 HTML 报告结构（仅 `trigger_reasons` 文案新增"换月日"）。
