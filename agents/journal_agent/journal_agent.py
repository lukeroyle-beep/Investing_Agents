from __future__ import annotations

from datetime import datetime, UTC

import pandas as pd

from agents.shared.event_log import append_artifact_written_event
from agents.journal_agent.history_migration import journal_write_lock, validate_journal_history
from shared.io_utils import write_csv
from shared.paths import FINAL_SHORTLIST_PATH, data_path
from shared.run_context import get_or_create_run_id
from shared.schemas import validate_trade_journal


FINAL_SHORTLIST_FILE = FINAL_SHORTLIST_PATH
JOURNAL_FILE = data_path("trade_journal.csv")
AGENT_NAME = "Journal Agent"
SOURCE_COLUMNS = [
    "ticker",
    "name",
    "market_regime",
    "adjusted_setup_score",
    "adjusted_setup_status",
    "risk_decision",
    "risk_notes",
]
JOURNAL_KEYS = ["run_id", "ticker"]


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

    # Review notes may contain literal values such as "NA". Retain them, and
    # validate a separate copy when comparing immutable source advice.
    df = pd.read_csv(JOURNAL_FILE, dtype=str, keep_default_na=False)

    if df.empty:
        return pd.DataFrame()

    return df


def build_journal_entries(df: pd.DataFrame, run_id: str) -> pd.DataFrame:
    run_timestamp = datetime.now(UTC).isoformat()

    available_columns = [col for col in SOURCE_COLUMNS if col in df.columns]
    journal_df = df[available_columns].copy()

    for col in SOURCE_COLUMNS:
        if col not in journal_df.columns:
            journal_df[col] = ""

    journal_df = journal_df[SOURCE_COLUMNS].copy()
    journal_df["run_id"] = run_id
    journal_df["journaled_at"] = run_timestamp
    journal_df["review_status"] = "open"
    journal_df["user_action"] = ""
    journal_df["outcome"] = ""
    journal_df["notes"] = ""

    return validate_trade_journal(journal_df)


def merge_journal_entries(
    existing: pd.DataFrame, proposed: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Append new advice keys; a sequential retry must not rewrite history.

    A run and ticker identify one advisory observation. Timestamps and manual
    review fields belong to the retained entry. Changed source advice under an
    existing key or duplicate keys require review, rather than silent repair.
    This is not a broker fill journal or a concurrent-writer transaction.
    """
    previous = validate_trade_journal(existing).reset_index(drop=True)
    candidate = validate_trade_journal(proposed).reset_index(drop=True)
    for label, frame in (("existing", previous), ("proposed", candidate)):
        if frame[JOURNAL_KEYS].eq("").any().any():
            raise ValueError(f"trade_journal: {label} entry has an empty run_id or ticker")
        if frame.duplicated(JOURNAL_KEYS).any():
            raise ValueError(f"trade_journal: {label} entries have duplicate run_id/ticker keys")

    previous_by_key = {
        (row["run_id"], row["ticker"]): row
        for _, row in previous.iterrows()
    }
    new_indices = []
    for index, row in candidate.iterrows():
        key = (row["run_id"], row["ticker"])
        old = previous_by_key.get(key)
        if old is None:
            new_indices.append(index)
            continue
        for column in SOURCE_COLUMNS:
            left, right = old[column], row[column]
            both_missing = pd.isna(left) and pd.isna(right)
            if not both_missing and (pd.isna(left) or pd.isna(right) or left != right):
                raise ValueError(
                    f"trade_journal: conflicting source advice for {key!r} in {column}"
                )

    additions = candidate.loc[new_indices].reset_index(drop=True)
    if additions.empty:
        return existing.copy(), additions
    # Preserve review fields and extra columns from the original rows. Schema
    # validation above operates on copies, not on the retained user content.
    combined = additions.copy() if existing.empty else pd.concat(
        [existing, additions], ignore_index=True
    )
    validate_trade_journal(combined)
    return combined, additions


def main() -> None:
    run_id = get_or_create_run_id()
    print(f"Run ID: {run_id}")

    shortlist_df = load_final_shortlist()

    if shortlist_df.empty:
        print("No final shortlist found. Run the pipeline first.")
        return

    with journal_write_lock(JOURNAL_FILE):
        validate_journal_history(JOURNAL_FILE)
        record_shortlist(shortlist_df, run_id)


def record_shortlist(shortlist_df: pd.DataFrame, run_id: str) -> None:
    """Called under the shared Journal/migration lock."""
    existing_journal_df = load_existing_journal()
    proposed_entries_df = build_journal_entries(shortlist_df, run_id=run_id)
    combined_df, new_entries_df = merge_journal_entries(
        existing_journal_df, proposed_entries_df
    )
    if new_entries_df.empty:
        print("No new journal entries; this run's advice is already recorded.")
        return

    write_csv(combined_df, JOURNAL_FILE)

    total_entries = len(combined_df)
    new_entries = len(new_entries_df)
    append_artifact_written_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        entity_type="journal",
        entity_id="trade_journal",
        message="Trade journal updated.",
        details={
            "output_path": str(JOURNAL_FILE),
            "new_entries": new_entries,
            "total_entries": total_entries,
        },
    )

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
