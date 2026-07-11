# Tick 级乌龙指 定义与检测标准（成交价价差突破 + 增量成交均价确认）

> 本文档锁定"乌龙指"的可观测定义与检测口径，作为检测器实现的权威基准。本轮（2026-07-09）在 `AU2606 2026-05-20` 真数据上对齐得出。
>
> **优先级**：本文档 > `2026-07-03-tick-fat-finger-trading-statistics-design.md` §6/§7/§9 > `src/tick_detector/` 现有代码口径。凡冲突以本文档为准（详见 §6）。
>
> **设计取向：简单优先。** 乌龙指本质就是"目标合约成交价相对同品种其他月份突然偏离 + 大量低价成交"。本文只用两个核心信号，不用复杂的 movement 模型 / 盘口 mid 几何。

## 1. 定义

> **乌龙指 = 单一合约的成交价突然偏离同品种其他月份给出的价差结构（向下或向上），伴随该快照增量成交均价(ΔTurnover/ΔVolume)显著偏离最后成交价（证明这一瞬间大量成交发生在远离盘口的价位，不是单笔抖动）；偏离快速回归。**

用大白话：目标合约和某近月合约的价差本来一直在一个范围内（比如正常 10–20 ticks），**突然价差突破变成 80 ticks**，同时**其他月份之间价差没变、其他月份价格也没怎么动**（证明是目标自己孤立偏离），**而且这瞬间的增量成交均价(ΔTurnover/ΔVolume)大幅偏离最后成交价**（证明一大笔单子砸在远离盘口的价位，不是报价抖动）——这就是乌龙指。

## 2. 为什么用"成交价"不用"盘口 mid"

本轮在真数据上的关键教训：**用盘口 mid 做偏离，会把 bid/ask 抖动的单帧毛刺当成乌龙指**。AU2606 2026-05-20 上，mid-based 偏离会抓出数十个"事件"，绝大多数的 LastPrice 都贴着 mid（没有真实低价成交），纯属盘口抖动（精确计数依赖 deprecated level 派阈值，非本文核心，故不锚定具体数字）。

**改用成交价（LastPrice / 增量成交均价）后**：抖动毛刺自然消失（它们没有真实低价成交）。两信号的分离度见 §6 标定表——`spread_breakout` 与 `intra_swing` 都能独立把 21:04 从全天隔离出来。成交价是"真金白银成交的价"，盘口 mid 只是"挂单价"——乌龙指是真实成交事件，必须用成交价。

## 3. 两个核心信号

### 3.1 价差突破 `spread_breakout`（主信号，bias-free）

对目标 T 时刻 t，参考合约 = 同品种当日成交量前 N 个活跃月（排除 T，**N=3–4**），并施加**流动性地板**剔除薄量合约：当日成交量 ≥ `peer_min_daily_volume`（起始 5,000 手）且可交易快照数 ≥ `peer_min_snapshots`（起始 55,000）。2026-05-20 AU 上该地板剔除仅成交 1,586 手 / 51,055 快照的稀疏 au2607，保留 au2608 / au2610 / au2612（即 N=3，与 §6 锚点同集）。**新鲜度**：peer 与基线的 asof mid 须满足 `max_reference_age`（起始 3s）新鲜度门，防 stale mid 污染 `spread_breakout`。价差用**目标的成交价 LastPrice** 对 **参考合约的盘口 mid**（参考价用 mid 求稳）：

```text
spread_i(t)        = T_LastPrice(t) − peer_i_mid_asof(t)
normal_spread_i(t) = median{ spread_i(s) : s ∈ [t − 60s, t − 10s] }   # 近 1 分钟基线，排除最近 10s
breakout_i(t)      = (normal_spread_i(t) − spread_i(t)) / tick_size   # 正=价差异常变宽(T偏低)
spread_breakout(t) = median_i( breakout_i(t) )                        # ticks，跨参考合约取中位
```

- 仅在 `ΔVolume > 0`（该快照有真实成交）时计算——LastPrice 无新成交时是陈旧的，不能算偏离。
- 向下乌龙指：`spread_breakout` 大正（T 成交价低于正常价差结构）。
- 向上乌龙指：镜像（T 成交价高于正常结构），`breakout_i` 取反。
- **bias-free**：纯价相对关系，不受累计成交量影响，全天各时段可比。

### 3.2 增量成交均价偏离 `intra_swing`（确认信号，证"瞬间大量成交在异常价位"）

**不用累计 VWAP**——累计均价在盘后段被巨大累计量稀释，最后时刻的乌龙指拉不动它。改用**每个快照新增成交**的均价（增量，不受累计量污染）：

```text
multiplier      = median{ ΔTurnover(t) / ΔVolume(t) / LastPrice(t) }   # 按合约当天自动标定（金=1000），无需配置表
trade_avg(t)    = ΔTurnover(t) / (ΔVolume(t) × multiplier)             # 该快照新增成交的均价（元）
intra_swing(t)  = | trade_avg(t) − LastPrice(t) | / tick_size          # 增量均价对最后价的偏离（ticks）= 秒内价格波动代理
```

- 仅在 `ΔVolume > 0` 时计算（有新增成交才有意义）。
- **意义**：`trade_avg` 是这 500ms 内**新增成交的量价加权均价**，`LastPrice` 只是最后一笔。二者偏离大 = 这瞬间大量成交发生在远离最后价的价位 = 乌龙指砸盘/拉升的深度。单笔抖动、盘口毛刺的 `trade_avg ≈ LastPrice`（偏离≈0），产生不了 `intra_swing`。
- **这正是"用成交额/成交量估算秒内价格波动"**——弥补 snapshot 数据只有 LastPrice、无秒内最高最低价的限制。**真实深度说明（21:04，已用真 tick 复核）**：该秒 370 手大单实际砸穿所有卖价、**最低成交到 ~830（近跌停 830.48）**；snapshot 的 `LastPrice=993.92` 只是反弹后的最后一笔，**严重低估事件**。`trade_avg=940.5`（落在 [830, 993.92] 区间）/`intra_swing=2670t` 才反映真实扫单深度。自洽校验：370×940.5×1000=347,985,000 ≈ 该秒 ΔTurnover（实测 347,993,860，差 0.003%），无计数器错配。**结论**：snapshot 的 LastPrice 看不到秒内扫单深度，必须靠成交额/成交量增量还原——这正是 `intra_swing` 不可替代的价值。
- **无累计量污染**：纯增量，盘初盘后可比。
- `multiplier` 按合约当天自动标定（取当日 `ΔTurnover/ΔVolume/LastPrice` 的中位，乌龙指是 1/数万的离群点，不影响中位），无需品种配置表。**caveat（M5）**：目前仅 AU 实测（中位=1000），跨品种未验证；v1 标定在金，其他品种上线前需复核或保留 `CONTRACT_MULTIPLIERS` 配置兜底。

## 4. 廉价过滤（防明显垃圾）

- **有向 onset**：`onset_down(t) = max(0, spread_breakout(t) − spread_breakout(t − 3s))`。因 `spread_breakout` 正向 = 价差异常变宽（T 偏低），向下爆发时它在**增大**，故 onset 取其 3s 增量、再 `max(0,·)` 只保留"突然变大"的方向，排除"偏离突然收回"的回归段。要求 ≥ `ONSET_TICKS`。**符号纠正（F1）**：早期手稿写作 `max(0, −(spread_breakout(t) − spread_breakout(t−3s)))`，负号方向反了——按该式 onset 在 21:04 爆发段会归零（实测 =0）、反在回归段误触发，验收必败。已去掉前导负号。
- **快速回归**：候选必须在 `~10s` 内 `spread_breakout` 收回到峰值的 0.5 倍以内（`fast_recovery`），才最终分类为乌龙指；否则 `no_fast_recovery` 不计。回归是定义的必备原子，故作确认门而非纯事后标签。
- **开盘保护**：每个交易时段（夜盘 21:00 / 日盘 09:00 / 下午 13:30）开盘后 `open_guard_seconds`（起始 60s）内不触发——开盘集合竞价段价差结构不稳、易假突破。**基线窗 caveat（M2）**：open_guard 只挡触发时刻 t，不挡基线窗起点 t−60s；故事件若发生在某时段开盘后 60–120s 内，其基线窗 [t−60s, t−10s] 会伸进开盘段、可能受开盘紊乱污染。v1 接受该边界（21:04 等夜盘事件不受影响）；若日后需严格，把 open_guard 同时覆盖基线窗起点 t−60s 即可。
- **基线有效**：`normal_spread_i` 窗内有效样本 < `baseline_min_samples`（起始 20）则该参考合约本时刻记缺失、不进中位。

## 5. 触发口径（阈值已用真数据标定起始值）

每根快照（向下；向上镜像），逐快照门全部满足才进候选池：

```text
is_tradable_session(t)                   # M1：仅可交易时段快照参与（剔除盘后结算 tick，如 15:00 收盘 tick）
ΔVolume > 0
spread_breakout(t) ≥ DEPTH_TICKS         # 起始 30（实测 21:04=70；全天其余可交易快照 max=22.5t 且落在 09:00 开盘保护窗内，保护窗外 max≈22.0t，干净分隔）
intra_swing(t)     ≥ INTRA_SWING_TICKS   # 起始 500（实测 21:04=2670；全天其余可交易快照 max=40.4t≈0.81元，66 倍分隔）
onset_down(t)      ≥ ONSET_TICKS         # 起始 8（实测 21:04=71，用 §4 修正符号）
not in open_guard_window
baseline_min_samples 满足
```

候选 → 同合约 10s 窗合并 → 锚点取 `spread_breakout` 最大者 → `fast_recovery` 确认（见下行）→ 计为乌龙指。

```text
fast_recovery(t+10s)                     # H3：最终确认门——锚点后 10s 内 spread_breakout 收回到峰值 0.5 倍以内，否则标 no_fast_recovery 不计
```

> 两道核心门槛（价差突破 ≥30 **且** 增量均价偏离 ≥500t）任一单独都能在 2026-05-20 隔离出 21:04；二者联用是 belt-and-suspenders。`onset / fast_recovery / 开盘保护 / is_tradable_session` 是防边界垃圾的廉价过滤。

## 6. 标定锚点（2026-05-20 真数据，已实测）

参考合约（流动性地板后）= {au2608, au2610, au2612}（N=3，剔除仅 1,586 手 / 51,055 快照的稀疏 au2607）。

| 事件 | spread_breakout | intra_swing | onset | Δvol | 期望判定 |
|---|---|---|---|---|---|
| `AU2606 2026-05-20 21:04:35.500`（真乌龙指）| **70.0t** | **2670t（53.4元）** | **71** | 370 | **触发**（向下）|
| 其余可交易候选（14 个候选事件）| ≤22.5t | ≤40.4t（0.81元）| — | — | 不触发（双不够）|
| `AU2606 2026-05-20 11:08`（缓跌反例）| 小（价差没突破）| ≈0 | 小 | — | 不触发（缓跌，瞬间无大量异常成交）|

> **onset=71** 用 §4 修正后的公式 `max(0, spread_breakout(t) − spread_breakout(t−3s))` 测得（sb(t)=70.0, sb(t−3s)=−1.0）；若沿用负号反式则 onset=0，21:04 反而不触发——正是 F1 纠正点。
>
> **两信号分离度（全天 44,904 个"可交易 ∧ Δvol>0"快照）**：`spread_breakout` 全天（excl 21:04）max=22.5t（且落在 09:00 开盘保护窗内；保护窗外 max≈22.0t），21:04=70 → 3 倍以上分隔；`intra_swing` 全天（excl 21:04）max=40.4t（0.81元），21:04=2670 → 66 倍分隔；偏离 LastPrice >5 元（250t）的**只有 21:04 这 1 个**。两维度都能独立隔离 21:04。
>
> **候选定义（精确）**：`is_tradable_session ∧ Δvol>0 ∧ spread_breakout≥15`，同合约 10s 合并后 = **15 个候选事件**（含 21:04），即其余 **14** 个。§5 的 AND 门（≥30 ∧ ≥500 ∧ onset≥8 ∧ fast_recovery）只放行 21:04。

**真实深度说明（21:04，H1）**：该秒 370 手大单实际砸穿所有卖价、**最低成交到 ~830（近跌停 830.48）**；snapshot 的 `LastPrice=993.92` 只是反弹后末笔，**严重低估事件**。`trade_avg=940.5`（落在 [830, 993.92]）/`intra_swing=2670t` 才反映真实扫单深度。自洽校验：370×940.5×1000=347,985,000 ≈ ΔTurnover（实测 347,993,860，差 0.003%），无计数器错配。这正是 snapshot 口径"用 ΔTurnover/ΔVolume 还原秒内深度"的价值——LastPrice 看不到秒内扫单，必须靠成交额/成交量增量还原。

任何实现必须：21:04 命中、其余 14 个候选不命中、11:08 缓跌不命中、开盘段（21:00 / 09:00 artifact）被开盘保护挡掉、盘后结算 tick（如 15:00）被 `is_tradable_session` 挡掉。

## 7. 与现有代码/文档的关系（supersession）

| 现有口径 | 处置 |
|---|---|
| 大 spec §4 基础派生（`mid_price` / `spread` / `ΔVolume` / `ΔTurnover` / `snapshot_seq` / `timestamp` / `is_tradable_session`）| **沿用**；本文在其上新增 `spread_breakout` / `trade_avg` / `intra_swing` 派生 |
| 大 spec §5 `tick_size` / `contract_multiplier` 配置表（`AU,0.02,1000`）| `tick_size` **仍需**（或价格序列兜底）；`multiplier` **改为按合约当日自动标定**（见本文 §3.2），不再依赖 `CONTRACT_MULTIPLIERS` 配置 |
| 大 spec §6 `expected_price = baseline_mid × exp(peer_return)`（movement 模型）| **废止作核心信号**；本文用成交价价差突破取代 |
| 大 spec §7 触发分支（`visible_last_drop` / `strong_visible_last_drop` / `hidden_avg_trade_drop`）| **按本文 §5 重写**：主信号 `spread_breakout` + `intra_swing` |
| 大 spec §9 回归判定（price-based）| **被本文 §4 快速回归（spread_breakout 回收）替换**；口径不同，不可复用旧测试 |
| 大 spec §8 事件合并（10s 窗）| 沿用 |
| 大 spec §12 交易回测（用 `expected_price` 算挂单价）| 属未来工作，本文不废止 §12 回测；`expected_price` / `spread_breakout` 在其中的角色由回测设计另定（M4：旧 §8 只是事件合并，回测在旧 §12）|
| `reference_selection.py` `peer_excess_down_ticks`（move 派）/ `expected_price_*` | **不再是核心信号**；降为可选辅证或删 |
| `event_detection.py` `OPEN_GUARD_SECONDS=60` | **保留**（本文 §4 开盘保护沿用）|
| `snapshot_avg_trade_price`（hidden 分支，仅 AU）| 本文 `intra_swing`（由 `trade_avg = ΔTurnover/ΔVolume` 派生）取代之作确认信号；multiplier 自动标定**目前仅 AU 验证（中位=1000），跨品种未测**，其他品种上线前需复核或保留 `CONTRACT_MULTIPLIERS` 兜底（M5）|
| `CONTRACT_MULTIPLIERS = {"AU": 1000}` AU-only 限制 | **解除**：本文信号只需 tick_size + 价差 + 增量成交均价，multiplier 自动标定，不需品种配置；**caveat（M5）**：仅 AU 实测，跨品种未验证，上线前需复核或保留配置兜底 |

> 本文同时取代上一轮的 "level 派"（mid-based level_deviation）草案——mid-based 在真数据上抓出数十个盘口抖动假事件（精确计数依赖 deprecated level 派阈值，非本文核心），已证伪。

## 8. 已知边界（诚实声明）

1. **只有 LastPrice，无秒内高低价**：用**增量成交均价** `trade_avg = ΔTurnover/ΔVolume/multiplier` 弥补——`|trade_avg − LastPrice|`（即 `intra_swing`）是该快照内价格波动的代理，能识别"瞬间大量成交在异常价位"。**用增量不用累计**：累计均价在盘后段被巨大累计量稀释，会漏掉最后时刻的乌龙指。
2. **multiplier 自动标定**：`trade_avg` 需要合约乘数（金=1000），按"当日 `ΔTurnover/ΔVolume/LastPrice` 的中位"自动得到，无需品种配置表（乌龙指是离群点，不影响中位）。**caveat（M5）**：目前仅 AU 验证（中位=1000），跨品种未测；v1 标定在金，其他品种上线前需复核或保留 `CONTRACT_MULTIPLIERS` 配置兜底。
3. **小 ΔVolume 不是噪声源**（更正版，M3）：实测 `Δvol=1` 时 `trade_avg≈LastPrice`、`intra_swing=0`（12,711 个 dv=1 快照无一例外），并不产生假阳性；`intra_swing` 随 Δvol 单调增大（dv∈[1,2] max=4.5t，dv∈[51,200] max=40.4t），真正的乌龙指级偏离（2670t）只在 dv=370 出现。可见"`trade_avg` 在 1–2 手噪声大"是误诊。真正的 `intra_swing` 抖动来自中大 Δvol 的计数器行为，由 §5 的 `INTRA_SWING_TICKS≥500` 高门槛挡掉。若仍设 `ΔVolume ≥ min`（如 5 手），其理由是"太小成交不构成乌龙指"（量级过滤），而非"防均价噪声"。
4. **全天量选参考合约** = 用了未来信息，**仅限离线复盘检测**，不用于交易回测（回测的参考合约选取须只用当时可见信息）。
5. **夜盘**：事件可能在夜盘（21:04 即是）。需按自然时间排序处理跨午夜；v1 检测覆盖夜盘+日盘所有连续交易段。
6. **向上检测**：定义上下皆可，但 v1 只标定向下（无向上真锚点，且首个向上信号受 09:00 开盘 artifact 污染）。向上为 future。

## 9. 待标定参数（实现前用真数据确认）

- 基线窗 `[t−60s, t−10s]`、`baseline_min_samples`（起始 20）
- `DEPTH_TICKS`（起始 30）、`INTRA_SWING_TICKS`（起始 500）、`ONSET_TICKS`（起始 8）、onset Δ（起始 3s）
- 回归阈值 0.5、回归窗 10s
- 参考合约数 N（起始 3–4 活跃月）、**参考合约流动性地板** `peer_min_daily_volume`（起始 5,000 手）/ `peer_min_snapshots`（起始 55,000）（M6：剔除 au2607 类稀疏近月）、参考合约新鲜度上限 `max_reference_age`（起始 3s，防 stale mid 污染 `spread_breakout`）
- `open_guard_seconds`（起始 60）、合并窗 10s、session 断点 60s（沿用现口径）
