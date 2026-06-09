# 期货乌龙指日线初筛器实现计划

> **废弃说明：** 本文档对应旧的实现与评分体系，已不再作为当前代码实现依据。当前第一版请统一以 [2026-06-04-daily-screen-ace-sync-implementation.md](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/plans/2026-06-04-daily-screen-ace-sync-implementation.md:1) 为准。

> **给执行型代理的要求：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐项实现本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 构建一个面向使用的日线初筛工具。用户只需要输入若干品种编码（如 `AU`、`JD`）和开始/结束日期，系统自动拉取相关合约数据，识别疑似乌龙指可疑日期，并输出 HTML 报告和辅助明细文件。

**架构：** 新实现按职责拆成几个模块：用户入口、数据访问层、参考合约构造、样本过滤、候选评分、防误判处理、结果导出、HTML 报告渲染。实现顺序以“能跑通用户主流程”为优先，先打通 `symbols + 时间区间 -> 可疑日期 -> HTML`，再补充附加统计。

**技术栈：** Python、pandas、pathlib、jinja2（或标准字符串模板）、pytest

---

## 执行约束

### 1. 数据源失败语义

- `TUSHARE_TOKEN` 必须从本地配置文件读取，不允许在代码中硬编码
- 默认先用 `Tushare`
- 若失败，重试 `2` 次
- 若仍失败，再切到 `AKShare`
- 若两个数据源都失败：
  - 不终止整次分析
  - 该合约对应日期标记为 `数据不足`
  - 继续处理其他合约和其他日期

### 2. 历史扩窗规则

- 用户输入的 `start_date` 仅作为最终展示区间起点
- 实际取数时，至少向前额外拉取 `20` 个交易日以上的缓冲数据
- 后续所有 20 日窗口都基于扩窗后的数据计算

### 3. 缺失日容忍规则

- 20 日窗口内某些日期缺失时，不应直接判整个样本失败
- 只要过去有效样本 `>= 10`，就允许继续计算
- 若有效样本 `< 10`，则该样本标记为 `数据不足`

### 4. 测试隔离规则

- 所有单元测试必须使用本地 fixture / mock
- 单元测试不允许真实联网
- 只有手工 smoke 验证允许真实走 `Tushare` / `AKShare`

### 5. 本地缓存复用规则

- 数据访问层应优先检查本地已有缓存数据是否可直接复用
- 若本地缓存不能完整覆盖目标区间，不应直接整段重拉，而应先做多文件拼接与缺口识别
- 若存在多个同合约缓存文件，必须先分析这些文件联合起来已覆盖哪些日期区间
- 在联合缓存基础上识别中间缺口、前向缺口和后向缺口
- 只有缓存联合覆盖仍不足的缺口部分，才触发真实数据源访问
- 现有缓存文件主要分布在：
  - `data/csv_data/data/`
- 旧缓存可作为第一版开发与验证的数据来源，但不能假设其一定覆盖任意品种和任意日期区间
- 缺口补齐后，必须把旧缓存数据与新增远程数据合并成一个新的全量缓存文件
- 新缓存文件的文件名必须更新为新的完整覆盖区间
- 合并成功后，应删除同合约旧缓存文件，避免后续重复命中旧文件
- 同一 `trade_date` 出现缓存数据与新拉数据冲突时，默认优先保留“本次新拉数据”
- 覆盖判断、缺口识别、有效样本统计全部按实际 `trade_date` 集合进行，而不是按自然日逐天推断
- 文件名仍使用自然日期边界，但分析语义以交易日集合为准

### 6. 元数据缓存规则

- `contract_meta` 也必须支持本地缓存
- 第一版至少按品种缓存一份元数据文件
- 若元数据缓存可用，则优先复用
- 若元数据缓存不足，再走 `Tushare -> 重试 2 次 -> AKShare`
- 元数据缓存更新后，也要进行覆盖和字段校验，避免把坏元数据写入主缓存

### 7. 输出目录策略

- 默认输出到 `output/<timestamp>-<symbols>/`
- 若用户显式传入 `--output-dir`：
  - 直接写入该目录
- 若目标目录已存在：
  - 默认报错并退出
  - 不允许静默覆盖

### 8. 状态口径统一规则

- 原始数据访问层可使用 `data_unavailable` 表示“双数据源都失败，未拿到该日期数据”
- 评分和样本过滤输出层统一使用“数据不足类状态”，例如 `invalid_insufficient_history`
- `data_unavailable` 不直接暴露给最终 HTML 报告主表
- `data_unavailable` 主要用于日志、中间表和调试排查

### 9. HTML 主输出优先

- HTML 不是附加产物，而是第一版主输出
- 在结果表构造完成时，就必须先验证最小 HTML 负载结构足够渲染页面
- 不允许等所有内部模块都完成后，才第一次打开 HTML
- HTML 顶层先展示品种摘要
- 可疑日期明细默认平铺，但必须支持按品种筛选

---

## 文件结构

### 新增文件

- `src/daily_screen/__init__.py`
  - 包入口。
- `config/local_config.json`
  - 本地运行配置文件，至少包含 `tushare_token`。
- `src/daily_screen/input_model.py`
  - 用户入口参数定义，例如 `symbols`、`start_date`、`end_date`。
- `src/daily_screen/data_access.py`
  - 根据品种编码和时间区间拉取相关合约日线与元数据。
- `src/daily_screen/schemas.py`
  - 输入输出字段定义和共享常量。
- `src/daily_screen/reference_selection.py`
  - 主参考合约与邻近参考合约选择逻辑。
- `src/daily_screen/sample_filter.py`
  - 样本校验、生命周期过滤、样本状态赋值。
- `src/daily_screen/scoring.py`
  - `A/B/C/D/E` 评分逻辑，必须以评分设计文档为唯一规则来源。
- `src/daily_screen/guards.py`
  - guard 标记和评分后处理规则，必须以评分设计文档为唯一规则来源。
- `src/daily_screen/result_builder.py`
  - 生成可疑日期明细和品种摘要结果。
- `src/daily_screen/report_html.py`
  - 生成 HTML 报告。
- `src/daily_screen/pipeline.py`
  - 从用户参数到最终输出文件的端到端编排。
- `run_daily_screen.py`
  - 本地运行入口脚本。
- `templates/report.html.j2`
  - HTML 报告模板。
- `tests/conftest.py`
  - 共享测试夹具入口。
- `tests/fixtures/`
  - 最小日线样本、合约元数据样本、主参考切换样本、系统性行情日样本等共享测试数据。
- `tests/test_input_model.py`
  - 用户输入参数测试。
- `tests/test_data_access.py`
  - 数据访问层测试。
- `tests/test_reference_selection.py`
  - 主参考和邻近参考选择测试。
- `tests/test_sample_filter.py`
  - 样本过滤测试。
- `tests/test_scoring.py`
  - `A/B/C/D/E` 评分测试。
- `tests/test_guards.py`
  - guard 规则测试。
- `tests/test_result_builder.py`
  - 可疑日期结果和品种摘要测试。
- `tests/test_report_html.py`
  - HTML 报告渲染测试。
- `tests/test_pipeline_smoke.py`
  - 端到端冒烟测试。

### 需要修改的现有文件

- `README.md`
  - 增加新的使用方式：输入品种编码与时间区间，输出 HTML 报告。

---

### 任务 1：定义用户入口和返回结果骨架

**文件：**
- 新增：`src/daily_screen/__init__.py`
- 新增：`src/daily_screen/input_model.py`
- 新增：`src/daily_screen/pipeline.py`
- 测试：`tests/test_input_model.py`
- 测试：`tests/test_pipeline_smoke.py`

- [ ] **步骤 1：先写失败的用户入口测试**

```python
from src.daily_screen.pipeline import analyze_commodities


def test_analyze_commodities_accepts_symbols_and_date_range():
    result = analyze_commodities(
        symbols=["AU", "JD"],
        start_date="20240101",
        end_date="20241231",
    )

    assert "html_report_path" in result
    assert "suspicious_dates" in result
```

- [ ] **步骤 2：运行测试，确认先失败**

运行：`pytest tests/test_input_model.py tests/test_pipeline_smoke.py -v`
预期：因为缺少 `analyze_commodities` 或模块不存在而失败

- [ ] **步骤 3：添加最小输入模型和 pipeline 桩实现**

```python
# src/daily_screen/input_model.py
from dataclasses import dataclass


@dataclass
class AnalysisRequest:
    symbols: list[str]
    start_date: str
    end_date: str
    output_dir: str | None = None
```

```python
# src/daily_screen/pipeline.py
def analyze_commodities(symbols, start_date, end_date):
    return {
        "html_report_path": "",
        "suspicious_dates": [],
    }
```

- [ ] **步骤 3.1：补上输入校验规则**

实现要求：

- `symbols` 允许来自列表或逗号字符串，但内部必须统一成大写去重后的列表
- `start_date` / `end_date` 必须校验格式合法
- `start_date` 不得晚于 `end_date`
- `output_dir` 若为空，则后续由 pipeline 自动生成默认目录

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_input_model.py tests/test_pipeline_smoke.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/__init__.py src/daily_screen/input_model.py src/daily_screen/pipeline.py tests/test_input_model.py tests/test_pipeline_smoke.py
git commit -m "feat: define daily screen user entrypoint"
```

---

### 任务 2：实现数据访问层

**文件：**
- 新增：`src/daily_screen/data_access.py`
- 新增：`src/daily_screen/schemas.py`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_data_access.py`

- [ ] **步骤 1：先写失败的数据访问测试**

```python
def test_load_commodity_data_returns_daily_bar_and_contract_meta():
    result = load_commodity_data(
        symbols=["AU"],
        start_date="20240101",
        end_date="20240201",
    )

    assert "daily_bar" in result
    assert "contract_meta" in result
```

```python
def test_load_commodity_data_keeps_only_requested_symbols():
    result = load_commodity_data(
        symbols=["JD"],
        start_date="20240101",
        end_date="20240201",
    )

    assert set(result["daily_bar"]["commodity"].unique()) <= {"JD"}
```

- [ ] **步骤 2：运行测试，确认先失败**

运行：`pytest tests/test_data_access.py -v`
预期：因为没有 `load_commodity_data` 而失败

- [ ] **步骤 3：实现数据访问层骨架**

实现要求：

- 输入 `symbols + start_date + end_date`
- 输出：
  - `daily_bar`
  - `contract_meta`
- 内部允许复用旧项目已有的数据抓取逻辑，但必须通过新模块包装
- 第一版先统一标准化字段，不在数据访问层做评分逻辑
- 先固定“品种编码 -> 合约集合”的识别规则，再写拉数逻辑
- 优先从 `config/local_config.json` 读取 `tushare_token`
- 不允许在新实现中硬编码 token
- 在访问真实数据源之前，先检查本地缓存是否可复用
- 数据源优先级固定为：默认 `Tushare`，失败重试 `2` 次，仍失败再切到 `AKShare`
- 合约发现优先使用 `Tushare fut_basic`
- 日线拉取与元数据补全默认走 `Tushare`
- 只有在 `Tushare` 连续失败后，才允许回退到 `AKShare`
- 若 `Tushare` 与 `AKShare` 都失败，则该合约对应日期的数据标记为 `数据不足`，继续处理其他合约和其他日期，不中断整次分析
- 后续评分和过滤若因为数据不足无法计算，也统一标记为 `数据不足`，不抛出整批失败
- 所有需要 20 日历史窗口的规则，都必须容忍区间内个别日期缺失：跳过缺失日，只要过去有效样本 `>= 10` 即继续计算
- 正式分析区间开始前，数据访问层至少向前额外拉取 `20` 个交易日以上的缓冲数据

- [ ] **步骤 3.1：补上合约集合识别规则**

实现要求：

- 对每个 `symbol`，先列出分析区间附近可能出现的候选合约
- 过滤掉在 `[start_date, end_date]` 内无日线记录的合约
- 过滤掉在分析区间内完全无成交或几乎无成交的合约
- 输出至少两个内部结果：
  - `commodity_contracts_map`
  - `contract_availability`
- 优先通过 `Tushare fut_basic` 获取该品种合约列表与元数据
- 若 `Tushare` 调用失败，则按“重试 2 次 -> 切到 AKShare”的规则执行

这一步必须独立实现，不能在后续参考构造阶段临时拼接。

- [ ] **步骤 3.2：补上单合约拉数和补元数据规则**

实现要求：

- `load_contract_daily(contract_code, start_date, end_date)` 默认先走 `Tushare`
- 但在访问 `Tushare` 前，必须先检查本地缓存中该合约的所有相关缓存文件
- 若存在多个缓存文件，则先拼接这些缓存文件的已覆盖数据，再判断缺口
- 若 `Tushare` 失败，重试 `2` 次
- 若仍失败，再切换到 `AKShare`
- `load_contract_meta_for_contracts(contracts)` 默认先走 `Tushare`
- 若 `Tushare` 失败，重试 `2` 次
- 若仍失败，再由 `AKShare` 或本地规则补足可获得字段
- 所有成功返回的数据都必须标准化成统一字段，不允许把数据源差异泄漏到后续评分层
- 若两个数据源都失败，则为该合约该日期保留占位记录，并显式标记 `data_unavailable`
- 这种 `data_unavailable` 记录在后续样本过滤阶段应归入 `invalid_insufficient_history` 或同类“数据不足”状态，而不是静默丢弃
- 取数时对每个品种的实际请求起始日，要在用户 `start_date` 基础上向前扩展至少 `20` 个交易日，用于支撑首段样本的历史窗口计算
- 缓存策略必须支持以下流程：
  - 找出该合约所有缓存文件
  - 读取并标准化这些缓存文件
  - 按 `trade_date` 合并、排序、去重
  - 若同一 `trade_date` 出现缓存数据与新拉数据冲突，默认保留“本次新拉数据”
  - 判断合并后的缓存联合覆盖区间与真实所需区间之间还缺哪些日期段
  - 只对缺失日期段发起远程拉取
  - 将旧缓存数据与新拉回数据再次合并、排序、去重
  - 写出新的全量缓存文件，例如从多份旧缓存合并成 `future_AU2408_20230101_20260401.csv`
  - 新全量缓存成功写出后，必须先校验：
    - 新文件可读
    - 关键字段齐全
    - 覆盖区间不小于旧缓存并包含新增缺口区间
    - 行数不小于去重后的历史并集
  - 只有在上述校验通过后，才允许删除同合约旧缓存文件

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_data_access.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/data_access.py src/daily_screen/schemas.py src/daily_screen/pipeline.py tests/test_data_access.py
git commit -m "feat: add data access layer for commodity inputs"
```

---

### 任务 3：实现参考合约构造

**文件：**
- 新增：`src/daily_screen/reference_selection.py`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_reference_selection.py`

- [ ] **步骤 1：先写失败的参考构造测试**

```python
def test_selects_main_reference_by_highest_volume(reference_input_df):
    enriched = attach_references(reference_input_df)

    assert "main_reference_contract" in enriched.columns
```

```python
def test_marks_reference_change_day(reference_switch_input_df):
    enriched = attach_references(reference_switch_input_df)

    assert "main_reference_changed_today" in enriched.columns
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_reference_selection.py -v`
预期：失败

- [ ] **步骤 3：实现主参考和邻近参考构造**

实现要求：

- 主参考合约固定为当日同品种成交量最大的活跃合约
- 生成 `main_reference_contract`
- 生成 `main_reference_changed_today`
- 生成有效的邻近参考合约字段
- 显式定义邻近参考缺失时的退化行为

退化要求：

- 若有效邻近参考为 0 个：
  - 邻近参考字段置空
  - 后续 `C3` 直接记 `0`
  - 样本候选等级不得进入最高等级
- 若有效邻近参考只有 1 个：
  - 允许继续评分
  - 在结果中显式标记 `weak_peer_reference_flag`

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_reference_selection.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/reference_selection.py tests/test_reference_selection.py src/daily_screen/pipeline.py
git commit -m "feat: add reference construction"
```

---

### 任务 4：实现样本过滤和样本状态

**文件：**
- 新增：`src/daily_screen/sample_filter.py`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_sample_filter.py`

- [ ] **步骤 1：先写失败的样本过滤测试**

```python
def test_marks_invalid_basic_data_when_ohlc_relationship_is_broken(make_daily_bar, contract_meta_df):
    daily_bar = make_daily_bar(high=10, low=12)
    filtered = assign_sample_status(daily_bar, contract_meta_df)

    assert filtered.loc[0, "sample_status"] == "invalid_basic_data"
```

```python
def test_marks_invalid_low_liquidity_when_volume_is_zero(make_daily_bar, contract_meta_df):
    daily_bar = make_daily_bar(volume=0)
    filtered = assign_sample_status(daily_bar, contract_meta_df)

    assert filtered.loc[0, "sample_status"] == "invalid_low_liquidity"
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_sample_filter.py -v`
预期：失败

- [ ] **步骤 3：实现样本过滤**

实现要求：

- 输出 `sample_status`
- 支持基础数据质量剔除
- 支持极低流动性剔除
- 支持生命周期边界剔除
- 支持历史样本不足剔除
- 支持主参考不可用剔除

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_sample_filter.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/sample_filter.py tests/test_sample_filter.py src/daily_screen/pipeline.py
git commit -m "feat: add sample filtering"
```

---

### 任务 5：实现 A/B/C/D/E 候选评分

**文件：**
- 新增：`src/daily_screen/scoring.py`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_scoring.py`

- [ ] **步骤 1：先写失败的评分测试**

```python
def test_scoring_outputs_component_columns(valid_scoring_input_df):
    scored = score_candidates(valid_scoring_input_df)

    assert {"A_score", "B_score", "C_score", "D_score", "E_score", "candidate_score"} <= set(scored.columns)
```

```python
def test_a_plus_b_is_capped_at_25(scoring_cap_input_df):
    scored = score_candidates(scoring_cap_input_df)

    assert scored["candidate_score"].iloc[0] >= 0
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_scoring.py -v`
预期：失败

- [ ] **步骤 3：实现评分逻辑**

实现要求：

- 评分实现必须以以下文档为唯一规则来源：
  - [2026-06-02-fat-finger-scoring-design.md](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/specs/2026-06-02-fat-finger-scoring-design.md:1)
- 若代码行为与评分设计文档冲突，应优先修正文档或代码，不允许在实现计划里再维护第三套规则
- 所有历史窗口使用过去 `20` 个有效交易日
- 不包含当天
- 输出 `A/B/C/D/E` 及总分
- `A + B` 合计最多 `25`
- 主参考合约自身异常必须是正式支持场景，不能靠等级层补丁绕过

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_scoring.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/scoring.py tests/test_scoring.py src/daily_screen/pipeline.py
git commit -m "feat: implement suspicious day scoring"
```

---

### 任务 6：实现 guard 规则

**文件：**
- 新增：`src/daily_screen/guards.py`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_guards.py`

- [ ] **步骤 1：先写失败的 guard 测试**

```python
def test_systemic_market_day_blocks_top_level_candidate(guard_input_df):
    guarded = apply_guards(guard_input_df)

    assert guarded.loc[0, "candidate_level"] != "top"
```

```python
def test_gap_like_reversion_reduces_b_score(guard_input_df):
    guarded = apply_guards(guard_input_df)

    assert guarded.loc[0, "B_score"] <= guard_input_df.loc[0, "B_score"]
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_guards.py -v`
预期：失败

- [ ] **步骤 3：实现 guard 处理**

实现要求：

- guard 实现必须以以下文档为唯一规则来源：
  - [2026-06-02-fat-finger-scoring-design.md](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/specs/2026-06-02-fat-finger-scoring-design.md:1)
- guard 只负责拦截、降级、少量分数修正
- 不允许通过 guard 层直接“补提分”来修正评分系统缺陷
- 触发关键 guard 时，不允许进入最高等级候选
- 输出 `candidate_level`

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_guards.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/guards.py tests/test_guards.py src/daily_screen/pipeline.py
git commit -m "feat: add suspicious day guard rules"
```

---

### 任务 7：生成可疑日期结果和品种摘要

**文件：**
- 新增：`src/daily_screen/result_builder.py`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_result_builder.py`

- [ ] **步骤 1：先写失败的结果测试**

```python
def test_build_results_outputs_suspicious_dates_table(guarded_candidates_df):
    result = build_results(guarded_candidates_df)

    assert "suspicious_dates" in result
```

```python
def test_build_results_outputs_commodity_summary(guarded_candidates_df):
    result = build_results(guarded_candidates_df)

    assert "commodity_summary" in result
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_result_builder.py -v`
预期：失败

- [ ] **步骤 3：实现结果构造**

实现要求：

- 输出可疑日期明细表
- 输出品种摘要表
- 优先围绕人工查看设计字段
- 保证关键字段至少包括：
  - `commodity`
  - `contract`
  - `trade_date`
  - `candidate_score`
  - `candidate_level`
  - `trigger_reasons`
  - `main_reference_contract`

- [ ] **步骤 3.1：先产出最小 HTML 负载结构**

实现要求：

- 在真正渲染 HTML 之前，先定义 `report_payload`
- 至少包含：
  - `overview`
  - `commodity_summary`
  - `suspicious_dates`
  - `detail_records`

后续 HTML 模板直接围绕该负载结构渲染，避免最后再回头改 pipeline。

- [ ] **步骤 3.2：增加最小 HTML 预览测试**

实现要求：

- 在 `result_builder` 阶段就构造一个最小 HTML 预览输入
- 验证 `report_payload` 已经足够支持：
  - 总览区
  - 品种摘要区
  - 可疑日期明细区
- 若关键字段不足，应在这里修正结果表结构，而不是等到正式 HTML 渲染阶段再回改

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_result_builder.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/result_builder.py tests/test_result_builder.py src/daily_screen/pipeline.py
git commit -m "feat: add suspicious date result tables"
```

---

### 任务 8：生成 HTML 报告

**文件：**
- 新增：`src/daily_screen/report_html.py`
- 新增：`templates/report.html.j2`
- 修改：`src/daily_screen/pipeline.py`
- 测试：`tests/test_report_html.py`

- [ ] **步骤 1：先写失败的 HTML 测试**

```python
def test_render_report_returns_html_text(report_input_payload):
    html = render_report_html(report_input_payload)

    assert "<html" in html.lower()
```

```python
def test_render_report_contains_suspicious_dates_table(report_input_payload):
    html = render_report_html(report_input_payload)

    assert "可疑日期" in html
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_report_html.py -v`
预期：失败

- [ ] **步骤 3：实现 HTML 报告**

实现要求：

- HTML 报告至少包含：
  - 总览区
  - 品种摘要表
  - 可疑日期明细表
  - 单条详情展示区
- 第一版优先保证可读、可筛选、可排序
- 交互可先用轻量前端脚本或简单表格行为实现
- 先做一个最小可打开骨架页，再逐步补齐交互和样式

- [ ] **步骤 4：运行测试**

运行：`pytest tests/test_report_html.py -v`
预期：通过

- [ ] **步骤 5：提交**

```bash
git add src/daily_screen/report_html.py templates/report.html.j2 tests/test_report_html.py src/daily_screen/pipeline.py
git commit -m "feat: add html report output"
```

---

### 任务 9：打通端到端主流程并导出文件

**文件：**
- 修改：`src/daily_screen/pipeline.py`
- 新增：`run_daily_screen.py`
- 测试：`tests/test_pipeline_smoke.py`

- [ ] **步骤 1：先写失败的端到端冒烟测试**

```python
def test_pipeline_returns_html_path_and_tables():
    result = analyze_commodities(
        symbols=["AU", "JD"],
        start_date="20240101",
        end_date="20241231",
    )

    assert "html_report_path" in result
    assert "suspicious_dates_path" in result
    assert "commodity_summary_path" in result
```

- [ ] **步骤 2：运行测试**

运行：`pytest tests/test_pipeline_smoke.py -v`
预期：失败

- [ ] **步骤 3：实现最终编排**

实现要求：

- 接收 `symbols + start_date + end_date`
- 调用数据访问层
- 构造参考合约
- 样本过滤
- 候选评分
- 应用 guard
- 构造结果表
- 渲染 HTML
- 写出：
  - `analysis_report.html`
  - `suspicious_dates.csv`
  - `summary.json` 或 `commodity_summary.csv`
- 固定单次运行输出目录结构

- [ ] **步骤 3.1：固定输出目录结构**

实现要求：

- 默认输出到 `output/<timestamp>-<symbols>/`
- 目录内至少包含：
  - `analysis_report.html`
  - `suspicious_dates.csv`
  - `summary.json`
- pipeline 返回结果中必须包含这些文件的绝对或可解析路径

- [ ] **步骤 4：添加本地脚本入口**

脚本要求：

- 支持最基本的命令行参数
- 至少支持：
  - `--symbols AU,JD`
  - `--start-date 20240101`
  - `--end-date 20241231`
  - `--output-dir ...`
- 对非法日期格式、空 symbols、重复 symbols 给出明确报错
- 对已存在的输出目录按“默认报错，不覆盖”处理

- [ ] **步骤 5：运行测试**

运行：`pytest tests/test_pipeline_smoke.py -v`
预期：通过

- [ ] **步骤 6：提交**

```bash
git add src/daily_screen/pipeline.py run_daily_screen.py tests/test_pipeline_smoke.py
git commit -m "feat: wire end-to-end suspicious date analysis flow"
```

---

### 任务 10：补充附加统计和文档

**文件：**
- 修改：`README.md`
- 可选新增：`src/daily_screen/yearly_summary.py`
- 测试：`tests/test_pipeline_smoke.py`

- [ ] **步骤 1：更新 README**

至少补充：

- 新的使用方式
- 输入参数示例
- 输出文件说明
- 第一版只做日线可疑日期初筛

- [ ] **步骤 2：如果成本可控，再实现附加统计模块**

附加统计只作为辅助结果，可包括：

- 品种级候选数量
- 合约级候选数量
- 区间内最高分日期

注意：

- 不要让附加统计反过来主导主流程

- [ ] **步骤 3：运行完整定向测试集**

运行：`pytest tests/test_input_model.py tests/test_data_access.py tests/test_reference_selection.py tests/test_sample_filter.py tests/test_scoring.py tests/test_guards.py tests/test_result_builder.py tests/test_report_html.py tests/test_pipeline_smoke.py -v`
预期：全部通过

- [ ] **步骤 4：手工运行一次主流程**

运行示例：

```bash
python run_daily_screen.py --symbols AU,JD --start-date 20240101 --end-date 20241231 --output-dir tmp/report_au_jd
```

预期：

- 正常生成 HTML 报告
- 正常生成可疑日期明细文件
- 路径输出清晰

- [ ] **步骤 5：提交**

```bash
git add README.md run_daily_screen.py src/daily_screen templates tests
git commit -m "docs: finalize suspicious date html workflow"
```
