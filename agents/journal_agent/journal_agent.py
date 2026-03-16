from __future__ import annotations

from datetime import datetime, UTC

import pandas as pd

from shared.io_utils import read_csv, write_csv
from shared.paths import FINAL_SHORTLIST_PATH, data_path
from shared.run_context import get_or_create_run_id


FINAL_SHORTLIST_FILE = FINAL_SHORTLIST_PATH
JOURNAL_FILE = data_path("trade_journal.csv")


def load_final_shortlist() -> pd.DataFrame:
    if not FINAL_SHORTLIST_FILE.exists():
        return pd.DataFrame()

    df = pd.read_csv(FINAL_SHORTLIST_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def load_existing_journal() -> pd.DataFrame:
    if not JOURNAL_FILE.exists():
        return pd.DataFrame()

    df = pd.read_csv(JOURNAL_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def build_journal_entries(df: pd.DataFrame, run_id: str) -> pd.DataFrame:
    run_timestamp = datetime.now(UTC).isoformat()

    columns_to_keep = [
        "ticker",
        "name",
        "market_regime",
        "adjusted_setup_score",
        "adjusted_setup_status",
        "risk_decision",
        "risk_notes",
    ]

    available_columns = [col for col in columns_to_keep if col in df.columns]
    journal_df = df[available_columns].copy()

    for col in columns_to_keep:
        if col not in journal_df.columns:
            journal_df[col] = ""

    journal_df = journal_df[columns_to_keep].copy()
    journal_df["run_id"] = run_id
    journal_df["journaled_at"] = run_timestamp
    journal_df["review_status"] = "open"
    journal_df["user_action"] = ""
    journal_df["outcome"] = ""
    journal_df["notes"] = ""

    return journal_df


def main() -> None:
    run_id = get_or_create_run_id()
    print(f"Run ID: {run_id}")

    shortlist_df = load_final_shortlist()

    if shortlist_df.empty:
        print("No final shortlist found. Run the pipeline first.")
        return

    existing_journal_df = load_existing_journal()
    new_entries_df = build_journal_entries(shortlist_df, run_id=run_id)

    if existing_journal_df.empty:
        combined_df = new_entries_df.copy()
    else:
        combined_df = pd.concat([existing_journal_df, new_entries_df], ignore_index=True)

    write_csv(combined_df, JOURNAL_FILE)

    total_entries = len(combined_df)
    new_entries = len(new_entries_df)

    print("\nJournal Agent finished.")
    print(f"Saved journal to: {JOURNAL_FILE}")

    print("\nRun summary:")
    print(f"Run ID: {run_id}")
    print(f"New journal entries added: {new_entries}")
    print(f"Total journal entries: {total_entries}")

    print("\nLatest entries:")
    print(new_entries_df)


if __name__ == "__main__":
    main()