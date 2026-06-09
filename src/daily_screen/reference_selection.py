from __future__ import annotations

import pandas as pd


def attach_references(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"])

    main_ref = (
        out.sort_values(["commodity", "trade_date", "volume", "contract"], ascending=[True, True, False, True])
        .groupby(["commodity", "trade_date"], as_index=False)
        .first()[["commodity", "trade_date", "contract"]]
        .rename(columns={"contract": "main_reference_contract"})
    )
    out = out.merge(main_ref, on=["commodity", "trade_date"], how="left")

    main_ref_by_day = main_ref.sort_values(["commodity", "trade_date"]).copy()
    main_ref_by_day["main_reference_changed_today"] = (
        main_ref_by_day.groupby("commodity")["main_reference_contract"].transform(lambda s: s.ne(s.shift(1)).fillna(False))
    )
    out = out.merge(
        main_ref_by_day[["commodity", "trade_date", "main_reference_changed_today"]],
        on=["commodity", "trade_date"],
        how="left",
    )
    return out
