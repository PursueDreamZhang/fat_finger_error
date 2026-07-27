# 程序化网格回放性能优化分阶段执行计划

## 目标

在不改变当前程序化网格回放口径的前提下，降低：

- 每日行情准备耗时；
- W/D/S × 延迟场景状态机回放耗时；
- 统计和成交详情生成耗时；
- 多次调整参数后重复计算 fair 的耗时；
- 报告生成时的内存峰值。

本计划是给后续 agent 逐阶段执行的操作手册。每个阶段必须完成自己的测试、基线对比和性能记录，只有通过阶段闸门才允许进入下一阶段。

## 不可改变的业务口径

优化期间不得改变以下行为：

- `attach_fair_price_metrics()` 的 fair、basis、noise、阈值和参考合约规则；
- `LastPrice`、区间成交均价、买卖一触发成交的判断；
- W/D/S 网格生成和突破重定锚规则；
- ACK 延迟、撤单时序、对冲等待和超时规则；
- 止损、回归退出、时间退出、收盘退出；
- 资金、保证金、报撤限制；
- 候选事件成交与正常行情成交的分类；
- CSV 字段、HTML 默认展示方式和成交收益口径。

允许新增内部缓存、计时字段和可选运行模式，但默认模式必须保持现有输出语义。

## 主要文件范围

| 文件 | 用途 |
|---|---|
| `src/programmatic_grid.py` | 网格循环、每日准备、详情上下文、报告输出 |
| `src/programmatic_simulation.py` | 单日状态机、订单生命周期、盯市和统计 |
| `scripts/run_programmatic_grid.py` | 命令行入口和运行参数 |
| `tests/test_programmatic_grid.py` | 网格组合、汇总、详情上下文 |
| `tests/test_programmatic_simulation.py` | 状态机、成交、对冲、退出 |
| `tests/fixtures/programmatic_grid/` | 小型基线和黄金结果 |
| `scripts/benchmark_programmatic_grid.py` | 阶段计时和性能比较脚本 |

不新增第三方依赖；统一使用现有 Python、pandas、numpy、pytest 和标准库。

## Agent 执行协议

每个阶段严格按以下顺序执行：

1. 阅读本阶段的“范围”和“禁止事项”。
2. 先补测试或黄金基线，再改实现。
3. 只修改本阶段列出的文件和逻辑。
4. 执行本阶段指定测试。
5. 执行固定烟雾回放并与基线比较。
6. 记录本阶段各分段耗时、内存（能可靠测量时）和输出差异。
7. 通过闸门后，勾选本阶段并进入下一阶段。
8. 任一测试或口径对比失败，立即停止，不进入下一阶段；报告失败阶段、失败命令、首个差异和建议回滚点。若只是性能闸门未达到，但语义和测试全部通过，则按本阶段的“保留简单实现/跳过本子阶段”规则记录后继续，不得把“没有收益”误报成业务失败。

除非用户另行要求，agent 不执行 `git reset --hard`、`git checkout --`、删除用户输出或清理无关改动。阶段性提交不是本计划的必需条件。

每个通过阶段必须在执行日志中记录变更文件、基线签名、优化后签名、分段耗时、测试命令和通过结论。日志文件为：

```text
docs/superpowers/plans/2026-07-24-programmatic-grid-performance-optimization-log.md
```

如果当前工作流不允许提交，日志和阶段 patch 是回滚依据；不能只写“测试通过”而不保留比较结果。

### 固定执行顺序

agent 必须按下面的顺序推进，不能把后面的优化先合并进来：

```text
阶段 0 基线
  → 阶段 1 每日准备去重
  → 阶段 2A 活动订单索引
  → 阶段 2B pending/动作队列
  → 阶段 2C 轻量行情行访问
  → 阶段 2D 单帧 mark 缓存
  → 阶段 3 日统计标量化
  → 阶段 4 上下文按需切片
  → 阶段 5 fair 持久化缓存
  → 阶段 6 fair 内部优化（仅在 profiling 达到门槛时）
  → 阶段 7 多进程并行（可选）
  → 阶段 8 完整月份验收
```

每个箭头都代表前一阶段已经通过闸门。阶段 6 如果因为占比不足而跳过，必须在日志中写明跳过原因；如果已经进入阶段 6 但未通过，则按“统一停止条件”停住，不能自动跳到阶段 7。

## 固定测试与运行约定

测试统一使用：

```bash
./venv/bin/python -m pytest
```

固定小样本：

```bash
./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 \
  --end-date 20260302 \
  --output-dir output/programmatic_grid_perf_check_20260302
```

固定完整样本（只在阶段验收需要时运行，避免覆盖现有结果）：

```bash
./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid.example.json \
  --start-date 20260301 \
  --end-date 20260331 \
  --output-dir output/programmatic_grid_perf_check_20260301_20260331
```

固定 standalone 状态机烟雾样本：

```bash
./venv/bin/python scripts/run_programmatic_simulation.py \
  --config config/programmatic_simulation.example.json \
  --start-date 20260302 \
  --end-date 20260302 \
  --output-dir output/programmatic_simulation_perf_check_20260302
```

### benchmark 脚本契约

阶段 0 新增的 `scripts/benchmark_programmatic_grid.py` 必须支持：

```text
--config PATH
--start-date YYYYMMDD
--end-date YYYYMMDD
--repeat N
--output-dir PATH
--no-write
--core-only
--context-mode {all,none,selected}
--fair-cache-dir PATH
--workers N
```

- `--no-write` 只测 `run_programmatic_grid()`，不写 HTML/JSON；
- 在阶段 4 之前，`--no-write` 仍可能包含上下文构造耗时；必须依靠分段计时区分它。阶段 4 之后，`--context-mode none --no-write` 才代表不构造上下文的核心回放；
- `--core-only` 仅供阶段 0 的资源诊断，必须与 `--no-write` 同用；它调用内部“跳过详情构造”路径，不能替代默认 `all` 模式的语义基线或作为阶段通过依据；
- 未指定 `--no-write` 时，写出耗时必须单独计时；
- `--repeat` 每次使用独立临时子目录，不能互相覆盖；
- `--context-mode` 和 `--fair-cache-dir` 必须显式传给配置；
- `--workers` 在并行阶段前必须被拒绝或固定为 `1`，不能静默失效。

### 性能测量口径

- 同一阶段的基线和优化后运行必须使用相同配置、日期、Python 环境、数据目录和 `workers`；
- 单日重复运行以中位数作为主值，同时记录最小值、最大值和每次运行的分段值；
- 分段改善率定义为 `(baseline_median - optimized_median) / baseline_median`；
- 目标分段改善不足 10% 时，不得仅凭总耗时偶然下降宣称通过；
- 任一非目标核心分段回归超过 5%，必须在日志中解释并由人工决定是否保留；
- 内存只在能稳定取得峰值 RSS/等价指标时作为硬证据，否则标记为观察值，不得伪造精确数字。

### 比较范围

阶段 0 同时建立两套基线：

1. 单日基线：2026-03-02，用于每个阶段快速验收；
2. 完整月份基线：2026-03-01～2026-03-31，至少运行一次，用于最终验收。

阶段 0 不保存完整 79 MB 级上下文作为测试夹具；但必须保存可重建的上下文规范化签名和若干人工选定成交的完整字段。

## 统一阶段闸门

除各阶段专门验收外，每个阶段通过前必须同时满足：

1. 相关定向测试通过；
2. 常规回归通过：

   ```bash
   ./venv/bin/python -m pytest tests/ -q -k "not slow_"
   ```

3. 固定烟雾数据的以下结果与基线一致：
   - `summary` 每个场景的成交数、候选成交数、正常成交数、对冲失败数、净收益、动作峰值；
   - `daily` 每日统计；
   - `trades` 每笔成交的入场、对冲、退出、收益、状态和退出原因；
   - 默认兼容模式（当前代码等价于 `context_mode=all`）下，详情键、窗口边界、关键时点标记、目标/参考快照行数和规范化字段值；
   - `context_mode=none/selected` 只比较核心回放结果，不要求详情文件与 `all` 相同；
4. 所有数值差异都能解释：
   - 离散字段必须完全一致；
   - 浮点字段使用 `rtol=1e-6, atol=1e-9, equal_nan=True`；
5. 性能记录包含至少：
   - 数据加载；
   - fair/每日准备；
   - 场景状态机；
   - 日统计；
   - 成交上下文；
   - HTML/CSV/JSON 写出。

共享状态机还必须通过 standalone 回归：

```bash
./venv/bin/python -m pytest tests/test_programmatic_simulation.py -q
```

任何涉及 `_DayReplay` 的阶段都要执行该回归，不能只跑网格测试。

## 阶段 0：建立基线和分段计时

### 目的

先确认真正的耗时位置，防止把报告序列化时间误当成状态机时间，也防止为了很小的收益引入复杂结构。

### 修改范围

- 新增 `scripts/benchmark_programmatic_grid.py`；
- 新增 `tests/test_programmatic_grid_performance.py`；
- 必要时对 `src/programmatic_grid.py`、`src/programmatic_simulation.py` 增加默认关闭的计时钩子；
- 新增小型基线文件到 `tests/fixtures/programmatic_grid/20260302/`。

### 执行步骤

- [ ] 在任何性能改动前，用当前代码跑固定 20260302 烟雾样本。
- [ ] 用当前代码跑一次 20260301～20260331 完整月份样本；输出放到独立目录，不覆盖既有结果。
- [ ] 单日 `--no-write` 和端到端各连续运行至少 3 次，记录中位数；完整月份至少运行 1 次。
- [ ] 初始化执行日志文件，并记录当前工作树中与本计划无关的已有改动，后续阶段不得覆盖或回滚它们。
- [ ] 记录每个品种、每天、场景的：
  - 目标快照行数；
  - 场景数；
  - 成交数；
  - 订单日志数；
  - 状态迁移数；
  - 详情上下文数和快照行数。
- [ ] 保存 `summary.csv`、`daily.csv`、`trades.csv` 的可比较黄金副本；不保存 79 MB 级完整上下文作为测试夹具。
- [ ] 写一个 canonical compare helper，按稳定主键排序后比较 DataFrame，明确处理 NaN 和浮点容差。
- [ ] 增加独立合成 oracle，不直接从被测输出生成期望值，覆盖：
  - 同一时间键 ACK/撤单 ACK 的处理顺序；
  - action window 的 60 秒边界；
  - 买单优先级；
  - `LastPrice`、区间成交均价、买一/卖一相等触发及成交证据分类；
  - 价格带突破后的重定锚和 W/D/S 订单替换；
  - 数据断点后的首帧；
  - 目标成交前后 10 秒窗口；
  - 参考腿关键时点之前最近快照；
  - 无成交、对冲失败和收盘未平仓。
- [ ] 对当前上下文选取至少 1 个正常成交、1 个候选事件成交、1 个对冲失败成交，保存规范化后的完整目标/参考字段签名。
- [ ] 记录当前 HTML 和 context JSON 的大小，作为报告层基线。
- [ ] 可选生成 `cProfile/pstats` 报告，至少能定位到函数级热点；计时钩子默认关闭，不能污染正常输出。
- [ ] 为黄金副本和合成 oracle 记录内容签名；后续阶段只能更新实现，不能悄悄重写期望值。

### 验收命令

```bash
./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py -q
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --repeat 3 --no-write --output-dir output/programmatic_grid_benchmark_baseline
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.example.json \
  --start-date 20260301 --end-date 20260331 \
  --output-dir output/programmatic_grid_benchmark_full_baseline
./venv/bin/python scripts/run_programmatic_simulation.py \
  --config config/programmatic_simulation.example.json \
  --start-date 20260302 --end-date 20260302 \
  --output-dir output/programmatic_simulation_benchmark_baseline
```

### 阶段闸门

- 黄金文件可重复生成；
- 连续三次的结果签名一致；
- 单日和完整月份基线均已保存；
- 独立合成 oracle 通过；
- 计时能明确区分“状态机”和“上下文/报告”；
- 基线比较测试通过。

不满足时停留在阶段 0，不开始优化。

## 阶段 1：每日准备数据和场景配置去重

### 目的

消除同一品种、同一天、不同场景之间重复的排序、元数据解析、fair 衍生列和配置标准化。

### 修改范围

- `src/programmatic_grid.py`
- `src/programmatic_simulation.py`
- `tests/test_programmatic_grid.py`
- `tests/test_programmatic_simulation.py`

### 执行步骤

- [ ] 在日期循环外预先构造每个品种的标准化 `scenario_config`，不再每天每场景调用 `normalize_programmatic_simulation_config()`。
- [ ] 让 `_prepare_grid_day()` 返回已稳定排序的目标和对冲数据，并在准备阶段完成：
  - tick size；
  - contract multiplier；
  - fair 有效性；
  - `last_down_ticks` 等目标复盘派生列。
- [ ] 增加仅供网格内部使用的 prepared 输入入口；该入口必须显式标记 `assume_sorted=True`，不能让公开函数默认相信调用方已排序。
- [ ] 保证原有公开的 `simulate_programmatic_day()` 仍能接收普通、未排序 DataFrame，并保留原来的排序行为；只有网格路径使用日级准备结果。
- [ ] 为“已经排序 / 未排序 / 空帧 / 重复时间键”补测试。
- [ ] 运行 standalone simulation smoke，确认共享 `_DayReplay` 入口没有改变非网格输出。

### 禁止事项

- 不改变 fair 算法；
- 不改变 DataFrame 行顺序；
- 不删除任何输出字段；
- 不把不同交易日的数据混入同一个准备缓存。

### 验收命令

```bash
./venv/bin/python -m pytest tests/test_programmatic_grid.py tests/test_programmatic_simulation.py -q
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --repeat 3 --output-dir output/programmatic_grid_benchmark_stage1
```

### 阶段闸门

- 全部统一阶段闸门通过；
- `summary/daily/trades` 与阶段 0 黄金结果一致；
- 每日准备或场景初始化分段下降至少 10%；若该分段低于总耗时 5%，记录占比后跳过进一步复杂化；若介于两者之间但未达 10%，保留简单实现并记录“收益不足”，继续进入阶段 2A，不得为了过门槛继续堆抽象。

## 阶段 2A：活动订单索引

### 目的

消除每帧扫描和排序全部历史订单的开销，同时保持订单生命周期和日志完整。

### 修改范围

- `src/programmatic_simulation.py`
- `tests/test_programmatic_simulation.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] 增加活动目标订单索引，至少区分 `target_buy` 和 `target_sell`。
- [ ] 明确生命周期：
  - submit/ack：加入索引；
  - cancel_requested：仍保留在索引；
  - cancel_ack/filled/expired：移出索引；
  - replacement 切换时先处理旧单，再加入新单。
- [ ] `_first_target_fill()` 只按现有买单优先、卖单其次的顺序检查活动订单。
- [ ] 先记录现有同方向订单的完整排序键（价格、创建/ACK 时间、订单号等），索引内必须保持同样的稳定顺序，不能只保留“买先卖后”这一层语义。
- [ ] `_cancel_target_orders()`、`_has_live_target_orders()` 和 replacement 旧单列表只访问活动索引。
- [ ] 历史订单仍写入 `order_log`，不得为了索引而删除日志。

### 阶段闸门

- 合成 oracle 的订单生命周期、买单优先级和订单事件序列逐项一致；
- 20260302 黄金结果的成交、退出和订单日志一致；
- 核心状态机分段耗时下降至少 10%，否则保留简单实现并进入阶段 2B。

## 阶段 2B：pending 与动作窗口队列

### 目的

减少 `pending` 和 `action_keys` 的列表重建，同时锁死同一时间键的原有处理顺序。

### 修改范围

- `src/programmatic_simulation.py`
- `tests/test_programmatic_simulation.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] `pending` 队列元素必须带单调递增 `insertion_seq`。
- [ ] due 事件排序键固定为：

  ```text
  (due_key, kind_order, insertion_seq)
  ```

  其中 `kind_order` 必须复现当前 `(due_key, kind)` 的字典序。
- [ ] 每一帧先取出全部 `due_key <= current_key` 的事件，整批处理完后才调用 `_maybe_submit_replacement()` 和 `_maybe_complete_replacement()`。
- [ ] `action_keys` 使用 deque 时保持当前严格边界 `action_key > key - 60000`。
- [ ] 增加同一 `due_key`、相同 kind、跨 60 秒边界的专门测试。

### 阶段闸门

- pending 事件序列和状态迁移序列逐项一致；
- action count、峰值动作数、是否触发 `order_action_limit` 一致；
- 分段耗时下降至少 10%；若语义一致但未达到门槛，撤回本子阶段的复杂队列实现，保留简单版本并进入阶段 2C。

## 阶段 2C：轻量行情行访问

### 目的

只替换热循环中的 pandas `Series` 创建，不改状态机业务分支。

### 修改范围

- `src/programmatic_simulation.py`
- `tests/test_programmatic_simulation.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] 先用基准测试比较“有限字段 dict records”和“NumPy 列数组”两种实现，只保留更快且内存可接受的一种。
- [ ] 只缓存状态机实际需要的字段；上下文展示字段不进入热循环缓存。
- [ ] 网格内部使用轻量入口；公开 `simulate_programmatic_day()` 继续接受普通 DataFrame。
- [ ] `_row_key`、`_row_quote`、`_valid_fair`、`_fill_evidence` 的空值和 bool 语义保持一致。

### 阶段闸门

- standalone simulation 和网格结果均与黄金一致；
- 断点、无效盘口、NaN、开盘保护和收盘路径均有测试；
- `iterrows()` 热循环耗时下降至少 10%；若语义一致但未达到门槛，撤回本子阶段实现并进入阶段 2D。

## 阶段 2D：单帧 mark 结果缓存

### 目的

避免同一行情帧内重复计算目标/对冲可执行价和 PnL。

### 修改范围

- `src/programmatic_simulation.py`
- `tests/test_programmatic_simulation.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] 缓存生命周期严格限制为“当前 `market_time_key`、当前 trade 状态和当前方向”。
- [ ] 只在 `_update_worst_mark()` 与紧接着的止损判断之间复用；
- [ ] 一旦发生 hedge fill、target exit、flatten 或状态迁移，立即失效；
- [ ] 不跨行情帧复用报价，不把缓存写入交易输出。

### 阶段闸门

- 最差浮亏、止损、退出原因和净收益逐项一致；
- 对冲前、对冲后、紧急平仓和 quote unavailable 分支均有测试；
- 若目标分段改善低于 10%，或引入复杂缓存状态，撤回该子阶段缓存并进入阶段 3，不影响后续统计阶段。

### 阶段 2A～2D 统一验收命令

每个子阶段都要单独执行以下命令；不能等 2D 完成后才一次性比较：

```bash
./venv/bin/python -m pytest tests/test_programmatic_simulation.py tests/test_programmatic_grid_performance.py -q
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --repeat 3 --no-write \
  --output-dir output/programmatic_grid_benchmark_stage2
```

每个子阶段通过后在执行日志中追加一条记录，再进入下一个子阶段；失败时停在对应的 2A/2B/2C/2D，不得把多个未验收改动一起带入下一阶段。

## 阶段 3：日统计改为标量累计

### 目的

避免“每天 × 每个场景”重复创建临时 DataFrame 并扫描订单、过渡和成交日志。

### 修改范围

- `src/programmatic_simulation.py`
- `src/programmatic_grid.py`
- `tests/test_programmatic_grid.py`
- `tests/test_programmatic_simulation.py`

### 执行步骤

- [ ] 在 `_DayReplay` 中维护日级标量统计，或在 `_finish_day()` 一次性从已有列表计算。
- [ ] 不改变 `simulate_programmatic_day()` 现有返回键；如需 `day_stats`，通过内部 replay 方法或独立 helper 返回，避免隐式改变公共契约。
- [ ] 网格路径直接使用 `day_stats` 构造 `daily_rows`，并逐项对照 `_summary_row()` 的现有字段和 NaN 语义，不再调用 `build_programmatic_summary(...).iloc[0]`。
- [ ] 成交先保留为记录列表；只有网格全部日期完成后才构造完整的 `trades` DataFrame，不能在中途丢失成交日志。
- [ ] 确保没有成交的场景仍然能正确记录动作数、暂停数、重报价数和峰值动作数。
- [ ] 对 `win_rate`、`worst_net_pnl`、无成交 NaN、收盘未平仓和对冲失败分别写标量统计测试。

### 验收命令

```bash
./venv/bin/python -m pytest tests/test_programmatic_grid.py tests/test_programmatic_simulation.py -q
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --repeat 3 --output-dir output/programmatic_grid_benchmark_stage3
```

### 阶段闸门

- `daily` 和最终 `summary` 与黄金结果一致；
- 无成交场景、对冲失败场景和收盘未平场景分别有回归测试；
- 日统计耗时下降，且没有为了速度删除订单/过渡日志。

## 阶段 4：成交详情上下文按需切片

### 目的

把详情生成从“每笔成交扫描全天 DataFrame”改为按已排序时间键直接切片，并把报告生成和核心回放解耦。

### 修改范围

- `src/programmatic_grid.py`
- `scripts/run_programmatic_grid.py`
- `tests/test_programmatic_grid.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] 为 `_context_rows()` 使用 `np.searchsorted` 定位 `[start_key, end_key]`，不再对整帧做布尔扫描。
- [ ] 明确切片边界：起点使用 `side="left"`，终点使用 `side="right"`，保证前 10 秒和后 10 秒均为闭区间；重复时间键必须按原稳定顺序全部保留。
- [ ] 每个品种/日期/合约只准备一次详情所需的轻量记录，多个成交窗口复用。
- [ ] 在每日准备阶段只计算一次 `last_down_ticks`，上下文构造不再复制目标全表并重复计算。
- [ ] 保持目标成交前 10 秒、退出后 10 秒和参考合约 as-of 口径不变。
- [ ] 增加 `context_mode`：
  - `all`：保持现有完整详情行为；
  - `none`：不生成成交上下文，适合参数搜索；
  - `selected`：只为配置中明确列出的场景生成上下文。
- [ ] 明确定义配置：

  ```json
  {
    "context_mode": "selected",
    "context_scenarios": ["NI2605::Q01-L01", "FU2606::Q02-L03"]
  }
  ```

  `context_scenarios` 使用 `instrument::scenario_id`；不存在的组合必须报配置错误。
- [ ] 默认仍使用 `all`，直到阶段 4 验收完成；`none/selected` 只改变报告附加物，不改变回放结果。
- [ ] 当 `context_mode=none` 时，HTML 明确显示“本次运行未生成成交详情”，不能伪装成无成交。
- [ ] 新的上下文转换必须复现 pandas JSON 的规范化语义：
  - NaN 转为 JSON `null`；
  - numpy 标量转为普通 Python 标量；
  - 字段顺序、空值和浮点精度保持；
  - 中文字段保持。
- [ ] 复用同一份规范化 JSON 字符串写入详情 JSON 和 HTML；单文件 HTML 仍须离线可打开，不能为了省内存默认改成外链。
- [ ] `none/selected` 模式下，HTML 的查看详情按钮必须显示明确的“未生成上下文”状态，并增加对应渲染测试。

### 验收命令

```bash
./venv/bin/python -m pytest tests/test_programmatic_grid.py tests/test_programmatic_grid_performance.py -q
./venv/bin/python scripts/run_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --output-dir output/programmatic_grid_context_all_20260302
```

另用同一配置分别运行 `context_mode=all/none/selected`，比较核心 CSV；`all` 与 `selected` 还要比较选中成交的完整规范化上下文，`none` 只验证明确的未生成提示。

### 阶段闸门

- `all` 模式的详情窗口、关键时点、参考 as-of 行和字段与黄金结果一致；
- `none` 模式的 `summary/daily/trades` 与 `all` 完全一致；
- `selected` 模式只生成配置列出的场景，且不存在静默漏配；
- 详情构造耗时或报告内存至少下降 10%，或确认该分段低于总耗时 5% 后不再扩展复杂度；
- 不得因为关闭上下文而改变任何成交或收益结果。

## 阶段 5：持久化每日 fair 缓存

### 目的

用户反复调整 W/D/S、延迟和资金参数时，跳过不随场景变化的 fair 计算。

### 修改范围

- `src/programmatic_grid.py`
- `scripts/run_programmatic_grid.py`
- 新增必要的缓存工具测试
- 不修改 `src/tick_detector/reference_selection.py` 的业务算法

### 执行步骤

- [ ] 新增可选 `fair_cache_dir`，默认关闭或使用明确的项目缓存目录。
- [ ] 固定 `FAIR_CACHE_SCHEMA_VERSION`，缓存目录与本次运行输出目录分离。
- [ ] 缓存键至少包含：
  - 品种、目标合约、参考合约列表；
  - 交易日；
  - tick size；
  - `max_fair_age_ms`；
  - fair 算法版本号及 schema 版本；
  - 目标和每个参考合约源文件的路径及内容指纹（大小/mtime 只能作为快速预检，不能作为唯一有效性依据）；
  - 日线 bounds 数据的路径及内容指纹；
  - `top_volume_peer_contracts` 及其顺序。
- [ ] 缓存命中时读取已 enriched 的目标帧；未命中时执行现有 fair 流程并原子写入。
- [ ] 内容指纹在一次运行中按源文件计算并复用，不能在日期×场景循环中重复哈希；写缓存使用临时文件加原子替换，避免中断或并行读取拿到半文件。
- [ ] 缓存损坏、版本不匹配或字段不完整时自动放弃缓存并重算。
- [ ] 明确缓存格式：优先使用 Parquet 加 JSON manifest；若因 object 列只能使用 pickle，必须只读取指定 cache_dir 中由当前程序生成且 manifest 完全匹配的文件。
- [ ] 首版只缓存 enriched 目标帧，不同时缓存所有原始合约，避免扩大磁盘和内存范围。
- [ ] 缓存命中后仍验证必需字段（包括新增的详情派生字段）；字段缺失必须 miss。
- [ ] 如果阶段 6 改变 fair 算法、enriched 字段或序列化结构，必须递增 `FAIR_CACHE_SCHEMA_VERSION`，不能复用旧缓存。

### 验收命令

```bash
./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py -q
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --fair-cache-dir output/programmatic_grid_fair_cache \
  --repeat 2 --output-dir output/programmatic_grid_benchmark_cache
```

### 阶段闸门

- 首次运行（cache miss）与不开缓存的结果完全一致；
- 第二次运行（cache hit）与第一次结果完全一致；
- 修改参考合约、fair 参数、任一源文件指纹、日线 bounds 或缓存版本后必须 miss；
- fair 计算耗时在 cache hit 时被跳过或显著下降；
- 缓存读取失败不能导致静默使用过期结果。

## 阶段 6：fair 内部优化分支（按 profiling 决定）

### 进入条件

阶段 0 的计时显示 `fair/每日准备` 占单进程总耗时至少 40%。若低于 40%，只记录“跳过本阶段”的原因，直接进入阶段 7。

### 目的

只优化 `attach_fair_price_metrics()` 内部的数据访问和窗口计算，不改变任何 fair、basis、noise、阈值公式。

### 修改范围

- `src/tick_detector/reference_selection.py`
- `tests/test_tick_detector_reference_selection.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] 先用 profiling 定位 Pass 1、peer as-of、Pass 2 的具体热点；
- [ ] 只做数组复用、已排序输入复用和输出写入优化；
- [ ] 不引入近似 percentile、近似 median、固定行数窗口或新依赖；
- [ ] 维持不规则时间窗口、左右边界、NaN 和 object 列语义。

### 阶段闸门

- enriched frame、fair、noise、阈值、blocked reason 和参考集合逐列与基线一致；
- fair/每日准备分段耗时下降至少 10%；否则停止该分支；
- 如果已经进入本阶段但未通过，必须按统一协议停住并等待人工判断，不能自动跳到阶段 7。

## 阶段 7：按交易日/品种的多进程并行（可选）

### 目的

在单进程优化完成后，利用独立交易日之间的天然并行性缩短墙钟时间。

### 前置条件

- 阶段 0～5 全部通过；
- 阶段 6 若满足进入条件则必须通过，若不满足则记录跳过；
- 已确认剩余主要耗时仍在状态机或每日准备，而不是 HTML 写出；
- 单进程 `workers=1` 的结果已固定为黄金基线。

### 修改范围

- `src/programmatic_grid.py`
- `scripts/run_programmatic_grid.py`
- `tests/test_programmatic_grid_performance.py`

### 执行步骤

- [ ] 增加 `workers` 参数，默认 `1`，不改变现有运行方式。
- [ ] 以“品种 + 交易日”为任务边界；一个 worker 内一次准备当天数据，再运行该天所有场景。
- [ ] worker 必须使用与单进程相同的历史 lookback、日线 bounds、参考合约集合和排序规则；不能因为按天切分而丢掉 fair 所需的前置历史。
- [ ] worker 函数必须是模块顶层函数，保证 macOS spawn 模式可启动。
- [ ] 主进程按 `(instrument, trade_date, scenario_id)` 稳定排序合并。
- [ ] 不把一个大 DataFrame 为每个场景单独序列化发送给 worker。
- [ ] worker 只返回核心 daily/trades/orders/transitions；不得返回完整 HTML 或完整 `trade_contexts`。
- [ ] 并行首轮只验收 `context_mode=none`；`all`/`selected` 的上下文在主进程生成，或单独做小样本验证。
- [ ] 若后续支持并行下的 `all/selected`，主进程必须用同一源数据做第二次上下文切片，或只接收选中成交所需的紧凑窗口；不得依赖 worker 返回完整上下文，也不得把大帧复制回主进程。
- [ ] worker 异常、缺失日和质量排除必须回传结构化错误，不能让主进程静默丢日期。
- [ ] 先验证 `workers=2`，再考虑更高并发；避免同时解压过多 ZIP 造成磁盘争用。

### 验收命令

```bash
./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py -q
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --workers 1 --context-mode none --repeat 2 \
  --output-dir output/programmatic_grid_benchmark_workers1
./venv/bin/python scripts/benchmark_programmatic_grid.py \
  --config config/programmatic_grid.smoke.json \
  --start-date 20260302 --end-date 20260302 \
  --workers 2 --context-mode none --repeat 2 \
  --output-dir output/programmatic_grid_benchmark_workers2
```

### 阶段闸门

- `workers=1` 与 `workers=2` 的核心输出逐项一致；
- worker 模式下异常、缺失日和质量排除仍能稳定记录；
- 墙钟时间至少改善 20%，且内存峰值和磁盘争用可接受；
- 达不到收益时保留 `workers=1`，不为并行继续增加复杂度。

## 阶段 8：完整月份最终验收

### 执行步骤

- [ ] 用 `config/programmatic_grid.example.json` 跑 20260301～20260331 到新的输出目录。
- [ ] 分别记录：
  - 总耗时；
  - 每个品种耗时；
  - 每日准备、状态机、详情和写出耗时；
  - 峰值内存（可可靠获取时）；
  - 总场景数、成交数、上下文数。
- [ ] 与阶段 0 的完整月份基线比较：
  - summary；
  - daily；
  - trades；
  - 选择原因和风险标签；
  - `context_mode=all` 的上下文数量与关键字段。
- [ ] 运行 standalone simulation smoke，确认共享状态机优化没有影响非网格入口。
- [ ] 运行全量常规测试：

  ```bash
  ./venv/bin/python -m pytest tests/ -q -k "not slow_"
  ```

### 最终通过条件

- 核心交易结果逐项一致；
- 没有未解释的浮点、排序或空值差异；
- 性能报告列出优化前后各分段耗时，而不是只给一个总倍数；
- 默认运行命令仍可用；
- `context_mode=all` 仍能打开离线详情；
- `context_mode=none` 可用于快速参数筛选；
- 所有失败或跳过的阶段都有明确记录。

## 统一停止条件

出现以下任一情况，agent 必须停止当前阶段并等待人工判断：

- 成交数、成交分类、退出原因或净收益变化；
- 订单事件或状态迁移顺序变化；
- fair、合理价可靠性、参考合约集合或阈值变化；
- 详情窗口边界、as-of 规则或关键时点标记变化；
- 性能没有改善，却引入了难以维护的抽象；
- 只能通过减少数据、放宽成交条件或关闭风险控制来获得速度；
- 缓存可能读到过期或不完整结果；
- 并行结果不稳定或无法复现。

## 执行结束后的交付物

完成后必须留下：

- 本计划中各阶段 checkbox 和通过/停止记录；
- 性能基线与最终报告；
- 固定烟雾样本的黄金比较测试；
- 完整月份新输出目录；
- 一段中文总结，分别说明：
  - 哪些阶段实际完成；
  - 每阶段耗时变化；
  - 是否有任何业务口径变化；
  - 日常调参推荐使用的运行模式。
