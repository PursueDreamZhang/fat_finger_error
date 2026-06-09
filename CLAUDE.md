# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

期货品种乌龙指日线初筛工具。基于历史日线数据，筛查哪些合约-交易日可能存在"疑似乌龙指"特征，输出 HTML 报告和 CSV/JSON 明细。

**注意：** 这是日线级初筛器，不是逐笔成交复盘工具。输出结果需要人工结合分时、逐笔、盘口等信息进一步确认。

## 常用命令

### 运行主程序
```bash
./venv/bin/python run_daily_screen.py --symbols AU,JD --start-date 20260101 --end-date 20260601
```

参数：
- `--symbols`: 品种编码，多个用逗号分隔
- `--start-date`: 开始日期，格式 YYYYMMDD
- `--end-date`: 结束日期，格式 YYYYMMDD
- `--output-dir`: 可选，指定输出目录

### 运行测试
```bash
./venv/bin/pytest tests/
./venv/bin/pytest tests/test_scoring.py  # 单个测试文件
./venv/bin/pytest tests/test_scoring.py::test_function_name  # 单个测试
```

## 架构

### 核心流程（调用链）

```
run_daily_screen.py
  → pipeline.py: analyze_commodities()
    → data_access.py: load_commodity_data()       # 拉数据、缓存管理
    → reference_selection.py: attach_references() # 标记主参考合约
    → sample_filter.py: assign_sample_status()    # 判定样本有效性
    → scoring.py: score_candidates()              # A/C/E 评分
    → result_builder.py: build_results()          # 构建最终结果
    → report_html.py: render_report_html()        # 生成 HTML 报告
```

### 评分体系 (A/C/E)

- **A 分** (0-20): 合约自身当天的极值异常强度（range_pct、extreme_pct 历史分位数）
- **C 分** (0-60): 相对同品种其他活跃合约的结构失真程度（structure_residual、uniqueness_gap）
- **E 分** (-20~0): 可信度惩罚（样本状态、活跃合约数量、流动性）

总分 = A + C + E，裁剪到 [0, 100]

等级阈值：
- `< 30`: none
- `30-49`: low  
- `50-64`: medium
- `>= 65`: high

### 关键常量（scoring.py）

- `ACTIVE_CONTRACT_LIMIT = 5`: 每日最多取成交量前 5 的合约作为活跃合约
- `ROLLING_WINDOW = 20`: 历史分位数滚动窗口
- `ROLLING_MIN_PERIODS = 10`: 滚动窗口最少样本数

### 数据源

- **主源**: Tushare（合约发现、元数据、日线）
- **回退**: AKShare（日线补拉）

配置文件：`config/local_config.json`（tushare_token）

### 缓存策略

缓存目录：`data/csv_data/data/`

- 文件命名：`future_{contract}_{start}_{end}.csv`
- 支持多缓存文件命中和缺口区间增量补拉
- `.empty` 标记避免重复联网确认无数据区间
- 历史缓冲：向前扩 45 个自然日（`TRADING_LOOKBACK_BUFFER_DAYS`）

## 输出文件

每次运行生成独立目录（默认 `output/{timestamp}-{symbols}/`）：

- `analysis_report.html` - 主报告，支持排序、过滤、点击查看计算链路
- `suspicious_dates.csv` - 命中的可疑日期
- `commodity_summary.csv` - 品种级汇总
- `all_samples.csv` - 全部样本（含无效样本）
- `summary.json` - HTML 使用的结构化 payload

## 旧版文件

仓库保留的旧版脚本（fat_finger_detector.py、fat_finger_example.py 等）不是当前主链路的核心实现，仅作历史参考。

## 设计文档

- `docs/superpowers/specs/2026-06-02-fat-finger-daily-screen-design.md` - 整体设计
- `docs/superpowers/specs/2026-06-02-fat-finger-scoring-design.md` - 评分设计
- `docs/daily_screen_code_handover.md` - 代码梳理交接文档
