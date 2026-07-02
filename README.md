# fat_finger_error

期货品种乌龙指日线初筛项目。

当前仓库的主目标是：给定若干期货品种编码和一个时间区间，基于历史**日线**数据，筛查哪些 `合约-交易日` 可能存在“疑似乌龙指”特征，并输出可人工查看的 HTML 报告和配套明细文件。

## 1. 这个项目现在在做什么

当前主实现不是逐笔成交复盘，也不是交易所规则复刻，而是一个**日线级初筛器**。

它试图回答的问题是：

- 某个品种在给定区间内有哪些日期值得重点复核
- 哪个具体合约在这些日期最可疑
- 它为什么可疑
- 哪些样本因为数据不足、流动性不足或对照不足而不能正式判断

当前输出的结果应理解为：

- “疑似乌龙指候选日期”
- 不是交易所最终认定结果
- 需要结合分时、逐笔、盘口、公告等信息进一步人工确认

## 2. 当前主入口

当前实际生效的主入口是：

- [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)

对应的主实现目录是：

- [src/daily_screen/](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen)

最常用的运行方式：

```bash
./venv/bin/python run_daily_screen.py --symbols AU,JD --start-date 20260101 --end-date 20260601
```

支持参数：

- `--symbols`
  - 品种编码，多个用逗号分隔，例如 `AU,JD`
- `--start-date`
  - 开始日期，格式 `YYYYMMDD`
- `--end-date`
  - 结束日期，格式 `YYYYMMDD`
- `--output-dir`
  - 可选，指定输出目录

## 3. 当前主流程

当前主流程的调用链是：

1. [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1) `main()`
2. [pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:12) `analyze_commodities(...)`
3. [data_access.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/data_access.py:36) `load_commodity_data(...)`
4. [reference_selection.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/reference_selection.py:6) `attach_references(...)`
5. [sample_filter.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/sample_filter.py:9) `assign_sample_status(...)`
6. [scoring.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/scoring.py:11) `score_candidates(...)`
7. [result_builder.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/result_builder.py:8) `build_results(...)`
8. [report_html.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/report_html.py:8) `render_report_html(...)`

可以把它理解成：

`输入品种和日期 -> 读本地 parquet -> 标记主参考 -> 判定样本状态 -> A/C/E 评分 -> 构造结果 -> 输出 HTML/CSV/JSON`

## 4. 数据从哪里来

当前数据层在：

- [data_access.py](src/daily_screen/data_access.py:1)

### 数据源

本地 parquet 数据集，目录 `data/1d_futures/`，布局 `{年份}/{YYYYMMDD}.parquet`，每个文件为某交易日全市场合约。无在线拉取，无缓存补缺口。

字段：`code`（带交易所后缀，如 `A2501.DCE`）、`date`、`pre_close`、`pre_settle`、`open`、`high`、`low`、`close`、`settle`、`vol` 等。loader 解析 `code` 得品种/合约，滤除连续合约（无月份后缀，如 `A.DCE`）。

依赖：需 `pyarrow`（见 `requirements.txt`）。

### 日期区间

`start_date` 向前扩 45 自然日（`TRADING_LOOKBACK_BUFFER_DAYS`）作为历史缓冲，供滚动分位数使用。合约上市/到期日取自 parquet 全历史（不受分析窗口截断）。

## 5. 当前评分思想

当前评分体系固定收缩为三部分：

- `A`
  - 该合约自己当天的极值异常强度
- `C`
  - 该合约相对同品种其他活跃合约的结构失真程度
- `E`
  - 可信度惩罚

总分：

```text
candidate_score = A + C + E
candidate_score = min(100, max(0, candidate_score))
```

等级：

- `< 30` -> `none`
- `30 ~ 49` -> `low`
- `50 ~ 64` -> `medium`
- `>= 65` -> `high`

注意：

- 这是“值得人工复核的候选评分”
- 不是“直接认定乌龙指的结论分”

## 6. 当前输出文件

每次运行会在一个独立输出目录下生成：

- `analysis_report.html`
  - 主报告，支持排序、过滤、点击行查看完整计算链路
- `suspicious_dates.csv`
  - 正式命中的可疑日期
- `commodity_summary.csv`
  - 品种级汇总
- `all_samples.csv`
  - 区间内全部样本，包括无效样本
- `summary.json`
  - HTML 使用的完整结构化 payload

## 7. 当前仓库里哪些文件最重要

### 主实现

- [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)
  - 命令行入口
- [src/daily_screen/pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:1)
  - 主流程编排
- [src/daily_screen/data_access.py](src/daily_screen/data_access.py:1)
  - 读本地 parquet 数据
- [src/daily_screen/sample_filter.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/sample_filter.py:1)
  - 无效样本判定
- [src/daily_screen/scoring.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/scoring.py:1)
  - `A/C/E` 评分
- [src/daily_screen/result_builder.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/result_builder.py:1)
  - 最终结果构建
- [src/daily_screen/report_html.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/report_html.py:1)
  - HTML 报告生成

### 设计文档

- [主设计稿](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/specs/2026-06-02-fat-finger-daily-screen-design.md:1)
  - 说明目标、输入输出、样本状态、HTML 报告等整体设计
- [评分设计文档](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/superpowers/specs/2026-06-02-fat-finger-scoring-design.md:1)
  - 说明 `A/C/E`、活跃合约定义、历史窗口、等级阈值

### 交接文档

- [代码梳理文档](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/daily_screen_code_handover.md:1)
  - 详细串联代码调用链、数据来源、缓存逻辑、评分与输出

## 8. 旧版文件的定位

仓库里还保留了一批旧版脚本，例如：

- [fat_finger_detector.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/fat_finger_detector.py:1)
- [fat_finger_example.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/fat_finger_example.py:1)
- [fat_finger_annual_summary.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/fat_finger_annual_summary.py:1)
- [analyze_top5_varieties.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/analyze_top5_varieties.py:1)

这些文件当前仍可作为历史参考，但它们**不是现在这套 HTML 日线初筛主链路的核心实现**。

如果只是接手当前新版，不建议先从这些旧脚本入手。

## 9. 当前能力边界

当前仓库已经具备：

- 输入品种编码和日期区间，自动发现相关合约
- 从本地 parquet 读取日线
- 基于样本状态和 `A/C/E` 评分筛查可疑日期
- 输出 HTML 报告、CSV 明细和 JSON 结构化结果
- 保留无效样本并解释“为什么这条样本不能正式判断”

当前仓库不覆盖：

- 逐笔成交级乌龙指识别
- 分时级价格跳变复盘
- 委托簿/盘口结构分析
- 交易所认定规则精确复现
- 自动化人工复核结论系统

## 10. 建议的阅读顺序

如果你要理解当前新版，建议按下面顺序阅读：

1. [run_daily_screen.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/run_daily_screen.py:1)
2. [src/daily_screen/pipeline.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/pipeline.py:1)
3. [src/daily_screen/data_access.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/data_access.py:1)
4. [src/daily_screen/sample_filter.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/sample_filter.py:1)
5. [src/daily_screen/scoring.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/scoring.py:1)
6. [src/daily_screen/result_builder.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/result_builder.py:1)
7. [src/daily_screen/report_html.py](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/src/daily_screen/report_html.py:1)
8. [docs/daily_screen_code_handover.md](/Users/zhangchunfu/Nutstore%20Files/code/python/fat_finger_error/docs/daily_screen_code_handover.md:1)
9. 两份设计文档
