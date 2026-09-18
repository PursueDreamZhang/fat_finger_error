# Mid + LastPrice 异常检测

该模块针对原始 tick 快照，使用新增成交时的 `LastPrice` 与一档盘口中价
`(BidPrice1 + AskPrice1) / 2` 的偏离筛查疑似孤立异常成交。它不使用
`Turnover`、`AveragePrice` 或 `interval_vwap`，因此适合检查这些累计字段不可靠的品种。

```bash
./venv/bin/python run_mid_analyzer.py \
  --input data/tick2026 \
  --symbols SA,FG,PX \
  --start-date 20260301 \
  --end-date 20260331 \
  --output-dir output/mid-march
```

输入可以是一个 CSV、ZIP、单日目录或包含月份/日期目录的数据根目录。目录扫描会按
“直接 CSV → 日 ZIP → 其他 ZIP”选择同一合约交易日的唯一来源，并在
`data_quality.csv` 与 `run_manifest.json` 中记录重复来源。

输出包括：

- `analysis_report.html`：本地可打开的汇总、筛选、事件窗口图和原始行表；
- `threshold_summary.csv`、`threshold_summary_by_day.csv`：阈值/方向/Raw-Strict 统计；
- `event_details.csv`：候选事件、锚点、恢复、MFE/MAE；
- `deviation_distribution.csv`：合约日与合约全区间偏离分布；
- `data_quality.csv`、`run_manifest.json`：字段有效性、断点、回退和来源信息。

`Strict` 要求向下事件的 LastPrice 低于买一，向上事件的 LastPrice 高于卖一；
`Raw` 只要求偏离达到阈值。恢复率的缺失观察不计入恢复率分母，60 秒窗口不足的事件
不进入完整窗口 MFE/MAE 汇总。

