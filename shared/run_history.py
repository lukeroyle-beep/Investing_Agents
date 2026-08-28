from __future__ import annotations

from pathlib import Path

import pandas as pd

from shared.io_utils import ensure_parent_dir, write_managed_csv_with_schema
from shared.paths import RUN_HISTORY_PATH
from shared.schema_registry import get_file_schema
from shared.schemas import RUN_HISTORY_SCHEMA as RUN_HISTORY_SCHEMA_SPEC
from shared.sqlite_sidecar import upsert_run_history_row


RUN_HISTORY_SCHEMA = get_file_schema("run_history.csv")
RUN_HISTORY_COLUMNS = RUN_HISTORY_SCHEMA.canonical_column_order
STATUS_STARTED = "started"
STATUS_VALIDATING = "validating"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
UNRESOLVED_STATUSES = {STATUS_STARTED, STATUS_VALIDATING}
_LEGACY_STATUS_MAP = {"running": STATUS_STARTED, "success": STATUS_SUCCEEDED}


def ensure_run_history_file() -> Path:
    """
    Ensure the run history CSV exists with the expected header.
    """
    ensure_parent_dir(RUN_HISTORY_PATH)

    if not RUN_HISTORY_PATH.exists():
        write_managed_csv_with_schema(
            pd.DataFrame(columns=RUN_HISTORY_COLUMNS, dtype=str),
            RUN_HISTORY_PATH,
            schema=RUN_HISTORY_SCHEMA_SPEC,
            producer="Pipeline Orchestrator",
        )

    return RUN_HISTORY_PATH


def _read_run_history() -> pd.DataFrame:
    ensure_run_history_file()
    df = pd.read_csv(RUN_HISTORY_PATH, dtype=str, keep_default_na=False)

    for column in RUN_HISTORY_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    output_df = df[RUN_HISTORY_COLUMNS].astype(str).copy()
    output_df["status"] = output_df["status"].map(
        lambda value: _LEGACY_STATUS_MAP.get(str(value).strip().lower(), str(value).strip().lower())
    )
    return output_df


def _write_run_history(df: pd.DataFrame) -> None:
    output_df = df.copy()
    output_df = output_df[RUN_HISTORY_COLUMNS].fillna("").astype(str).copy()
    write_managed_csv_with_schema(
        output_df,
        RUN_HISTORY_PATH,
        schema=RUN_HISTORY_SCHEMA_SPEC,
        producer="Pipeline Orchestrator",
    )


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


def find_unresolved_run_records() -> list[dict[str, str]]:
    """
    Return run-history rows still marked started or validating.

    These rows represent an interrupted or concurrent pipeline run until an
    operator deliberately resolves them. The pipeline must fail closed rather
    than start a new economic-state mutation path while a previous run is still
    unresolved.
    """
    df = _read_run_history()
    if df.empty:
        return []

    running_rows = df[df["status"].astype(str).str.strip().str.lower().isin(UNRESOLVED_STATUSES)]
    return [
        {column: str(row.get(column, "")) for column in RUN_HISTORY_COLUMNS}
        for _, row in running_rows.iterrows()
    ]


def find_running_run_records() -> list[dict[str, str]]:
    """Compatibility alias for unresolved started/validating records."""
    return find_unresolved_run_records()


def assert_no_unresolved_running_runs(new_run_id: str | None = None) -> None:
    """
    Fail closed when a prior run is unresolved or has unverified success.

    `new_run_id` is excluded so the caller can use this after a run row has
    already been created for the current process. The normal start path calls it
    before appending the new row.
    """
    normalised_new_run_id = str(new_run_id or "").strip()
    df = _read_run_history()
    unresolved = [
        {column: str(row.get(column, "")) for column in RUN_HISTORY_COLUMNS}
        for _, row in df[
            df["status"].astype(str).str.strip().str.lower().isin(UNRESOLVED_STATUSES)
        ].iterrows()
        if str(row.get("run_id", "")).strip() != normalised_new_run_id
    ]
    if not unresolved:
        _assert_latest_succeeded_run_has_verifiable_proof(
            df,
            new_run_id=normalised_new_run_id,
        )
        return

    run_ids = ", ".join(str(row.get("run_id", "")).strip() for row in unresolved)
    raise RuntimeError(
        "Cannot start a new pipeline run while previous run-history records "
        f"remain running: {run_ids}. Resolve the interrupted run manually before retrying."
    )


def _assert_latest_succeeded_run_has_verifiable_proof(
    df: pd.DataFrame,
    *,
    new_run_id: str = "",
) -> None:
    """Reject an apparent success that lacks complete finalization proof.

    Only the latest prior row is checked because older manifests intentionally
    describe historical artifact versions that later successful runs replace.
    """
    candidates = df[
        df["run_id"].astype(str).str.strip() != str(new_run_id).strip()
    ]
    if candidates.empty:
        return
    latest = candidates.iloc[-1]
    status = str(latest.get("status", "")).strip().lower()
    if status != STATUS_SUCCEEDED:
        return
    run_id = str(latest.get("run_id", "")).strip()
    state_dir = RUN_HISTORY_PATH.parent
    record_path = state_dir.parent / "runs" / run_id / "run_finalization.json"
    try:
        from shared.run_finalizer import validate_finalization_record
        from shared.sqlite_parity import validate_sqlite_dual_write_parity

        validate_finalization_record(record_path, state_dir=state_dir)
        parity = validate_sqlite_dual_write_parity(
            run_id=run_id,
            state_dir=state_dir,
        )
        if not parity.passed:
            raise RuntimeError("required CSV/SQLite parity does not pass")
    except Exception as exc:
        raise RuntimeError(
            f"Cannot start a new pipeline run because latest succeeded run {run_id} "
            f"lacks complete, verifiable finalization proof: {exc}. Resolve it "
            "with tools/resolve_interrupted_run.py before retrying."
        ) from exc


def _require_status(
    df: pd.DataFrame,
    idx: int,
    run_id: str,
    allowed: set[str],
) -> str:
    current_status = _get_current_status(df, idx)
    if current_status not in allowed:
        raise ValueError(
            f"Run history record for run_id={run_id} has invalid transition source: status={current_status}"
        )
    return current_status


def start_run_record(run_id: str, started_at: str) -> None:
    """
    Append a new run-history row with status set to started.
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
                "status": STATUS_STARTED,
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


def begin_run_validation(run_id: str) -> None:
    df = _read_run_history()
    run_id = _require_non_blank(run_id, "run_id")
    idx = _find_matching_run_index(df, run_id)
    _require_status(df, idx, run_id, {STATUS_STARTED})
    df.at[idx, "status"] = STATUS_VALIDATING
    _write_run_history(df)
    upsert_run_history_row(_row_payload(df, idx))


def complete_run_record(run_id: str, completed_at: str) -> None:
    """
    Mark an existing run-history row as successfully completed.
    """
    df = _read_run_history()
    run_id = _require_non_blank(run_id, "run_id")
    completed_at = _require_non_blank(completed_at, "completed_at")
    idx = _find_matching_run_index(df, run_id)
    _require_status(df, idx, run_id, {STATUS_VALIDATING})

    df.at[idx, "completed_at"] = completed_at
    df.at[idx, "status"] = STATUS_SUCCEEDED

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
    current_status = _get_current_status(df, idx)
    if current_status == STATUS_FAILED:
        return
    _require_status(df, idx, run_id, UNRESOLVED_STATUSES)

    df.at[idx, "completed_at"] = completed_at
    df.at[idx, "status"] = "failed"
    df.at[idx, "failed_agent"] = str(failed_agent).strip()
    df.at[idx, "error_message"] = str(error_message).strip()

    _write_run_history(df)
    upsert_run_history_row(_row_payload(df, idx))


def force_fail_run_record(
    run_id: str,
    completed_at: str,
    failed_agent: str,
    error_message: str,
) -> None:
    """Resolve an ambiguous or partially finalized run to failed."""
    df = _read_run_history()
    run_id = _require_non_blank(run_id, "run_id")
    completed_at = _require_non_blank(completed_at, "completed_at")
    idx = _find_matching_run_index(df, run_id)
    if _get_current_status(df, idx) == STATUS_FAILED:
        return
    df.at[idx, "completed_at"] = completed_at
    df.at[idx, "status"] = STATUS_FAILED
    df.at[idx, "failed_agent"] = str(failed_agent).strip()
    df.at[idx, "error_message"] = str(error_message).strip()
    _write_run_history(df)
    upsert_run_history_row(_row_payload(df, idx))


def get_run_record(run_id: str) -> dict[str, str]:
    df = _read_run_history()
    idx = _find_matching_run_index(df, _require_non_blank(run_id, "run_id"))
    return _row_payload(df, idx)
