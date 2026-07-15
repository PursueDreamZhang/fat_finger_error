# Tick 检测器性能优化实施计划（审查修订版）

## 目标与边界

目标是降低单品种及 81 品种 Tick 检测的端到端耗时，同时保持当前检测行为不变。

本计划只做“同样输入更快得到同样输出”的优化，不改变：

- fair price、noise、阈值、候选和事件合并规则；
- 参考合约选择规则；
- CSV/HTML 字段定义；
- 交易时段、开盘保护、数据断点和恢复判断；
- 当前全量有效合约检测范围。

“只检测成交量前 N 个合约”另做业务覆盖实验，不纳入本版本。

## 验收标准

每个阶段都必须同时满足以下条件，未满足则停止继续优化并回查：

1. 常规测试通过：
   `./venv/bin/python -m pytest tests/ -q -k 'not slow_'`
2. 合成数据的 enriched frame 与基线逐列一致：离散列精确一致，浮点列使用 `rtol=1e-6, atol=1e-9, equal_nan=True`。
3. 真实 JD 数据的 enriched frame、候选事件和恢复结果与基线一致。
4. 真实 AU2606 锚点若存在，加入同样的逐列比较；当前仓库未找到现成的 `test_slow_au2606_real_anchor`，不得假定它已存在。
5. 候选事件的合约、事件时间、触发原因、事件合并边界、恢复标签必须精确一致。
6. 每阶段记录独立耗时，不能只比较总耗时。

## 阶段 0：建立可复现基线

### 修改文件

- 新增 `tests/test_tick_detector_perf_golden.py`
- 新增 `tests/fixtures/golden_jd_enriched.csv`
- 新增 `tests/fixtures/golden_jd_events.csv`
- 必要时新增 `tests/fixtures/golden_au2606_events.csv`

### 工作内容

1. 用当前未优化代码运行合成数据，保存完整 enriched frame 基线。
2. 用 `data/tick2026/202605/20260520` 的 JD 数据运行全链路，保存：
   - 每个 target contract 的 enriched frame；
   - 候选事件；
   - 恢复字段。
3. 比较前先按稳定主键对齐，不能只分别排序离散列后直接比较数值列。
4. 基线测试覆盖全部会影响检测的列，而不是只比较最终 CSV 的少量字段。
5. 增加阶段计时输出：
   - CSV 加载；
   - `prepare_contract_snapshots`；
   - `_build_peer_aligned`；
   - Pass 1；
   - Pass 2；
   - candidate detection；
   - recovery；
   - HTML 输出。

### 验收

- 基线测试通过。
- 至少连续运行 3 次，记录中位数耗时。
- 记录机器、Python、NumPy、pandas 版本和数据路径。

## 阶段 1：低风险 Tick 帧优化

### 修改文件

- `src/tick_detector/tick_io.py`
- `tests/test_tick_io.py`

### 工作内容

1. 将 `_collapse_same_time_key` 改为基于连续 key 的首尾位置数组，保留：
   - 最后一行盘口和累计字段；
   - `snapshot_seq_start/end`；
   - 原有输出顺序。
2. 将 `market_time_key` 的逐行 `apply(axis=1)` 改为数组解析。
3. 将 `is_open_protected` 的逐行 `apply(axis=1)` 改为布尔数组。
4. 将 `_invalidate_blocked_diffs` 改为相邻数组比较，保持以下规则：
   - session 首条；
   - 非连续交易行；
   - gap 大于 3 秒；
   - 开盘保护；
   - 回退或零增量。

### 风险控制

- 保留 `_compute_market_time_key` 和 `_is_open_protected_row` 作为单行参考实现，便于随机样本对照。
- 增加跨午夜、重复时间键、已有序号范围列、session 边界和 gap 边界测试。

### 验收

- `tests/test_tick_io.py` 全部通过。
- 合成和 JD enriched frame 与基线一致。
- 记录 `prepare_contract_snapshots` 耗时变化。

## 阶段 2：Peer as-of 对齐优化

### 修改文件

- `src/tick_detector/reference_selection.py`
- `tests/test_tick_detector_reference_selection.py`

### 工作内容

将 `_build_peer_aligned` 中每个 peer 的逐目标行循环改为：

1. `pos = np.searchsorted(peer_keys, target_keys, side='right') - 1`；
2. 对越界位置做安全裁剪；
3. 一次性计算 age、mid、bid、ask、涨跌停和 session 有效性；
4. 通过 `np.where` 写出 `asof_mid`、`asof_spread_ticks`、`asof_key` 和 `diff`。

### 禁止改变

- `side='right'` 语义；
- 3 秒最大陈旧时间；
- 无效报价、涨跌停附近报价的过滤；
- NaN 的传播方式。

### 验收

- 增加边界测试：目标时间早于首个 peer、刚好 3 秒、超过 3 秒、相同时间键、无效报价。
- 对每个 peer 的 5 个输出数组逐元素比较基线。
- 记录 `_build_peer_aligned` 和 Pass 1 耗时。

## 阶段 3：Pass 2 噪声历史优化

### 修改文件

- `src/tick_detector/reference_selection.py`
- `tests/test_tick_detector_reference_selection.py`

### 工作内容

保留外层目标行循环，只消除窗口内的 Python 逐点循环：

1. 用 `searchsorted` 得到 `[lo, hi)`；
2. 对 `delta_volume`、`fair_price_reliable`、`fair_price` 建立一次性布尔 mask；
3. 用 NumPy 切片计算 `last_noise`、`vwap_noise`、`depth_noise`；
4. 预分配所有输出数组，最后一次性写回 DataFrame；
5. 保持当前窗口是 `[t-300s, t-10s]`，并保持当前左右边界。

### 重点风险

- 不把不规则 Tick 窗口错误地改成固定行数窗口。
- 不把“只存在一个通道”的 depth noise 错误地丢弃；当前规则是取存在的通道，两个都存在时取 max。
- 不在本阶段引入 rolling median、近似分位数或新依赖。

### 验收

- 对所有 noise、MAD、阈值、样本数、span、blocked reason 逐列比较。
- 对边界窗口、NaN、无成交、fair price 不可靠样本增加单元测试。
- 若数值一致但耗时收益小于预期，先记录结果，不直接引入复杂数据结构。

## 阶段 4：Pass 1 输出写入优化

### 修改文件

- `src/tick_detector/reference_selection.py`
- `tests/test_tick_detector_reference_selection.py`

### 工作内容

1. 预分配数值、布尔和字符串输出数组。
2. 保留 `__valid_peer_contracts` 和 `__peer_bases` 的对象数组语义。
3. Pass 1 结束后一次性写入 DataFrame。
4. 暂不改变动态 spread p95、basis median、fair median/MAD 的计算方式。

### 验收

- enriched frame 全列比较通过。
- 特别核对 `reference_blocked_reason`、peer 顺序和空字典/空列表类型。
- 单独记录 Pass 1 耗时，确认写入优化没有被误认为算法优化收益。

## 阶段 5：排序与重复数据复用

### 修改文件

- `src/tick_detector/reference_selection.py`
- 必要时 `src/tick_detector/event_detection.py`
- 必要时 `run_tick_detector.py`

### 工作内容

1. 确认 prepared frame 已稳定排序后，避免每个 target 重复 `sort_values`。
2. 优先缓存 NumPy 数组和索引，不缓存可被后续流程修改的 DataFrame 引用。
3. 恢复阶段单独验证 peer 索引和事件窗口，不假设检测阶段缓存可以直接复用。

### 验收

- 目标合约数量变化、事件为空、多个事件恢复、重复时间键场景全部通过。
- 只在 profiling 证明排序占比明显时保留缓存改动。

## 阶段 6：端到端验收

### 工作内容

1. 跑常规测试。
2. 跑合成全链路测试。
3. 跑 JD 真实数据测试。
4. 跑 AU2606 锚点测试（若补齐数据和测试）。
5. 对 20260520 先跑一个大品种，再跑一个小品种，最后跑全 81 品种。
6. 全量运行使用新的独立输出目录，不覆盖既有报告。

### 性能报告

报告至少包含：

- 优化前后总耗时；
- 各阶段耗时和占比；
- 单品种耗时分布；
- 事件数量；
- 候选合约/时间/触发原因差异；
- 恢复标签差异；
- 数值最大绝对误差和相对误差。

只有在真实数据逐条一致后，才能报告加速倍数。

## 阶段 7：主力合约范围实验（独立任务）

该阶段不属于等价优化，只有前面全部完成后才能开始。

1. 增加显式参数，例如 `--target-limit N`，默认保持当前全量行为。
2. 只限制 target，不改变 peer 选择逻辑。
3. 对 N=3、5、10 与全量运行比较：
   - 目标合约覆盖率；
   - 候选事件覆盖率；
   - 高优先级事件是否遗漏；
   - 总耗时。
4. 输出“速度收益—事件漏检风险”表后，再决定是否作为日常运行模式。

## 停止条件

出现以下任一情况时停止当前阶段，不进入下一阶段：

- 候选事件数量或事件主键变化；
- 触发原因、合并边界或恢复标签变化；
- `reference_blocked_reason`、peer 集合或阈值出现未解释差异；
- 数值误差超过验收容差；
- 性能没有改善且引入了明显复杂度；
- 只能通过放宽业务规则来获得速度收益。
