# 程序化网格深度参数生成器

## 1. 职责

生成器只做一件事：从已审查的乌龙指事件中，为每个 `品种 + 合约` 生成一组低侧价格深度：

```text
T：总触达深度
W：fair 迁移带宽
D：低侧报价距离
S：重定锚步长
T = W + D
```

它不展开 P50/P70/P85 候选，不生成回放配置，不计算成交、收益、对冲或风控，也不输出报告。回放器负责使用这组数做后续验证。

## 2. 输入

输入契约保持不变：必须是经过日线边界 manifest 绑定、并带确认深度的注释 CSV，例如：

```text
output/20260301_20260331-range-confirmed-depth/
tick_candidate_events_annotated_confirmed_depth.csv
```

生成器仍强制校验所有既有必需列。其中实际用于生成深度的字段为：

|字段|作用|
|---|---|
|`品种`、`合约`|每个组合独立输出一行|
|`事件确认深度_跳`|计算 T 的唯一数据来源|
|`确认深度来源`|确认深度必须可追溯|
|`回归标签`、`有效参考合约数`、`合理价不确定性_跳`、`数据质量标记`、`日线边界判定`|事件有效性筛选|
|`日线边界规则或数据版本`、`日线边界清单SHA256`、`源事件CSV SHA256`|范围绑定；三项必须非空且全文件一致，两个 SHA256 必须合法|

`突发偏离_跳`、确认深度基点和恢复确认秒数仍保留在输入契约中，但不参与单组 `T/W/D/S` 计算。

## 3. 单组公式

先筛掉不合格事件：质量或日线边界不允许、回归标签不允许、有效参考合约少于 2、fair 不确定性超过阈值、确认深度非正或来源为空的事件都不参与。

每个合约至少需要 8 条合格事件。满足后固定计算：

```text
T = ceil(P70(事件确认深度_跳))
W = ceil(T × 0.40)
D = T - W
S = ceil(W × 0.50)
```

因此每个合约最多只有一行。P70 作为原 P50/P70/P85 中的中间代表档；`W=40%T` 和 `S=W/2` 是固定的起始拆分，是否适合交易由回放器判断。

可选 `--config` 只允许调整基础筛选和这组公式：

```json
{
  "min_eligible_samples": 8,
  "max_fair_uncertainty_ticks": 10.0,
  "total_touch_quantile": 0.70,
  "width_ratio": 0.40
}
```

## 4. 运行与输出

```bash
./venv/bin/python scripts/generate_programmatic_grid_parameters.py \
  --input output/20260301_20260331-range-confirmed-depth/tick_candidate_events_annotated_confirmed_depth.csv \
  --output-dir output/programmatic_parameter_shapes_202603
```

输出目录只产生一个文件：

```text
parameter_shapes.csv
```

列固定为：

```text
commodity,target_contract,T_ticks,W_ticks,D_ticks,S_ticks
```

三月数据中的 AP605 按默认公式会输出：

```text
AP,AP605,108,44,64,22
```

合约没有出现在文件中，表示其合格事件数不足 8，或无法形成 `D > 0`；生成器不会再输出统计状态或候选说明。

## 5. 与回放器的边界

将 `W_ticks`、`D_ticks`、`S_ticks` 填入回放器的 `quote_shapes` 后，回放器负责：

- 全天成交与正常行情误成交验证；
- fair reference、对冲合约、时间参数和风控参数；
- 收益、最差日、对冲失败和动作峰值评价。

历史阶段 6 的多档位回放脚本和结果保留为历史验收记录，不是此生成器的输出契约。

## 6. 测试

```bash
./venv/bin/python -m pytest tests/test_programmatic_parameter_generator.py
```
