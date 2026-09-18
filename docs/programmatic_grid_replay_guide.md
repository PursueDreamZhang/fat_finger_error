# 程序化网格回放器说明

## 1. 它解决什么问题

程序化网格回放器用于评估“提前挂双向被动单，目标腿成交后立即对冲”的乌龙指交易设想。它不是实盘下单程序，也不是交易所成交回报还原器。

回放以约 500ms 的快照为输入：目标腿挂单锚点和重定锚由目标合约 `LastPrice` 驱动，`fair_price` 保留用于诊断对比；目标合约的 `LastPrice`、区间成交均价与买卖一判断模拟成交；目标腿成交后按对冲合约当时或之前最近一笔买卖一档价模拟对冲和退出（对冲、退出与盯市均不加滑点）。

候选事件 CSV **只用于成交后的归因标签**（候选事件成交或正常行情成交），不参与下单、撤单、重定锚或成交判定。因此“模拟成交数”不等于“乌龙指机会数”。

## 2. 快速使用

使用示例配置运行单日回放：

```bash
./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid.stage0_smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --output-dir output/programmatic_grid_smoke
```

推荐在反复调 W/D/S、延迟或资金参数时启用 fair 缓存：

```bash
./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid.example.json \
  --start-date 20260301 --end-date 20260331 \
  --output-dir output/programmatic_grid_NI_202603 \
  --fair-cache-dir output/programmatic_grid_fair_cache
```

`output_dir` 每次必须不同；多品种并行时可共用 `fair_cache_dir`，但首次同时计算相同品种、相同日期时可能重复建一次缓存。

性能基准（不写报告）使用：

```bash
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.stage0_smoke.json \
  --start-date 20260302 --end-date 20260302 --repeat 2 --no-write \
  --output-dir /private/tmp/grid_benchmark \
  --fair-cache-dir /private/tmp/grid_fair_cache
```

## 3. 配置结构

配置范例见 `config/programmatic_grid.example.json`。

顶层主要字段：

|字段|含义|
|---|---|
|`output_dir`|本次 CSV、JSON、HTML 输出目录|
|`base`|所有品种共用的回放、风险和数据路径参数|
|`instruments`|目标、参考与对冲合约定义|
|`quote_shapes`|网格形状，包含 `W_pct`、`D_pct`、`S_pct`；数值单位为百分比|
|`latency_profiles`|撤单、新单、对冲与确认延迟档位|
|`context_mode`|详情生成模式，默认 `all`|
|`context_scenarios`|`selected` 模式下需要详情的 `instrument::scenario_id` 列表|
|`fair_cache_dir`|可选；每日 fair 缓存目录|
|`base.quote_spread_multiple`|买卖一价差保护倍数 N，默认 `2`；单配置入口使用顶层同名字段|
|`base.enable_hedge`|是否执行参考合约对冲，默认 `true`；单配置入口使用顶层同名字段|

`W_pct/D_pct/S_pct` 的含义：

- `W_pct`：诊断用合理价带半宽（百分比）；
- `D_pct`：带外被动报价距离（百分比）；
- `S_pct`：`LastPrice` 持续越过诊断价带边界后触发重定锚的步长（百分比）。

三个百分比字段必须显式提供，并且是有限正数；`W_pct + D_pct` 必须小于 100，`S_pct` 必须小于 100。旧的 `W/D/S` 或固定 tick 字段不会自动换算，需重新生成百分比参数。

例如 `LastPrice` 为 100、`W_pct=10`、`D_pct=10`、`S_pct=10` 时，报价为 80 买入和 120 卖出；锚点为 200 时会按同样百分比重新换算实际跳数。每次初始报价或重定锚都换算一次，同一轮挂单期间固定。`LastPrice` 确认跌破诊断价带下沿后，会按延迟与撤改单规则重新锚定；这不是每个快照追价。fair 仍计算并展示，但不参与挂单锚点或重定锚决策。

目标腿开仓还会检查盘口价差：按当前网格锚点换算得到的实际 `T_ticks = W_ticks + D_ticks` 必须严格大于 `N × (卖一 - 买一) / tick_size`。默认 `N=2`，可通过 `quote_spread_multiple` 调整。买一或卖一无效时采取保守策略，撤销目标挂单并暂停，盘口价差恢复后再重新报价。该保护只作用于目标腿开仓，不改变对冲和平仓成交口径。

`enable_hedge=false` 时，目标腿成交后不提交对冲，也不执行对冲腿保证金检查；沿用 `hedged_exit_delay_ms` 延迟后只平目标腿，退出原因记为 `no_hedge_exit`。该模式仍保留对冲合约配置和参考快照结构，但对冲成交价、对冲盈亏为空。它不是持仓到收盘模式，目标腿到期无法按可执行盘口平仓时仍按收盘未平处理。

`instruments` 中：

- `target_contract`：挂被动单、识别成交的目标合约；
- `fair_reference_contracts`：构建无未来合理价的参考合约列表；
- `hedge_contract`：目标成交后的对冲腿；可与某个 fair reference 相同。

## 4. 成交、对冲和风险口径

### 4.1 成交证据

默认 `observable_cross_assumed` 模型下，买单限价被以下任一可观察条件触发时视为模拟成交：

- 有区间成交量，且 `LastPrice <= 限价`；
- 有区间成交量，且区间成交均价 `interval_vwap <= 限价`；
- 卖一价格不高于限价，且卖一量满足配置的完整手数要求。

卖单方向取相反条件。它们是快照条件下的成交假设，不能表述为真实成交。

### 4.2 对冲与退出

目标腿成交后，回放等待 `hedge_submit_latency_ms`，以对冲合约 as-of 快照的可执行一档价格模拟对冲（不加滑点）。若超出 `max_hedge_wait_ms` 仍无可用报价，则按对冲失败直接平目标腿。

退出由对冲驱动，不再依赖 fair：对冲成交后延迟 `hedged_exit_delay_ms` 平两腿（`hedged_exit`）；对冲超时未成则直接平目标腿（`hedge_failure_exit`）；另有数据断点紧急平仓和收盘强平。fair 仅用于诊断和详情对比。报告会同时给出目标盈亏、对冲盈亏、净收益、未对冲最差浮亏和对冲后最差浮亏。

### 4.3 必须注意的边界

- 不模拟盘口排队位置、真实委托回报、撮合优先级或交易所限频；
- 成交与对冲不能保证在同一快照内真实完成；
- 数据断点、无有效盘口或价差保护不满足时进入暂停；目标合约 `LastPrice` 缺失、为 0 或无效时撤销目标挂单并暂停，恢复后再按确认时间重新报价；fair 是否可靠不影响目标腿报价；
- 结果只能用于筛选可研究的参数组合，实盘前还需结合真实手续费、保证金、交易权限和风控限制复核。

## 5. 输出与 HTML 报告

|文件|内容|
|---|---|
|`programmatic_grid_summary.csv`|参数组合级汇总与筛选结果|
|`programmatic_grid_daily.csv`|组合逐日成交、收益、失败和动作峰值|
|`programmatic_grid_trades.csv`|每笔模拟交易的进出、盈亏与风险字段|
|`programmatic_grid_trade_contexts.json`|单笔复盘窗口快照、成交前报价计算行和关键流程事件|
|`programmatic_grid_report.html`|可离线打开的分层交互报告|
|`programmatic_grid_run_config.json`|本次实际生效配置|

HTML 默认优先展示有成交组合。点击组合后可按候选事件/正常行情、方向、成交证据、退出原因和状态筛选单笔交易；点击“查看详情”可查看：

- **成交前 10 秒报价计算**：每个目标快照的 W/D/S 百分比及按当时锚点换算的 tick 数、fair（诊断）、报价锚点、合理价带、买卖挂单价、盘口和报价状态；锚点与挂单价按当时最近的状态转换取值，实际锚点和重定锚信号来自 `LastPrice`。
- **交易全流程**：目标成交、对冲提交/成交、平仓提交/成交、关键状态切换及失败原因，按时间排序；这部分只展示关键交易生命周期，不展开全部 ACK/撤单日志。
- **原始快照表**：保留目标、参考和对冲合约的目标成交前后窗口，并在关键时点标记成交、对冲和退出。

`context_mode=none` 时，报告会明确提示未生成详情。已有旧版 HTML 不会自动体现 LastPrice 锚定逻辑，需要重新运行生成报告。

## 6. 详情模式与缓存

`context_mode`：

- `all`：每笔模拟成交都生成详情，默认模式；
- `none`：不生成详情，适合大量参数搜索；
- `selected`：仅生成 `context_scenarios` 指定组合的详情。

fair 缓存只保存已经 enriched 的目标帧，不缓存原始合约数据。缓存键包含交易日、合约关系、tick、fair 时效参数、tick 数据容器与日线 bounds 的内容指纹；schema、manifest 或必需字段不匹配时自动重算。缓存命中不会改变成交、收益和详情口径。

## 7. 代码结构

```text
scripts/run_programmatic_grid.py
  └─ src/programmatic_grid.py
       ├─ load/normalize_programmatic_grid_config()  配置校验与默认值
       ├─ build_grid_scenarios()                     W/D/S × 延迟笛卡尔组合
       ├─ run_programmatic_grid()                    品种/日期/场景主循环
       ├─ _prepare_grid_day()                        加载帧、fair、缓存、事件键
       ├─ build_trade_contexts()（已移至 simulation.py，grid import）                单笔前后窗口与 as-of 参考快照
       ├─ _render_grid_report()                      离线 HTML 报告
       └─ write_programmatic_grid_outputs()          CSV/JSON/HTML 写出

src/programmatic_simulation.py
  ├─ normalize_programmatic_simulation_config()      单场景参数规范化
  ├─ _DayReplay                                  全天状态机、订单、仓位与风控
  ├─ _simulate_programmatic_day_prepared()       网格内部复用已排序帧入口
  ├─ simulate_programmatic_day()                 对外单日回放入口
  ├─ build_trade_contexts()                      单笔前后窗口与 as-of 参考快照（grid/simulation 共用）
  ├─ _render_programmatic_report()               单配置离线 HTML 报告（含成交复盘）
  └─ write_programmatic_simulation_outputs()     单配置 CSV/HTML 写出

src/tick_detector/reference_selection.py
  └─ attach_fair_price_metrics()                  无未来合理价与可靠性指标

src/tick_detector/tick_io.py
  └─ 读取 tick CSV/zip、快照预处理与日线边界校验
```

网格层只缓存和复用“不随场景变化”的每日结果；每个 W/D/S 与延迟场景仍独立运行 `_DayReplay`，保证订单状态、撤改单延迟、成交和对冲逻辑互不污染。

## 8. 推荐工作流

1. 先用 `stage0_smoke` 单日配置检查改动与速度；
2. 用 `context_mode=none` 和共享 fair 缓存搜索较大的参数网格；
3. 将候选组合切到 `selected` 或 `all`，查看单笔交易前后的快照链路；
4. 按品种拆分配置并行跑全月，输出目录分开；
5. 以正常行情误成交率、对冲失败、最差单日、保证金和报撤峰值共同筛选，而非只看净收益。

## 9. 按品种命令级并行

回放器目前不在代码内部自动启动多个工作进程。多品种全月运行时，推荐按品种准备独立配置文件，再由 shell 并行启动多个普通回放命令。

例如从 `config/programmatic_grid.example.json` 分别制作：

- `config/programmatic_grid_NI.json`：`instruments` 只保留 `NI2605`；
- `config/programmatic_grid_FU.json`：`instruments` 只保留 `FU2606`。

在 macOS/zsh 终端运行：

```bash
./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid_NI.json \
  --start-date 20260301 --end-date 20260331 \
  --output-dir output/programmatic_grid_NI_202603 \
  --fair-cache-dir output/programmatic_grid_fair_cache_NI \
  > /private/tmp/grid_NI_202603.log 2>&1 &

./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid_FU.json \
  --start-date 20260301 --end-date 20260331 \
  --output-dir output/programmatic_grid_FU_202603 \
  --fair-cache-dir output/programmatic_grid_fair_cache_FU \
  > /private/tmp/grid_FU_202603.log 2>&1 &

wait
```

- 命令末尾的 `&` 使该品种在后台运行，随后可立即启动下一个品种；`wait` 会等待全部任务结束；
- 每个进程的 `output_dir` 必须不同，否则 CSV、JSON 和 HTML 会相互覆盖；
- 首次并行建缓存时，建议每个品种使用不同 `fair_cache_dir`，避免同时写同一个缓存文件；
- 可通过 `tail -f /private/tmp/grid_NI_202603.log` 查看某个品种的实时日志；
- 完成后分别打开每个输出目录中的 `programmatic_grid_report.html`。

## 10. 单配置回放（run_programmatic_simulation）

网格回放用于扫多组 W/D/S × 延迟参数；若只想验证**一组已知参数**，用单配置入口更快，且报告同样含成交复盘。两者共用 `_DayReplay`，成交/对冲/退出/复盘口径完全一致。

```bash
./venv/bin/python scripts/run_programmatic_simulation.py \
  --config config/programmatic_simulation.example.json \
  --start-date 20260303 --end-date 20260303 \
  --output-dir output/programmatic_simulation_ap605_20260303
```

`--start-date` / `--end-date` / `--output-dir` 可覆盖配置里的同名项。配置范例见 `config/programmatic_simulation.example.json`，主要字段：

|字段|含义|
|---|---|
|`commodity` / `target_contract`|品种与目标合约|
|`fair_reference_contracts`|构建无未来 fair 的参考合约（≥2 个）|
|`hedge_contract`|对冲腿，必须属于上面的参考集|
|`enable_hedge`|是否执行对冲，默认 `true`；设为 `false` 时延迟后只平目标腿|
|`band_half_width_pct` / `outer_quote_offset_pct` / `reanchor_step_pct`|即 W / D / S，百分比单位（`1.0` 表示 1%）|
|`hedged_exit_delay_ms`|对冲成交后延迟多久平两腿|
|`max_order_actions_per_minute`|每分钟报撤动作上限|
|`events_csv`|候选事件 CSV，仅用于成交后归因标签，可为空|

输出（写入 `output_dir`）：

|文件|内容|
|---|---|
|`programmatic_summary.csv`|汇总（overall + 逐日）|
|`programmatic_trades.csv`|每笔模拟交易明细|
|`quote_state_transitions.csv`|状态机转换链|
|`order_lifecycle.csv`|订单挂/撤/成交生命周期|
|`programmatic_report.html`|离线报告，含成交复盘（点“查看详情”看目标+参考+对冲快照，复盘数据内嵌本文件，不单独出 JSON）|
|`programmatic_skipped_days.csv`|跳过的交易日|
|`run_config.json`|本次生效配置|

与网格回放的区别仅在：单配置不扫参数网格、不写 `programmatic_grid_trade_contexts.json`（复盘直接内嵌 report），输出文件名为 `programmatic_*`（网格为 `programmatic_grid_*`）。

## 11. 自动选参驱动的 Mid 距离带回放

`docs/mid_analyzer/auto_parameter_selector.py` 生成的 `auto_parameters.csv` 可以直接作为回放参数。该入口按合约和方向分别读取 `TargetDistance`、`MinDistance`、`MaxDistance`，每秒用目标合约 `Mid=(BidPrice1+AskPrice1)/2` 检查挂单；距离落在闭区间内保持原单，越界只撤对应方向。成交采用订单生效后的 `LastPrice` 触达且 `delta_volume > 0` 的整手快照假设。

本模式固定不对冲；成交后按 `--hold-seconds` 指定的时间，用目标合约可执行一档盘口平仓。5 秒是上游模型的统计观察窗口，不会被回放器当作平仓时间。

如需覆盖手续费、手数、资金、订单生效延迟或 tick/乘数，可额外传入一个只包含执行假设的 JSON：`--config execution_overrides.json`。该 JSON 禁止 W/D/S、fair 参考和对冲字段；省略时使用代码默认值。

```bash
./venv/bin/python scripts/run_programmatic_simulation.py \
  --parameters output/auto_result/auto_parameters.csv \
  --input data/tick2026 \
  --start-date 20260401 --end-date 20260430 \
  --hold-seconds 2 \
  --output-dir output/mid_distance_202604
```

该模式的输出包括 `parameters_used.csv`、`replay_summary.csv`、`replay_trades.csv`、`quote_checks.csv`、`replay_skipped_days.csv` 和 `replay_index.html`；每个合约另有同名检查、订单与交易文件。`SafeDistance`、事件数量和 `FollowRatio` 只作为参数快照保留，不在成交时再次筛选；参数 CSV 中 `NO_QUALIFIED_DISTANCE` 的方向不会挂单。
