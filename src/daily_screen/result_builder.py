from __future__ import annotations

import pandas as pd

ACTIVE_CONTRACT_LIMIT = 5


def build_results(df: pd.DataFrame, symbols: list[str], start_date: str, end_date: str) -> dict[str, object]:
    out = df.copy()
    suspicious_detail_columns = [
        "detail_id",
        "commodity",
        "contract",
        "trade_date",
        "candidate_score",
        "candidate_level",
        "sample_status",
        "trigger_reasons",
        "main_reference_contract",
        "A_score",
        "A1_score",
        "A2_score",
        "C_score",
        "C1_score",
        "C2_score",
        "E_score",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "volume",
        "range_pct",
        "extreme_pct",
        "range_q90",
        "range_q95",
        "range_q99",
        "extreme_q90",
        "extreme_q95",
        "extreme_q99",
        "raw_structure_residual",
        "excess_structure_residual",
        "normalized_structure_residual",
        "structure_q90",
        "structure_q95",
        "structure_q99",
        "raw_uniqueness_gap",
        "uniqueness_gap",
        "uniqueness_q90",
        "uniqueness_q95",
        "uniqueness_q99",
        "active_peer_count",
        "peer_high_median",
        "peer_low_median",
        "peer_volume_median",
        "peer_range_median",
        "peer_comparability_weak_flag",
        "target_liquidity_weak_flag",
    ]
    if out.empty:
        suspicious_dates = pd.DataFrame(
            columns=[
                "detail_id",
                "commodity",
                "contract",
                "trade_date",
                "candidate_score",
                "candidate_level",
                "trigger_reasons",
                "main_reference_contract",
            ]
        )
        all_samples = pd.DataFrame(
            columns=[
                "detail_id",
                "commodity",
                "contract",
                "trade_date",
                "candidate_score",
                "candidate_level",
                "sample_status",
                "trigger_reasons",
                "main_reference_contract",
            ]
        )
        commodity_summary = pd.DataFrame(columns=["commodity", "candidate_count", "contract_count", "max_candidate_score"])
    else:
        start_ts = pd.to_datetime(start_date, format="%Y%m%d")
        end_ts = pd.to_datetime(end_date, format="%Y%m%d")
        out["trade_date"] = pd.to_datetime(out["trade_date"])
        out = out.loc[(out["trade_date"] >= start_ts) & (out["trade_date"] <= end_ts)].copy()
        out["detail_id"] = (
            out["commodity"].astype(str)
            + "|"
            + out["contract"].astype(str)
            + "|"
            + out["trade_date"].dt.strftime("%Y-%m-%d")
        )
        out["trigger_reasons"] = out.apply(_build_trigger_reason, axis=1)
        peer_rows_by_detail_id = _build_peer_rows_map(out)
        all_samples = out.loc[:, [
            "detail_id",
            "commodity",
            "contract",
            "trade_date",
            "candidate_score",
            "candidate_level",
            "sample_status",
            "trigger_reasons",
            "main_reference_contract",
        ]].copy()
        all_samples = all_samples.sort_values(
            ["commodity", "trade_date", "candidate_score"],
            ascending=[True, True, False],
        ).reset_index(drop=True)
        volume_pass = (
            (out["candidate_level"] != "none")
            & (
                out["peer_volume_median"].isna()
                | (out["volume"] >= 0.1 * out["peer_volume_median"])
            )
        )
        suspicious_dates = out.loc[volume_pass, [
            "detail_id",
            "commodity",
            "contract",
            "trade_date",
            "candidate_score",
            "candidate_level",
            "trigger_reasons",
            "main_reference_contract",
        ]].copy()
        suspicious_dates = suspicious_dates.sort_values(["commodity", "trade_date", "candidate_score"], ascending=[True, True, False]).reset_index(drop=True)

        if suspicious_dates.empty:
            commodity_summary = pd.DataFrame(columns=["commodity", "candidate_count", "contract_count", "max_candidate_score"])
        else:
            commodity_summary = (
                suspicious_dates.groupby("commodity", as_index=False)
                .agg(
                    candidate_count=("trade_date", "count"),
                    contract_count=("contract", "nunique"),
                    max_candidate_score=("candidate_score", "max"),
                )
                .sort_values("candidate_count", ascending=False)
                .reset_index(drop=True)
            )

    overview = {
        "symbols": symbols,
        "start_date": start_date,
        "end_date": end_date,
        "candidate_count": int(len(suspicious_dates)),
        "invalid_sample_count": int((all_samples["sample_status"] != "valid").sum()) if not all_samples.empty else 0,
    }
    detail_records = (
        out
        .reindex(columns=suspicious_detail_columns)
        .sort_values(["candidate_score", "commodity", "trade_date"], ascending=[False, True, True])
        .to_dict(orient="records")
        if not out.empty
        else []
    )
    if detail_records:
        for record in detail_records:
            record["peer_rows"] = peer_rows_by_detail_id.get(record["detail_id"], [])
    invalid_samples = (
        out.loc[out["sample_status"] != "valid"]
        .reindex(columns=suspicious_detail_columns)
        .sort_values(["commodity", "trade_date", "contract"], ascending=[True, True, True])
        .to_dict(orient="records")
        if not out.empty
        else []
    )
    if invalid_samples:
        for record in invalid_samples:
            record["peer_rows"] = peer_rows_by_detail_id.get(record["detail_id"], [])
    report_payload = {
        "overview": overview,
        "commodity_summary": commodity_summary.to_dict(orient="records"),
        "suspicious_dates": suspicious_dates.to_dict(orient="records"),
        "detail_records": detail_records,
        "invalid_samples": invalid_samples,
    }
    return {
        "suspicious_dates": suspicious_dates,
        "all_samples": all_samples,
        "commodity_summary": commodity_summary,
        "report_payload": report_payload,
    }


def _build_trigger_reason(row: pd.Series) -> str:
    reasons: list[str] = []
    if row.get("A_score", 0) >= 8:
        reasons.append("振幅异常")
    if row.get("C_score", 0) >= 20:
        reasons.append("结构失真")
    if row.get("peer_comparability_weak_flag", False):
        reasons.append("可比合约不足")
    if row.get("target_liquidity_weak_flag", False):
        reasons.append("目标合约流动性偏弱")
    if row.get("active_peer_count", 0) == 1:
        reasons.append("仅1个活跃可比合约")
    if row.get("active_peer_count", 0) == 0:
        reasons.append("无活跃可比合约")
    if row.get("sample_status") != "valid":
        reasons.append(f"样本状态={_sample_status_label(row.get('sample_status'))}")
    return "；".join(reasons) if reasons else "无明显触发项"


def _sample_status_label(sample_status: object) -> str:
    labels = {
        "valid": "有效样本",
        "invalid_basic_data": "基础数据无效",
        "invalid_low_liquidity": "低流动性无效",
        "invalid_lifecycle_edge": "生命周期边界无效",
        "invalid_insufficient_history": "历史样本不足",
        "invalid_insufficient_peer": "对照不足",
    }
    value = str(sample_status)
    return labels.get(value, value)


def _build_peer_rows_map(df: pd.DataFrame) -> dict[str, list[dict[str, object]]]:
    peer_rows_by_detail_id: dict[str, list[dict[str, object]]] = {}
    required_columns = {
        "detail_id",
        "commodity",
        "contract",
        "trade_date",
        "sample_status",
        "volume",
        "high",
        "low",
        "close",
        "range_pct",
        "raw_structure_residual",
        "normalized_structure_residual",
        "candidate_score",
        "candidate_level",
    }
    if not required_columns.issubset(set(df.columns)):
        return peer_rows_by_detail_id
    display_columns = [
        "contract",
        "volume",
        "high",
        "low",
        "close",
        "range_pct",
        "raw_structure_residual",
        "normalized_structure_residual",
        "candidate_score",
        "candidate_level",
    ]

    for (_, trade_date), day in df.groupby(["commodity", "trade_date"], sort=False):
        active = day.loc[
            (day["sample_status"] == "valid")
            & (day["volume"] > 0)
        ].sort_values(["volume", "contract"], ascending=[False, True]).head(ACTIVE_CONTRACT_LIMIT)

        for _, row in day.iterrows():
            display = active.copy()
            if row["contract"] not in set(display["contract"].tolist()):
                display = pd.concat([row.to_frame().T, display], ignore_index=True)
            display = display.drop_duplicates(subset=["contract"], keep="first").reset_index(drop=True)
            display["is_target"] = display["contract"] == row["contract"]
            peer_rows_by_detail_id[str(row["detail_id"])] = display.loc[:, ["is_target", *display_columns]].to_dict(orient="records")

    return peer_rows_by_detail_id
