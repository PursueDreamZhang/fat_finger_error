# 本地 parquet 数据源替换设计

## 背景

当前数据接入层 `src/daily_screen/data_access.py`(约 1081 行)以 Tushare 为主源、AKShare 为回退,运行时在线发现合约、按缺口区间增量补拉日线、用 `.empty` 标记避免重复联网。

用户已获得一份完整的本地日线 parquet 数据集,覆盖 2010–2026 全市场合约。本次改造将数据源**完全替换**为这份本地数据,删除全部在线拉取与缓存补缺逻辑。

## 数据源现状

路径布局:`data/1d_futures/{年份}/{YYYYMMDD}.parquet`,共 4000 个文件,年份目录 2010–2026。每个文件 = 一个交易日的全市场合约。

字段:`code, date, pre_close, pre_settle, open, high, low, close, settle, change1, change2, vol, amount, oi, oi_chg`。

`code` 形态(已通过抽样验证):

- 带交易所简写后缀,后缀取值:`ZCE / DCE / SHF / INE / CFX / GFE`。
- 具体月合约:`{品种}{4位月份}.{交易所}`,如 `A2501.DCE`、`SR2501.ZCE`、`AG2512.SHF`。
- 月份位数在所有年份(含 2010 年起的老 CZCE)均已被规整为 **4 位**,无 3 位历史码。
- 连续/指数合约:`A.DCE`、`AG.SHF`、`SR.ZCE`(字母后无数字),需滤除。
- 极少量 code 末尾只有 1 位数字(疑似指数变体),同样被具体合约过滤规则排除。

## 目标

1. `load_commodity_data` 改为只从本地 parquet 读取,产出与现有 schema 完全一致的 `{daily_bar, contract_meta}`。
2. 删除在线拉取、缓存补缺、合约集合预生成等全部相关代码与入口。
3. 项目 venv 自包含安装 pyarrow。
4. 测试用本地 parquet fixture 覆盖。

## 非目标

- 不改造评分、样本过滤、参考合约、报告等下游模块。
- 不保留 Tushare/AKShare 作为兜底(用户已确认完全替换)。
- 不清理仓库内既有的旧版独立脚本(`cache_manager.py`、`fat_finger_detector.py` 等)。
- 不删除 `config/local_config.json`(tushare_token 成为死配置但文件保留,避免误伤)。

## 接口

```python
def load_commodity_data(
    symbols: list[str],
    start_date: str,            # YYYYMMDD
    end_date: str,              # YYYYMMDD
    *,
    data_dir: str | Path = "data/1d_futures",
) -> dict[str, pd.DataFrame]:
    ...
```

返回:
- `daily_bar`:列为 `REQUIRED_DAILY_COLUMNS = [trade_date, commodity, contract, open, high, low, close, pre_close, volume]`,行落在 `[expanded_start_date, end_date]` 区间(`expanded_start_date = start_date - 45 自然日`,沿用 `TRADING_LOOKBACK_BUFFER_DAYS`,供滚动分位数使用)。
- `contract_meta`:列为 `REQUIRED_META_COLUMNS = [commodity, contract, listed_date, last_trade_date, delivery_month]`。`listed_date`/`last_trade_date` **必须取该合约在 parquet 全历史内的真实首/末日**(见下文「数据流」Pass A),不能用分析窗口内的 min/max——否则下游 `sample_filter.py` 的 `invalid_lifecycle_edge`(窗口前 10 天 / 到期前 10 天)会被窗口边界系统性误伤。`delivery_month` 由合约代码末尾数字提取。

`pre_close` 容错链(必须保留,否则静默回归):`pre_close ?? pre_settle ?? 前一日 close`,其中"前一日 close" **必须按合约分组**取:`df.groupby(["commodity", "contract"])["close"].shift(1)`(按 `trade_date` 升序)。严禁全表排序后直接 `shift(1)`——那会把上一合约最后一行的 close 串到下一合约首行,产生跨合约污染。parquet 实测 `pre_close` 基本都有值,此链为防御性兜底。注意:旧实现的 `settle→pre_close` 映射语义偏弱(`settle` 是当日结算,非前日锚点),新链改用 `pre_settle`(前结算)更正确,故不再纳入 `settle`。

签名变化与贯穿:
- `load_commodity_data` 删除 `config_path`、`cache_dir` 形参,新增 `data_dir`。
- `analyze_commodities(..., data_dir: str | Path = "data/1d_futures")` 新增同名形参并转发给 loader,作为测试注入点(CLI 不变,不新增 `--data-dir` 标志)。
- 现有 `cache_dir`/`config_path` 形参从 `analyze_commodities` 与 `load_commodity_data` 一并移除。

## 数据流

规整 symbols(大写、去重、非空校验),校验 start ≤ end。`expanded_start_date = start_date - 45 天`。`data_dir` 下的全部年份目录记为 `available_years`(从目录名解析整数年份);其中 `<= end.year` 的用于 Pass A 与 Pass B 的历史侧,`> end.year` 的仅 Pass A 需要(用于拿真实 `last_trade_date`)。

**文件名约定(两个 Pass 共享)**:`{year}/` 下被处理的文件必须形如 `YYYYMMDD.parquet`。命中 `*.parquet` 但文件名不符合该格式者**硬报错**(不静默跳过——无声丢数据比 fail-fast 危险);非 `.parquet` 条目(如 `.DS_Store`)由 glob 自然忽略,不报错。

**Pass A —— 全历史 meta 边界(只读 `[code, date]` 两列)**:扫描 `available_years` **全部年份**(不只是到 `end.year`——否则真实 `last_trade_date` 会被截断到 `end.year`,窗口右缘样本会被 `sample_filter.py` 误判 `invalid_lifecycle_edge`),对每个 `code`(去后缀后形如 `[A-Z]+\d{3,4}`)记录全局 `min(trade_date)`/`max(trade_date)`。这一步保证 `listed_date`/`last_trade_date` 是合约在数据集内的真实首/末日,不受分析窗口截断影响。只读两列,parquet 列式存储下代价低。`# ponytail: 全历史扫描换正确性,若变慢可缓存 contract→(first,last) 索引文件`

**Pass B —— 窗口 daily_bar(读 OHLC 必要列)**:仅读文件名日期落在 `[expanded_start_date, end_date]` 的 parquet(年份范围 `[expanded_start.year, end.year]`),取列 `code, date, pre_close, pre_settle, open, high, low, close, vol`。

**合并与规整**:
1. 解析 `code`:去后缀得 contract,正则 `^[A-Z]+` 提取 commodity;仅保留 `commodity ∈ symbols` 且 contract 形如 `[A-Z]+\d{3,4}`(实测 4 位,保留 3-4 位做廉价保险)的行。连续合约(`A.DCE`/`AG.SHF`/`SR.ZCE`,字母后无数字)自动滤除。
2. 列映射与类型:`date → trade_date(to_datetime)`,`vol → volume`,`open/high/low/close/pre_close/pre_settle → to_numeric`。
3. `pre_close` 容错:`pre_close ?? pre_settle ?? df.groupby(["commodity","contract"])["close"].shift(1)`(按合约分组,杜绝跨合约污染)。
4. 丢行规则:`trade_date` 解析失败、或 `open/high/low/close` 任一缺失的行丢弃(沿用现有 `_standardize_daily` 行为)。
5. 按 `[commodity, contract, trade_date]` 去重(保留最后一条)。
6. `daily_bar` 裁剪到 `[expanded_start_date, end_date]`。

**组装 contract_meta**:对在 daily_bar 中出现的每个 `(commodity, contract)`,从 Pass A 的全历史边界取 `listed_date`/`last_trade_date`;`delivery_month` 用 `re.search(r"(\d{3,4})$", contract)` 提取。按 `(commodity, contract)` 去重。

## 保留与复用

复用现有辅助(签名/语义不变,从旧文件保留):
- `_expand_start_date(start_date)` 与常量 `TRADING_LOOKBACK_BUFFER_DAYS`。
- 常量 `REQUIRED_DAILY_COLUMNS`、`REQUIRED_META_COLUMNS`(来自 `schemas.py`)。

commodity 提取与 `delivery_month` 提取均**内联为向量化 `str.extract`**(Pass A/B 处理百万级行,标量 `_extract_commodity` / `_build_contract_meta_from_code` 的逐行 regex 性能不足,且无其他调用方)。故不再保留这两个标量函数。

## 删除清单

`src/daily_screen/data_access.py` 内删除:
- 在线拉取:`_fetch_contract_daily_from_tushare`、`_fetch_contract_daily_from_akshare`、`_fetch_contract_daily_remote`、`_fetch_symbol_meta_from_tushare`、`_fetch_symbol_meta_from_akshare`、`_fetch_all_symbol_meta_from_tushare`、`_fetch_exchange_contract_snapshot`、`_collect_contracts_from_dce_realtime`、`_collect_contracts_from_exchange_snapshots` 及其辅助 `_build_snapshot_dates`、`_extract_symbol_contracts_from_snapshot`、`_standardize_exchange_snapshot`。
- 合约集合预生成:`build_contract_sets`、`_load_contract_set`、`_write_contract_set`、`_collect_contracts_from_local_sources`、`_collect_contracts_from_cache_files`、`_collect_contracts_from_output_samples`、`_collect_contracts_from_fallback_source`、`_build_fallback_contract_candidates`、`_discover_contracts_for_symbol`、`_discover_contracts_from_fallback_source`、`_is_specific_contract_code`。
- 缓存补缺:`_load_contract_daily_with_cache`、`_list_contract_cache_files`、`_list_contract_empty_ranges`、`_load_cached_contract_file`、`_get_cache_file_date_bounds`、`_calculate_missing_ranges`、`_is_range_covered_by_empty_marker`、`_write_empty_marker`、`_rewrite_contract_cache`、`_merge_daily_frames`、`_lookup_meta_row`。
- 元信息缓存:`_load_symbol_meta`、`_load_cached_meta`、`_write_meta_cache`、`_filter_meta_by_date`、`_standardize_meta`、`_fill_meta_dates_from_daily`、`_drop_meta_without_dates_or_daily_data`、`_format_optional_date`、`_standardize_daily`(列映射逻辑并入新 loader)、`_build_contract_meta_from_code`(`delivery_month` 提取逻辑内联)。
- 配置与常量:`LocalConfig`、`CacheFileInfo`、`load_local_config`、`EXCHANGE_BY_SYMBOL`、`DCE_REALTIME_NAME_BY_SYMBOL`、`CACHE_FILE_PATTERN`、`EMPTY_FILE_PATTERN`、`META_FILE_PATTERN`、`CONTRACT_SET_FILE_PATTERN`、`TUSHARE_RETRY_TIMES`、`TUSHARE_HOME_DIR`、`_prepare_tushare_home`。
- 未提交改动:`build_contract_sets` 相关新增内容(本次重写覆盖,不再需要——parquet 本身已含全部合约)。

仓库根删除:
- `build_contract_sets.py`(入口脚本,失去作用)。

## 依赖

- 项目 venv 安装 `pyarrow`(读取 parquet 必需)。若存在 `requirements*.txt` 则同步登记。

## 测试

重写 `tests/test_data_access.py`:在 `tmp_path` 下构造最小 parquet 数据集(跨年份、跨交易所、含连续合约、含区间外日期、含目标品种与非目标品种、含窗口前历史),断言:
- 仅返回目标品种、具体月合约(连续合约被滤除)。
- 日期区间正确(`expanded_start` 含 45 天缓冲,`end_date` 不超)。
- 列与类型符合 schema。
- **`contract_meta` 全历史边界(一对对称断言,缺一不可)**:
  - 左边:构造一个比 `expanded_start_date` 更早、含目标合约的文件,断言该合约 `listed_date` 落在窗口外(早于 `expanded_start_date`)。
  - 右边:构造一个 `end.year` 之后(未来年份)、含目标合约的文件,断言该合约 `last_trade_date` 取自该未来年份的真实末日(晚于 `end_date`)。这条专门防止实现退化成"Pass A 只扫到 `end.year`"而测试仍绿。
- **`pre_close` 容错链**:构造 `pre_close` 缺失但 `pre_settle` 有值的行,断言被 `pre_settle` 回填;再构造两者皆缺,断言被前一日 `close` 回填,不产生 `pre_close<=0` 的无效样本。
- 跨年份目录正确合并。

更新 `tests/test_pipeline_smoke.py`:通过 `analyze_commodities(..., data_dir=tmp_fixture)` 注入本地 parquet fixture,跑通全链路,验证不依赖仓库真实数据、不联网。

## 迁移检查清单

「代码换了、认知没换」是假完成。以下必须一起收口:

1. **依赖(BLOCKER,最先做)**:用 `./venv/bin/python -m pip install pyarrow` 安装(注意:`./venv/bin/pip` 的 shebang 指向已失效的 Nutstore 旧路径,直接调 pip 会报错,必须走 `python -m pip`)。仓库当前无 `requirements.txt`/`pyproject.toml`,新建 `requirements.txt` 至少登记 `pyarrow`(并补 `pandas`、`numpy` 等现有依赖,否则依赖管理继续裸奔)。未装 pyarrow 则 parquet 一行都读不了,此步是其余所有步骤的前提。
2. **失败提示(按场景拆开,各自钉死最小信息;不要让 pyarrow 底层异常裸抛)**:
   - `data_dir` 不存在 → 异常消息至少含 `data_dir` 路径(此场景无品种/日期上下文)。
   - 年份目录名非法(非纯数字) → 异常消息至少含该目录名。
   - `{year}/` 下存在 `.parquet` 但文件名非 `YYYYMMDD.parquet` → 硬报错(不静默跳过),异常消息含 `file_path` 与所属年份目录。
   - 单个 parquet 读取失败(文件名合法但内容读不出) → 异常消息至少含 `file_path`、`pass_name`(A/B)、从文件名解析到的 `trade_date`;若已进入 Pass B 的 symbol 过滤阶段,额外附带当前 `symbols`。Pass A 全市场扫描阶段无单一品种上下文,不要硬塞。
   - 实现不得为迎合一句笼统描述而做出含糊异常:不同失败场景走不同分支、带不同字段。
3. **文档同步**(全部描述旧「Tushare/AKShare/csv_data 缓存/补缺口」模型):
   - `README.md`:§4.2 数据源、§4.3 缓存策略、§流程简介(第 66 行)、§输出/附录相关行。
   - `CLAUDE.md`:「数据源」「缓存策略」两节、调用链注释。
   - `AGENTS.md`:与 CLAUDE.md 同步(CLAUDE.md 的副本)。
   - `docs/daily_screen_code_handover.md`:第 88 行 `analyze_commodities` 旧签名(`cache_dir`/`config_path`)、数据来源/缓存逻辑描述。
4. **测试替换**:`tests/test_data_access.py`、`tests/test_pipeline_smoke.py`(连同其当前未提交的改动)整体重写为本地 parquet 模型。

## 风险与对策

- **Pass A 全历史扫描代价**:为拿真实上市/到期日(含分析窗口之后的真实 `last_trade_date`),Pass A 扫描 `data_dir` 下全部年份(仅 `[code, date]` 两列)。对策:列裁剪 + parquet 列式读取已足够快;若数据量增长变慢,落一个 `contract_date_bounds.json` 索引文件缓存(`build_contract_sets.py` 可改造为生成它),Pass A 直接读索引。
- **大日期区间内存**:Pass B 按文件名日期预过滤 + 仅读必要列;若仍偏大,按年份分批规整后再 concat(本次先做最简实现)。
- **未覆盖到的合约/日期**:用户已确认完全替换,接受 parquet 未覆盖则不出现(不回退远程)。
- **签名变更影响面**:`load_commodity_data` 与 `analyze_commodities` 删除 `cache_dir`/`config_path`、新增 `data_dir`,需同步 pipeline 与全部调用方/测试;通过重写测试 + 文档同步覆盖。
- **venv pip 损坏**:`./venv/bin/pip` shebang 指向失效的 Nutstore 旧路径,装依赖必须走 `./venv/bin/python -m pip`;此为环境既存问题,迁移清单已计入。
