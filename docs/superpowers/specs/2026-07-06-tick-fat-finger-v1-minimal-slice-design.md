# Tick 级乌龙指交易统计 v1（单日最小回测切片）设计

> 本设计是 `2026-07-03-tick-fat-finger-trading-statistics-design.md`（下称「大 spec」）的 v1 落地切片。大 spec 描述最终研究平台；本文档只定义**第一刀**：在已知乌龙指日验证核心检测信号，并产出可审计、可复算的链路。
>
> **v2 修订**：v1 草案经一轮对抗式审查（实际打开真 CSV、比对 MD5、统计 spread 分布），修正了 1 个 FATAL 数据 bug 与若干 HIGH 级口径问题，验证日从"扫一天碰运气"改为"在已知乌龙指日 AU2606 2026-05-20 上验证"。变更清单见 §9。

## 1. 目标与验收

在**已知乌龙指日**验证 `expected_price` 能否分离"目标合约相对同品种活跃合约的孤立向下偏离"，并与简单基线对照。

**验证目标事件**：`AU2606 2026-05-20`。日线系统多版本 output 一致标注 `80.0 / high / 振幅异常；结构失真`；tick 数据 `data/tick2026/202605/20260520.zip` 含 `au2606_20260520.csv`（7.5MB）。`20260105` 作为开发期迭代日（已解压、跑得快），不入正式验收。

> v1 只检测向下偏离（大 spec §1：第一版只做"向下异常后买入回归"）。若 AU2606 当日偏离实为向上，向下检测不触发，则回退到 step-0 探测（宽 `min_deviation_bps` 扫当日其他向下事件，或换 `20260519`）。

**硬验收**：在 AU2606 2026-05-20 上核对：

1. simple / full 两版 expected_price 都给出显著 down_deviation（AU2606 砸穿、peer 不动 → expected 远高于 target_mid）。
2. full 版 age 校验**有判别力**：`full_blocked_reason` 分布显示 age 既非常处处通过也非处处挡掉。
3. 模拟成交路径时序合法（`order_live_from_snapshot < fill_snapshot_seq`），`conservative_fill` 在该事件上成交。
4. 回归标签与肉眼盘口回归一致；recovery 窗口未跨 session 断点。
5. 从 `simulated_trades.csv` 能反算该事件的成交与收益。

不以"事件数 / 推荐排序"为验收标准——单日单事件回答不了统计问题，v1 只回答"信号与链路是否正确"。

## 2. v1 边界（不做清单）

继承大 spec §13「第一版不做」全部，另加：

- 不做全量参数网格：offset `{3,8,21}`，`target_profit_ticks=5`、`timeout_seconds=60`。
- **回测口径收敛**：fill 仅 `conservative_fill`，exit 仅 `bid_exit + timeout_exit`。
- 不做品种配置表 / 金额收益：tick_size 推导，所有样本 `research_only`。
- 不做月份 / 时段 / 合约桶统计、不做 `opportunity_score` 排序与推荐门槛。
- **不处理夜盘**：所有品种按纯日盘建模，`delta_volume` 在 day-open 首条记 unknown。
- 不入库 notebook（开发期可作探索工具）。

## 3. 输入

- 数据：`data/tick2026/{YYYYMM}/{YYYYMMDD}.zip`（loader 直接读 zip；已解压目录如 `20260105/` 也兼容，便于迭代）。
- 验证日：`--date 20260520`，默认 `--symbols AU`（聚焦 AU2606 核对）。
- 开发期：`--date 20260105` 全量迭代（无需解压等待）。
- 输出：`output/tick_{date}/`。

## 4. 模块划分（薄包 4 模块 + 1 入口）

```
src/tick_stats/
  tick_io.py
  expected_price.py
  replay.py
run_tick_replay.py
```

### 4.1 tick_io.py — 读盘与派生

`load_day_snapshots(date, symbols=None) -> dict[commodity, DataFrame]`

- **读 zip 或解压目录**：glob `<CONTRACT>_<DATE>.csv`，支持从 zip 流式读取单文件，避免全量解压入内存。
- **parse_status 判定（F1 修正，大 spec §5.1 有 bug）**：**先看文件名**，含 `主力连续|当月连续|下月连续|当季连续|下季连续|隔季连续` → `continuous_alias`（直接弃，不入池）；否则用 `InstrumentID` 解析 commodity / contract_month，解析失败 → `unknown_suffix` / `unknown_format`。只保留 `parse_status == ok`。
  > 实测 `AP主力连续_*.csv` 与 `AP605_*.csv` 的 `InstrumentID` 都是 `AP605`、MD5 相同。只看 InstrumentID 会让每个主力合约数据双倍加载，merge_asof / snapshot_seq / 事件全部失真。
- 每合约按 `(timestamp, snapshot_seq)` 排序，派生：
  - `timestamp = TradingDay + UpdateTime + UpdateMillisec`、`snapshot_seq`（同合约文件内行序）。
  - `session_state` / `is_tradable_session`，按下表（v1 日盘 only，近似）：

    | 时段 | session_state | is_tradable |
    |---|---|---|
    | < 09:00 | pre_open_snapshot | false |
    | 09:00–10:15 | continuous_trading | true |
    | 10:15–10:30 | intermission | false |
    | 10:30–11:30 | continuous_trading | true |
    | 11:30–13:30 | lunch | false |
    | 13:30–15:00 | continuous_trading | true |
    | >= 15:00 | post_close_snapshot | false |

    `// ponytail: 未覆盖集合竞价细节与交易所差异，v2 再精化。`
  - `delta_volume` / `delta_turnover`：首条、`session_state` 切换后首条、**相邻快照 timestamp 差 > 60s**（长时间断点，含午休 / 夜盘边界）首条一律 `unknown`；`delta_volume < 0` 不参与成交判断。
  - `mid_price`、`spread`、`spread_ticks`；`BidPrice1<=0` 或 `AskPrice1<=0` 不参与。
  - **tick_size（M3）**：从品种当日 diagnostic_rank=1（主力）合约推导（高频最小正差值），复用到该品种全部合约；推导失败 → `tick_size_source=unknown`，该品种标 `research_only` 且不进事件检测。
  - **流动性过滤（H3）**：合约当日中位 `spread_ticks > 5` → 剔出 target 与 reference 候选池。

### 4.2 expected_price.py — 两版并排（核心）

**参考合约集合**：v1 用 `diagnostic_rank`（当日成交量 top N，N≤5，排除 target）。`// ponytail: v1 单日无 tradable_rank；v2 接入前一交易日再切。`

**peer 质量过滤（H4）**：参考合约当根快照自身 `spread_ticks <= 3` 且不在涨跌停附近（`LastPrice` 距 `Upper/LowerLimitPrice` ≥ `limit_buffer_ticks=2` ticks）；否则该 peer 不参与当根 median。

**偏离口径统一（H3）**：down_deviation 用**同口径** mid 比较，不用 mid-vs-LastPrice 混口径：

```text
down_deviation_ticks = (expected_price - target_mid) / tick_size
down_deviation_bps   = (expected_price - target_mid) / expected_price * 10000
```

并要求 `abs(LastPrice - target_mid) <= tick_size`（确保 LastPrice 未脱离盘口），否则该 tick 不算偏离候选。

**时间常数（H1 修正：跨交易所快照频率差 5–10 倍，快照计数不可比，统一用秒）**：

- `lookback_seconds = 3`（peer return 计算窗口）
- `max_reference_age_seconds = 3`（as-of 报价年龄上限）
- 均标**待标定**。

**简单基线 `expected_price_simple`**：

```text
peer_mid_asof_i(t) = 参考合约 i 在 <=t 的最近有效 mid_price   # 与 full 共用 merge_asof
peer_simple_return(t) =
    median(log(peer_mid_asof_i(t) / peer_mid_asof_i(t - lookback_seconds)))
expected_price_simple(target, t) =
    target_mid_asof(t - lookback_seconds) * exp(peer_simple_return(t))
```

与 full 共用同一套 `merge_asof` 机制；**唯一差别是不做 age 校验**。对照要回答的科学问题：age 限制在快照数据上是否真提升信号。

**完整版 `expected_price_full`**：在 simple 基础上加三类时间 age 校验——`peer_current_age`、`peer_lookback_age`、`target_baseline_age`（参考合约 / 目标合约 as-of 报价距今秒数）均 ≤ `max_reference_age_seconds=3`；任一超限 → `expected_price_full = NaN` 且记录 `full_blocked_reason ∈ {age_peer_current, age_peer_lookback, age_target_baseline, not_blocked}`。

as-of 对齐：`merge_asof` 按 timestamp 排序；同秒多条快照（郑商所 `UpdateMillisec=0`）用 `snapshot_seq` 作 tiebreaker，必要时逐快照回溯。此逻辑配独立 pytest 覆盖"同秒多条"场景（N2）。

### 4.3 replay.py — 事件检测 + 回测

`detect_events(df_with_expected) -> events_df`：

- 候选 tick 条件（大 spec §7）：`is_tradable_session`、`delta_volume>0`、`down_deviation_bps>=min_deviation_bps`（v1 全局默认 `30`，待标定）、`down_deviation_ticks>=3`、`spread_ticks<=3`、涨跌停 buffer（`limit_buffer_ticks=2`）、`reference_contract_count>=2`、`peer_move_limit_ticks<=3`。
- 事件合并：`merge_window_seconds=10`，`event_time` 取最深偏离 tick 时间，`event_depth_ticks` 取最大 down_deviation_ticks（基于 full 版）。
- 回归标签（M2）：`recovery_windows={30,60,180,300}s`，分别取窗口内最高 `BidPrice1`（quote）/ 最高 `LastPrice ∧ delta_volume>0`（trade）。**窗口在 session 断点处截断**；被截断 → `recovery_label=truncated`，不参与回归判定。

`simulate_trades(events_df, snaps) -> trades_df`（M4 收敛）：

- offset `{3,8,21}` × `target_profit_ticks=5` × `timeout_seconds=60`。
- 成交口径：仅 `conservative_fill`（`LastPrice <= order_price - tick_size ∧ delta_volume > min_fill_volume`，`min_fill_volume=1`）。
- 平仓口径：仅 `bid_exit`（`BidPrice1 >= exit_target_price`，`exit_price=exit_target_price`）+ `timeout_exit`（持仓 ≥ `timeout_seconds`，`exit_price=BidPrice1`）。
- 订单生命周期：`quote_latency_snapshots=1`、`order_latency_snapshots=0`、`reprice_interval_snapshots=2`（大 spec §8.1）。
- 触发口径：事件检测同时算 simple / full 两版 deviation；**回测以 full 版触发的事件为准**，避免回测表翻倍；trade 行内同时记 simple 版 deviation 备查。
- 每笔记录大 spec §8.4 字段，`fill_price = order_price`。

### 4.4 run_tick_replay.py — 入口

argparse：`--date`、`--symbols`、`--output-dir`。按品种分批跑完整链路再合并事件。默认输出 `output/tick_{date}/`。

## 5. 输出（2 文件）

### 5.1 `tick_event_replay.csv`（大 spec §11.1 子集 + 对照列）

```text
trade_date
commodity
contract
event_time
event_low_price
tick_size
tick_size_source
parse_status
session_state
event_volume
spread_ticks
reference_contract_count
expected_price_simple_at_event
down_deviation_simple_ticks
down_deviation_simple_bps
expected_price_full_at_event
down_deviation_full_ticks
down_deviation_full_bps
full_blocked_reason
event_depth_ticks
quote_recovery_30s_ticks
quote_recovery_60s_ticks
quote_recovery_180s_ticks
quote_recovery_300s_ticks
trade_recovery_30s_ticks
trade_recovery_60s_ticks
trade_recovery_180s_ticks
trade_recovery_300s_ticks
recovery_label
night_session_coverage
```

### 5.2 `simulated_trades.csv`（大 spec §11.2 字段，口径收敛后）

```text
trade_id
matched_event_id
trade_date
commodity
contract
offset_ticks
raw_order_price
order_price
quote_snapshot_time / quote_snapshot_seq
order_submit_snapshot
order_live_from_snapshot
decision_snapshot
fill_snapshot_seq
fill_tick_time
fill_time
fill_price
last_price_at_fill
penetration_ticks
delta_volume_at_fill
fill_model              # v1 恒为 conservative
exit_rule               # bid | timeout
exit_target_price
target_profit_ticks
timeout_seconds
exit_time
exit_price
hold_seconds
gross_profit_ticks
max_adverse_ticks
max_favorable_ticks
exit_reason
expected_price_at_fill
expected_price_at_exit
down_deviation_simple_ticks_at_fill   # 备查对照
```

`net_profit_ticks` / `cost_ticks` 因无配置表留空。后续任何胜率/收益/回撤必须能从本表复算。

## 6. 关键风险与对策

- **连续合约双倍加载**（已修，F1）：文件名优先判定 parse_status。
- **age 跨交易所不可比**（已修，H1）：统一秒制。
- **mid vs LastPrice 口径污染**（已修，H3）：同口径 mid 比较 + 流动性过滤。
- **peer 质量污染 expected**（已修，H4）：peer 自身 spread / 涨跌停过滤。
- **夜盘缺失**（已隔离，H5）：v1 日盘 only，day-open 首条 unknown。
- **recovery 跨 session**（已修，M2）：窗口截断 + `truncated` 标。
- **merge_asof 同秒 tiebreaker**：独立 pytest 覆盖（N2）。
- **验收不通过的预期反应**：若 full 在 AU2606 该事件上 age 处处挡掉（`full_blocked_reason` 全是 `age_*`），说明阈值太严，记录待标定——这是 v2 决策点，不是 bug。

## 7. 测试策略（ponytail：每模块 1 个 runnable check + pytest）

- `expected_price`：① "target 孤立下跌 + peer 同步小动" → full / simple deviation 都大；② "全品种同步下跌" → 两版 deviation 都小（验证 peer return median 的价值，同时验证 H4 peer 质量过滤不误伤同步下跌）；③ 同秒多条快照 → age 与 as-of 配对正确（N2）。
- `replay`：合成必然成交 + 必然回归的事件 → 断言 fill / exit 路径与时序合法（`order_live_from_snapshot < fill_snapshot_seq`）。
- `tick_io`：合成"真实合约 + 主力连续（同 InstrumentID）"两组 CSV → 确认主力连续被文件名剔出、target 只加载一次（F1 回归测试）。
- pytest 放 `tests/`，沿用 tmp_path 造 fixture 惯例。

## 8. 与大 spec 的差异清单

| 项 | 大 spec | v1 |
|---|---|---|
| 输出文件 | 8 | 2（事件 + 回测） |
| expected_price | 仅 full（§6） | simple + full 并排 + `full_blocked_reason` |
| 偏离口径 | expected(mid) vs LastPrice | expected(mid) vs target_mid（同口径） |
| age / lookback 单位 | 快照计数（6） | 秒（3），跨交易所可比 |
| 参考合约排序口径 | `tradable_rank`（前一交易日） | `diagnostic_rank`（当日，单日限制） |
| offset 网格 | `{1,2,3,5,8,13,21,34}` | `{3,8,21}` |
| fill 口径 | touch + conservative | 仅 conservative |
| exit 口径 | bid + last_trade + stop_loss + timeout | bid + timeout |
| 品种配置表 | 必需 | 不做，tick_size 主力推导复用 |
| 维度统计 / 推荐排序 | 全套 | 不做 |
| 连续合约识别 | InstrumentID（§5.1，有 bug） | **文件名优先**（修 bug） |
| 夜盘 | 处理 | 不处理 |
| recovery 跨 session | 未定义 | 窗口截断 + `truncated` |
| `min_deviation_bps` | 品种级配置 | 全局默认 30（待标定） |
| 验证日 | — | AU2606 2026-05-20（已知乌龙指） |

v2 扩展触发条件：v1 验收通过 + 决策出 simple / full 取舍 + 品种配置表就位 → 按大 spec 补维度统计与推荐排序。

## 9. v2 修订记录（本轮对抗式审查落地）

| 编号 | 问题 | 处理 |
|---|---|---|
| F1 | 连续合约文件 InstrumentID 与真合约相同，只看 InstrumentID 双倍加载 | 文件名优先判定 parse_status |
| F2 | 单日乌龙指千里挑一，硬验收可能 0 事件 | 验证日改 AU2606 2026-05-20（日线多版本标 high / 振幅异常） |
| H1 | snapshot_age 按快照计数，跨交易所不可比 | lookback / age 统一改秒制（3s） |
| H2 | simple / full 对照缺判别力 | 加 `full_blocked_reason` 列 |
| H3 | expected(mid) vs LastPrice 口径污染 | 同口径 mid 比较 + 中位 spread 流动性过滤 |
| H4 | peer 质量过滤缺失 | peer 自身 spread / 涨跌停 filter |
| H5 | 夜盘数据缺失未处理 | v1 日盘 only，day-open 首条 unknown |
| H6 / M1 | session 时段表 / 长断点阈值未定义 | 显式时段表 + `>60s` 断点阈值 |
| M2 | recovery 窗口跨午休抓到 13:30 开盘价 | 窗口在 session 断点截断 + `truncated` 标 |
| M3 | tick_size 在近月稀疏数据上推导污染 | 从品种主力推导，复用全品种 |
| M4 | 回测 24 行 / 事件，审计信噪比低 | 收敛到 conservative × {bid, timeout} |
| N2 | merge_asof 同秒 tiebreaker 复杂 | 独立 pytest 覆盖（不单拆模块） |

**未采纳**：N1（砍掉回测到 v1.5）——与"最小回测"诉求冲突；M5（参数跨品种不一刀切）——记为已知边界，v1 验收只用于 AU。
