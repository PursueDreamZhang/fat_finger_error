# 基于检测事件的程序化网格参数候选生成器改造设计

> 2026-07-27 更新：本文保留为阶段 1–6 的历史设计与验收记录。当前生成器已收敛为“每合约一组 P70/40%/半 W 的 T/W/D/S”，不再输出多分位数候选、统计报告或回放准备产物；以 `docs/programmatic_parameter_generator_guide.md` 为现行契约。

> 状态：待实施。本文修正现有生成器把 `突发偏离_跳` 当作总触达深度的口径错误。生成器只读取检测结果 CSV，不扫描或重跑原始 Tick 数据；输出仍是研究候选，不是实盘参数或回放最优解。

## 1. 问题与目标

现有检测器在同一事件中计算了两类不同含义的量：

|指标|内部字段|当前 CSV 字段|含义|正确用途|
|---|---|---|---|---|
|突发偏离|`onset_ticks`|`突发偏离_跳`|候选锚点相对前 3 秒深度的新增偏离|检测触发与突发性诊断|
|事件确认深度|`event_depth_ticks`|未导出|合并事件锚点的最大确认成交偏离|总触达深度 `T` 的统计来源|

二者没有固定大小关系：区间分支的 `onset_ticks` 使用原始区间均价，而 `event_depth_ticks` 使用一秒合并成交均价优先的确认口径。因此不能把 `onset_ticks` 当作“真实深度”，也不能假定它必然比确认深度更小。

本次目标是让参数生成器按**事件确认深度**生成 `T=W+D` 的分位数候选，同时保留突发偏离供样本解释和风险诊断；新增 bps 报告用于跨品种比较，不改变运行时以 tick 为单位的网格参数。

## 2. 强制边界

- 输入仍是检测结果 `tick_candidate_events_annotated.csv`；参数生成器不得读取 zip、目录或原始 Tick 行。
- 不改变检测器的触发条件、参考合约选择、成交模型、对冲逻辑或全天回放逻辑。
- `T` 仅是事件深度产生的研究档位，不代表历史限价单一定成交，也不能跳过全天回放。
- `W`、`D`、`S` 不是同一类统计量。事件确认深度只决定 `T`；不能用 `W=P50(depth)`、`D=P90(depth)-P50(depth)` 直接替换其既有含义。
- 不新增未经验证的 `event_quality_score` 或以 `onset/depth` 自动过滤事件。检测器已有突发、参考数、fair 不确定性和数据质量门槛；新比值仅可用于诊断。
- 不把低侧统计直接转成双侧 `quote_shapes` 或实盘配置。

## 3. CSV 契约

### 3.1 新增检测器输出字段

检测器在写出中文事件 CSV 时必须新增：

|字段|来源与公式|用途|
|---|---|---|
|`事件确认深度_跳`|`event_depth_ticks`|生成器的唯一 `T` 主指标|
|`事件确认深度_基点`|`event_depth_ticks * tick_size / fair_price * 10000`|同一深度的相对幅度统计|
|`确认深度来源`|固定为 `detector_event_depth`|使新版检测产物与历史补列产物可审计地区分|

`突发偏离_跳` 保持原定义和字段名，不重命名、不复用。检测器输出的 `event_depth_ticks` 已使用事件合并锚点的确认成交深度；它是本项目当前可审计的“确认深度”，不是对逐笔最低成交价的宣称。

### 3.2 生成器输入规则

新版本生成器必须把三个“确认深度”字段列入必需列。`事件确认深度_跳` 与 `确认深度来源` 是逐行合格样本的硬条件；`事件确认深度_基点` 列必须存在，但单行可为空，因为 bps 只用于横向报告，不能改变 ticks 的 `T` 分布。缺失列时明确报错，不能静默回退到 `突发偏离_跳`。

### 3.3 注释 CSV 的生产链

检测器原始 `tick_candidate_events.csv` 不含 `日线边界判定`，故不能直接传给生成器。日线边界排除清单是**本改造的外部、版本化前置输入**，由日线边界审查/规则流程负责生成；本参数生成器和注释脚本不读取原始 Tick 数据，也不负责重新判定日线边界。

排除清单的最小行级契约为：

```text
交易日, 事件编号, 日线边界判定, 排除原因, 规则或数据版本
```

日线边界的稳定事件键为复合键 `(交易日, 事件编号)`：`事件编号` 本身可能在不同交易日重复。该复合键在源事件 CSV 和清单中都必须唯一，`日线边界判定` 只能是受支持的枚举。排除清单还必须带一个版本化 manifest，至少包含：

```text
source_events_sha256, trade_date_start, trade_date_end,
commodities, expected_event_count, boundary_rule_or_data_version
```

注释步骤必须以输入事件 CSV 的 SHA-256、日期范围、品种集合和事件数逐项校验 manifest；任一项不匹配即失败，不得把未列出的事件静默解释为已审查的 `保留`。成功后将 manifest 的规则/数据版本和清单哈希写入注释产物，保证该 CSV 脱离旁路文件后仍可审计。必须提供一个独立、CSV-only 的注释步骤：

```text
检测器事件 CSV + 日线边界排除清单（exclude_by_daily_guard.csv）
  → 按 (交易日, 事件编号) 合并
  → tick_candidate_events_annotated.csv
```

该步骤将排除清单中的事件标为其声明的判定，其余事件标为 `保留`，并保留全部原始事件字段。它不读取原始 Tick 数据。参数生成器只接受这个规范注释产物；注释步骤须校验排除清单的最小字段、复合键唯一性和未匹配排除项，并有端到端测试。

### 3.4 历史 CSV 一次性迁移

历史 CSV 的迁移必须严格且可降级。事件级 `触发原因` 是整个合并窗口的原因并集，不能用于判断锚点帧实际命中的通道；迁移必须只用锚点行已导出的阈值和偏离字段重建命中：

```text
visible_hit   = 末笔向下偏离_跳 >= 末笔触发阈值_跳
interval_hit  = 区间均价向下偏离_跳 >= 区间均价触发阈值_跳
                且 一秒合并均价向下偏离_跳 >= 区间均价触发阈值_跳
visible_depth = 末笔向下偏离_跳，仅当 visible_hit
interval_depth = 一秒合并均价向下偏离_跳，仅当 interval_hit
legacy_confirmed_depth_ticks = max(visible_depth, interval_depth)
```

不得回退到未经一秒合并确认的 `区间均价向下偏离_跳`，也不得把锚点未实际命中的通道偏离纳入最大值。若任一阈值或重建命中所需字段缺失、没有重建出正的命中通道，迁移产物将确认深度置空，并写入 `确认深度来源=legacy_depth_unverifiable`；生成器按无效确认深度排除该行。

可复原有效深度时，迁移产物写入 `确认深度来源=legacy_components_derived`。bps 必须由同一事件、同一 fair 价格下可验证的每跳 bps 推导；不能可靠推导时置空，禁止伪造为零。bps 为空不影响该行以确认深度 ticks 进入 `T` 分布，但会减少 bps 分位数的样本量。迁移不是生成器的默认兼容路径；新检测 CSV 的来源固定为 `detector_event_depth`。

## 4. 样本分层

每个 `品种 + 合约` 独立统计，保留三层：

1. `all_detected`：所有原始事件，仅作覆盖参考；
2. `eligible_event`：通过数据质量、日线边界、回归标签、参考数、fair 不确定性和正确认深度校验的事件；
3. `recovery_event`：`eligible_event` 内可确认恢复的事件，仅用于恢复风险统计。

`eligible_event` 用于确认深度的分位数；`persistent_10s` 与 `truncated` 不得因为未恢复而从深度分布消失。`recovery_event` 不能替代 `eligible_event`。

现有最少样本数、最大单日事件占比、数据质量、日线边界和 fair 不确定性规则保持不变。

## 5. 参数生成口径

### 5.1 总触达深度 T

对每个合约的合格样本定义：

```text
confirmed_depth_ticks = eligible_event.事件确认深度_跳
confirmed_depth_bps   = eligible_event.事件确认深度_基点
T(q) = max(1, ceil(quantile(confirmed_depth_ticks, q)))
```

默认候选档位保持：

```text
T_low  = P50(confirmed_depth_ticks)
T_mid  = P70(confirmed_depth_ticks)
T_high = P85(confirmed_depth_ticks)
```

`P90` 和最大值必须报告为尾部边界，但默认不生成常规候选。P90 档意味着只有约 10% 的合格事件达到该深度；如研究者要专门验证极端远端订单，只能通过显式配置把 P90 加入候选，且候选必须带 `tail_depth_candidate` 风险标签。

分位数向上取整后的 tick 档可合并来源。例如 P85 与 P90 同为 120 跳时，候选的 `T_source_quantile` 为 `P85/P90`；只要来源集合包含 P90，就必须带 `tail_depth_candidate`，不得依赖字符串等于 `P90`。

所有分位数按单合约计算，不能将不同月份的绝对 tick 直接混合。bps 分位数用于比较品种/合约的相对异常强度，不是第二套运行时网格参数。

### 5.2 W、D、S

网格定义不变：

```text
buy_limit = anchor - (W + D) * tick_size
T = W + D
```

第一阶段继续使用 `W = ceil(T * ratio)` 的 `{0.40, 0.55}` 研究性拆分，并在输出中标为 `W_heuristic`。配置必须满足 `0 < ratio < 1`；`total_touch_quantiles` 必须满足 `0 < q < 1`。校验必须同时覆盖 JSON 加载和直接构造 `ParameterGeneratorConfig` 的调用路径。计算后使用 `D = T-W`，若 `D <= 0` 则拒绝该候选，不得用 `max(1, T-W)` 篡改 `T=W+D`。因此它只是在固定 `T` 后压缩全天回放搜索空间，不是从事件统计推导出的最优带宽。

后续独立阶段才引入正常行情统计：

```text
W = normal_fair_move 的候选高分位数
D = T - W
S = {ceil(W * 0.50), W}
```

若 `D <= 0`，该 `T/W` 组合不生成候选。该阶段需要读取正常行情帧，故不属于当前 CSV-only 参数生成器改造。

### 5.3 突发偏离和 bps 的角色

生成器应报告 `onset_p50_ticks`、`onset_p90_ticks`，必要时报告 `onset_to_confirmed_depth_ratio` 的分布；后者仅说明“突发增量与确认深度”的关系，可大于 1，不作为概率、评分或新过滤条件。

`confirmed_depth_bps` 仅用于：

- 横向比较不同价格和 tick_size 的合约；
- 在报告中解释 P50/P90 相对偏离；
- 为未来可能的按锚价换算报价研究提供审计数据。

当前网格仍以 tick 运行。禁止将 tick 分位数和 bps 分位数做 `max(...)` 双重约束；它们是同一价格距离的两种单位，应在需要时按当前锚价和 tick_size 显式换算。

## 6. 输出契约

### 6.1 `parameter_event_statistics.csv`

每个合约一行，除既有漏斗、集中度、恢复率和恢复通道统计外，必须包含：

```text
confirmed_depth_p50_ticks
confirmed_depth_p70_ticks
confirmed_depth_p85_ticks
confirmed_depth_p90_ticks
confirmed_depth_max_ticks
confirmed_depth_p50_bps
confirmed_depth_p90_bps
confirmed_depth_bps_sample_count
onset_p50_ticks
onset_p90_ticks
depth_source
```

`confirmed_depth_bps_sample_count` 是 bps 有效子集的样本量；确认深度 ticks 合格样本不因 bps 为空而被排除。`depth_source` 是该合约合格样本的来源集合，按稳定排序以 `+` 连接，例如 `detector_event_depth` 或 `legacy_components_derived+detector_event_depth`。旧的 `depth_p*_ticks` 名称不再用于确认深度，以免与 onset 或原始组件口径混淆。

### 6.2 `parameter_candidates.csv`

候选继续只含低侧研究形状，核心字段调整为：

```text
commodity, target_contract,
T_source_quantile, total_touch_ticks,
W_heuristic_ratio, W_ticks, D_ticks, S_mode, S_ticks,
confirmed_depth_sample_count, active_event_days,
candidate_status, rationale, risk_label
```

候选的 `rationale` 必须说明 `T` 来自“事件确认深度”而非突发偏离。候选必须写出合格确认深度样本数和 `depth_source`。默认不输出 P90 候选；显式启用后，只要分位数来源集合含 P90 就增加尾部风险标签。

### 6.3 报告

HTML/Markdown 报告按合约同时展示：确认深度 ticks/bps 分位数、onset 分位数、样本漏斗、集中度、恢复风险和 W/D/S 候选。报告明确：确认深度只是候选事件中的可审计偏离，不能证明订单队列成交。

## 7. 回放衔接与验收

```text
检测结果 CSV（含确认深度）
  → 参数生成器：确认深度分位数产生 T
  → 启发式或未来正常行情统计拆分 W/D/S
  → 人工冻结目标、参考、对冲与研究日期
  → 全天网格回放
  → 事件触达、normal_move_fill、对冲失败、净收益、最差日、动作峰值共同筛选
```

验收标准：

- 同一输入 CSV 与配置下，输出稳定可复算；
- `T` 分位数只来自 `事件确认深度_跳`，改变 `突发偏离_跳` 不得改变 T；
- 事件确认深度 bps 符合价格、tick_size 与 fair_price 的换算；
- bps 缺失只影响 bps 统计样本数，不改变确认深度 ticks 的 T 分布；
- 缺失确认深度字段时明确失败，不静默回退 onset；
- 新检测事件 CSV 经日线边界注释后可端到端输入生成器；
- P90 默认只出现在统计中，显式启用时才生成带风险标签的候选，即使与 P85 合并为同一 tick 档；
- `0 < w_ratio < 1`、`0 < quantile < 1`；无效配置明确失败，且始终保持 `T=W+D`；
- 检测器 CSV 架构变更同步更新 golden fixture、真实样本摘要和黄金测试；
- 候选保持 `T=W+D`、`W>0`、`D>0`、`S>0`，且仍不能直接写入双向 `quote_shapes`。

## 8. 不做事项

- 不重跑或扫描原始 Tick 数据；
- 不修改检测器阈值或重新定义“乌龙指”；
- 不以 onset/depth 比值生成自动评分或过滤；
- 不让 bps 覆盖 tick 运行时价格规则；
- 不自动选择 hedge、fair reference、延迟、风控或实盘参数；
- 不在本次改造中实现正常 fair 迁移分布模块。
