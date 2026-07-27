# 程序化网格回放器说明

## 1. 它解决什么问题

程序化网格回放器用于评估“提前挂双向被动单，目标腿成交后立即对冲”的乌龙指交易设想。它不是实盘下单程序，也不是交易所成交回报还原器。

回放以约 500ms 的快照为输入：用无未来的 `fair_price` 决定是否重定锚，用目标合约的 `LastPrice`、区间成交均价与买卖一判断模拟成交；目标腿成交后按对冲合约当时或之前最近一笔买卖一加滑点模拟对冲和退出。

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
|`quote_shapes`|网格形状，包含 `W`、`D`、`S`|
|`latency_profiles`|撤单、新单、对冲与确认延迟档位|
|`context_mode`|详情生成模式，默认 `all`|
|`context_scenarios`|`selected` 模式下需要详情的 `instrument::scenario_id` 列表|
|`fair_cache_dir`|可选；每日 fair 缓存目录|

`W/D/S` 的含义：

- `W`：合理价带半宽（tick）；
- `D`：带外被动报价距离（tick）；
- `S`：合理价持续越过带边界后触发重定锚的步长（tick）。

例如合理价为 100、`W=10`、`D=10`、`S=10` 时，报价为 80 买入和 120 卖出。合理价确认跌破 90 后，会按延迟与撤改单规则重新锚定；这不是每个快照追价。

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

目标腿成交后，回放等待 `hedge_submit_latency_ms`，以对冲合约 as-of 快照的可执行一档价格加 `slippage_ticks` 模拟对冲。若超出 `max_hedge_wait_ms` 仍无可用报价，则按对冲失败退出。

退出包括合理价回归、止损、止盈、最长持有期、参考失效、数据断点和收盘强平。报告会同时给出目标盈亏、对冲盈亏、净收益、未对冲最差浮亏和对冲后最差浮亏。

### 4.3 必须注意的边界

- 不模拟盘口排队位置、真实委托回报、撮合优先级或交易所限频；
- 成交与对冲不能保证在同一快照内真实完成；
- 数据断点、无有效盘口、参考失效会进入暂停或紧急退出路径；
- 结果只能用于筛选可研究的参数组合，实盘前还需结合真实手续费、保证金、交易权限和风控限制复核。

## 5. 输出与 HTML 报告

|文件|内容|
|---|---|
|`programmatic_grid_summary.csv`|参数组合级汇总与筛选结果|
|`programmatic_grid_daily.csv`|组合逐日成交、收益、失败和动作峰值|
|`programmatic_grid_trades.csv`|每笔模拟交易的进出、盈亏与风险字段|
|`programmatic_grid_trade_contexts.json`|单笔复盘窗口快照|
|`programmatic_grid_report.html`|可离线打开的分层交互报告|
|`programmatic_grid_run_config.json`|本次实际生效配置|

HTML 默认优先展示有成交组合。点击组合后可按候选事件/正常行情、方向、成交证据、退出原因和状态筛选单笔交易；点击“查看详情”可查看目标成交前 10 秒到退出后 10 秒的目标、参考和对冲快照。`context_mode=none` 时，报告会明确提示未生成详情。

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
       ├─ build_grid_trade_contexts()                单笔前后窗口与 as-of 参考快照
       ├─ _render_grid_report()                      离线 HTML 报告
       └─ write_programmatic_grid_outputs()          CSV/JSON/HTML 写出

src/programmatic_simulation.py
  ├─ normalize_programmatic_simulation_config()      单场景参数规范化
  ├─ _DayReplay                                  全天状态机、订单、仓位与风控
  ├─ _simulate_programmatic_day_prepared()       网格内部复用已排序帧入口
  └─ simulate_programmatic_day()                 对外单日回放入口

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
