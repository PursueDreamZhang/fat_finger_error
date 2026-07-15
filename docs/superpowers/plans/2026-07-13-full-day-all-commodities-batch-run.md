# 全天全品种批量运行 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让检测器支持全天 87 个品种批量运行（按 10 个品种一批分批执行），产出按品种拆分的 HTML 报告和合并 CSV。

**Architecture:** 三步改造——(1) 用一次性推导工具从真实数据生成 87 品种参数表，暂停并由用户人工审核；(2) 审核通过后将参数落为静态配置，`run_detection` 增加 `--commodities` 多品种参数，HTML 按品种拆分输出、replay payload 按品种即时释放；(3) 提供 9 批分批运行脚本。

**Tech Stack:** Python 3.13, pandas, numpy, pytest。无新依赖。

## 全局约束

- 设计文档 §4.1 原则：tick_size、contract_multiplier 优先用权威配置，自动推导只做 sanity check。
- 郑商所品种（AP/CF/CJ/FG/MA/OI 等 21 个）的 AveragePrice 是价格单位（元/吨），推导 multiplier≈1，数学上与 `interval_vwap = ΔTurnover/ΔVolume/multiplier` 公式自洽——multiplier=1 时得到的就是正确价格单位均价。
- `_average_price_matches_multiplier` 的 `|Turnover/Volume - AveragePrice| <= 0.5` 校验是已有保护，参数配错时 `avg_trade_price_enabled=False`、`interval_vwap` 自动失效，不会静默产出错误候选。
- `--commodities`（复数）是新参数；`--commodity`（单数，旧）保留向后兼容。
- HTML 按品种拆分：每个品种一个 `event_replay_{品种}.html`；CSV 保持合并。
- Task 1 完成后必须暂停，等待用户人工审核；未经用户明确确认，不得执行 Task 2，也不得把自动推导结果标记为 `validated`。
- 本次 `validated` 的含义是“已通过本次人工审核，可进入全品种实验测试”，不等同于已完成跨交易日或权威来源生产验证。
- 每个 profile 必须记录 `parameter_source`（如 `AUTO_INFERRED_20260520`、`AUTHORITATIVE_MANUAL:交易所规则表` 或 `OTHER_DAY_MANUAL:20260521`）和审核记录；缺失品种不能只补数值而不留来源。
- 审核记录字段固定为 `review_status`、`reviewed_by`、`reviewed_at`、`review_note`；Task 1 输出 `review_status="pending"`，用户确认后才改为 `approved`。

---

## 0. 文件范围

| 文件 | 改动 |
|---|---|
| `scripts/derive_commodity_profiles.py`（新建） | 一次性工具：从指定日期数据推导 87 品种 (tick_size, multiplier) 表 |
| `tests/test_derive_commodity_profiles.py`（新建） | 推导函数单元测试与模块命令 smoke test |
| `src/tick_detector/tick_io.py` | `COMMODITY_PROFILES` 扩展到 87 品种 |
| `run_tick_detector.py` | `--commodities` 多品种参数、HTML 按品种拆分、replay payload 按品种即时释放 |
| `tests/test_tick_io.py` | 补充多品种 profile 断言 |
| `tests/test_run_tick_detector.py` | 多品种参数、HTML 拆分断言 |
| `scripts/run_all_commodities_batched.sh`（新建） | 9 批分批运行脚本 |

不新增配置中心、数据库或第三方依赖。不改 `attach_fair_price_metrics` 算法（接受长运行时间）。

---

## 1. Task 1：品种参数推导工具

**文件：** `scripts/derive_commodity_profiles.py`（新建）

**目的：** 从指定日期的 tick 数据推导全部品种的 tick_size 和 contract_multiplier，输出为可直接粘贴的 Python 字典。产物人工审核后落为 Task 2 的静态配置。

**推导方法（以 20260520 数据生成候选参数，待人工审核）：**

- **multiplier**：先仅读取各合约的 `Volume` 选出当日成交量最大的代表合约，再按该代表文件的原始快照顺序计算 `median(ΔTurnover / ΔVolume / LastPrice)`，取最接近的整数。避免为选代表合约重复运行检测链的全量逐行归一化；参数仅用于人工审核。郑商所品种会得到 ≈1（因 AveragePrice 是价格单位），这是正确的——公式自洽。
- **tick_size**：对代表合约的所有 `BidPrice1`、`AskPrice1`、`LastPrice` 正值排序后取相邻差值，再求差值 GCD（`math.gcd` 需先把价格放大到整数）；这不是任意两两差值，人工审核按该实际算法检查。

- [ ] **Step 1：创建推导工具脚本**

```python
# scripts/derive_commodity_profiles.py
"""一次性工具：从 tick 数据推导 87 品种的 tick_size 和 contract_multiplier。

用法：
    ./venv/bin/python -m scripts.derive_commodity_profiles \
        --tick-day-path data/tick2026/202605/20260520

输出可直接粘贴到 tick_io.py COMMODITY_PROFILES 的 Python 字典。
"""
from __future__ import annotations

import argparse
import math
import re
from functools import reduce
from pathlib import Path

import numpy as np
import pandas as pd

from src.tick_detector.tick_io import (
    iter_day_contract_files,
    load_contract_snapshots,
)


def derive_profiles(tick_day_path: str) -> dict[str, dict[str, object]]:
    """推导全部品种的 tick_size 和 contract_multiplier。"""
    # 按品种分组文件
    grouped: dict[str, list] = {}
    for cf in iter_day_contract_files(tick_day_path):
        code = _commodity_from_file_name(cf.file_name)
        if code is None:
            continue
        grouped.setdefault(code, []).append(cf)

    source_date_match = re.search(r"(20\d{6,8})", str(tick_day_path))
    source_date = source_date_match.group(1) if source_date_match else "UNKNOWN_DATE"
    profiles: dict[str, dict[str, object]] = {}
    for commodity in sorted(grouped):
        files = grouped[commodity]
        # 先仅读取 Volume，选成交量最大的合约作为代表
        best_vol = -1.0
        best_file = None
        for cf in files:
            with cf.open_handle() as handle:
                vol = float(pd.read_csv(handle, usecols=["Volume"])["Volume"].max())
            if vol > best_vol:
                best_vol = vol
                best_file = cf
        if best_file is None:
            continue
        raw = load_contract_snapshots(best_file)
        if raw.empty or raw["parse_status"].iloc[0] != "ok":
            continue
        raw["delta_volume"] = raw["Volume"].diff()
        raw["delta_turnover"] = raw["Turnover"].diff()
        tick_size = _derive_tick_size(raw)
        multiplier = _derive_multiplier(raw)
        if tick_size is not None and multiplier is not None:
            profiles[commodity] = {
                "tick_size": tick_size,
                "contract_multiplier": multiplier,
                "parameter_profile": "AUTO_INFERRED_V1",
                "validation_status": "pending_manual_review",
                "parameter_source": f"AUTO_INFERRED_{source_date}",
                "review_status": "pending",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_note": None,
            }
            print(f"  {commodity}: tick_size={tick_size}, multiplier={multiplier}")
    expected = set(
        "A AD AG AL AO AP AU B BB BC BR BU BZ C CF CJ CS CU CY EB EC EG FB FG FU HC I IC IF IH IM J JD JM JR L LC LG LH LU M MA NI NR OI OP P PB PD PF PG PK PL PM PP PR PS PT PX RB RI RM RR RS RU SA SC SF SH SI SM SN SP SR SS T TA TF TL TS UR V WH WR Y ZC ZN".split()
    )
    missing = sorted(expected - set(profiles))
    extra = sorted(set(profiles) - expected)
    if extra:
        raise RuntimeError(f"出现预期集合之外的品种：{extra}")
    if missing:
        print(f"\n# 自动推导未覆盖 {len(missing)} 个品种：{missing}")
        print("# 这些品种可能是当日无成交或样本不足，必须人工从权威配置补齐后才能进入 Task 2。")
    return profiles


def _derive_multiplier(df: pd.DataFrame) -> int | None:
    """multiplier = round(median(ΔTurnover / ΔVolume / LastPrice))。"""
    dv = df["delta_volume"]
    dt = df["delta_turnover"]
    lp = df["LastPrice"]
    mask = (
        dv.notna() & (dv > 0)
        & dt.notna() & (dt > 0)
        & lp.notna() & (lp > 0)
    )
    if mask.sum() < 10:
        return None
    ratios = (dt[mask] / dv[mask] / lp[mask]).to_numpy()
    med = float(np.median(ratios))
    # 取最接近的整数；郑商所品种会得到 1
    return max(1, round(med))


def _derive_tick_size(df: pd.DataFrame) -> float | None:
    """tick_size = 所有价格差值的 GCD。

    价格放大到 8 位小数整数后求 GCD，再缩回。
    """
    SCALE = 10**8
    prices = []
    for col in ("BidPrice1", "AskPrice1", "LastPrice"):
        vals = df.loc[df[col] > 0, col].to_numpy(dtype=float)
        prices.extend(vals)
    if len(prices) < 10:
        return None
    arr = np.array(sorted(set(prices)))
    diffs = np.diff(arr)
    diffs = diffs[diffs > 0]
    if len(diffs) == 0:
        return None
    int_diffs = [int(round(d * SCALE)) for d in diffs if round(d * SCALE) > 0]
    if not int_diffs:
        return None
    gcd_val = reduce(math.gcd, int_diffs)
    if gcd_val <= 0:
        return None
    ts = gcd_val / SCALE
    if ts < 1e-4:
        raise ValueError(f"推导出的 tick_size={ts} 过小，疑似浮点噪声或非网格价格")
    # 规范化：去掉浮点误差
    for prec in (1, 2, 3, 4, 5):
        rounded = round(ts, prec)
        if abs(ts - rounded) < 1e-9:
            return rounded
    return ts


def _commodity_from_file_name(file_name: str) -> str | None:
    stem = Path(file_name).stem.rsplit("_", 1)[0]
    if any(kw in stem for kw in ("主力连续", "当月连续", "下月连续", "当季连续", "下季连续", "隔季连续")):
        return None
    import re
    match = re.match(r"^([A-Za-z]+)", stem)
    return match.group(1).upper() if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="推导品种参数表")
    parser.add_argument("--tick-day-path", required=True)
    args = parser.parse_args(argv)
    profiles = derive_profiles(args.tick_day_path)
    print(f"\n# 共 {len(profiles)} 个品种")
    print("\nCOMMODITY_PROFILES = {")
    for code, p in sorted(profiles.items()):
        print(f'    "{code}": {{"tick_size": {p["tick_size"]}, "contract_multiplier": {p["contract_multiplier"]}, "parameter_profile": "AUTO_INFERRED_V1", "validation_status": "pending_manual_review", "parameter_source": "{p["parameter_source"]}", "review_status": "pending", "reviewed_by": None, "reviewed_at": None, "review_note": None}},')
    print("}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2：运行工具生成参数表**

```bash
./venv/bin/python -m scripts.derive_commodity_profiles \
  --tick-day-path data/tick2026/202605/20260520
```

预期：输出可自动推导的 `COMMODITY_PROFILES` 字典，以及无法从当日数据推导的缺失清单。实跑 20260520 得到 81 个候选；`BB/JR/PM/RI/WH/ZC` 无有效差分，不能从该单日自动推导。允许 Task 1 输出少于 87 个，这不是失败，必须在人工审核阶段用权威配置或其他有效交易日数据补齐。

- [ ] **Step 3：人工审核输出**

检查要点：
- AU: tick_size=0.02, multiplier=1000 ✓（基准对照）
- CU: tick_size=10, multiplier=5
- AG: tick_size=2（或推导值）, multiplier=15
- 郑商所品种（AP/CF/CJ/FG/MA/OI 等）: multiplier=1（价格单位 AveragePrice）
- 无 tick_size=0 或 multiplier=0 的品种
- 对缺失清单逐项补齐权威参数，不能用空值或猜测值直接标记 `validated`

将自动推导结果与人工补齐结果合并，确认最终键集恰好为 87 个，再保存备用供 Task 2 粘贴。**到此必须暂停并等待用户明确确认。**

审核确认至少记录以下结果：
- 87 个品种键集完整，无缺失、重复或额外品种；
- 每个 `tick_size` 为正数且符合该品种最小变动价位；
- 每个 `contract_multiplier` 为正整数，并与 `Turnover / Volume / LastPrice` 的中位数接近；
- AU 保持人工基准值；异常值必须在确认前修正或明确排除。

只有用户明确回复审核通过后，才允许进入 Task 2，并将这 87 个品种的
`validation_status` 从 `pending_manual_review` 改为 `validated`。

- [ ] **Step 4：补充推导工具测试**

新增 `tests/test_derive_commodity_profiles.py`，至少覆盖：
- `_derive_tick_size` 的正常网格、浮点噪声过小门禁和样本不足；
- `_derive_multiplier` 的正常值和有效差分不足；
- 连续合约过滤、文件名品种解析；
- `parameter_source` 随输入日期生成；
- 缺失品种输出清单、额外品种报错；
- `./venv/bin/python -m scripts.derive_commodity_profiles --help` 命令级 smoke test，确保 `src` 可导入。

建议提交：

```bash
git add scripts/derive_commodity_profiles.py tests/test_derive_commodity_profiles.py
git commit -m "feat(tick-detector): add commodity profile derivation tool"
```

## 2. Task 2：扩展 COMMODITY_PROFILES 到 87 品种

**文件：** `src/tick_detector/tick_io.py`、`tests/test_tick_io.py`

- [ ] **Step 1：先写失败测试**

在 `tests/test_tick_io.py` 末尾追加：

```python
def test_commodity_profiles_cover_all_validated_commodities():
    """87 个品种都应有 validated profile。"""
    import re
    from src.tick_detector.tick_io import COMMODITY_PROFILES
    # 已知 20260520 数据有 87 个品种
    validated = {
        code for code, p in COMMODITY_PROFILES.items()
        if p.get("validation_status") == "validated"
    }
    # AU 是人工标定的基准
    assert "AU" in validated
    assert COMMODITY_PROFILES["AU"]["parameter_profile"] == "AU_V1"
    # 自动标定的品种用 AUTO_INFERRED_V1
    auto = {
        code for code, p in COMMODITY_PROFILES.items()
        if p.get("parameter_profile") == "AUTO_INFERRED_V1"
    }
    # 至少覆盖主要活跃品种
    for code in ("CU", "AG", "AL", "NI", "ZN", "SC", "RB", "JD", "CF", "AP"):
        assert code in validated, f"{code} 缺少 validated profile"
    expected = {
        "A", "AD", "AG", "AL", "AO", "AP", "AU", "B", "BB", "BC", "BR",
        "BU", "BZ", "C", "CF", "CJ", "CS", "CU", "CY", "EB", "EC", "EG",
        "FB", "FG", "FU", "HC", "I", "IC", "IF", "IH", "IM", "J", "JD",
        "JM", "JR", "L", "LC", "LG", "LH", "LU", "M", "MA", "NI", "NR",
        "OI", "OP", "P", "PB", "PD", "PF", "PG", "PK", "PL", "PM", "PP",
        "PR", "PS", "PT", "PX", "RB", "RI", "RM", "RR", "RS", "RU", "SA", "SC",
        "SF", "SH", "SI", "SM", "SN", "SP", "SR", "SS", "T", "TA", "TF",
        "TL", "TS", "UR", "V", "WH", "WR", "Y", "ZC", "ZN",
    }
    assert len(validated) == 87
    assert validated == expected
    for code, profile in COMMODITY_PROFILES.items():
        assert profile["tick_size"] > 0
        assert profile["contract_multiplier"] > 0
        source = profile.get("parameter_source", "")
        assert (
            re.fullmatch(r"AUTO_INFERRED_20\d{6}", source)
            or source.startswith("AUTHORITATIVE_MANUAL:")
            or re.fullmatch(r"OTHER_DAY_MANUAL:20\d{6}", source)
        )
        assert profile["review_status"] == "approved"
        assert profile["reviewed_by"]
        assert profile["reviewed_at"]
        assert profile["review_note"]
        if code != "AU":
            assert profile["parameter_profile"] in {"AUTO_INFERRED_V1", "MANUAL_REVIEW_V1"}


def test_au_profile_unchanged_after_expansion():
    """扩展后 AU profile 保持人工标定值不变。"""
    from src.tick_detector.tick_io import COMMODITY_PROFILES
    au = COMMODITY_PROFILES["AU"]
    assert au["tick_size"] == 0.02
    assert au["contract_multiplier"] == 1000
    assert au["parameter_profile"] == "AU_V1"
    assert au["validation_status"] == "validated"
```

- [ ] **Step 2：运行测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_tick_io.py::test_commodity_profiles_cover_all_validated_commodities -v
```

预期：FAIL（只有 AU，缺 CU/AG/AL 等）。

- [ ] **Step 3：粘贴 Task 1 审核通过的参数表**

将 Task 1 Step 3 审核通过的 `COMMODITY_PROFILES` 字典粘贴到 `src/tick_detector/tick_io.py`，替换只有 AU 的旧定义。确保 AU 的 `parameter_profile` 保持 `"AU_V1"`；自动推导品种使用 `"AUTO_INFERRED_V1"`，人工补齐品种使用 `"MANUAL_REVIEW_V1"`。所有条目必须保留 `parameter_source` 和完整审核字段；同时更新 `tick_io.py` 中“第一版只有 AU validated”的过时注释。

- [ ] **Step 4：运行测试确认通过**

```bash
./venv/bin/python -m pytest tests/test_tick_io.py -q
```

- [ ] **Step 5：提交**

```bash
git add src/tick_detector/tick_io.py tests/test_tick_io.py
git commit -m "feat(tick-detector): expand COMMODITY_PROFILES to 87 commodities"
```

## 3. Task 3：`--commodities` 多品种参数 + HTML 按品种拆分

**文件：** `run_tick_detector.py`、`tests/test_run_tick_detector.py`

这是本计划最核心的改动。当前 `run_detection` 把所有品种的候选都累积到 `all_events` 和 `replay_payload`，最后统一写一个 CSV 和一个 HTML。改为：按品种即时写 HTML，CSV 仍合并。

- [ ] **Step 1：先写失败测试**

在 `tests/test_run_tick_detector.py` 追加：

```python
def test_commodities_param_accepts_multiple_codes():
    """--commodities AU,AG 逗号分隔多品种"""
    args = parse_args([
        "--tick-day-path", "data/tick2026/202605/20260520.zip",
        "--commodities", "AU,AG",
    ])
    assert args.commodities == "AU,AG"


def test_commodities_backward_compat_with_single_commodity():
    """旧 --commodity 仍然可用"""
    args = parse_args([
        "--tick-day-path", "data/tick2026/202605/20260520.zip",
        "--commodity", "AU",
    ])
    assert args.commodity == "AU"
    assert args.commodities is None


def test_run_detection_splits_html_per_commodity(tmp_path):
    """多品种运行时每个品种一个 HTML 文件"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    _write_csv(day_dir / "au2606_20260520.csv", [_tick_row("au2606", "09:01:30", 0)])
    _write_csv(day_dir / "au2608_20260520.csv", [_tick_row("au2608", "09:01:30", 0)])
    _write_csv(day_dir / "au2610_20260520.csv", [_tick_row("au2610", "09:01:30", 0)])
    output_dir = tmp_path / "out"
    result = run_detection(
        tick_day_path=str(day_dir),
        commodities="AU",
        output_dir=str(output_dir),
    )
    # AU 有独立 HTML
    assert (output_dir / "event_replay_AU.html").exists()
    # 合并 CSV 存在
    assert (output_dir / "tick_candidate_events.csv").exists()
    assert "event_replay_AU" in str(result.get("event_replay_htmls", ""))


def test_run_detection_csv_merges_across_commodities(tmp_path):
    """多品种 CSV 合并为一个文件"""
    day_dir = tmp_path / "20260520"
    day_dir.mkdir()
    # 两个品种各 3 合约
    for code in ("au2606", "au2608", "au2610"):
        _write_csv(day_dir / f"{code}_20260520.csv", [_tick_row(code, "09:01:30", 0)])
    for code in ("ag2606", "ag2608", "ag2610"):
        _write_csv(day_dir / f"{code}_20260520.csv", [_tick_row(code, "09:01:30", 0)])
    output_dir = tmp_path / "out"
    run_detection(
        tick_day_path=str(day_dir),
        commodities="AU,AG",
        output_dir=str(output_dir),
    )
    # 两个 HTML 文件
    assert (output_dir / "event_replay_AU.html").exists()
    assert (output_dir / "event_replay_AG.html").exists()
    # 合并 CSV 含表头
    csv_path = output_dir / "tick_candidate_events.csv"
    assert csv_path.exists()
    events = pd.read_csv(csv_path)
    assert "合约" in events.columns
```

- [ ] **Step 2：运行测试确认失败**

```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py::test_commodities_param_accepts_multiple_codes -v
```

预期：FAIL（`--commodities` 参数不存在）。

- [ ] **Step 3：实现 `--commodities` 参数**

在 `build_parser` 增加 `--commodities` 参数：

```python
    parser.add_argument("--commodities", required=False,
                        help="可选，逗号分隔的品种列表，如 AU,AG,CU。优先于 --commodity")
```

在 `parse_args` 中增加参数校验：当 `--commodities` 含多个品种且同时传入
`--contract` 时直接 `parser.error`，避免输出其他品种的误导性空 HTML。

- [ ] **Step 4：改写 `run_detection` 支持 HTML 按品种拆分**

`run_detection` 的签名改为接受 `commodities: str | None`，内部按品种拆分 HTML 写盘。核心改动逻辑：

同时从 `src.tick_detector.tick_io` 导入 `COMMODITY_PROFILES`，用于校验请求品种是否已配置。

```python
def run_detection(*, tick_day_path: str, output_dir: str,
                  commodity: str | None = None, contract: str | None = None,
                  commodities: str | None = None) -> dict[str, object]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # 解析品种列表：--commodities 优先于 --commodity
    if commodities:
        target_commodities = [c.strip().upper() for c in commodities.split(",") if c.strip()]
    elif commodity:
        target_commodities = [commodity.upper()]
    elif contract:
        target_commodities = [_commodity_from_contract(contract)]
    else:
        target_commodities = None  # 全部品种

    if commodities is not None and not target_commodities:
        raise ValueError("--commodities 不能为空或只包含逗号/空白")

    contract_filter = contract.upper() if contract else None
    all_events: list[pd.DataFrame] = []
    all_diagnostics: list[dict[str, object]] = []
    html_files: list[str] = []
    commodity_files = _group_contract_files_by_commodity(tick_day_path, target_commodities)
    if target_commodities:
        requested = set(target_commodities)
        missing_data = sorted(requested - set(commodity_files))
        missing_profiles = sorted(requested - set(COMMODITY_PROFILES))
        unvalidated_profiles = sorted(
            code for code in requested
            if code in COMMODITY_PROFILES
            and COMMODITY_PROFILES[code].get("validation_status") != "validated"
        )
        if missing_data or missing_profiles or unvalidated_profiles:
            raise ValueError(
                f"请求品种不可运行：数据缺失={missing_data}，profile缺失={missing_profiles}，未审核={unvalidated_profiles}"
            )

    for commodity_index, (commodity_code, contract_files) in enumerate(
        commodity_files.items(), start=1
    ):
        # ... 装载 day_frames（与当前逻辑相同）...

        # 每品种独立收集 events / replay_payload / diagnostics
        commodity_events: list[pd.DataFrame] = []
        commodity_payload: dict[str, dict[str, object]] = {}
        commodity_diagnostics: list[dict[str, object]] = []

        commodity_targets = [
            code for code in sorted(day_frames)
            if contract_filter is None or code.upper() == contract_filter
        ]
        for target_code in commodity_targets:
            events, diag = _detect_contract(target_code, day_frames)
            commodity_diagnostics.append(diag)
            all_diagnostics.append(diag)
            if events is not None and not events.empty:
                commodity_events.append(events)
                all_events.append(events)
                _build_replay_payload(events, target_code, day_frames, commodity_payload)

        # 即时写品种 HTML（释放 replay_payload 内存）
        commodity_events_df = _build_events_df(commodity_events)
        html_path = output_path / f"event_replay_{commodity_code}.html"
        html_path.write_text(
            render_event_replay_html(commodity_events_df, commodity_payload, commodity_diagnostics),
            encoding="utf-8",
        )
        if len(commodity_files) == 1:
            (output_path / "event_replay.html").write_text(
                html_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        html_files.append(str(html_path))
        print(f"  - 写出 {html_path.name}", flush=True)

    # 合并 CSV
    events_df = _build_events_df(all_events)
    events_df_chinese = _map_to_chinese_csv(events_df)
    csv_path = output_path / "tick_candidate_events.csv"
    events_df_chinese.to_csv(csv_path, index=False)

    return {
        "tick_candidate_events_csv": str(csv_path),
        "event_replay_htmls": html_files,
        # 单品种调用保留旧键，避免现有调用方无提示失效。
        "event_replay_html": str(output_path / "event_replay.html") if len(html_files) == 1 else None,
    }
```

关键变化：
- `target_commodities` 是列表而非单个字符串；`_group_contract_files_by_commodity` 改为接受列表过滤。
- 每品种处理完即时写 HTML，`commodity_payload` 在下一品种循环时自然释放；`all_events` 仍为合并 CSV 保留，内存收益主要来自 replay 明细而不是事件表。
- 返回值新增 `event_replay_htmls`（列表）；单品种运行时保留旧的 `event_replay_html` 键，并继续写出 `event_replay.html` 兼容别名。
- `all_events` 仍累积用于合并 CSV（CSV 行数远小于 HTML，内存可控）。

同步更新 `_group_contract_files_by_commodity` 签名：

```python
def _group_contract_files_by_commodity(
    tick_day_path: str,
    target_commodities: list[str] | None,
) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for contract_file in iter_day_contract_files(tick_day_path):
        commodity_code = _commodity_from_file_name(contract_file.file_name)
        if commodity_code is None:
            continue
        if target_commodities and commodity_code not in target_commodities:
            continue
        grouped.setdefault(commodity_code, []).append(contract_file)
    return dict(sorted(grouped.items()))
```

- [ ] **Step 5：更新 `main` 和旧测试**

`main` 函数传递 `commodities=args.commodities`。旧的 `test_run_detection_*` 测试调用 `run_detection` 时补 `commodities=None` 参数。单品种输出同时验证 `event_replay_AU.html` 和兼容别名 `event_replay.html`。`--commodities` 与 `--contract` 同时指定多个品种时应直接报参数冲突；单品种时也必须校验 contract 的品种前缀与 selected commodity 一致。

同时禁止 `--commodities` 与 `--commodity` 同时传入；请求列表中任何不存在于实际数据/profile 的品种都必须在解析或分组阶段报错，不能静默生成空报告。

- [ ] **Step 6：运行测试确认通过**

```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py -q
```

补充参数与输入校验测试：
- `--commodities AU,AG --contract AU2606` 必须在解析阶段失败；
- `--commodities AU --commodity AG` 必须失败；
- `--commodities TYPO`、请求品种数据缺失、profile 缺失都必须失败，不能返回空 CSV 冒充成功；
- `--commodities ',,'` 必须失败，不能退化为运行全部品种；存在 `pending_manual_review` profile 时必须失败；
- 单品种旧键 `event_replay_html` 必须指向兼容文件 `event_replay.html`。

- [ ] **Step 7：提交**

```bash
git add run_tick_detector.py tests/test_run_tick_detector.py
git commit -m "feat(tick-detector): add --commodities param and split HTML per commodity"
```

## 4. Task 4：分批运行脚本

**文件：** `scripts/run_all_commodities_batched.sh`（新建）

**目的：** 87 个品种按 10 个一批分 9 批运行，每批输出到独立子目录，避免单次运行过长。

- [ ] **Step 1：确认品种列表**

从 Task 1 推导结果的键集，按字母序分 9 批：
- 批次 1-8：每批 10 个品种
- 批次 9：7 个品种

分批时注意：同一品种的合约不能跨批（否则 peer 参考合约加载不完整）。`--commodities` 参数保证品种原子性。

- [ ] **Step 2：生成分批脚本**

```bash
#!/usr/bin/env bash
# scripts/run_all_commodities_batched.sh
# 87 品种按 10 个一批分 9 批运行。
# 用法：./scripts/run_all_commodities_batched.sh <tick_day_path> <output_root>
#   tick_day_path: 如 data/tick2026/202605/20260520
#   output_root:   如 output/full-20260520

set -euo pipefail
TICK_DAY_PATH="${1:?用法: $0 <tick_day_path> <output_root>}"
OUTPUT_ROOT="${2:?用法: $0 <tick_day_path> <output_root>}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${REPO_ROOT}/venv/bin/python"

mkdir -p "$OUTPUT_ROOT"

# 87 品种按字母序分 9 批（8×10 + 1×7）
BATCHES=(
  "a,ad,ag,al,ao,au,b,bb,bc,br"
  "bu,bz,c,cs,cu,eb,ec,eg,fb,fu"
  "hc,i,j,jd,jm,l,lc,lg,lh,lu"
  "m,ni,nr,op,p,pb,pd,pg,pp,ps"
  "pt,rb,rr,ru,sc,si,sn,sp,ss,v"
  "wr,y,zn,AP,CF,CJ,CY,FG,IC,IF"
  "IH,IM,JR,MA,OI,PF,PK,PL,PM,PR"
  "PX,RI,RM,RS,SA,SF,SH,SM,SR,T"
  "TA,TF,TL,TS,UR,WH,ZC"
)

TOTAL=${#BATCHES[@]}
for i in "${!BATCHES[@]}"; do
  BATCH_NUM=$((i + 1))
  COMMODITIES="${BATCHES[$i]}"
  BATCH_DIR="${OUTPUT_ROOT}/batch-${BATCH_NUM}"
  echo ""
  echo "========================================"
  echo "批次 ${BATCH_NUM}/${TOTAL}: ${COMMODITIES}"
  echo "========================================"
  "$PYTHON" "${REPO_ROOT}/run_tick_detector.py" \
    --tick-day-path "$TICK_DAY_PATH" \
    --commodities "$COMMODITIES" \
    --output-dir "$BATCH_DIR"
done

echo ""
echo "全部 ${TOTAL} 批完成。各批次输出在 ${OUTPUT_ROOT}/batch-*/ 下。"
```

- [ ] **Step 3：验证脚本可执行**

```bash
chmod +x scripts/run_all_commodities_batched.sh
# 干跑确认品种列表正确（不执行检测，只看 echo）
bash -n scripts/run_all_commodities_batched.sh
```

- [ ] **Step 4：提交**

```bash
git add scripts/run_all_commodities_batched.sh
git commit -m "feat(tick-detector): add batched run script for 87 commodities"
```

## 5. Task 5：全链路验证

**文件：** 无新文件，验证现有代码。

- [ ] **Step 1：运行全部合成测试**

```bash
./venv/bin/python -m pytest tests/ -q -k "not slow_"
```

预期：全部通过。

- [ ] **Step 2：小批量真数据验证（1 批 10 品种）**

选一批小品种验证端到端正确性：

```bash
./venv/bin/python run_tick_detector.py \
  --tick-day-path data/tick2026/202605/20260520 \
  --commodities "a,ad,ag,al,ao,au,b,bb,bc,br" \
  --output-dir /tmp/tick-detector-batch1
```

验收：
- 每个 `validated` 品种有 `event_replay_{品种}.html`。
- `tick_candidate_events.csv` 含中文表头。
- AU2606 锚点仍在候选中（如果该品种在此批）。
- 无品种因 multiplier/tick_size 配置错误导致 `avg_trade_price_enabled` 全 False（抽查 CSV 诊断或日志）。

- [ ] **Step 3：旧契约清理检查**

```bash
if rg -n "tick_events\.csv|recovery_denominator_ticks|expected_price_simple" \
  run_tick_detector.py src/tick_detector scripts/ tests/; then
  echo "发现旧契约残留" >&2
  exit 1
fi
echo "CLEAN"
```

- [ ] **Step 4：AU 锚点回归测试**

```bash
./venv/bin/python -m pytest tests/test_run_tick_detector.py::test_slow_au2606_real_anchor_210435_hits_two_reasons_and_matches_design -v
```

预期：PASS（~7 分钟）。

- [ ] **Step 5：提交验证结果（如有改动）**

```bash
git add -A
git commit -m "test(tick-detector): verify batch run end-to-end" || echo "无改动"
```

## 6. 完成定义

以下条件同时满足才结束本阶段：

1. 87 品种参数表落为静态配置，`COMMODITY_PROFILES` 覆盖全部品种。
2. `--commodities` 参数支持逗号分隔多品种；HTML 按品种拆分输出。
3. 合成测试全部通过；AU2606 真数据锚点回归测试通过。
4. 分批运行脚本就绪，9 批品种列表正确。
5. 至少 1 批 10 品种真数据端到端运行成功，无参数配置错误。

**不做的事：**
- 不改 `attach_fair_price_metrics` 算法（接受长运行时间）。
- 不做向量化优化。
- 不自动合并各批次输出（批次间独立目录，人工汇总）。
- 不做多日批量（本计划只覆盖单日全品种）。

**后续（不在本计划范围）：**
- 全量 9 批真数据运行（预估 10-20 小时，可分多次跑）。
- 各批次 CSV 人工汇总。
- 性能优化（向量化 Pass 1/Pass 2）。
- 跨 2-3 个交易日复核 multiplier 稳定性。
