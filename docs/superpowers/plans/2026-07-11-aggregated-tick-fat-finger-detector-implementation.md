# 聚合 Tick 乌龙指候选检测器 实施计划

> 上游设计：[2026-07-10 聚合 Tick 乌龙指候选检测方案](../specs/2026-07-10-aggregated-tick-fat-finger-detector-design.md)
>
> 范围：只落地 `AU_V1` 离线候选检测、中文 CSV/HTML 复盘和数据质量诊断。不做回测、收益、推荐、向上检测；未标定品种不输出正式候选。
>
> 本计划取代旧 `2026-07-07-tick-fat-finger-light-detector-implementation.md` 中与触发口径、恢复口径、输出文件名冲突的实施步骤。

## 0. 实施边界

直接改造已有链路，不建立第二套检测器：

```text
run_tick_detector.py
  -> tick_io.py                 # 元数据、交易日时间键、同时间键合并、差分
  -> reference_selection.py     # 参考合约、fair_price、noise history
  -> event_detection.py         # 双成交通道、1 秒确认、onset、合并、回归
  -> report_html.py             # 中文 CSV/HTML 输出和诊断
```

旧检测器中的 `expected_price_simple/full`、move 派字段、`strong_visible_last_drop`、统一恢复比例、英文 CSV 表头和 `tick_events.csv` 不再进入正式候选链路。

保留目录/zip 读取、连续合约文件名过滤、`--commodity` / `--contract` 只过滤目标而不裁掉参考合约加载的行为。

## 1. 文件范围

| 文件 | 改动 |
|---|---|
| `src/tick_detector/tick_io.py` | AU 元数据、交易日时间键、同时间键合并、session、差分、区间均价、质量标记 |
| `src/tick_detector/reference_selection.py` | 参考合约、有效报价、价差基线、`fair_price`、noise history |
| `src/tick_detector/event_detection.py` | 双通道、1 秒确认、onset、合并、冻结 basis 回归 |
| `src/tick_detector/report_html.py` | 中文事件摘要、目标检测明细、参考合约原始表、诊断 |
| `run_tick_detector.py` | 链路编排、AU-only、中文 CSV、同窗口 replay payload |
| `tests/test_tick_io.py` | 时间轴、合并、差分、profile |
| `tests/test_tick_detector_reference_selection.py` | fair price、基线、noise、参考质量 |
| `tests/test_tick_detector_event_detection.py` | 触发、计数器确认、onset、合并、回归 |
| `tests/test_run_tick_detector.py` | 中文输出、AU-only、诊断、HTML 端到端 |

`tests/test_report_html.py` 覆盖的是日线 `src/daily_screen/report_html.py`，不属于本 tick renderer 的改动范围；tick HTML 继续由 `tests/test_tick_detector_event_detection.py` 与 `tests/test_run_tick_detector.py` 覆盖。

不新增配置中心、数据库、任务队列、前端构建或第三方依赖。AU profile 先放在 `tick_io.py` 的最小静态表中。

## 2. Task 1：元数据、交易日时间轴与同时间键合并

**文件：** `src/tick_detector/tick_io.py`、`tests/test_tick_io.py`

- [ ] 先写失败测试：
  - AU profile 为 `tick_size=0.02`、`contract_multiplier=1000`、`parameter_profile=AU_V1`、`validation_status=validated`。
  - 非 AU 标记 `unvalidated_commodity`，保留诊断但不能成为正式候选。
  - 夜盘 `21:xx`、午夜 `00:xx`、日盘按交易日周期排序。
  - `21:xx -> 00:xx` 跨午夜的 asof、300 秒基线、3 秒 onset、1 秒确认、10 秒合并、恢复和 HTML 切片都使用同一个时间键。
  - 同一 `market_time_key` 多行先合并，最后一行提供盘口/累计值，保留 `snapshot_seq_start/end`。
  - 差分只发生在不同时间键之间；session 首条、回退、无成交行不可触发。
  - AU 的 `21:00`、`09:00`、`10:30`、`13:30` 四段开盘后 `0–59s` 被保护，`60s` 后允许；每段首条不差分。
  - `MAX_DATA_GAP_SECONDS=3` 时连续；`gap == 3s` 允许，`gap > 3s` 视为数据断点。事件合并、onset 连续性、回归截断和 session 内差分都复用它。
  - `MAX_CONFIRMATION_GAP_SECONDS=1` 仅用于 1 秒均价确认窗，是比数据断点更严格的采样完整性条件。
  - 跨午夜事件输出仍保留原始 `交易日` 与 `显示时间=UpdateTime.UpdateMillisec`，不输出内部 cycle 数值或伪造自然日。
  - `AveragePrice` 仅校验单位，不能被当作计数器同步证据。

- [ ] 实现：
  - 在 `tick_io.py` 定义 AU profile 与 session profile。
  - 生成内部 `(trade_date, cycle_millis)`，对外保留原始时间字段。
  - 先按合约合并同时间键，再计算差分和 `interval_vwap`。
  - 明确 `market_time_key` 是 tick 链路唯一内部时间契约：Task 2–5 的排序、asof、窗口、间隔、断点、合并、恢复、replay 切片全部只使用它；自然日 `timestamp` 仅生成展示文本。
  - 对每个合并后时间键保留 `display_trade_date`、`display_time`；事件锚点/开始/结束/区间确认结束的 CSV 与 HTML 时间均从这些展示字段生成。
  - 在 `tick_io.py` 定义 `MAX_DATA_GAP_SECONDS=3` 与 `MAX_CONFIRMATION_GAP_SECONDS=1`，禁止在后续模块重写魔法数字。
  - 保留已有目录/zip loader、连续合约过滤和源文件字段。
  - 写入 `session_state`、`is_tradable_session`、`data_quality_flags`、`validation_status`。

- [ ] 验证：

```bash
./venv/bin/python -m pytest tests/test_tick_io.py -q
```

建议提交：

```bash
git add src/tick_detector/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick-detector): normalize trading clock and collapse same-time snapshots"
```

## 3. Task 2：参考合约、fair price 与 noise history

**文件：** `src/tick_detector/reference_selection.py`、`tests/test_tick_detector_reference_selection.py`

- [ ] 先写失败测试：
  - 离线阶段按当日成交量选同品种前 5 个真实参考合约。
  - peer 需要 `age <= 3s`、有效盘口、`spread <= p95 + 1 tick`、不贴近涨跌停。
  - 目标和 peer 的基线 mid 任一无效，不进 `basis_i`。
  - `[t-300s, t-10s]` 生成 `basis_i`、`fair_i`、`fair_price` 与不确定性。
  - 参考不足 2 个或不确定性超限时 `fair_price_reliable=False`。
  - 每个历史点 s 使用自己的前置窗口生成 `last_noise`、`vwap_noise`、`execution_depth_noise`。
  - 样本少于 100 或跨度少于 120 秒时标 `insufficient_noise_history`。
  - AU 锚点前得到 354 个 noise 样本、阈值约 24.88 tick、`fair_price≈995.33`。
  - 锚点后 peer 集合变化、重新计算基线时，冻结的 basis 不得变化。

- [ ] 实现：
  - 用 `attach_fair_price_metrics(...)` 或等价函数替换旧 `attach_reference_metrics(...)` 的正式口径。
  - 删除旧 `expected_price_simple/full` 与 move 派字段的候选职责。
  - 在 enriched target frame 写入 `fair_price`、`fair_uncertainty_ticks`、`valid_peer_count`、`peer_contracts`、noise 样本与阈值字段。
  - 同时写入仅供内部事件链使用的 `__valid_peer_contracts` 和 `__peer_bases`（合约到 `basis_i` 的映射）；事件合并时原样复制锚点值。它们不落入中文 CSV。
  - 不预建缓存系统；先保证 AU 单日可读、可测，性能问题另行度量。

- [ ] 验证：

```bash
./venv/bin/python -m pytest tests/test_tick_detector_reference_selection.py -q
```

建议提交：

```bash
git add src/tick_detector/reference_selection.py tests/test_tick_detector_reference_selection.py
git commit -m "feat(tick-detector): derive peer fair price and noise history"
```

## 4. Task 3：双成交通道、1 秒确认与 onset

**文件：** `src/tick_detector/event_detection.py`、`tests/test_tick_detector_event_detection.py`

- [ ] 先写失败测试：
  - `visible_execution_drop`：末笔相对 `fair_price` 超过动态阈值时命中。
  - `interval_execution_drop`：区间均价超过阈值且完整 1 秒合并均价仍异常时命中。
  - 可见分支可独立触发；未确认区间均价不能独立触发。
  - “单帧低均价、下一帧反向补偿、1 秒合并正常”标 `counter_lag_suspect` 且不触发。
  - `counter_sync_unconfirmed`、跨 session、累计字段回退时隐藏均价分支不触发。
  - 确认窗末端没有覆盖到 `t+1s`、内部相邻时间键 gap 超过 `1s` 时为不完整。
  - onset 用过去 3 秒深度；数据中断时不可把 `pre_depth` 当 0。
  - 开盘后 60 秒、noise 不足、peer 不可靠、非 AU 模式均不得触发。
  - AU 锚点命中两个原因；`11:08:09.000` 反例不命中。

- [ ] 实现：
  - 以 `fair_price` 替换 `last_vs_mid_down_ticks`、`peer_excess_down_ticks`、`snapshot_avg_trade_gap_ticks`。
  - 计算 `last_down_ticks`、`vwap_down_ticks`、`combined_vwap_1s`、`combined_vwap_down_ticks`、`candidate_execution_depth`、`onset_ticks`。
  - 在 `event_detection.py` 放一个共享的 1 秒合并 helper：输入起点和同 session 的 `market_time_key` 帧，输出 `window_complete`、`confirmation_end_key`、正增量合计、`combined_vwap`、阻断原因；Task 4 恢复阶段复用它，不能另写近似计算。
  - 完整性要求：窗口覆盖到 `t+1s`，中间相邻时间键 gap 不超过 `MAX_CONFIRMATION_GAP_SECONDS=1`，所有累计增量有效且未跨 session；仅累计有效正增量。
  - 写入 `interval_confirmation_end_time`；可见分支不等待未来数据。
  - 删除旧 `strong_visible_last_drop` 和固定成交量门槛。

- [ ] 验证：

```bash
./venv/bin/python -m pytest tests/test_tick_detector_event_detection.py -q
```

建议提交：

```bash
git add src/tick_detector/event_detection.py tests/test_tick_detector_event_detection.py
git commit -m "feat(tick-detector): detect fair-price execution anomalies"
```

## 5. Task 4：事件合并与冻结 basis 的同通道回归

**文件：** `src/tick_detector/event_detection.py`、`tests/test_tick_detector_event_detection.py`

- [ ] 先写失败测试：
  - 同合约候选在 10 秒内合并；跨 session 或数据断点不得合并。
  - 锚点取 `candidate_execution_depth` 最大者；事件成交量是开始至结束所有正增量之和。
  - 两个候选之间插入一个未命中但 `delta_volume>0` 的快照，事件成交量仍包含该行。
  - `gap == MAX_DATA_GAP_SECONDS` 可合并、`gap > MAX_DATA_GAP_SECONDS` 不可合并；后者进入恢复观察时标 `truncated`。
  - 恢复阶段冻结 `basis_i(anchor)` 与参考合约集合，不能重新把事件放进 baseline。
  - 可见、区间均价、买一通道分别记录首次恢复确认秒数。
  - 区间均价恢复秒数取完整 1 秒确认窗结束，而不是窗口起点。
  - AU 锚点输出 `visible_recovered_seconds=0.5`、`interval_recovered_seconds=1.5`、`recovery_label=trade_recovered_3s`。
  - session 断点、数据断点或参考价失效时输出 `truncated`。
  - 恢复阶段单帧均价正常、但共享 1 秒合并窗口仍异常时，区间通道不能标恢复。
  - 多候选合并时按原子原因去重，固定输出顺序为 `visible_execution_drop,interval_execution_drop`。
  - 锚点后的 noise/阈值发生变化时，恢复仍严格使用锚点的两类阈值；恢复函数不得从未来帧重新估计阈值。

- [ ] 实现：
  - 将合并函数改为 `merge_candidates(candidates, enriched_target_frame, max_data_gap_seconds=MAX_DATA_GAP_SECONDS)`；候选只决定事件边界与锚点，事件成交量从完整 enriched target frame 的 `[event_start_key, event_end_key]` 内所有有效正 `delta_volume` 回填。
  - 合并时把锚点的 `last_threshold_ticks`、`vwap_threshold_ticks`、`trigger_reasons`、`__valid_peer_contracts`、`__peer_bases`、`fair_price` 与必要展示字段原样复制到 event；中文 CSV 映射时排除双下划线内部字段。
  - 将恢复函数契约固定为 `attach_recovery_metrics(events, enriched_target_frame, reference_frames, profile)`：enriched target frame 提供锚点阈值、目标成交和共享 1 秒 helper 输入，reference frames 只提供冻结 peer 的 future asof 报价；函数不得重新估计 noise、阈值或 basis。
  - 重写恢复逻辑，移除 `recovery_denominator_ticks` 与统一 ratio，并复用 Task 3 的 1 秒 helper。
  - 每个触发通道回到自己的锚点阈值内才算成交恢复；报价只作为 `quote_only_*` 证据。
  - 合并 `trigger_reasons` 时按逗号拆分、去重并按可见后区间的固定顺序组合。

- [ ] 验证：

```bash
./venv/bin/python -m pytest tests/test_tick_detector_event_detection.py -q
```

建议提交：

```bash
git add src/tick_detector/event_detection.py tests/test_tick_detector_event_detection.py
git commit -m "feat(tick-detector): merge events and confirm channel recovery"
```

## 6. Task 5：中文 CSV、同窗口 HTML 与诊断

**文件：** `run_tick_detector.py`、`src/tick_detector/report_html.py`、`tests/test_run_tick_detector.py`、`tests/test_tick_detector_event_detection.py`

- [ ] 先写失败测试：
  - 输出 `tick_candidate_events.csv` 与 `event_replay.html`；零候选 CSV 仍含中文表头。
  - CSV 表头与设计文档一致，不出现内部英文键。
  - HTML 窗口严格为 `[锚点前 60 秒, 锚点后 10 秒]`，不跨 session。
  - 目标检测明细、事件摘要和诊断表均为中文表头。
  - 每个实际参与 fair price 的参考合约有同窗口原始快照表；不得出现合理价、偏离、阈值、noise、回归等计算列。
  - 空候选 HTML 可区分无事件、元数据阻断、参考不足、noise 不足与计数器异常。
  - JD 或 AP 小样本端到端运行时，CSV 仅有中文表头、HTML 显示“未标定品种”诊断，且没有候选详情。
  - 跨午夜 fixture 的 CSV/HTML `交易日`、`事件时间` 来自原始展示字段，不含 `cycle_millis` 或错误自然日。

- [ ] 实现：
  - 内部继续用英文 DataFrame 列；在 `run_detection(...)` 落盘前集中映射中文 CSV 表头。
  - 替换输出文件名、返回字段和空 DataFrame 列定义。
  - `contract_diagnostics` 是明确运行期契约：每个已加载检测目标无论是否产生候选，都追加一条 `validation_status` 与各阻断计数；`_append_contract_events(...)` 返回事件和诊断，`run_detection(...)` 汇总后交给 renderer。
  - `render_event_replay_html(events_df, replay_payload, contract_diagnostics)` 必须在 `events_df.empty` 时先渲染中文“合约运行诊断”表，再显示无候选提示；不得沿用旧的空事件立即返回分支。
  - replay payload 分为目标检测窗口、每个锚点 `__valid_peer_contracts` 的原始窗口、合约诊断统计；不得用日成交量 top-5 代替锚点实际 peer 集合。
  - 重写旧“参考合约对照”表：目标显示检测字段，参考只显示原始字段。
  - 保持静态 HTML，不引入前端构建或图表依赖。
  - 同步替换 `tests/test_run_tick_detector.py` 中旧 `tick_events.csv`、英文列、`recovery_denominator_ticks` 的断言。

- [ ] 验证：

```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py tests/test_tick_detector_event_detection.py -q
```

建议提交：

```bash
git add run_tick_detector.py src/tick_detector/report_html.py tests/test_run_tick_detector.py tests/test_tick_detector_event_detection.py
git commit -m "feat(tick-detector): render Chinese candidate outputs"
```

## 7. Task 6：全链路真数据验收

**先跑全部测试：**

```bash
./venv/bin/python -m pytest tests/ -q
```

**再跑 AU 真数据：**

```bash
./venv/bin/python run_tick_detector.py \
  --tick-day-path data/tick2026/202605/20260520 \
  --commodity AU \
  --contract AU2606 \
  --output-dir /tmp/tick-detector-au-20260520
```

**验收：**

- [ ] 生成 `/tmp/tick-detector-au-20260520/tick_candidate_events.csv` 与 `event_replay.html`。
- [ ] CSV 表头全为设计文档定义的中文字段。
- [ ] 存在 `交易日=20260520`、`合约=AU2606`、`事件时间=21:04:35.500` 的候选。
- [ ] 候选包含两个触发原因，`合理价≈995.33`、`区间成交均价≈940.52`、`回归标签=trade_recovered_3s`。
- [ ] `11:08:09.000` 不在候选中。
- [ ] HTML 显示目标前 60 秒后 10 秒的数据；参考合约只展示同窗口原始行情和中文表头。
- [ ] 非 AU 输入只有 `unvalidated_commodity` 诊断，不输出正式候选。

**旧契约清理检查：**

```bash
rg -n "tick_events\.csv|recovery_denominator_ticks|expected_price_simple|strong_visible_last_drop" \
  run_tick_detector.py src/tick_detector tests/test_tick_io.py \
  tests/test_tick_detector_reference_selection.py tests/test_tick_detector_event_detection.py \
  tests/test_run_tick_detector.py
```

预期：没有旧正式输出/触发契约残留；历史设计文档不在本检查范围内。

建议提交：

```bash
git add docs/superpowers/specs/2026-07-10-aggregated-tick-fat-finger-detector-design.md docs/superpowers/plans/2026-07-11-aggregated-tick-fat-finger-detector-implementation.md
git commit -m "docs(tick-detector): add aggregated tick implementation plan"
```

## 8. 完成定义

检测器完成不等于回测完成。只有以下条件同时满足才结束本阶段：

1. 合成测试覆盖同秒合并、计数器错位、noise 不足、双通道触发、冻结 basis 回归与中文输出。
2. 全部现有测试通过。
3. AU2606 锚点命中且 `11:08:09` 反例不命中。
4. CSV/HTML 让人工能看到中文事件证据、目标检测明细、参考合约原始同窗口快照和阻断诊断。

之后再单独设计回测；回测必须读取候选事件的确认可用时间，不能把区间均价未来 1 秒确认信息当成事件发生时已知。
