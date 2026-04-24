from __future__ import annotations

from pathlib import Path

import pandas as pd

from shared.io_utils import ensure_parent_dir, write_csv_file
from shared.paths import RUN_HISTORY_PATH
from shared.schema_registry import get_file_schema
from shared.sqlite_sidecar import upsert_run_history_row


RUN_HISTORY_SCHEMA = get_file_schema("run_history.csv")
RUN_HISTORY_COLUMNS = RUN_HISTORY_SCHEMA.canonical_column_order


def ensure_run_history_file() -> Path:
    """
    Ensure the run history CSV exists with the expected header.
    """
    ensure_parent_dir(RUN_HISTORY_PATH)

    if not RUN_HISTORY_PATH.exists():
        write_csv_file(
            pd.DataFrame(columns=RUN_HISTORY_COLUMNS, dtype=str),
            RUN_HISTORY_PATH,
        )

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
    write_csv_file(output_df, RUN_HISTORY_PATH)


def _row_payload(df: pd.DataFrame, idx: int) -> dict[str, str]:
    row = df.loc[idx, RUN_HISTORY_COLUMNS]
    return {column: str(row.get(column, "")) for column in RUN_HISTORY_COLUMNS}


def _find_matching_run_index(df: pd.DataFrame, run_id: str) -> int:
    matches = df.index[df["run_id"].astype(str).str.strip() == str(run_id).strip()].tolist()

    if not matches:
        raise ValueError(f"Run history record not found for run_id={run_id}")

    if len(matches) > 1:
        raise ValueError(f"Duplicate run history records found for run_id={run_id}")

    return int(matches[0])


def _require_non_blank(value: str, field_name: str) -> str:
    normalised = str(value).strip()
    if normalised == "":
        raise ValueError(f"{field_name} must be non-blank")
    return normalised


def _get_current_status(df: pd.DataFrame, idx: int) -> str:
    return str(df.at[idx, "status"]).strip().lower()


def find_running_run_records() -> list[dict[str, str]]:
    """
    Return existing run-history rows that are still marked as running.

    These rows represent an interrupted or concurrent pipeline run until an
    operator deliberately resolves them. The pipeline must fail closed rather
    than start a new economic-state mutation path while a previous run is still
    unresolved.
    """
    df = _read_run_history()
    if df.empty:
        return []

    running_rows = df[df["status"].astype(str).str.strip().str.lower() == "running"]
    return [
        {column: str(row.get(column, "")) for column in RUN_HISTORY_COLUMNS}
        for _, row in running_rows.iterrows()
    ]


def assert_no_unresolved_running_runs(new_run_id: str | None = None) -> None:
    """
    Fail closed when a prior run is still marked running.

    `new_run_id` is excluded so the caller can use this after a run row has
    already been created for the current process. The normal start path calls it
    before appending the new row.
    """
    normalised_new_run_id = str(new_run_id or "").strip()
    unresolved = [
        row
        for row in find_running_run_records()
        if str(row.get("run_id", "")).strip() != normalised_new_run_id
    ]
    if not unresolved:
        return

    run_ids = ", ".join(str(row.get("run_id", "")).strip() for row in unresolved)
    raise RuntimeError(
        "Cannot start a new pipeline run while previous run-history records "
        f"remain running: {run_ids}. Resolve the interrupted run manually before retrying."
    )


def _require_running_status_for_terminal_update(df: pd.DataFrame, idx: int, run_id: str) -> None:
    current_status = _get_current_status(df, idx)
    if current_status != "running":
        raise ValueError(
            f"Run history record for run_id={run_id} is already terminal or invalid: status={current_status}"
        )


def start_run_record(run_id: str, started_at: str) -> None:
    """
    Append a new run-history row with status set to running.
    """
    df = _read_run_history()
    run_id = _require_non_blank(run_id, "run_id")
    started_at = _require_non_blank(started_at, "started_at")

    assert_no_unresolved_running_runs(new_run_id=run_id)

    existing = df["run_id"].astype(str).str.strip()
    if (existing == run_id).any():
        raise ValueError(f"Run history record already exists for run_id={run_id}")

    new_row = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "started_at": started_at,
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
    upsert_run_history_row(_row_payload(output_df, len(output_df) - 1))


def complete_run_record(run_id: str, completed_at: str) -> None:
    """
    Mark an existing run-history row as successfully completed.
    """
    df = _read_run_history()
    run_id = _require_non_blank(run_id, "run_id")
    completed_at = _require_non_blank(completed_at, "completed_at")
    idx = _find_matching_run_index(df, run_id)
    _require_running_status_for_terminal_update(df, idx, run_id)

    df.at[idx, "completed_at"] = completed_at
    df.at[idx, "status"] = "success"

    _write_run_history(df)
    upsert_run_history_row(_row_payload(df, idx))


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
    run_id = _require_non_blank(run_id, "run_id")
    completed_at = _require_non_blank(completed_at, "completed_at")
    idx = _find_matching_run_index(df, run_id)
    _require_running_status_for_terminal_update(df, idx, run_id)

    df.at[idx, "completed_at"] = completed_at
    df.at[idx, "status"] = "failed"
    df.at[idx, "failed_agent"] = str(failed_agent).strip()
    df.at[idx, "error_message"] = str(error_message).strip()

    _write_run_history(df)
    upsert_run_history_row(_row_payload(df, idx))
