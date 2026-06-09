import pandas as pd

from src.daily_screen.reference_selection import attach_references


def test_selects_main_reference_by_highest_volume():
    reference_input_df = pd.DataFrame(
        {
            "commodity": ["PP", "PP", "PP"],
            "contract": ["PP2409", "PP2410", "PP2411"],
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "volume": [100, 200, 150],
        }
    )

    enriched = attach_references(reference_input_df)

    assert enriched.loc[enriched["contract"] == "PP2409", "main_reference_contract"].iloc[0] == "PP2410"


def test_marks_reference_change_day():
    reference_switch_input_df = pd.DataFrame(
        {
            "commodity": ["PP", "PP", "PP", "PP"],
            "contract": ["PP2409", "PP2410", "PP2409", "PP2410"],
            "trade_date": ["2024-01-02", "2024-01-02", "2024-01-03", "2024-01-03"],
            "volume": [100, 200, 300, 100],
        }
    )

    enriched = attach_references(reference_switch_input_df)

    changed = enriched.loc[enriched["trade_date"] == pd.Timestamp("2024-01-03"), "main_reference_changed_today"]
    assert changed.all()
