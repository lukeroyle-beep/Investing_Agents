from __future__ import annotations

from pathlib import Path

import pandas as pd

from shared.io_utils import ensure_parent_dir
from shared.paths import RUN_HISTORY_PATH
from shared.schema_registry import get_file_schema


RUN_HISTORY_SCHEMA = get_file_schema("run_history.csv")
RUN_HISTORY_COLUMNS = RUN_HISTORY_SCHEMA.canonical_column_order


def ensure_run_history_file() -> Path:
    """
    Ensure the run history CSV exists with the expected header.
    """
    ensure_parent_dir(RUN_HISTORY_PATH)

    if not RUN_HISTORY_PATH.exists():
        pd.DataFrame(columns=RUN_HISTORY_COLUMNS, dtype=str).to_csv(RUN_HISTORY_PATH, index=False)

    return RUN_HISTORY_PATH


def _read_run_history() -> pd.DataFrame:
    ensure_run_history_file()
    df = pd.read_csv(RUN_HISTORY_PATH, dtype=str, keep_default_na=False)

    for column in RUN_HISTORY_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    return df[RUN_HISTORY_COLUMNS].astype(str).copy()


def _write_run_history(df: pd.DataFrame) -> None:
    output_df = df.copy()
    output_df = output_df[RUN_HISTORY_COLUMNS].fillna("").astype(str).copy()
    output_df.to_csv(RUN_HISTORY_PATH, index=False)


def _find_matching_run_index(df: pd.DataFrame, run_id: str) -> int:
    matches = df.index[df["run_id"].astype(str).str.strip() == str(run_id).strip()].tolist()

    if not matches:
        raise ValueError(f"Run history record not found for run_id={run_id}")

    if len(matches) > 1:
        raise ValueError(f"Duplicate run history records found for run_id={run_id}")

    return int(matches[0])


def start_run_record(run_id: str, started_at: str) -> None:
    """
    Append a new run-history row with status set to running.
    """
    df = _read_run_history()

    existing = df["run_id"].astype(str).str.strip()
    if (existing == str(run_id).strip()).any():
        raise ValueError(f"Run history record already exists for run_id={run_id}")

    new_row = pd.DataFrame(
        [
            {
                "run_id": str(run_id).strip(),
                "started_at": str(started_at).strip(),
                "completed_at": "",
                "status": "running",
                "failed_agent": "",
                "error_message": "",
                "notes": "",
            }
        ],
        columns=RUN_HISTORY_COLUMNS,
        dtype=str,
    )

    output_df = pd.concat([df, new_row], ignore_index=True)
    _write_run_history(output_df)


def complete_run_record(run_id: str, completed_at: str) -> None:
    """
    Mark an existing run-history row as successfully completed.
    """
    df = _read_run_history()
    idx = _find_matching_run_index(df, run_id)

    df.at[idx, "completed_at"] = str(completed_at).strip()
    df.at[idx, "status"] = "success"

    _write_run_history(df)


def fail_run_record(
    run_id: str,
    completed_at: str,
    failed_agent: str,
    error_message: str,
) -> None:
    """
    Mark an existing run-history row as failed with failure details.
    """
    df = _read_run_history()
    idx = _find_matching_run_index(df, run_id)

    df.at[idx, "completed_at"] = str(completed_at).strip()
    df.at[idx, "status"] = "failed"
    df.at[idx, "failed_agent"] = str(failed_agent).strip()
    df.at[idx, "error_message"] = str(error_message).strip()

    _write_run_history(df)
