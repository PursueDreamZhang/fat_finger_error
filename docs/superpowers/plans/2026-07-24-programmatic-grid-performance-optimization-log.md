# 程序化网格回放性能优化执行日志

对应计划：[2026-07-24-programmatic-grid-performance-optimization.md](./2026-07-24-programmatic-grid-performance-optimization.md)

> 本文件只记录实际执行结果，不预填性能数字。每个阶段通过、跳过或停止后再补写对应条目。

## 执行环境

- 工作目录：`/Users/zhangchunfu/my/mac/python/fat_finger_error`
- Python/依赖版本：项目 `./venv/bin/python`
- 数据目录与数据版本：`data/tick2026`；2026-03 有 22 个 ZIP，合计约 4.1GB
- 开始时间：2026-07-24
- 与本计划无关的已有改动：`.gitignore`、交易统计设计文档、`src/tick_detector/reference_selection.py`，以及既有程序化回放/输出文件；未覆盖或回滚。

## 阶段状态

| 阶段 | 状态（待执行/通过/跳过/停止） | 备注 |
|---|---|---|
| 0 基线 | 通过（用户批准单日替代） | 保留完整月份限制记录；以 2026-03-02 单日完整详情签名作为后续口径基线。 |
| 1 每日准备去重 | 通过（收益不足） | 口径回归通过；单场景无法测出 10% 以上收益，保留最小实现后进入 2A。 |
| 2A 活动订单索引 | 通过 | 真实单日签名不变；状态机单次约 3.17s → 1.40s。 |
| 2B pending/动作队列 | 跳过（收益不足） | 口径回归通过；状态机改善约 1%，已撤回实现。 |
| 2C 轻量行情行访问 | 待执行 |  |
| 2D 单帧 mark 缓存 | 待执行 |  |
| 3 日统计标量化 | 待执行 |  |
| 4 上下文按需切片 | 待执行 |  |
| 5 fair 持久化缓存 | 待执行 |  |
| 6 fair 内部优化 | 待执行 |  |
| 7 多进程并行 | 待执行 |  |
| 8 完整月份验收 | 待执行 |  |

## 阶段记录模板

每个阶段至少记录以下内容：

```text
阶段：
状态：通过 / 跳过 / 停止
修改文件：
基线签名：
优化后签名：
定向测试：
常规回归：
基线分段耗时：
优化后分段耗时：
改善率：
内存观察：
输出差异：
失败命令或首个差异（如有）：
回滚点：
结论：
```

## 关键输出位置

- 单日基线：`output/programmatic_grid_stage0_baseline/run_01_wgm0mfue`
- 完整月份基线：未生成（阶段 0 停止条件）
- 最终完整月份输出：未生成
- benchmark 结果：轻量完整详情样本三次独立进程：核心耗时分别约 16.446s、16.460s、16.529s，中位数约 16.460s；签名均为 `8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083`。
- profile 结果：未生成

## 阶段记录

### 阶段 0：建立基线和分段计时

```text
阶段：0
状态：通过（用户于 2026-07-24 批准以单日完整详情基线替代完整月份闸门）
修改文件：src/programmatic_grid.py；scripts/benchmark_programmatic_grid.py；tests/test_programmatic_grid_performance.py；config/programmatic_grid.stage0_smoke.json；tests/fixtures/programmatic_grid/20260302/*；本计划与本日志。
基线签名：轻量完整详情样本 8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083；全量单日既有产物清单见 full_smoke_artifact_manifest.json。
优化后签名：不适用；阶段 0 只增加计时、签名和可选 core-only 诊断路径，默认 all 输出未改变。
定向测试：./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py tests/test_programmatic_grid.py tests/test_programmatic_simulation.py -q -> 23 passed, 1 skipped。
真实烟雾：env RUN_PROGRAMMATIC_GRID_STAGE0_SMOKE=1 ./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py::test_stage_zero_real_smoke_matches_baseline_signature -q -> 1 passed in 16.72s。
基线分段耗时：单次完整详情约 data_load 0.583s、fair_prepare 12.672s、state_machine 3.172s、trade_context 0.012s、写出 0.004s。
内存观察：单场景完整详情峰值 RSS 约 507MB；24 场景、711 笔成交的默认详情构造在当前执行资源中止。
输出差异：单场景 default all 与 core-only 的 summary/daily/trades/skipped_days 签名一致；core-only 仅不生成 trade_contexts。
失败命令或首个差异：完整 2026-03-02 24 场景 default all 在构造大量详情上下文时被执行环境中止，未产生业务字段差异。
回滚点：本阶段开始前的工作树；新增计时和 benchmark 可单独移除。
结论：完整月份 default all 基线尚未完成。用户已批准以单日完整详情基线进入阶段 1；完整参数网格一天 core-only 仍在结果汇总前被当前执行资源中止，说明后续阶段仍需用单日签名和定向测试守住口径。
```

### 阶段 1：每日准备数据和场景配置去重

```text
阶段：1
状态：通过（收益不足，保留最小实现）
修改文件：src/programmatic_grid.py；src/programmatic_simulation.py；tests/test_programmatic_grid.py；tests/test_programmatic_simulation.py。
基线签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
优化后签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
定向测试：./venv/bin/python -m pytest tests/test_programmatic_grid.py tests/test_programmatic_simulation.py tests/test_programmatic_grid_performance.py -q -> 26 passed, 1 skipped。
真实烟雾：env RUN_PROGRAMMATIC_GRID_STAGE0_SMOKE=1 ./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py::test_stage_zero_real_smoke_matches_baseline_signature -q -> 1 passed in 16.72s。
性能：单场景 core 16.467s（基线中位数约 16.460s），未达到 10% 门槛；多场景 core-only 在当前资源限制中止，不能取得可靠多场景数字。
输出差异：无；公开 simulate_programmatic_day 仍对未排序帧稳定排序，网格内部 prepared 路径才跳过重复排序。
结论：配置标准化从日期循环外提取，日级帧在 fair 前稳定排序；实现范围小且口径回归通过。按用户已批准的单日基线继续进入阶段 2A。
```

### 阶段 2A：活动订单索引

```text
阶段：2A
状态：通过
修改文件：src/programmatic_simulation.py；tests/test_programmatic_simulation.py。
基线签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
优化后签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
定向测试：./venv/bin/python -m pytest tests/test_programmatic_simulation.py tests/test_programmatic_grid.py tests/test_programmatic_grid_performance.py -q -> 27 passed, 1 skipped。
真实烟雾：env RUN_PROGRAMMATIC_GRID_STAGE0_SMOKE=1 ./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py::test_stage_zero_real_smoke_matches_baseline_signature -q -> 1 passed in 15.00s。
性能：state_machine 约 3.172s → 1.397s（单次轻量完整详情样本）；超过 10% 门槛。
输出差异：无；cancel_requested 保留在活动索引直到 cancel_ack，买单优先和同方向订单创建顺序保持。
结论：进入阶段 2B。
```

### 阶段 2B：pending 与动作窗口队列

```text
阶段：2B
状态：跳过（收益不足，已撤回）
修改文件：无保留实现；临时改动已撤回。
定向测试：队列实现期间 28 passed, 1 skipped；撤回后 27 passed, 1 skipped。
真实烟雾：队列实现期间完整详情签名仍为 8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
性能：state_machine 约 1.397s → 1.387s，改善不足 1%，未达到 10% 门槛。
结论：不保留 heap/deque 队列抽象，进入阶段 2C。
```

### 阶段 2C：替换状态机 `iterrows()` 热循环

```text
阶段：2C
状态：通过
修改文件：src/programmatic_simulation.py；tests/test_programmatic_simulation.py。
基线签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
优化后签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
定向测试：./venv/bin/python -m pytest tests/test_programmatic_simulation.py tests/test_programmatic_grid.py tests/test_programmatic_grid_performance.py -q -> 28 passed, 1 skipped。
真实烟雾：env RUN_PROGRAMMATIC_GRID_STAGE0_SMOKE=1 ./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py::test_stage_zero_real_smoke_matches_baseline_signature -q -> 1 passed in 13.94s。
性能：state_machine 约 1.397s → 0.405s（单次轻量完整详情样本）；超过 10% 门槛。
输出差异：无；使用 itertuples(name=None) 逐行取值，临时字典仍保留状态机原有的按字段读取语义；不缓存整日 records，避免额外内存副本。
结论：保留最小实现，进入阶段 2D。
```

### 阶段 2D：单帧 mark 结果复用

```text
阶段：2D
状态：跳过（收益不足，已撤回）
修改文件：无保留实现；临时改动已撤回。
定向测试：28 passed, 1 skipped。
真实烟雾：完整详情签名仍为 8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083（1 passed in 13.87s）。
性能：state_machine 约 0.405s → 0.413s，未达到 10% 门槛。
结论：不保留同帧盯市复用，进入阶段 3。
```

### 阶段 3：日统计改为标量累计

```text
阶段：3
状态：跳过（热点不成立）
修改文件：无。
依据：2C 后单日样本 daily_stats 约 0.003s，占 core 耗时不足 0.1%；即使完全消除也不具备可测量收益。
结论：不复制 build_programmatic_summary 的统计口径，避免维护两套日统计逻辑，进入阶段 4。
```

### 阶段 4：成交详情上下文按需切片

```text
阶段：4
状态：通过
修改文件：src/programmatic_grid.py；scripts/benchmark_programmatic_grid.py；tests/test_programmatic_grid.py；tests/test_programmatic_grid_performance.py。
基线签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
优化后签名：8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
定向测试：./venv/bin/python -m pytest tests/test_programmatic_grid.py tests/test_programmatic_simulation.py tests/test_programmatic_grid_performance.py -q -> 27 passed, 1 skipped。
真实烟雾：env RUN_PROGRAMMATIC_GRID_STAGE0_SMOKE=1 ./venv/bin/python -m pytest tests/test_programmatic_grid_performance.py::test_stage_zero_real_smoke_matches_baseline_signature -q -> 1 passed in 14.75s。
性能：单笔样本 trade_context 约 0.007s，样本不足以量化多成交场景收益；实现已将每笔/每合约的全天布尔扫描改为对稳定排序时间键的 left/right 二分切片。
输出差异：无；重复边界时间键的左闭右闭保留已有专门测试。
详情模式：新增 context_mode=all/none/selected 和 context_scenarios；默认 all 保持旧行为，selected 会校验 instrument::scenario_id，none 不构造详情，HTML 会明确提示未生成复盘。
详情派生字段：last_down_ticks 已在每日 fair 准备阶段计算一次，多个场景的详情复用，不再按场景复制目标帧。
结论：保留二分切片、上下文模式和每日派生字段复用；进入阶段 5。
```

### 阶段 5：持久化每日 fair 缓存

```text
阶段：5
状态：通过
修改文件：src/programmatic_grid.py；scripts/benchmark_programmatic_grid.py；tests/test_programmatic_grid.py；tests/test_programmatic_grid_performance.py。
缓存口径：fair_cache_dir 显式开启；键绑定 schema、交易日、品种、目标与参考合约、tick、fair 时效、当日 tick 数据容器和日线 bounds 的内容指纹。缓存帧使用 pickle，JSON manifest 校验 schema/key，临时文件原子替换；读取失败、manifest 不符或字段缺失均视为 miss。
定向测试：缓存命中和字段缺失失效测试通过。
真实基准：20260302 repeat=2，首次 fair_prepare 13.104s，第二次命中 0.054s；两次 overall signature 均为 8cd70a572a0c0f74eda69f27e7fbd6943b1db1349cc3277cec9f0b6164961083。
结论：缓存命中跳过 fair 计算且结果无漂移，进入阶段 6。
```

### 阶段 6：fair 内部优化

```text
阶段：6
状态：跳过（用户决定）
依据：阶段 5 的缓存命中已将 fair_prepare 从约 13.104s 降至 0.054s；日常参数研究不再受 fair 内部计算限制。首次生成缓存的优化收益不足以优先于验收工作。
```

### 阶段 7：内置并行回放

```text
阶段：7
状态：跳过（用户决定）
替代方案：按品种拆分配置，使用多个独立命令进程；每个进程使用独立 output_dir，可共享 fair_cache_dir。这样避免在代码中引入进程调度、汇总和并发写出协调。
```
