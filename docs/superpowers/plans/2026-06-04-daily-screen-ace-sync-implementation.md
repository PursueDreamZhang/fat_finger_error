# Daily Screen A/C/E Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 `src/daily_screen` 的实现从旧的 `A/B/C/D/E + guard` 体系，收缩并同步到当前已经确认的 `A/C/E` 评分设计文档与主设计稿。

**Architecture:** 本次不是增量加功能，而是收缩和对齐。先固定状态语义和结果输出，再重写 `scoring.py` 的主逻辑，随后移除 `guards.py` 在主流程中的角色，最后收紧结果构建、HTML 报告和端到端测试。整个实现以评分设计文档为唯一评分真相源，以主设计稿为流程和输出真相源。

**Tech Stack:** Python、pandas、pathlib、pytest

---

## Scope

本计划只覆盖“代码同步到最新设计文档”的工作，不扩展新数据源能力，不新增 UI 特性，不调整缓存总策略。

本计划假设以下文档已经被确认：

- `docs/superpowers/specs/2026-06-02-fat-finger-daily-screen-design.md`
- `docs/superpowers/specs/2026-06-02-fat-finger-scoring-design.md`

---

## Files

### 主要修改

- Modify: `src/daily_screen/scoring.py`
- Modify: `src/daily_screen/pipeline.py`
- Modify: `src/daily_screen/result_builder.py`
- Modify: `src/daily_screen/report_html.py`
- Modify: `src/daily_screen/sample_filter.py`
- Modify: `src/daily_screen/reference_selection.py`

### 预计删除或退出主流程

- Modify or Delete: `src/daily_screen/guards.py`

### 主要测试

- Modify: `tests/test_scoring.py`
- Modify or Delete: `tests/test_guards.py`
- Modify: `tests/test_result_builder.py`
- Modify: `tests/test_report_html.py`
- Modify: `tests/test_pipeline_smoke.py`
- Modify: `tests/test_sample_filter.py`

### 可选同步

- Modify: `README.md`

---

### Task 1: 固定状态语义和结果保留语义

**Files:**
- Modify: `src/daily_screen/sample_filter.py`
- Modify: `src/daily_screen/pipeline.py`
- Test: `tests/test_sample_filter.py`
- Test: `tests/test_pipeline_smoke.py`

- [ ] **Step 1: 先写失败测试，锁定“无效样本保留但不形成正式候选”的语义**

```python
def test_invalid_samples_are_retained_with_status(contract_meta_df, make_daily_bar):
    daily_bar = make_daily_bar(volume=0)

    filtered = assign_sample_status(daily_bar, contract_meta_df)

    assert len(filtered) == 1
    assert filtered.loc[0, "sample_status"] == "invalid_low_liquidity"
```

```python
def test_pipeline_keeps_invalid_rows_for_reporting(smoke_request):
    result = analyze_commodities(**smoke_request)

    assert "all_samples" in result
    assert "sample_status" in result["all_samples"].columns
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `venv/bin/python -m pytest tests/test_sample_filter.py tests/test_pipeline_smoke.py -v`
Expected: FAIL，因为当前主流程还没有明确保留 `all_samples` 或测试断言不成立

- [ ] **Step 3: 最小实现状态保留语义**

```python
# src/daily_screen/pipeline.py
return {
    "html_report_path": str(html_path),
    "suspicious_dates": built["suspicious_dates"],
    "commodity_summary": built["commodity_summary"],
    "all_samples": scored,
}
```

实现要求：

- `sample_filter.py` 继续只负责标记 `sample_status`
- `pipeline.py` 不在过滤阶段删除无效行
- 后续评分阶段再把无效样本统一压成 `A=0, C=0, E=-20, candidate_score=0`
- 无效样本统一只保留在 `all_samples`
- `suspicious_dates` 只保留 `candidate_level != "none"` 的正式候选
- HTML 主表默认展示正式候选
- 若需要展示“无法判断/数据不足”样本，放在详情区或附加区块，不与正式候选主表混排

- [ ] **Step 4: 运行测试，确认通过**

Run: `venv/bin/python -m pytest tests/test_sample_filter.py tests/test_pipeline_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/daily_screen/sample_filter.py src/daily_screen/pipeline.py tests/test_sample_filter.py tests/test_pipeline_smoke.py
git commit -m "refactor: retain invalid samples for reporting"
```

---

### Task 2: 先重写评分测试到 A/C/E 口径

**Files:**
- Modify: `tests/test_scoring.py`
- Modify: `tests/test_result_builder.py`

- [ ] **Step 1: 先删掉旧的 A/B/C/D/E 断言，改成 A/C/E 断言**

```python
def test_scoring_outputs_ace_columns(valid_scoring_input_df):
    scored = score_candidates(valid_scoring_input_df)

    assert {"A_score", "C_score", "E_score", "candidate_score", "candidate_level"} <= set(scored.columns)
    assert "B_score" not in scored.columns
    assert "D_score" not in scored.columns
```

```python
def test_invalid_sample_is_forced_to_zero_score(invalid_scoring_input_df):
    scored = score_candidates(invalid_scoring_input_df)

    row = scored.iloc[0]
    assert row["A_score"] == 0
    assert row["C_score"] == 0
    assert row["E_score"] == -20
    assert row["candidate_score"] == 0
    assert row["candidate_level"] == "none"
```

```python
def test_single_peer_sample_is_scored_but_penalized(single_peer_input_df):
    scored = score_candidates(single_peer_input_df)

    row = scored.iloc[0]
    assert row["active_peer_count"] == 1
    assert row["E_score"] == -10
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `venv/bin/python -m pytest tests/test_scoring.py tests/test_result_builder.py -v`
Expected: FAIL，因为当前实现仍输出 `B_score` / `D_score` 且结果构建仍依赖旧字段

- [ ] **Step 3: 把结果构建测试也同步到新字段**

```python
def test_build_results_uses_ace_fields():
    scored_df = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2606"],
            "trade_date": ["2026-05-20"],
            "candidate_score": [68.0],
            "candidate_level": ["high"],
            "A_score": [18.0],
            "C_score": [55.0],
            "E_score": [-5.0],
            "sample_status": ["valid"],
        }
    )

    result = build_results(scored_df, ["AU"], "20260101", "20260601")

    assert "A_score" in result["suspicious_dates"].columns
    assert "C_score" in result["suspicious_dates"].columns
    assert "E_score" in result["suspicious_dates"].columns
```

- [ ] **Step 4: 再运行测试，保持失败但断言口径已切换**

Run: `venv/bin/python -m pytest tests/test_scoring.py tests/test_result_builder.py -v`
Expected: FAIL，但失败点应集中在实现未同步，而不是测试仍在引用旧分项

- [ ] **Step 5: Commit**

```bash
git add tests/test_scoring.py tests/test_result_builder.py
git commit -m "test: rewrite scoring expectations to ace model"
```

---

### Task 3: 重写 scoring.py 为 A/C/E 体系

**Files:**
- Modify: `src/daily_screen/scoring.py`
- Test: `tests/test_scoring.py`

- [ ] **Step 1: 写最小实现骨架，先删除 B/D 产物**

```python
def score_candidates(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        for column in ["A_score", "C_score", "E_score", "candidate_score", "candidate_level"]:
            out[column] = pd.Series(dtype="float64")
        return out
```

实现要求：

- 删除 `B_score` / `D_score`
- 不再做 `A+B` 封顶
- `candidate_level` 在 `scoring.py` 内直接生成

- [ ] **Step 2: 实现活跃 peer 集合和 active_peer_count**

```python
def _build_active_peer_sets(df: pd.DataFrame) -> pd.DataFrame:
    # 1. sample_status == valid
    # 2. volume > 0
    # 3. 同品种同日按 volume 排序取前 5
    # 4. 目标合约自己的 peer 不包含自己
    return enriched_df
```

实现要求：

- 生成 `active_peer_count`
- 生成 `peer_high_median`
- 生成 `peer_low_median`
- 生成 `peer_volume_median`
- 这套 `active peer` 逻辑完全属于评分体系内部
- 不放进 `reference_selection.py`
- `reference_selection.py` 继续只负责主参考/邻近参考，服务辅助解释和 HTML 展示

- [ ] **Step 3: 实现 A 分**

```python
out["range_pct"] = (out["high"] - out["low"]) / out["close"].replace(0, np.nan)
out["extreme_pct"] = np.maximum(
    (out["high"] - out["close"]).clip(lower=0),
    (out["close"] - out["low"]).clip(lower=0),
) / out["close"].replace(0, np.nan)
```

实现要求：

- `A1` 使用 `range_pct` 历史分位数
- `A2` 使用 `extreme_pct` 历史分位数
- `A = A1 + A2`

- [ ] **Step 4: 实现 C 分**

```python
out["upper_residual"] = ((out["high"] - out["peer_high_median"]) / out["close"].replace(0, np.nan)).clip(lower=0)
out["lower_residual"] = ((out["peer_low_median"] - out["low"]) / out["close"].replace(0, np.nan)).clip(lower=0)
out["structure_residual"] = pd.concat(
    [out["upper_residual"], out["lower_residual"]],
    axis=1,
).max(axis=1, skipna=True)
```

实现要求：

- `C1` 使用 `structure_residual` 的历史分位数
- `C2` 使用 `uniqueness_gap` 的历史分位数
- `C2` 的历史样本必须要求 `active_peer_count >= 2`

- [ ] **Step 5: 实现 E 分和无效样本统一处理**

```python
invalid_mask = (out["sample_status"] != "valid") | (out["active_peer_count"] == 0)
out.loc[invalid_mask, ["A_score", "C_score"]] = 0.0
out.loc[invalid_mask, "E_score"] = -20.0
```

实现要求：

- `peer_comparability_weak_flag`
- `target_liquidity_weak_flag`
- `active_peer_count == 1` 时允许打分但 `E=-10`
- `active_peer_count == 0` 时直接 `candidate_score=0`

- [ ] **Step 5.1: 先补两个关键弱化标志的失败测试**

```python
def test_marks_peer_comparability_weak_when_peer_count_is_below_recent_norm(peer_comparability_input_df):
    scored = score_candidates(peer_comparability_input_df)

    row = scored.iloc[-1]
    assert row["peer_comparability_weak_flag"] is True
    assert row["E_score"] == -10
```

```python
def test_marks_target_liquidity_weak_when_volume_is_far_below_peer_median(target_liquidity_input_df):
    scored = score_candidates(target_liquidity_input_df)

    row = scored.iloc[-1]
    assert row["target_liquidity_weak_flag"] is True
    assert row["E_score"] == -10
```

```python
def test_single_peer_sample_remains_scoreable_but_zero_peer_is_invalid(single_peer_and_zero_peer_input_df):
    scored = score_candidates(single_peer_and_zero_peer_input_df)

    one_peer = scored.iloc[0]
    zero_peer = scored.iloc[1]

    assert one_peer["active_peer_count"] == 1
    assert one_peer["candidate_score"] >= 0
    assert zero_peer["active_peer_count"] == 0
    assert zero_peer["candidate_score"] == 0
```

- [ ] **Step 6: 实现 candidate_level**

```python
score = out["candidate_score"]
levels = pd.Series("none", index=out.index, dtype="object")
levels.loc[(score >= 30) & (score < 50)] = "low"
levels.loc[(score >= 50) & (score < 65)] = "medium"
levels.loc[score >= 65] = "high"
levels.loc[out["C_score"] < 20] = levels.where(levels == "none", "low")
levels.loc[(score >= 65) & (out["C_score"] < 35)] = "medium"
out["candidate_level"] = levels
```

- [ ] **Step 7: 运行评分测试**

Run: `venv/bin/python -m pytest tests/test_scoring.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/daily_screen/scoring.py src/daily_screen/reference_selection.py tests/test_scoring.py
git commit -m "refactor: rewrite scoring to ace model"
```

---

### Task 4: 让 guards.py 退出第一版主流程

**Files:**
- Modify: `src/daily_screen/pipeline.py`
- Delete or Deprecate: `src/daily_screen/guards.py`
- Modify or Delete: `tests/test_guards.py`
- Modify: `tests/test_pipeline_smoke.py`

- [ ] **Step 1: 先写失败测试，锁定“pipeline 输出不再依赖 guard 结果字段”**

```python
def test_pipeline_output_has_no_guard_fields(smoke_request):
    result = analyze_commodities(**smoke_request)

    assert "blocked_from_high" not in result["all_samples"].columns
    assert "guarded_medium" not in set(result["all_samples"]["candidate_level"].astype(str))
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `venv/bin/python -m pytest tests/test_pipeline_smoke.py tests/test_guards.py -v`
Expected: FAIL

- [ ] **Step 3: 从 pipeline 中删除 guards 调用**

```python
scored = score_candidates(filtered)
built = build_results(scored, normalized_symbols, start_date, end_date)
```

实现要求：

- 移除 `apply_guards` import
- 若 `guards.py` 第一版无保留价值，直接删除文件和测试
- 若暂时保留文件，只允许保留注释或兼容包装，不再进入主流程

- [ ] **Step 4: 运行测试**

Run: `venv/bin/python -m pytest tests/test_pipeline_smoke.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/daily_screen/pipeline.py src/daily_screen/guards.py tests/test_pipeline_smoke.py tests/test_guards.py
git commit -m "refactor: remove guards from first version pipeline"
```

---

### Task 5: 重写结果构建和 HTML 报告字段

**Files:**
- Modify: `src/daily_screen/result_builder.py`
- Modify: `src/daily_screen/report_html.py`
- Test: `tests/test_result_builder.py`
- Test: `tests/test_report_html.py`

- [ ] **Step 1: 先写失败测试，锁定详情展示字段**

```python
def test_result_builder_keeps_sample_status_and_reasons():
    scored_df = pd.DataFrame(
        {
            "commodity": ["AU"],
            "contract": ["AU2606"],
            "trade_date": ["2026-05-20"],
            "candidate_score": [68.0],
            "candidate_level": ["high"],
            "A_score": [18.0],
            "C_score": [55.0],
            "E_score": [-5.0],
            "sample_status": ["valid"],
        }
    )

    result = build_results(scored_df, ["AU"], "20260101", "20260601")

    row = result["suspicious_dates"].iloc[0]
    assert "sample_status" in row.index
```

```python
def test_html_report_renders_without_guard_fields(report_payload):
    html = render_report_html(report_payload)
    assert "sample_status" in html
    assert "candidate_level" in html
```

- [ ] **Step 2: 运行测试，确认先失败**

Run: `venv/bin/python -m pytest tests/test_result_builder.py tests/test_report_html.py -v`
Expected: FAIL

- [ ] **Step 3: 重写 result_builder 的 trigger_reasons**

```python
def _build_trigger_reasons(row: pd.Series) -> list[str]:
    reasons: list[str] = []
    if row.get("A_score", 0) >= 8:
        reasons.append("自身极值异常")
    if row.get("C_score", 0) >= 24:
        reasons.append("同品种结构异常")
    if row.get("E_score", 0) <= -10:
        reasons.append("可信度下降")
    if row.get("sample_status") != "valid":
        reasons.append(f"样本状态: {row.get('sample_status')}")
    return reasons
```

实现要求：

- 去掉旧的 `B_score` / `weak_peer_reference_flag` 解释
- HTML 详情区以 `sample_status` 和 `trigger_reasons` 为主
- 不再依赖 guard 字段

- [ ] **Step 4: 运行测试**

Run: `venv/bin/python -m pytest tests/test_result_builder.py tests/test_report_html.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/daily_screen/result_builder.py src/daily_screen/report_html.py tests/test_result_builder.py tests/test_report_html.py
git commit -m "refactor: align result output with ace scoring"
```

---

### Task 6: 端到端回归和清理

**Files:**
- Modify: `tests/test_pipeline_smoke.py`
- Modify: `README.md`

- [ ] **Step 1: 更新 smoke 测试口径**

```python
def test_pipeline_returns_html_and_scored_candidates(smoke_request):
    result = analyze_commodities(**smoke_request)

    assert "html_report_path" in result
    assert "suspicious_dates" in result
    assert "commodity_summary" in result
    assert "all_samples" in result
```

- [ ] **Step 2: 跑完整测试集**

Run: `venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 3: 更新 README 的评分描述**

```markdown
当前第一版评分体系已经收缩为 `A/C/E`：

- `A` 自身极值异常
- `C` 同品种结构极值异常
- `E` 可信度惩罚
```

- [ ] **Step 4: 再跑一次完整测试集**

Run: `venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_pipeline_smoke.py README.md
git commit -m "docs: sync readme with ace scoring model"
```

---

## Self-Review

### Spec coverage

- 主设计稿中的“样本保留但标记无效”由 Task 1 覆盖
- 评分文档中的 `A/C/E`、`active_peer_contracts`、`target_liquidity_weak_flag`、无效样本规则由 Task 2 和 Task 3 覆盖
- 第一版不单独引入 `guard` 机制由 Task 4 覆盖
- HTML 详情区改为展示 `sample_status` 和 trigger reasons 由 Task 5 覆盖
- README 文案同步由 Task 6 覆盖

### Placeholder scan

- 无 `TODO` / `TBD`
- 所有测试步骤都给出了最小断言
- 所有执行命令都给出了明确命令和预期

### Type consistency

- 统一以 `A_score / C_score / E_score / candidate_score / candidate_level` 为结果字段
- 不再计划维护 `B_score / D_score / blocked_from_high / guarded_medium`

---

Plan complete and saved to `docs/superpowers/plans/2026-06-04-daily-screen-ace-sync-implementation.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
