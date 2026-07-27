# 参数候选生成器确认深度改造实施计划

> 2026-07-27 更新：本文记录此前阶段 1–6 的实施过程。现行生成器输出多组 T/W/D/S 组合，但不再负责统计报告或回放准备；当前输入与输出契约以 `docs/programmatic_parameter_generator_guide.md` 为准。

> 前置设计：[2026-07-25-programmatic-grid-parameter-generator-design.md](../specs/2026-07-25-programmatic-grid-parameter-generator-design.md)。本计划只实现 CSV 契约、CSV-only 注释/迁移和生成器纠偏；不重跑原始 Tick 数据，不改全天网格回放逻辑。

## 目标

让 `T=W+D` 的候选深度来自检测器的 `event_depth_ticks`（中文 CSV：`事件确认深度_跳`），而不是 `onset_ticks`（中文 CSV：`突发偏离_跳`）。生成器只接受带日线边界、确认深度和确认深度来源的规范注释 CSV；同时输出确认深度 bps、onset 诊断和可审计来源。

## 阶段 1：检测器导出规范确认深度

**文件：** `run_tick_detector.py`、`tests/test_run_tick_detector.py`、`tests/test_tick_detector_event_detection.py`、`tests/test_tick_detector_perf_golden.py`、对应 fixture 与真实样本摘要。

1. 在中文 CSV 列映射中新增：

   ```text
   event_depth_ticks -> 事件确认深度_跳
   event_depth_bps   -> 事件确认深度_基点
   depth_source      -> 确认深度来源
   ```

2. 在 `_finalize_event_fields()` 中按事件锚点的 `fair_price`、目标 `tick_size` 计算 `event_depth_bps`，并固定 `depth_source=detector_event_depth`。
3. 保留 `onset_ticks -> 突发偏离_跳` 原映射，禁止重命名或复用。
4. 为可见、区间与合并事件增加测试，断言 CSV 同时导出 onset、确认深度 ticks/bps 和来源；确认深度等于合并锚点的 `candidate_execution_depth`。
5. 更新 `golden_synthetic_events.csv`、JD 事件摘要/哈希，并把 `tests/test_tick_detector_perf_golden.py` 纳入验证命令。

**通过条件：** 新导出的原始检测 CSV 可独立区分“突发性”和“确认深度”，黄金测试同步通过。

## 阶段 2：建立范围绑定的日线边界注释产物

**文件：** 新增注释脚本及其测试；相关使用说明。

1. 将日线边界排除清单定义为外部、版本化前置输入；其生成责任不在参数生成器改造范围内。清单至少包含：

   ```text
   交易日, 事件编号, 日线边界判定, 排除原因, 规则或数据版本
   ```

   稳定事件键为复合键 `(交易日, 事件编号)`；禁止单独用 `事件编号` 合并，因为同一合约、同一时刻可在不同交易日重复。

2. 清单必须配套一个 manifest，最少包含：

   ```text
   source_events_sha256, trade_date_start, trade_date_end,
   commodities, expected_event_count, boundary_rule_or_data_version
   ```

3. 注释脚本输入区间合并后的 `tick_candidate_events.csv`、清单和 manifest。它必须校验源 CSV SHA-256、日期范围、品种集合、事件数、清单必需列和复合键唯一性；任何不匹配、清单包含源 CSV 不存在的复合键或源 CSV 缺少键字段均明确失败。
4. 校验通过后，清单中的事件使用其声明判定，其余事件写 `保留`；输出完整 `tick_candidate_events_annotated.csv`，并将规则/数据版本和清单哈希写入每行或同名强绑定元数据。
5. 生成器入口只接受该规范注释 CSV；原始检测 CSV 不满足契约时明确报错。
6. 增加端到端测试：原始事件 CSV + 匹配 manifest/边界清单 → 注释 CSV → 参数生成器输入契约；错误哈希、错日期范围、错品种、错事件数、不完整清单、以及“同合约同时间但不同交易日”的重复 `事件编号` 均必须安全处理。

**通过条件：** `保留` 可证明来自匹配范围的边界审查，生成器不再依赖手工补列；整个链路不读取原始 Tick 数据。

## 阶段 3：纠正生成器主指标与输出契约

**文件：** `src/programmatic_parameter_generator.py`、`tests/test_programmatic_parameter_generator.py`。

1. 将 `事件确认深度_跳`、`事件确认深度_基点`、`确认深度来源` 列入必需输入列；确认深度 ticks 与来源是逐行合格样本的硬条件，bps 列允许单行为空。
2. `eligible_event` 的有效性判断改为校验确认深度 ticks 与来源，删除以 `突发偏离_跳` 判定 `invalid_depth` 的路径；不得因 bps 为空排除 ticks 有效事件。
3. 将生成 `T`、日深度集中度和确认深度统计的序列替换为 `事件确认深度_跳`。
4. 输出下列明确命名的统计字段：

   ```text
   confirmed_depth_p50/p70/p85/p90/max_ticks
   confirmed_depth_p50/p90_bps
   confirmed_depth_bps_sample_count
   onset_p50/onset_p90_ticks
   depth_source
   ```

5. 将候选的 `sample_count` 替换为 `confirmed_depth_sample_count`，同时写入 `depth_source`；候选说明固定为“事件确认深度统计候选”。
6. `depth_source` 对合约内 ticks 合格样本的来源去重、稳定排序后以 `+` 连接；来源缺失直接拒绝。bps 分位数只在 bps 有效子集计算。增加混合来源、来源缺失与 bps 缺失测试。

**测试：** 构造 onset 与确认深度不同的样本，断言 T 只随确认深度变化；验证 bps 分位数及其样本数、缺失/零/负确认深度、样本不足、单日集中、恢复风险和来源输出。

**通过条件：** 任何仅改变 `突发偏离_跳` 的输入，不改变 `T`、`D`、`W`、`S` 候选；仅清空 bps 不改变 ticks 候选。

## 阶段 4：候选配置安全边界与 P90 语义

**文件：** `src/programmatic_parameter_generator.py`、配置加载测试、候选测试。

1. 通过 `ParameterGeneratorConfig.__post_init__()` 或共享 `_validate_config()` 校验 `total_touch_quantiles` 与 `w_ratios`，使 JSON 加载和直接构造 `ParameterGeneratorConfig(...)` 都拒绝不满足 `0 < q < 1`、`0 < ratio < 1` 的配置。
2. 默认 `total_touch_quantiles` 保持 `[0.50, 0.70, 0.85]`；P90 始终写入统计，不默认生成候选。
3. 显式配置 `0.90` 时，若其向上取整 tick 与其他分位数相同，保留合并来源，例如 `P85/P90`；来源集合含 P90 即添加 `tail_depth_candidate`。
4. 计算 `D=T-W`；若 `D<=0` 拒绝候选，禁止以 `max(1, T-W)` 伪造正值。断言所有成功候选严格满足 `T=W+D`。
5. 继续将 W/D/S 标为启发式；本阶段不把 P50/P90 直接拆为 W/D。

**通过条件：** 恶意配置在所有公开调用路径明确失败，分位数去重不丢失 P90 风险语义，所有候选保持数学不变量。

## 阶段 5：严格迁移历史三月 CSV

**文件：** 新增一次性迁移脚本及其测试；独立输出目录。

1. 输入规范注释 CSV，固定输出为：

   ```text
   output/20260301_20260331-range-confirmed-depth/
   tick_candidate_events_annotated_confirmed_depth.csv
   ```

   不覆盖 `output/20260301_20260331-range/tick_candidate_events_annotated.csv`。

2. 只由锚点字段重建实际命中通道，不使用事件级 `触发原因`：

   ```text
   visible_hit   = 末笔向下偏离_跳 >= 末笔触发阈值_跳
   interval_hit  = 区间均价向下偏离_跳 >= 区间均价触发阈值_跳
                   且 一秒合并均价向下偏离_跳 >= 区间均价触发阈值_跳
   visible_depth = 末笔向下偏离_跳，仅当 visible_hit
   interval_depth = 一秒合并均价向下偏离_跳，仅当 interval_hit
   confirmed_depth = max(visible_depth, interval_depth)
   ```

   禁止回退 `区间均价向下偏离_跳` 作为确认深度，也禁止将锚点未实际命中的通道偏离纳入最大值。

3. 任一重建所需阈值/偏离字段缺失、或没有重建出正的命中通道时，确认深度置空并标记 `确认深度来源=legacy_depth_unverifiable`；生成器按无效 ticks 深度排除。
4. 可验证行标记 `确认深度来源=legacy_components_derived`。bps 无法从同事件可验证每跳 bps 推导时置空；不得伪造零，也不得因此排除 ticks 有效事件。
5. 测试覆盖：合并事件但锚点仅命中一个通道、仅区间命中但末笔更深、仅可见命中但区间更深、合并均价缺失、阈值缺失、来源标记、源文件不被覆盖、迁移产物可被生成器处理。

**通过条件：** 历史产物的每一条有效确认深度可由锚点实际命中的通道逐行复算，任何不可验证事件都不会抬高 T。

## 阶段 6：真实样本验收与回放

**文件：** 独立参数候选输出目录、更新后的使用说明。

1. 生成器只读取阶段 5 的固定迁移产物：

   ```text
   output/20260301_20260331-range-confirmed-depth/
   tick_candidate_events_annotated_confirmed_depth.csv
   ```

2. 参数输出写入新的独立目录，绝不覆盖 `output/programmatic_parameter_candidates_202603_exploratory/`。
3. 对 AP605、PX605、EB2606、L2609 对比旧 onset 口径与新确认深度口径的 P50/P70/P85/P90；只陈述实际差异，不预设新参数必然更深或更浅。
4. 人工选择少量 `research_candidate_ready` 合约，将 P50/P70/P85 低侧候选转为研究假设，运行全天回放；P90 尾部候选单独比较。
5. 用候选事件触达、`normal_move_fill`、对冲失败、净收益、最差日和动作峰值共同筛选；生成器仍不是最终参数选择器。

## 验证命令

```bash
./venv/bin/python -m pytest \
  tests/test_tick_detector_event_detection.py \
  tests/test_run_tick_detector.py \
  tests/test_tick_detector_perf_golden.py \
  tests/test_programmatic_parameter_generator.py
```

阶段 2 和阶段 5 新增脚本后，将其针对性测试加入同一命令；不得以“只跑了生成器单测”替代检测 CSV 架构的黄金验证。

## 不做事项

- 不修改 `programmatic_grid.py`、成交模型、对冲或风控；
- 不扫描、解压或重跑原始 Tick 数据；
- 不自动选 hedge/fair reference/延迟/实盘参数；
- 不新建基于 onset 比值的质量评分；
- 不实现正常 fair 迁移分布模块；该模块只在本计划验收后另立设计。
