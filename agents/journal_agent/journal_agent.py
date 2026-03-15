import os
from datetime import datetime, UTC

import pandas as pd


FINAL_SHORTLIST_FILE = os.path.join("data", "final_shortlist.csv")
JOURNAL_FILE = os.path.join("data", "trade_journal.csv")


def load_final_shortlist():
    if not os.path.exists(FINAL_SHORTLIST_FILE):
        return pd.DataFrame()

    df = pd.read_csv(FINAL_SHORTLIST_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def load_existing_journal():
    if not os.path.exists(JOURNAL_FILE):
        return pd.DataFrame()

    df = pd.read_csv(JOURNAL_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def build_journal_entries(df):
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

    journal_df = df[columns_to_keep].copy()
    journal_df["journaled_at"] = run_timestamp
    journal_df["review_status"] = "open"
    journal_df["user_action"] = ""
    journal_df["outcome"] = ""
    journal_df["notes"] = ""

    return journal_df


def main():
    shortlist_df = load_final_shortlist()

    if shortlist_df.empty:
        print("No final shortlist found. Run the pipeline first.")
        return

    existing_journal_df = load_existing_journal()
    new_entries_df = build_journal_entries(shortlist_df)

    if existing_journal_df.empty:
        combined_df = new_entries_df
    else:
        combined_df = pd.concat([existing_journal_df, new_entries_df], ignore_index=True)

    os.makedirs("data", exist_ok=True)
    combined_df.to_csv(JOURNAL_FILE, index=False)

    total_entries = len(combined_df)
    new_entries = len(new_entries_df)

    print("\nJournal Agent finished.")
    print(f"Saved journal to: {JOURNAL_FILE}")

    print("\nRun summary:")
    print(f"New journal entries added: {new_entries}")
    print(f"Total journal entries: {total_entries}")

    print("\nLatest entries:")
    print(new_entries_df)


if __name__ == "__main__":
    main()