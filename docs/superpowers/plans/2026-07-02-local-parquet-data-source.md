# 本地 parquet 数据源替换 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把乌龙指初筛工具的数据源从 Tushare/AKShare 在线拉取完全替换为本地 parquet 数据集(`data/1d_futures/`),删除全部在线/缓存/合约集合逻辑。

**Architecture:** 原地重写 `src/daily_screen/data_access.py` 为单文件 parquet loader。两遍扫描:Pass A 读全历史 `[code, date]` 拿每合约真实首末日(供下游 lifecycle 判定),Pass B 读窗口内 OHLC 生成 daily_bar。`load_commodity_data` 保持同名,签名简化(`cache_dir`/`config_path` → `data_dir`),`analyze_commodities` 同步转发 `data_dir` 作为测试注入点。

**Tech Stack:** Python 3.13、pandas、pyarrow(parquet 读取,本次新增依赖)、pytest。

**执行前置:** 当前在 master 分支,开始前应 `git checkout -b feat/local-parquet-data-source` 建分支再执行(系统约定:不在 master 直接提交)。spec 未提交,实现完一起提交。

参考 spec:[docs/superpowers/specs/2026-07-02-local-parquet-data-source-design.md](../specs/2026-07-02-local-parquet-data-source-design.md)

---

## Task 1: 安装 pyarrow + 建立 requirements.txt(BLOCKER)

未装 pyarrow 则 parquet 一行都读不了,此步是其余所有步骤的前提。注意 `./venv/bin/pip` 的 shebang 指向已失效的 Nutstore 旧路径,必须走 `./venv/bin/python -m pip`。

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: 安装 pyarrow**

Run:
```bash
./venv/bin/python -m pip install pyarrow
```
Expected: 安装成功(若报 `No module named pip`,先 `./venv/bin/python -m ensurepip --upgrade`)。

- [ ] **Step 2: 验证可读写 parquet**

Run:
```bash
./venv/bin/python -c "import pyarrow, pandas as pd; print('pyarrow', pyarrow.__version__); df=pd.DataFrame({'a':[1]}); df.to_parquet('/tmp/_t.parquet'); print(pd.read_parquet('/tmp/_t.parquet').shape)"
```
Expected: 打印版本号和 `(1, 1)`,无异常。

- [ ] **Step 3: 创建 requirements.txt**

Create `requirements.txt`:
```
pyarrow>=24.0.0
pandas>=2.0.0
numpy>=1.24.0
pytest>=7.0.0
```
(版本下限按当前 venv 实际版本,可用 `./venv/bin/python -c "import pandas,numpy,pytest; print(pandas.__version__, numpy.__version__, pytest.__version__)"` 核对后调整。)

- [ ] **Step 4: Commit**

```bash
git add requirements.txt
git commit -m "chore: 新增 pyarrow 依赖与 requirements.txt"
```

---

## Task 2: 重写 data_access.py 与其测试(TDD)

先把测试写全(编码 spec 全部断言),跑 → 红(旧 loader 签名不匹配/找不到 contract_set 缓存),再重写 loader → 绿。

**Files:**
- Rewrite: `src/daily_screen/data_access.py`
- Rewrite: `tests/test_data_access.py`

- [ ] **Step 1: 写完整测试文件 tests/test_data_access.py**

Create `tests/test_data_access.py`(整文件覆盖):
```python
from __future__ import annotations

import pandas as pd
import pytest

from src.daily_screen.data_access import load_commodity_data
from src.daily_screen.schemas import REQUIRED_DAILY_COLUMNS, REQUIRED_META_COLUMNS


def _write_day(tmp_path, year: int, date: str, rows: list[dict]) -> None:
    year_dir = tmp_path / str(year)
    year_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(year_dir / f"{date}.parquet")


_UNSET = object()


def _row(code, date, close, *, pre_close=_UNSET, pre_settle=_UNSET, open_=_UNSET, high=_UNSET, low=_UNSET, vol=1000):
    # 用哨兵 _UNSET 区分"未传 → 默认 close"与"显式 None → 写 NaN(用于测 pre_close 回填)"
    return {
        "code": code,
        "date": date,
        "pre_close": close if pre_close is _UNSET else pre_close,
        "pre_settle": close if pre_settle is _UNSET else pre_settle,
        "open": close if open_ is _UNSET else open_,
        "high": close + 1 if high is _UNSET else high,
        "low": close - 1 if low is _UNSET else low,
        "close": close,
        "vol": vol,
    }


def test_filters_to_requested_symbols_and_drops_continuous(tmp_path):
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 100, vol=1000),
        _row("A.DCE", "20250105", 100, vol=5000),       # 连续合约,须滤除
        _row("SR2501.ZCE", "20250105", 6000, vol=2000),  # 非目标品种
    ])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    daily = result["daily_bar"]
    assert list(daily.columns) == REQUIRED_DAILY_COLUMNS
    assert set(daily["commodity"]) == {"A"}
    assert set(daily["contract"]) == {"A2501"}


def test_date_window_includes_buffer_and_caps_end(tmp_path):
    _write_day(tmp_path, 2024, "20241201", [_row("A2501.DCE", "20241201", 90)])   # 缓冲区(45天内)
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])  # 窗口内
    _write_day(tmp_path, 2025, "20250220", [_row("A2501.DCE", "20250220", 110)])  # 窗口外
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    dates = set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d"))
    assert "20241201" in dates   # 45 天缓冲包含
    assert "20250105" in dates
    assert "20250220" not in dates  # 超过 end_date 截断


def test_contract_meta_listed_date_from_pre_window_history(tmp_path):
    # A2501 真实首日在窗口之前(20241101),窗口从 20250101 起
    _write_day(tmp_path, 2024, "20241101", [_row("A2501.DCE", "20241101", 80)])
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    meta = result["contract_meta"]
    assert list(meta.columns) == REQUIRED_META_COLUMNS
    row = meta.loc[meta["contract"] == "A2501"].iloc[0]
    assert row["listed_date"] == pd.Timestamp("2024-11-01")  # 真实首日,落在窗口外


def test_contract_meta_last_trade_date_from_future_year(tmp_path):
    # A2501 在窗口(2025)出现,真实末日在未来年份(2026)
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])
    _write_day(tmp_path, 2026, "20260315", [_row("A2501.DCE", "20260315", 130)])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    meta = result["contract_meta"]
    row = meta.loc[meta["contract"] == "A2501"].iloc[0]
    assert row["last_trade_date"] == pd.Timestamp("2026-03-15")  # 取自未来年份,不是 end.year


def test_pre_close_falls_back_to_pre_settle(tmp_path):
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 104, pre_close=None, pre_settle=98),
    ])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    assert result["daily_bar"]["pre_close"].iloc[0] == 98


def test_pre_close_prev_close_no_cross_contract_pollution(tmp_path):
    # A2501 20250105 close=100;B2501 20250105 与 20250106,pre_close/pre_settle 都缺
    # 若全局 shift:B2501 20250106 会错拿 A2501 的 100
    # groupby 后:B2501 20250106 pre_close 应=B2501 20250105 的 close=200
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 100, pre_close=None, pre_settle=None),
        _row("B2501.DCE", "20250105", 200, pre_close=None, pre_settle=None),
    ])
    _write_day(tmp_path, 2025, "20250106", [
        _row("B2501.DCE", "20250106", 210, pre_close=None, pre_settle=None),
    ])
    result = load_commodity_data(["A", "B"], "20250101", "20250110", data_dir=tmp_path)
    daily = result["daily_bar"]
    b206 = daily.loc[(daily["contract"] == "B2501") & (daily["trade_date"] == pd.Timestamp("2025-01-06"))]
    assert b206["pre_close"].iloc[0] == 200  # 同合约前一日 close,不是 A 的 100


def test_merges_across_years(tmp_path):
    # expanded_start = 20250101 - 45d = 20241117;20241230 落在缓冲区内,应保留
    _write_day(tmp_path, 2024, "20241230", [_row("A2501.DCE", "20241230", 95)])
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "20250105", 100)])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    dates = set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d"))
    assert dates == {"20241230", "20250105"}


def test_misnamed_parquet_raises(tmp_path):
    (tmp_path / "2025").mkdir(parents=True)
    pd.DataFrame([{"code": "A2501.DCE", "date": "20250105"}]).to_parquet(tmp_path / "2025" / "bad_name.parquet")
    with pytest.raises(ValueError, match="非 YYYYMMDD"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)


def test_missing_data_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="data_dir 不存在"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path / "nope")


def test_data_dir_is_file_raises(tmp_path):
    file_path = tmp_path / "not_a_dir.parquet"
    file_path.write_bytes(b"x")
    with pytest.raises(NotADirectoryError, match="不是目录"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=file_path)


def test_invalid_year_dir_raises(tmp_path):
    (tmp_path / "2025").mkdir()
    (tmp_path / "abc").mkdir()
    with pytest.raises(ValueError, match="年份目录名非法"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)


def test_parses_lenient_date_formats(tmp_path):
    # spec 要求 date → to_datetime(无 format);锁死宽松解析,防止退化成只认 YYYYMMDD
    _write_day(tmp_path, 2025, "20250105", [_row("A2501.DCE", "2025-01-05", 100)])  # ISO 字符串
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    assert set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d")) == {"20250105"}


def test_corrupt_parquet_raises_with_context(tmp_path):
    # 文件名合法但内容非 parquet → 异常必须带 file_path / pass / file_date
    (tmp_path / "2025").mkdir(parents=True)
    (tmp_path / "2025" / "20250105.parquet").write_bytes(b"not a parquet file")
    with pytest.raises(RuntimeError, match=r"parquet 读取失败.*file_path=.*pass=[AB].*file_date=20250105"):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)


def test_clips_rows_by_trade_date_not_just_filename(tmp_path):
    # 文件名 20250105 在窗口内,但文件里塞了一行 date=20250220(窗口外)→ 必须按 trade_date 裁掉
    _write_day(tmp_path, 2025, "20250105", [
        _row("A2501.DCE", "20250105", 100),
        _row("A2501.DCE", "20250220", 999),  # 与文件名不一致的脏行
    ])
    result = load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
    dates = set(result["daily_bar"]["trade_date"].dt.strftime("%Y%m%d"))
    assert dates == {"20250105"}  # 20250220 被裁掉


def test_pass_b_failure_attaches_symbols(tmp_path):
    # 文件只有 code/date 列:Pass A 读 [code,date] 成功,Pass B 读 OHLC 列失败
    # → 异常须带全四字段:file_path / pass=B / file_date / symbols
    (tmp_path / "2025").mkdir(parents=True)
    pd.DataFrame([{"code": "A2501.DCE", "date": "20250105"}]).to_parquet(tmp_path / "2025" / "20250105.parquet")
    with pytest.raises(
        RuntimeError,
        match=r"parquet 读取失败.*file_path=.*pass=B.*file_date=20250105.*symbols=",
    ):
        load_commodity_data(["A"], "20250101", "20250110", data_dir=tmp_path)
```

`test_merges_across_years` 的 20241230 落在 45 天缓冲区内(expanded_start = 20250101 − 45 = 20241117),故预期返回 2 行。

- [ ] **Step 2: 跑测试确认全红**

Run:
```bash
./venv/bin/pytest tests/test_data_access.py -v
```
Expected: 全部 FAIL(旧 loader 用 `cache_dir`/`config_path`,被以 `data_dir=` 调用会 TypeError,或 `contract_set_*.json` 缺失报错)。

- [ ] **Step 3: 重写 src/daily_screen/data_access.py(整文件替换)**

整文件覆盖为:
```python
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src.daily_screen.schemas import REQUIRED_DAILY_COLUMNS, REQUIRED_META_COLUMNS

TRADING_LOOKBACK_BUFFER_DAYS = 45
CONTRACT_CODE_PATTERN = re.compile(r"^[A-Z]+\d{3,4}$")
FILE_DATE_PATTERN = re.compile(r"^(?P<date>\d{8})\.parquet$")


def load_commodity_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
    *,
    data_dir: str | Path = "data/1d_futures",
) -> dict[str, pd.DataFrame]:
    normalized_symbols = _normalize_symbols(symbols)
    _validate_dates(start_date, end_date)

    data_path = Path(data_dir)
    if not data_path.exists():
        raise FileNotFoundError(f"data_dir 不存在: {data_path}")
    if not data_path.is_dir():
        raise NotADirectoryError(f"data_dir 不是目录(传入了文件): {data_path}")

    available_years = _list_available_years(data_path)
    if not available_years:
        raise FileNotFoundError(f"data_dir 下无年份目录: {data_path}")

    expanded_start_date = _expand_start_date(start_date)
    bounds = _scan_contract_date_bounds(data_path, available_years, normalized_symbols)
    daily_bar = _build_daily_bar(data_path, available_years, expanded_start_date, end_date, normalized_symbols)
    contract_meta = _build_contract_meta(daily_bar, bounds)

    return {"daily_bar": daily_bar, "contract_meta": contract_meta}


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    for symbol in symbols:
        upper = str(symbol).strip().upper()
        if upper and upper not in normalized:
            normalized.append(upper)
    if not normalized:
        raise ValueError("symbols 不能为空")
    return normalized


def _validate_dates(start_date: str, end_date: str) -> None:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    end_ts = datetime.strptime(end_date, "%Y%m%d")
    if start_ts > end_ts:
        raise ValueError("start_date 不能晚于 end_date")


def _expand_start_date(start_date: str) -> str:
    start_ts = datetime.strptime(start_date, "%Y%m%d")
    return (start_ts - timedelta(days=TRADING_LOOKBACK_BUFFER_DAYS)).strftime("%Y%m%d")


def _list_available_years(data_path: Path) -> list[int]:
    years: list[int] = []
    for entry in sorted(data_path.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry.name.isdigit() and len(entry.name) == 4):
            raise ValueError(f"年份目录名非法(需为4位数字): {entry.name}")
        years.append(int(entry.name))
    return years


def _iter_parquet_files(data_path: Path, years: list[int]) -> list[tuple[int, Path, str]]:
    result: list[tuple[int, Path, str]] = []
    for year in years:
        year_dir = data_path / str(year)
        if not year_dir.exists():
            continue
        for path in sorted(year_dir.glob("*.parquet")):
            matched = FILE_DATE_PATTERN.match(path.name)
            if not matched:
                raise ValueError(f"文件名非 YYYYMMDD.parquet: {path} (年份目录 {year})")
            result.append((year, path, matched.group("date")))
    return result


def _read_parquet_safe(
    path: Path,
    columns: list[str],
    pass_name: str,
    file_date: str,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    try:
        return pd.read_parquet(path, columns=columns)
    except Exception as exc:
        message = f"parquet 读取失败: file_path={path}, pass={pass_name}, file_date={file_date}"
        if symbols is not None:
            message += f", symbols={symbols}"
        raise RuntimeError(message) from exc


def _split_code_body(code: object) -> str:
    return str(code).split(".")[0].upper()


def _scan_contract_date_bounds(
    data_path: Path,
    years: list[int],
    symbols: list[str],
) -> dict[str, tuple[pd.Timestamp, pd.Timestamp]]:
    # Pass A: 全历史 [code, date],按目标品种过滤,拿每合约真实首/末日。
    # 流式聚合:逐文件读、逐合约更新 min/max,bounds dict 只持有"目标品种的合约"条目(全市场约两千个),
    # 不同时持有全历史行,避免把 2010-2026 全量行 concat 进内存。
    # ponytail: 全历史扫描换正确性;若遍历 4000 文件仍慢,可落 contract_date_bounds.json 索引,本函数改为读索引。
    files = _iter_parquet_files(data_path, years)
    if not files:
        return {}
    symbol_set = set(symbols)
    bounds: dict[str, list[pd.Timestamp]] = {}
    for _, path, file_date in files:
        df = _read_parquet_safe(path, ["code", "date"], "A", file_date)
        df = df.dropna(subset=["code", "date"])
        if df.empty:
            continue
        body = df["code"].map(_split_code_body)
        commodity = body.str.extract(r"^([A-Z]+)")[0]
        df = df.assign(contract=body, commodity=commodity)
        df = df.loc[
            df["commodity"].isin(symbol_set) & df["contract"].str.match(CONTRACT_CODE_PATTERN)
        ]
        if df.empty:
            continue
        df["trade_date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["trade_date"])
        if df.empty:
            continue
        for contract, group in df.groupby("contract"):
            day_min = group["trade_date"].min()
            day_max = group["trade_date"].max()
            if contract in bounds:
                if day_min < bounds[contract][0]:
                    bounds[contract][0] = day_min
                if day_max > bounds[contract][1]:
                    bounds[contract][1] = day_max
            else:
                bounds[contract] = [day_min, day_max]
    return {contract: (lo, hi) for contract, (lo, hi) in bounds.items()}


def _build_daily_bar(
    data_path: Path,
    years: list[int],
    expanded_start_date: str,
    end_date: str,
    symbols: list[str],
) -> pd.DataFrame:
    start_ts = datetime.strptime(expanded_start_date, "%Y%m%d")
    end_ts = datetime.strptime(end_date, "%Y%m%d")
    window_years = [y for y in years if start_ts.year <= y <= end_ts.year]
    files = _iter_parquet_files(data_path, window_years)
    selected = [
        (path, file_date)
        for _, path, file_date in files
        if start_ts <= datetime.strptime(file_date, "%Y%m%d") <= end_ts
    ]
    if not selected:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    columns = ["code", "date", "pre_close", "pre_settle", "open", "high", "low", "close", "vol"]
    frames = [_read_parquet_safe(path, columns, "B", file_date, symbols) for path, file_date in selected]
    df = pd.concat(frames, ignore_index=True)
    daily = _standardize_daily(df, symbols)
    if daily.empty:
        return daily
    # 按解析后的 trade_date 再裁一次:防脏数据(文件内行日期与文件名不一致)越界
    daily = daily.loc[
        (daily["trade_date"] >= start_ts) & (daily["trade_date"] <= end_ts)
    ].reset_index(drop=True)
    return daily


def _standardize_daily(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    symbol_set = set(symbols)
    body = df["code"].map(_split_code_body)
    commodity = body.str.extract(r"^([A-Z]+)")[0]
    df = df.assign(contract=body, commodity=commodity)
    df = df.loc[
        df["commodity"].isin(symbol_set) & df["contract"].str.match(CONTRACT_CODE_PATTERN)
    ].copy()
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    df["trade_date"] = pd.to_datetime(df["date"], errors="coerce")
    for column in ["open", "high", "low", "close", "pre_close", "pre_settle", "vol"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=["trade_date", "open", "high", "low", "close"]).copy()
    if df.empty:
        return pd.DataFrame(columns=REQUIRED_DAILY_COLUMNS)
    df = df.sort_values(["commodity", "contract", "trade_date"]).reset_index(drop=True)
    df["pre_close"] = df["pre_close"].where(df["pre_close"].notna(), df["pre_settle"])
    previous_close = df.groupby(["commodity", "contract"])["close"].shift(1)
    df["pre_close"] = df["pre_close"].where(df["pre_close"].notna(), previous_close)
    df["volume"] = df["vol"]
    df = df.drop_duplicates(subset=["commodity", "contract", "trade_date"], keep="last")
    return df[REQUIRED_DAILY_COLUMNS].reset_index(drop=True)


def _build_contract_meta(
    daily_bar: pd.DataFrame,
    bounds: dict[str, tuple[pd.Timestamp, pd.Timestamp]],
) -> pd.DataFrame:
    if daily_bar.empty:
        return pd.DataFrame(columns=REQUIRED_META_COLUMNS)
    contracts = daily_bar[["commodity", "contract"]].drop_duplicates().reset_index(drop=True)
    bounds_rows = [
        {"contract": contract, "listed_date": low, "last_trade_date": high}
        for contract, (low, high) in bounds.items()
    ]
    bounds_df = pd.DataFrame(bounds_rows)
    out = contracts.merge(bounds_df, on="contract", how="left")
    out["delivery_month"] = out["contract"].astype(str).str.extract(r"(\d{3,4})$")
    return out[REQUIRED_META_COLUMNS].reset_index(drop=True)
```

> 说明(对 spec 的合理细化):commodity 提取用向量化 `str.extract`(Pass A/B 性能),不保留标量 `_extract_commodity`;`_expand_start_date` 与 `TRADING_LOOKBACK_BUFFER_DAYS` 按规格保留。

- [ ] **Step 4: 跑测试确认全绿**

Run:
```bash
./venv/bin/pytest tests/test_data_access.py -v
```
Expected: 15 passed。

- [ ] **Step 5: Commit**

```bash
git add src/daily_screen/data_access.py tests/test_data_access.py
git commit -m "feat: 数据源替换为本地 parquet,重写 data_access 与测试"
```

---

## Task 3: 更新 pipeline.py 签名 + 重写 smoke 测试

`analyze_commodities` 删除 `cache_dir`/`config_path`、新增 `data_dir` 并转发;smoke 测试用临时 parquet fixture 经 `data_dir` 注入,不依赖仓库真数据、不联网。

**Files:**
- Modify: `src/daily_screen/pipeline.py`
- Rewrite: `tests/test_pipeline_smoke.py`

- [ ] **Step 1: 改 pipeline.py 签名与 loader 调用**

在 `src/daily_screen/pipeline.py` 把 `analyze_commodities` 的形参块:
```python
    output_dir: str | None = None,
    *,
    cache_dir: str | Path = "data/csv_data/data",
    config_path: str | Path = "config/local_config.json",
) -> dict:
```
改为:
```python
    output_dir: str | None = None,
    *,
    data_dir: str | Path = "data/1d_futures",
) -> dict:
```

并把 `loaded = load_commodity_data(...)` 调用:
```python
    loaded = load_commodity_data(
        normalized_symbols,
        start_date,
        end_date,
        cache_dir=cache_dir,
        config_path=config_path,
    )
```
改为:
```python
    loaded = load_commodity_data(
        normalized_symbols,
        start_date,
        end_date,
        data_dir=data_dir,
    )
```
其余不动(`_normalize_symbols`/`_validate_dates`/`_resolve_output_dir` 保持)。

- [ ] **Step 2: 重写 tests/test_pipeline_smoke.py**

整文件覆盖为:
```python
from __future__ import annotations

import pandas as pd

from src.daily_screen.pipeline import analyze_commodities


def _row(code, date, close, *, vol=1000):
    return {
        "code": code,
        "date": date,
        "pre_close": close,
        "pre_settle": close,
        "open": close,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "vol": vol,
    }


def _build_fixture(tmp_path) -> str:
    # 数据跨度须远大于分析窗口,确保窗口内样本 lifecycle 合法:
    #   listed_days = trade_date - listed_date >= 10 且 days_to_last_trade >= 10
    # 窗口 20250115-20250128;数据 20241201-20250228(前有 ~6 周历史供 rolling,后留 ~1 个月避免 lifecycle_edge)
    dates: list[str] = []
    for month, year in ((12, 2024), (1, 2025), (2, 2025)):
        for day in range(1, 29):
            if day % 7 in (0, 6):  # 粗略跳周末
                continue
            dates.append(f"{year}{month:02d}{day:02d}")
    dates = sorted(set(dates))
    for idx, date in enumerate(dates):
        year = int(date[:4])
        year_dir = tmp_path / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        base = 100 + idx
        rows = [
            _row("A2505.DCE", date, base, vol=2000),
            _row("A2507.DCE", date, base + 1, vol=1500),
        ]
        pd.DataFrame(rows).to_parquet(year_dir / f"{date}.parquet")
    return str(tmp_path)


def test_pipeline_runs_on_local_parquet_fixture(tmp_path):
    data_dir = _build_fixture(tmp_path / "data")
    output_dir = tmp_path / "out"
    result = analyze_commodities(
        symbols=["A"],
        start_date="20250115",
        end_date="20250228",
        output_dir=str(output_dir),
        data_dir=data_dir,
    )
    assert (output_dir / "analysis_report.html").exists()
    assert (output_dir / "suspicious_dates.csv").exists()
    assert (output_dir / "summary.json").exists()
    assert "html_report_path" in result
    samples = pd.read_csv(output_dir / "all_samples.csv")
    # 锁住"fixture 真的产生有效样本",而不是跑一条全无效的通路
    assert (samples["sample_status"] == "valid").any(), "fixture 应至少产生一个有效样本"
    # 锁住正常主路径:有效样本确实被评分过、结果列齐备
    # (防止 scoring/result_builder 回归导致 candidate_score 列缺失或全空,而 suspicious_dates 永远空却仍判绿)
    assert "candidate_score" in samples.columns and "candidate_level" in samples.columns
    valid_scored = samples.loc[
        (samples["sample_status"] == "valid") & samples["candidate_score"].notna()
    ]
    assert len(valid_scored) > 0, "应有有效样本被评分(candidate_score 非空)"
```

- [ ] **Step 3: 跑 smoke 测试**

Run:
```bash
./venv/bin/pytest tests/test_pipeline_smoke.py -v
```
Expected: 1 passed。若因 fixture 日期/活跃合约不足报错,把 `start_date` 前移或 `dates` 数量加到 20+。

- [ ] **Step 4: Commit**

```bash
git add src/daily_screen/pipeline.py tests/test_pipeline_smoke.py
git commit -m "feat: pipeline 转发 data_dir,smoke 测试改用本地 parquet fixture"
```

---

## Task 4: 删除失效入口与旧测试脚手架 + 全量回归

清理失效入口与 conftest 旧 fixture,确认无残留引用,跑全量测试。

**Files:**
- Delete: `build_contract_sets.py`、`tests/conftest.py`

- [ ] **Step 1: 删除 build_contract_sets.py**

Run:
```bash
git rm build_contract_sets.py
```

- [ ] **Step 2: 删除 tests/conftest.py**

`tests/conftest.py` 里仅有的三个 fixture(`cache_dir`/`config_path`/`sample_contract_cache`)只被 `test_data_access.py`、`test_pipeline_smoke.py` 使用,而这两个文件已在 Task 2/3 重写不再引用它们。重写后该文件全是死代码,整文件删除。

先二次确认无其他引用:
```bash
grep -rnE "cache_dir|config_path|sample_contract_cache" tests/ --include="*.py" | grep -v "conftest.py"
```
Expected: 无输出(说明只剩 conftest.py 自己)。然后:
```bash
git rm tests/conftest.py
```

- [ ] **Step 3: grep 残留引用(大小写不敏感,覆盖别名,排除既定不动的文件)**

```bash
grep -rniE "tushare|akshare|csv_data|cache_dir|config_path|load_local_config|_fetch_|_load_symbol_meta|EXCHANGE_BY_SYMBOL|DCE_REALTIME|contract_set|build_contract_sets|补缓存|增量补|tushare_token" \
  src/ tests/ run_daily_screen.py 2>/dev/null || echo "no matches"
```
Expected: `no matches`。说明:
- 旧独立脚本(`cache_manager.py`、`fat_finger_detector.py`、`fat_finger_example.py`、`fat_finger_annual_summary.py`、`analyze_top5_varieties.py`、`main.py`)按 spec 非目标**不在本任务范围**,故未纳入 grep 路径;它们残留旧说法属预期。
- 若在 `src/`/`tests/`/`run_daily_screen.py` 命中真实代码引用,逐个修正。

- [ ] **Step 4: 跑全量测试**

Run:
```bash
./venv/bin/pytest tests/ -v
```
Expected: 全绿。典型:`test_data_access.py`(15)、`test_pipeline_smoke.py`(1)、`test_scoring.py`/`test_sample_filter.py`/`test_reference_selection.py`/`test_result_builder.py`/`test_report_html.py`/`test_input_model.py` 保持原状全绿(它们不依赖数据源,也不再用 conftest 的旧 fixture)。

- [ ] **Step 5: Commit**

`git rm` 已把两个删除暂存,直接提交即可。**不要用 `git add -A`**——仓库可能有其他未提交改动,会被一并带入。
```bash
git commit -m "chore: 删除失效的 build_contract_sets 入口与旧测试 fixture"
```

---

## Task 5: 文档同步

把描述旧「Tushare/AKShare/csv_data 缓存/补缺口」模型的活文档改成「本地 parquet」。范围:README、CLAUDE、AGENTS、handover、`docs/new/` 下的活设计稿。

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`
- Modify: `docs/daily_screen_code_handover.md`
- Modify: `docs/new/主设计稿.md`、`docs/new/代码梳理文档.md`、`docs/new/交接说明.md`、`docs/new/评分设计文档.md`(凡是含旧数据源/缓存说法的段落)

**明确不在范围内**(避免半完成误判):
- 旧独立脚本:`cache_manager.py`、`fat_finger_detector.py`、`fat_finger_example.py`、`fat_finger_annual_summary.py`、`analyze_top5_varieties.py`、`main.py`(spec 非目标)。
- 历史设计记录:`docs/superpowers/specs/2026-06-02-*`、`docs/superpowers/plans/2026-06-02-*`(旧实现的设计文档,记录旧系统设计,已被本次 spec 取代,不改动)。
- `docs/new/run_daily_screen.sh`:若是纯调用 `run_daily_screen.py` 的脚本(CLI 未变),无需改;若含旧缓存目录参数则同步。

- [ ] **Step 1: README.md**

定位「数据源」「缓存策略」相关段落(约第 66/78-80/85/89-99/164/184/204 行)。把「Tushare 主源 / AKShare 回退 / 缓存目录 data/csv_data/data / 增量补拉 / .empty 标记」整段替换为:
```markdown
### 数据源

本地 parquet 数据集,目录 `data/1d_futures/`,布局 `{年份}/{YYYYMMDD}.parquet`,每个文件为某交易日全市场合约。无在线拉取,无缓存补缺口。

字段:`code`(带交易所后缀,如 `A2501.DCE`)、`date`、`pre_close`、`pre_settle`、`open`、`high`、`low`、`close`、`settle`、`vol` 等。loader 解析 `code` 得品种/合约,滤除连续合约(无月份后缀,如 `A.DCE`)。

依赖:需 `pyarrow`(见 `requirements.txt`)。

### 日期区间

`start_date` 向前扩 45 自然日(`TRADING_LOOKBACK_BUFFER_DAYS`)作为历史缓冲,供滚动分位数使用。合约上市/到期日取自 parquet 全历史(不受分析窗口截断)。
```
同步把流程简介(第 66 行)`拉数据和补缓存` 改为 `读本地 parquet`;附录里 `自动拉取日线数据并做缓存增量补数` 改为 `从本地 parquet 读取日线`。

- [ ] **Step 2: CLAUDE.md**

把 `## 架构` 下 `### 数据源` 与 `### 缓存策略` 两段替换为与上文 README 同样的「本地 parquet」描述;调用链注释里 `# 拉数据、缓存管理` 改为 `# 读本地 parquet`。

- [ ] **Step 3: AGENTS.md**

AGENTS.md 是 CLAUDE.md 的副本,同步 Step 2 的改动(两文件该段保持一致)。

- [ ] **Step 4: docs/daily_screen_code_handover.md(整段重写 data_access 章节)**

该文档 §3.2 的 `analyze_commodities` 旧签名(约第 88 行):
```markdown
- `analyze_commodities(symbols, start_date, end_date, output_dir=None, cache_dir="data/csv_data/data", config_path="config/local_config.json")`
```
改为:
```markdown
- `analyze_commodities(symbols, start_date, end_date, output_dir=None, data_dir="data/1d_futures")`
```

该文档第 4 章「数据获取层」(约 105 行起到该章结束)**整段替换**为下面的新内容。原因:旧第 4 章是 `load_local_config`/`tushare_token`/`_load_symbol_meta`/`_fetch_symbol_meta_from_tushare`/`_fetch_symbol_meta_from_akshare`/`_discover_contracts_for_symbol`/`_load_contract_daily_with_cache` 等已删除函数的完整调用链讲解,只换几句口径会留下"局部改新、整体仍误导"的旧架构事实。

```markdown
## 4. 数据获取层：从本地 parquet 读取

### 4.1 核心文件

文件：
- [data_access.py](src/daily_screen/data_access.py)

职责（纯本地读取，无在线拉取、无缓存补缺口、无空标记）：
- 扫描本地 parquet 数据集
- 解析合约代码、过滤目标品种、滤除连续合约
- 构造 `daily_bar` 与 `contract_meta`

### 4.2 数据源

本地 parquet 数据集，目录 `data/1d_futures/`，布局 `{年份}/{YYYYMMDD}.parquet`，每个文件为某交易日全市场合约。依赖 `pyarrow`（见 `requirements.txt`）。

字段（节选）：`code`（带交易所后缀，如 `A2501.DCE`）、`date`、`pre_close`、`pre_settle`、`open`、`high`、`low`、`close`、`vol`。

`code` 解析：去后缀得 contract（`A2501`）、正则 `^[A-Z]+` 得 commodity（`A`）；仅保留 `[A-Z]+\d{3,4}` 的具体月合约，连续合约（`A.DCE`/`AG.SHF`，无月份后缀）自动滤除。

### 4.3 为什么会向前扩窗

函数 `_expand_start_date(start_date)`，常量 `TRADING_LOOKBACK_BUFFER_DAYS = 45`。用户分析 `[start_date, end_date]`，取数时向前扩 45 自然日作为缓冲，供样本状态判定与评分的滚动窗口使用（避免分析起点被判历史不足）。

### 4.4 主函数与两遍扫描

主函数：
- `load_commodity_data(symbols, start_date, end_date, *, data_dir="data/1d_futures")`

返回 `{daily_bar, contract_meta}`。两遍扫描：

**Pass A —— 全历史 meta 边界**（`_scan_contract_date_bounds`）：扫描 `data_dir` 下全部年份，只读 `[code, date]` 两列，流式聚合每合约全局 `min`/`max(trade_date)`。保证 `listed_date`/`last_trade_date` 是合约在数据集内的真实首/末日，不受分析窗口截断——否则下游 `sample_filter` 的 `invalid_lifecycle_edge` 会被窗口边界误伤。

**Pass B —— 窗口 daily_bar**（`_build_daily_bar` + `_standardize_daily`）：仅读文件名日期落在 `[expanded_start_date, end_date]` 的 parquet，取 OHLC 必要列；解析 `code`、过滤品种、映射列类型；按 `[commodity, contract, trade_date]` 去重；最后按解析后的 `trade_date` 再裁一次（防文件内行日期与文件名不一致的脏数据越界）。

### 4.5 pre_close 容错链

`pre_close ?? pre_settle ?? 前一日 close`，其中"前一日 close"按合约分组取：`df.groupby(["commodity","contract"])["close"].shift(1)`（杜绝跨合约污染）。parquet 实测 `pre_close` 基本都有值，此链为防御性兜底，避免制造 `pre_close<=0` 的无效样本。

### 4.6 contract_meta 组装

`_build_contract_meta`：对 daily_bar 中出现的每个合约，从 Pass A 的全历史边界取 `listed_date`/`last_trade_date`；`delivery_month` 由合约代码末尾数字提取。

### 4.7 失败处理

按场景抛清晰错误（不让 pyarrow 底层异常裸抛）：
- `data_dir` 不存在 → `FileNotFoundError`（带路径）
- `data_dir` 是文件不是目录 → `NotADirectoryError`
- 年份目录名非 4 位数字 → `ValueError`（带目录名）
- `{year}/` 下 `.parquet` 文件名非 `YYYYMMDD.parquet` → `ValueError`（带 file_path）
- 单个 parquet 内容读不出 → `RuntimeError`，带 `file_path`/`pass`(A/B)/`file_date`，Pass B 阶段额外带 `symbols`
```

文档其余章节(§5 评分、§6 输出等)**不动**。

另外,第 1 章「文档目的」开头仍有 3 个旧口径 bullet(描述拉取/降级/缓存补缺,与新架构冲突,会和完成标准"活文档无旧说法"打架),同步替换。把:
```markdown
- 数据从哪里拉取
- 拉取失败时如何重试和降级
- 本地缓存怎么命中、补缺、回写
```
改为:
```markdown
- 数据从本地 parquet 哪里读取
- 读取失败时如何报错
- 本地 parquet 数据如何进入分析链路
```

- [ ] **Step 5: docs/new/ 活设计稿(分两档处理)**

**档一:`docs/new/代码梳理文档.md` —— 整段重写 data_access 章节。** 该文档与 handover 同构,数据获取层章节同样是旧调用链完整讲解(`_load_symbol_meta`/`_fetch_*`/`_discover_contracts_for_symbol`/`_load_contract_daily_with_cache` 等)。把该章节**整段替换**为 Step 4 给出的新 §4 内容(措辞一致),不要只换几句。**并同步第 1 章「文档目的」的 3 个旧 bullet**(数据从哪里拉取 / 拉取失败时如何重试和降级 / 本地缓存怎么命中、补缺、回写)改为 Step 4 给出的新口径。其余章节(评分、输出)不动。

**档二:`docs/new/主设计稿.md`、`docs/new/交接说明.md`、`docs/new/评分设计文档.md` —— 段落级。** 只把「Tushare 主源 / AKShare 回退 / csv_data 缓存 / 增量补拉」段落替换为「本地 parquet」说明(措辞与 README 一致);若无实质数据源段落、只在标题/目录引用旧词,加一行 banner 即可:
```markdown
> 数据源已于 2026-07 迁移为本地 parquet,详见 `docs/superpowers/specs/2026-07-02-local-parquet-data-source-design.md`。
```

**严格限定(两档共用)**:只改「数据源 / 缓存 / 拉数 / data_access 调用链」相关内容;**不要改**纯评分逻辑(A/C/E 公式、阈值)、示例数据表、与数据源无关的章节。目的是消除数据源与旧调用链的误导,不是把设计稿重写一遍。

- [ ] **Step 6: 跑全量测试再确认绿**

Run:
```bash
./venv/bin/pytest tests/ -q
```
Expected: 全绿(文档改动不应影响测试,作收尾确认)。

- [ ] **Step 7: Commit**

**只暂存本任务实际改动的文件,逐个列名;不用 `git add -A`、不用目录通配 `docs/new/`**(仓库可能有其他未提交改动)。`docs/new/` 下若某文件实际无需改,从下面的列表里去掉它,不要为了凑列表硬改。
```bash
git add README.md CLAUDE.md AGENTS.md docs/daily_screen_code_handover.md \
  docs/new/主设计稿.md docs/new/代码梳理文档.md docs/new/交接说明.md docs/new/评分设计文档.md
git commit -m "docs: 数据源说明同步为本地 parquet"
```

---

## 完成标准

- `./venv/bin/pytest tests/` 全绿。
- 主链路无旧符号:`src/`、`tests/`、`run_daily_screen.py` 中 grep(大小写不敏感)`tushare|akshare|csv_data|cache_dir|config_path|contract_set|_fetch_|补缓存|增量补` 无命中。
- `run_daily_screen.py --symbols A --start-date 20250101 --end-date 20250301` 能基于 `data/1d_futures/` 跑通并产出 HTML(手动抽验一次)。
- **活文档无旧说法**:README、CLAUDE、AGENTS、`docs/daily_screen_code_handover.md`、`docs/new/` 下活设计稿均描述本地 parquet。
- **明确允许残留旧说法**(不在验收范围):旧独立脚本(`cache_manager.py`、`fat_finger_*.py`、`analyze_top5_varieties.py`、`main.py`)、历史设计记录(`docs/superpowers/{specs,plans}/2026-06-02-*`)。
