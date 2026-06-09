# 期货乌龙指日线初筛代码梳理文档

## 1. 文档目的

这份文档用于代码交接，目标是把**当前实际生效的日线初筛实现**完整串起来，回答下面几个问题：

- 用户从哪里输入参数
- 数据从哪里拉取
- 拉取失败时如何重试和降级
- 本地缓存怎么命中、补缺、回写
- 数据进入分析后经历了哪些处理
- 最终如何形成“可疑日期”和“无效样本”
- HTML、CSV、JSON 是由哪些文件和函数生成的

这份文档描述的是**当前主实现**：

- [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)
- [src/daily_screen/](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen)

不是旧版的：

- [fat_finger_detector.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/fat_finger_detector.py:1)

旧版脚本仍然保留在仓库里，但当前这套“输入品种编码 + 时间区间，输出 HTML 报告”的主链路，已经切换到 `daily_screen` 目录下的新实现。

## 2. 当前主流程总览

当前主流程的调用链如下：

1. 用户执行 [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)
2. `main()`
3. 调用 [pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:12) 里的 `analyze_commodities(...)`
4. `analyze_commodities(...)` 依次调用：
   - [data_access.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/data_access.py:36) `load_commodity_data(...)`
   - [reference_selection.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/reference_selection.py:6) `attach_references(...)`
   - [sample_filter.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/sample_filter.py:9) `assign_sample_status(...)`
   - [scoring.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/scoring.py:11) `score_candidates(...)`
   - [result_builder.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/result_builder.py:8) `build_results(...)`
   - [report_html.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/report_html.py:8) `render_report_html(...)`
5. `pipeline` 把结果写到输出目录：
   - `analysis_report.html`
   - `suspicious_dates.csv`
   - `commodity_summary.csv`
   - `all_samples.csv`
   - `summary.json`

可以把这条链路理解成：

`命令行参数 -> 数据加载 -> 主参考标注 -> 样本状态判定 -> A/C/E 评分 -> 结果组装 -> HTML/CSV/JSON 输出`

## 3. 输入入口和输出入口

### 3.1 命令行入口

文件：

- [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)

主函数：

- `main()`

接收的参数：

- `--symbols`
  - 品种编码，多个用逗号分隔，例如 `AU,JD`
- `--start-date`
  - 开始日期，格式 `YYYYMMDD`
- `--end-date`
  - 结束日期，格式 `YYYYMMDD`
- `--output-dir`
  - 可选，手工指定输出目录

示例：

```bash
./venv/bin/python run_daily_screen.py --symbols AU --start-date 20260101 --end-date 20260601 --output-dir output/au_20260101_20260601_v23
```

### 3.2 主分析入口

文件：

- [pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:12)

主函数：

- `analyze_commodities(symbols, start_date, end_date, output_dir=None, cache_dir="data/csv_data/data", config_path="config/local_config.json")`

它做三类事情：

1. 校验输入
2. 串联整条分析链路
3. 落地最终输出文件

辅助函数：

- `_normalize_symbols(...)`
  - 统一大写，去重，过滤空值
- `_validate_dates(...)`
  - 限制 `start_date <= end_date`
- `_resolve_output_dir(...)`
  - 若用户未传目录，则自动创建 `output/<timestamp>-<symbols>/`

## 4. 数据获取层：从哪里拉数据，怎么拉

### 4.1 核心文件

文件：

- [data_access.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/data_access.py:1)

这是整条链路里最复杂的一层，负责：

- 读取本地配置
- 发现合约
- 读取本地缓存
- 计算缺失区间
- 从远程增量补数
- 合并缓存并回写
- 构造 `daily_bar` 和 `contract_meta`

### 4.2 读取本地配置

函数：

- `load_local_config(config_path="config/local_config.json")`

读取的配置文件：

- [config/local_config.json](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/config/local_config.json:1)

当前只关心一个字段：

- `tushare_token`

### 4.3 为什么会向前扩窗

函数：

- `_expand_start_date(start_date)`

当前常量：

- `TRADING_LOOKBACK_BUFFER_DAYS = 45`

含义：

- 用户要分析 `start_date ~ end_date`
- 取数时不会只拉用户区间
- 而是会把开始日期向前多扩 `45` 个自然日

原因：

- 样本状态和评分都依赖历史窗口
- 当前历史窗口是“向前 20 个有效样本，至少要 10 个有效样本”
- 所以必须提前多拉一段缓冲数据，避免分析起点附近全部被打成历史不足

### 4.4 先拉元数据，再发现合约

主函数：

- `load_commodity_data(symbols, start_date, end_date, ...)`

对每个品种 `symbol`，先调用：

- `_load_symbol_meta(...)`

它的逻辑是：

1. 先读本地缓存的 `contract_meta_{symbol}.csv`
2. 如果缓存里已有覆盖目标区间的元数据，就优先用缓存
3. 否则尝试从 Tushare 拉
4. 如果 Tushare 拉不到，再退回 AKShare 补元数据

元数据相关函数：

- `_load_cached_meta(...)`
- `_write_meta_cache(...)`
- `_filter_meta_by_date(...)`
- `_fetch_symbol_meta_from_tushare(...)`
- `_fetch_symbol_meta_from_akshare(...)`
- `_standardize_meta(...)`

#### Tushare 元数据接口

函数：

- `_fetch_symbol_meta_from_tushare(...)`

内部调用：

- `pro.fut_basic(...)`

拿到的核心字段会被标准化为：

- `commodity`
- `contract`
- `listed_date`
- `last_trade_date`
- `delivery_month`
- `ts_code`

#### AKShare 元数据补位逻辑

函数：

- `_fetch_symbol_meta_from_akshare(...)`

注意：

- 这里不是直接联网去 AKShare 拉“完整历史合约列表”
- 而是退回到**本地已存在缓存文件名**，再反推出已有合约
- 如果本地完全没有这个品种的缓存，AKShare 这条元数据补位路径可能拿不到东西

### 4.5 合约发现逻辑

函数：

- `_discover_contracts_for_symbol(symbol, meta_df, cache_dir)`

合约来源有三部分：

1. 元数据表中的 `contract`
2. 本地缓存目录里 `future_<contract>_<start>_<end>.csv`
3. 本地空标记文件 `future_<contract>_<start>_<end>.empty`

也就是说，当前的“合约发现”不是只靠单一远程源，而是：

- 元数据
- 本地缓存
- 空标记

三者并集。

### 4.6 单合约日线如何加载

函数：

- `_load_contract_daily_with_cache(...)`

这是数据层的核心函数。

它的处理顺序是：

1. 找出该合约所有已有缓存文件
2. 读取这些缓存文件
3. 合并为一份缓存数据
4. 根据用户需要区间计算缺失区间
5. 对每个缺失区间判断是否已经被 `.empty` 标记覆盖
6. 若未覆盖，则远程拉取
7. 拉到后与缓存合并
8. 把合并后的全量区间写回成一个更大的缓存文件
9. 删除旧的小缓存文件
10. 最后再裁剪回当前所需分析区间

相关函数：

- `_list_contract_cache_files(...)`
- `_load_cached_contract_file(...)`
- `_merge_daily_frames(...)`
- `_calculate_missing_ranges(...)`
- `_list_contract_empty_ranges(...)`
- `_is_range_covered_by_empty_marker(...)`
- `_rewrite_contract_cache(...)`

### 4.7 缺失区间怎么补

函数：

- `_calculate_missing_ranges(required_start, required_end, covered_ranges)`

逻辑不是“缓存不完全就全量重拉”，而是：

- 先看当前已有缓存覆盖了哪些日期区间
- 只把真正缺口区间找出来
- 只对缺口远程补数

例如已经有：

- `future_AU2606_20230101_20240401.csv`
- `future_AU2606_20250101_20260401.csv`

如果这次需要：

- `20230501 ~ 20251201`

那么系统不会重拉整段，而是只补中间缺的那一段，再把旧缓存和新补的数据合并成新的大文件。

### 4.8 空标记文件的作用

函数：

- `_write_empty_marker(...)`

文件格式：

- `future_<contract>_<start>_<end>.empty`

含义：

- 某个缺失区间已经尝试联网拉过
- 但远程没有拿到数据

后续再次分析时，如果这个缺口完全被 `.empty` 覆盖，就不会重复联网拉这一段。

### 4.9 远程日线拉取顺序

函数：

- `_fetch_contract_daily_remote(...)`

执行顺序：

1. 优先 Tushare
2. Tushare 重试 `2` 次
3. 还失败则切换 AKShare

相关常量：

- `TUSHARE_RETRY_TIMES = 2`

#### Tushare 日线接口

函数：

- `_fetch_contract_daily_from_tushare(...)`

内部调用：

- `pro.fut_daily(...)`

要求：

- 本地配置里有 `tushare_token`
- 该合约有对应 `ts_code`

另外，函数内部会调用：

- `_prepare_tushare_home()`

它会把 `HOME` 指向：

- `.runtime/tushare_home`

原因：

- 避免 Tushare 在当前环境里把运行时文件写到系统 `HOME` 出问题

#### AKShare 日线接口

函数：

- `_fetch_contract_daily_from_akshare(...)`

内部调用：

- `ak.futures_zh_daily_sina(symbol=...)`

它会先尝试：

- 大写合约代码

不行再试：

- 小写合约代码

### 4.10 日线标准化

函数：

- `_standardize_daily(raw_df, contract_code)`

职责：

- 统一不同数据源字段名
- 转换数值类型
- 增加 `commodity`、`contract`
- 按日期排序
- 自动补 `pre_close`

注意这里有一条关键逻辑：

- 若原始数据没有 `pre_close`
- 则用 `close.shift(1)` 补

最终保留字段由 [schemas.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/schemas.py:1) 约束：

- `trade_date`
- `commodity`
- `contract`
- `open`
- `high`
- `low`
- `close`
- `pre_close`
- `volume`

### 4.11 数据层最终产出什么

`load_commodity_data(...)` 最终返回两张表：

1. `daily_bar`
   - 所有品种、所有合约、所有交易日的日线明细
2. `contract_meta`
   - 合约元数据

其中 `daily_bar` 已经被裁剪回：

- `扩窗后的 start_date ~ end_date`

也就是：

- 前面多取了一段缓冲
- 后面分析时依然能看到这段历史

## 5. 主参考合约标注

文件：

- [reference_selection.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/reference_selection.py:6)

函数：

- `attach_references(df)`

它不负责评分，只负责给每一条 `合约-交易日` 打上辅助解释字段。

当前保留的字段只有两个：

1. `main_reference_contract`
   - 同品种、同交易日里，按 `volume` 最大的那个合约
2. `main_reference_changed_today`
   - 相邻交易日的主参考是否发生切换

实现方式：

- 先按 `commodity, trade_date, volume desc, contract asc` 排序
- 每个 `commodity + trade_date` 取第一条，作为主参考

这一步的结果会被后续两处使用：

1. 样本状态层判断“主参考是否可用”
2. 报告页展示“主参考合约”

## 6. 样本状态层：哪些记录可以评分，哪些不可以

文件：

- [sample_filter.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/sample_filter.py:9)

函数：

- `assign_sample_status(daily_bar, contract_meta)`

这一步的核心目标不是删数据，而是给每条记录打上 `sample_status`。

### 6.1 状态判定顺序

当前顺序是：

1. 先默认全部设为 `valid`
2. 判定基础数据无效
3. 判定极低流动性
4. 判定近 20 条历史中的有效成交天数不足
5. 判定元数据不足
6. 判定生命周期边界
7. 判定历史有效样本不足
8. 判定主参考/peer 不足

### 6.2 当前主要状态

#### `valid`

- 可进入正式评分

#### `invalid_basic_data`

触发条件包括：

- OHLC 缺失
- `high < low`
- `open` 不在 `[low, high]`
- `close` 不在 `[low, high]`
- `pre_close <= 0`
- `volume < 0`

#### `invalid_low_liquidity`

触发条件包括：

- `volume == 0`
- 在已有至少 10 条历史记录可看的前提下，前 20 条历史记录中的有效成交天数 `< 10`

#### `invalid_insufficient_history`

两类情况会触发：

1. 缺少 `listed_date / last_trade_date`
2. 向前累计的历史有效样本数 `< 10`

这里要注意当前已经改成：

- **不是只看前 20 条历史记录里有几个 valid**
- 而是看**向前累计已有多少个历史有效样本**

也就是说，中间夹着很多无效日时，系统会继续回看更早的有效样本。

#### `invalid_lifecycle_edge`

触发条件：

- `listed_days < 10`
- `days_to_last_trade < 10`

#### `invalid_insufficient_peer`

触发条件：

- 没有 `main_reference_contract`
- 主参考合约自身不是 `valid`
- 同品种同日可分析样本数 `< 2`

### 6.3 这一层的输出作用

输出仍然是一张 DataFrame，但多了一列：

- `sample_status`

后续：

- 评分层只对 `valid` 样本打正式分
- 无效样本仍保留到最终结果里，用于说明“为什么这天没法算”

## 7. 评分层：A / C / E 是怎么计算的

文件：

- [scoring.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/scoring.py:11)

函数：

- `score_candidates(df)`

这一步是整套逻辑的核心。

## 7.1 先派生自身波动指标

在 `score_candidates(...)` 开头先计算：

- `range_pct = (high - low) / close`
- `extreme_pct = max(|high - close|, |low - close|) / close`

这两个量只服务于 `A`。

## 7.2 同日横截面基线怎么构造

函数：

- `_attach_same_day_peer_metrics(df)`

对每个 `commodity + trade_date`：

1. 先找出同日活跃合约集合
2. 规则是：
   - `sample_status == valid`
   - `volume > 0`
   - 按 `volume` 从大到小排序
   - 只取前 `5` 个

常量：

- `ACTIVE_CONTRACT_LIMIT = 5`

对每条记录，再把自己从这个活跃集合里排除，得到：

- `active_peer_count`

如果有 peer，则继续计算：

- `peer_high_median`
- `peer_low_median`
- `peer_volume_median`
- `peer_range_median`

再计算结构残差：

- `upper_residual`
- `lower_residual`
- `raw_structure_residual`
- `excess_structure_residual`
- `normalized_structure_residual`

最后再做一个“同日唯一性”：

- `raw_uniqueness_gap`
- `uniqueness_gap`

可以把这里理解成：

- 先算“这个合约相对同日其他活跃合约偏了多少”
- 再算“这种偏离是不是同日横截面里明显只有它最突出”

## 7.3 历史窗口如何计算

函数：

- `_attach_history_metrics(df)`

当前常量：

- `ROLLING_WINDOW = 20`
- `ROLLING_MIN_PERIODS = 10`

当前实现口径已经和文档一致：

- 历史分位数和中位数不是简单在最近 20 行上滚动
- 而是只在“符合条件的历史有效样本序列”上滚动

对应函数：

- `_rolling_quantile_on_eligible(series, eligible_mask, quantile)`
- `_rolling_median_on_eligible(series, eligible_mask)`

### A 的历史窗口

使用条件：

- `sample_status == valid`

计算：

- `range_q90 / range_q95 / range_q99`
- `extreme_q90 / extreme_q95 / extreme_q99`

### C 的历史窗口

使用条件：

- `sample_status == valid`
- `active_peer_count >= 2`

计算：

- `structure_q90 / structure_q95 / structure_q99`
- `uniqueness_q90 / uniqueness_q95 / uniqueness_q99`

### E 的辅助历史指标

计算：

- `rolling_median_volume`
- `rolling_median_active_peer_count`

再据此派生：

- `peer_comparability_weak_flag`
- `target_liquidity_weak_flag`

## 7.4 A 分

函数：

- `_score_a_components(df)`

### A1

拿 `range_pct` 去和历史分位数比较：

- 命中 `P90` -> `4`
- 命中 `P95` -> `8`
- 命中 `P99` -> `12`

### A2

拿 `extreme_pct` 去和历史分位数比较：

- 命中 `P90` -> `3`
- 命中 `P95` -> `5`
- 命中 `P99` -> `8`

最终：

- `A_score = min(20, A1 + A2)`

## 7.5 C 分

函数：

- `_score_c_components(df)`

### C1

看：

- `normalized_structure_residual`

按历史分位数打分：

- `12 / 24 / 40`

如果历史阈值 `<= 0`，会走兜底阈值：

- `0.25 / 0.50 / 1.00`

### C2

看：

- `uniqueness_gap`

按历史分位数打分：

- `5 / 10 / 20`

如果历史阈值 `<= 0`，同样走兜底阈值：

- `0.25 / 0.50 / 1.00`

最终：

- `C_score = min(60, C1 + C2)`

## 7.6 E 分

函数：

- `_score_e(df)`

`E` 不加分，只做可信度惩罚。

规则：

- `sample_status != valid` 或 `active_peer_count <= 0` -> `-20`
- `active_peer_count == 1` 或 `peer_comparability_weak_flag == True` 或 `target_liquidity_weak_flag == True` -> `-10`
- `active_peer_count == 2` 且无弱标记 -> `-5`
- 其他 -> `0`

## 7.7 无效样本统一收口

在 `score_candidates(...)` 结尾，统一再做一次：

- `invalid_mask = (sample_status != "valid") | (active_peer_count <= 0)`

对这些样本强制：

- `A_score = 0`
- `C_score = 0`
- `E_score = -20`
- `candidate_score = 0`
- `candidate_level = "none"`

### 7.8 等级映射

函数：

- `_assign_candidate_level(df)`

当前规则：

- `< 30` -> `none`
- `30 ~ 49` -> `low`
- `50 ~ 64` -> `medium`
- `>= 65` -> `high`

另外还保留一条结构约束：

- `C_score < 20` 时会压到较低等级

## 8. 结果构建层：怎样从打分表变成最终输出

文件：

- [result_builder.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/result_builder.py:8)

函数：

- `build_results(df, symbols, start_date, end_date)`

这一步做四类事：

1. 把扩窗数据裁回用户真正请求区间
2. 构造最终展示字段
3. 把结果拆成“可疑日期 / 全样本 / 品种摘要 / 详情 payload”
4. 给 HTML 提供完整明细数据

### 8.1 裁回用户请求区间

虽然数据层为了历史窗口提前多拉了缓冲数据，但在结果构建时，会重新按：

- `start_date <= trade_date <= end_date`

裁回用户请求区间。

### 8.2 生成详情主键

字段：

- `detail_id = commodity|contract|YYYY-MM-DD`

这个字段是 HTML 表格点击弹层时的主键。

### 8.3 生成触发原因摘要

函数：

- `_build_trigger_reason(row)`

会根据：

- `A_score`
- `C_score`
- `peer_comparability_weak_flag`
- `target_liquidity_weak_flag`
- `active_peer_count`
- `sample_status`

拼出中文摘要，例如：

- `振幅异常；结构失真`
- `样本状态=历史样本不足`

### 8.4 生成 peer 对照表

函数：

- `_build_peer_rows_map(df)`

对每一条记录，都会生成一份同日活跃合约对比表：

- 目标合约
- 同日 peer 合约
- volume/high/low/close
- `range_pct`
- `raw_structure_residual`
- `normalized_structure_residual`
- `candidate_score`
- `candidate_level`

这个结构会被 HTML 弹层直接消费。

### 8.5 最终拆出的几个结果对象

#### `suspicious_dates`

筛选条件：

- `candidate_level != "none"`

这是“正式可疑日期结果表”。

#### `all_samples`

包含所有请求区间内样本，无论有效无效。

#### `commodity_summary`

按品种聚合：

- 候选数量
- 可疑合约数
- 最高分

#### `report_payload`

这是 HTML 使用的完整 JSON payload，包含：

- `overview`
- `commodity_summary`
- `suspicious_dates`
- `detail_records`
- `invalid_samples`

## 9. HTML 报告层：页面是怎么生成的

文件：

- [report_html.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/report_html.py:8)

函数：

- `render_report_html(report_payload)`

### 9.1 这个函数做什么

它不是模板文件 + 前端框架模式，而是：

- 直接在 Python 里拼一整段 HTML 字符串
- 再嵌入 CSS
- 再嵌入原生 JavaScript

### 9.2 页面包含哪些区域

1. 顶部概览卡片
   - 品种
   - 时间区间
   - 候选数量
   - 无效样本数量
2. 品种摘要表
3. 可疑日期表
4. 无效样本表
5. 点击行后弹出的详情弹层

### 9.3 页面支持哪些交互

JavaScript 主要实现了：

- 品种筛选
- 关键字过滤
- 可疑日期按分数排序
- 点击可疑日期行打开详情
- 点击无效样本行打开详情
- 弹层 ESC 关闭

关键 JS 函数包括：

- `sortRowsByScore(...)`
- `applyFilters()`
- `showRecordDetail(detailId)`
- `closeRecordDetail()`

### 9.4 详情弹层展示什么

`showRecordDetail(...)` 会把一条记录完整展开为：

1. 基础数据
2. 最终结论
3. 同日基线
4. A 计算过程
5. C 计算过程
6. E 计算过程
7. 同日活跃合约对比表

它不是只展示结果分，而是从：

- 原始 OHLC
- 历史阈值
- 同日 peer 中位数
- A/C/E 分项

一路串到最后结论。

### 9.5 无效样本怎么展示

报告页里无效样本不会被删掉。

当前实现会：

- 在无效样本表中保留它们
- 把内部状态码转成中文文案
- 在弹层中明确说明“这条记录没有进入正式候选计算”

状态码到中文的映射在：

- Python：`_sample_status_label(...)`
- JavaScript：`sampleStatusLabel(...)`

## 10. 最终输出文件说明

由 [pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:34) 统一写出：

### `analysis_report.html`

- 给人工查看的主报告
- 带排序、过滤、弹层详情

### `suspicious_dates.csv`

- 只包含正式命中的可疑日期

### `commodity_summary.csv`

- 品种级摘要

### `all_samples.csv`

- 请求区间内全部样本
- 包含可疑样本、普通样本、无效样本

### `summary.json`

- HTML 的完整结构化 payload
- 也方便后续二次开发或程序消费

## 11. 一次运行最终得出的“结论”是什么

这套程序最终不会直接输出“某天一定是乌龙指”。

它输出的是三类结论：

### 11.1 可疑日期

满足：

- 样本有效
- A/C/E 综合后有候选等级

输出形式：

- `candidate_score`
- `candidate_level`
- `trigger_reasons`

这代表：

- 这一天值得人工重点复核

### 11.2 无效样本

代表：

- 不是“没异常”
- 而是“这条样本当前不能做正式判断”

常见原因：

- 基础数据不合法
- 低流动性
- 历史样本不足
- 对照不足

### 11.3 普通有效样本

代表：

- 通过了样本过滤
- 也完成了评分
- 但最终没有达到候选等级

也就是：

- 程序看过了
- 但不认为这一天值得重点人工复核

## 12. 交接时最容易混淆的几点

### 12.1 当前主链路不是 `fat_finger_detector.py`

当前主链路是：

- `run_daily_screen.py`
- `src/daily_screen/*`

旧文件还在，但不是这套 HTML 报告分析的主入口。

### 12.2 README 里还保留了较多旧版描述

当前 [README.md](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/README.md:1) 仍然较多描述旧版 `fat_finger_detector.py` 思路。

如果是交接当前新版，应优先看：

1. 这份代码梳理文档
2. [主设计稿](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/specs/2026-06-02-fat-finger-daily-screen-design.md:1)
3. [评分设计文档](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/specs/2026-06-02-fat-finger-scoring-design.md:1)
4. `src/daily_screen/` 实现

### 12.3 历史窗口口径已经更新

当前实现已经是：

- 向前找 `20` 个**有效样本**
- 至少需要 `10` 个**有效样本**

不是：

- 简单看前 `20` 条历史记录

### 12.4 本项目输出的是“候选”，不是最终认定

这套程序的定位一直是：

- 日线级初筛器

不是：

- 交易所认定系统
- 逐笔成交级复盘系统

## 13. 建议的阅读顺序

如果接手人要快速理解，建议按这个顺序读：

1. [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)
2. [pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:12)
3. [data_access.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/data_access.py:36)
4. [sample_filter.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/sample_filter.py:9)
5. [scoring.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/scoring.py:11)
6. [result_builder.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/result_builder.py:8)
7. [report_html.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/report_html.py:8)
8. 再看两份设计文档

如果接手人要快速定位某个问题：

- 数据不全/缓存命中异常：先看 `data_access.py`
- 某天为什么成了无效样本：先看 `sample_filter.py`
- 某天为什么得高分或低分：先看 `scoring.py`
- 页面表格/弹层展示不对：先看 `result_builder.py` 和 `report_html.py`
