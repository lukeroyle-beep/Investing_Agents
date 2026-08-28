from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from shared.artifact_manifest import validate_run_id
from shared.run_finalizer import (
    FINALIZATION_RECORD_FILE,
    RunFinalizationError,
    redact_failure_message,
    validate_finalization_record,
)
from shared.schema_registry import get_file_schema
from shared.sqlite_sidecar import get_connection, initialise_db, transaction


_STATUS_MAP = {
    "running": "started",
    "success": "succeeded",
}


class InterruptedRunResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class InterruptedRunResolution:
    run_id: str
    prior_status: str
    resolved_status: str
    used_finalization_proof: bool
    audit_path: Path


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".csv.tmp",
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".json.tmp",
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _read_control_frame(path: Path, file_name: str) -> pd.DataFrame:
    schema = get_file_schema(file_name)
    if not path.is_file():
        raise InterruptedRunResolutionError(f"Required control artifact is missing: {file_name}")
    try:
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise InterruptedRunResolutionError(f"Cannot read {file_name}: {exc}") from exc
    missing = sorted(set(schema.required_columns) - set(frame.columns))
    if missing:
        raise InterruptedRunResolutionError(f"{file_name} is missing columns: {missing}")
    return frame[schema.canonical_column_order].fillna("").astype(str)


def _find_run_index(frame: pd.DataFrame, run_id: str, file_name: str) -> int:
    matches = frame.index[frame["run_id"].astype(str).str.strip() == run_id].tolist()
    if len(matches) != 1:
        raise InterruptedRunResolutionError(
            f"{file_name} must contain exactly one row for run_id={run_id}"
        )
    return int(matches[0])


def _upsert_sqlite_rows(
    db_path: Path,
    run_history_row: dict[str, str],
    reconciliation_row: dict[str, str] | None,
) -> None:
    initialise_db(db_path=db_path)
    with get_connection(db_path=db_path) as connection:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO run_history
                (run_id, started_at, completed_at, status, failed_agent, error_message, notes)
                VALUES (:run_id, :started_at, :completed_at, :status, :failed_agent,
                        :error_message, :notes)
                ON CONFLICT(run_id) DO UPDATE SET
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    status=excluded.status,
                    failed_agent=excluded.failed_agent,
                    error_message=excluded.error_message,
                    notes=excluded.notes
                """,
                run_history_row,
            )
            if reconciliation_row is not None:
                columns = get_file_schema(
                    "run_reconciliation_summary.csv"
                ).canonical_column_order
                values = {column: reconciliation_row.get(column, "") for column in columns}
                column_sql = ", ".join(columns)
                placeholder_sql = ", ".join(f":{column}" for column in columns)
                update_sql = ", ".join(
                    f"{column}=excluded.{column}" for column in columns if column != "run_id"
                )
                connection.execute(
                    f"INSERT INTO run_reconciliation_summary ({column_sql}) "
                    f"VALUES ({placeholder_sql}) ON CONFLICT(run_id) DO UPDATE SET {update_sql}",
                    values,
                )


def resolve_interrupted_run(
    runtime_dir: Path,
    run_id: str,
    *,
    now: datetime | None = None,
) -> InterruptedRunResolution:
    """Resolve an ambiguous run to failed unless complete success proof validates."""
    runtime_dir = runtime_dir.expanduser().resolve()
    run_id = validate_run_id(run_id)
    state_dir = runtime_dir / "state"
    runs_dir = runtime_dir / "runs"
    control_dir = runtime_dir / "control"
    history_path = state_dir / "run_history.csv"
    reconciliation_path = state_dir / "run_reconciliation_summary.csv"
    history = _read_control_frame(history_path, "run_history.csv")
    history_idx = _find_run_index(history, run_id, "run_history.csv")
    prior_status = _STATUS_MAP.get(
        str(history.at[history_idx, "status"]).strip().lower(),
        str(history.at[history_idx, "status"]).strip().lower(),
    )

    proof_valid = False
    proof_error = ""
    record_path = runs_dir / run_id / FINALIZATION_RECORD_FILE
    if record_path.is_file():
        try:
            result = validate_finalization_record(record_path, state_dir=state_dir)
            proof_valid = result.run_id == run_id
        except Exception as exc:
            proof_error = redact_failure_message(exc)

    audit_path = control_dir / "interrupted_run_resolutions.json"
    if audit_path.is_file():
        try:
            audit_rows = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InterruptedRunResolutionError(
                "Interrupted-run audit trail is corrupt; refusing to change run state"
            ) from exc
        if not isinstance(audit_rows, list):
            raise InterruptedRunResolutionError("Interrupted-run audit trail must be a list")
    else:
        audit_rows = []

    resolved_status = "succeeded" if proof_valid else "failed"
    completed_at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    if prior_status == "failed":
        resolved_status = "failed"
    elif prior_status == "succeeded" and not proof_valid:
        resolved_status = "failed"

    history.at[history_idx, "status"] = resolved_status
    if not str(history.at[history_idx, "completed_at"]).strip():
        history.at[history_idx, "completed_at"] = completed_at
    if resolved_status == "failed":
        history.at[history_idx, "failed_agent"] = "Interrupted Run Resolver"
        history.at[history_idx, "error_message"] = (
            "Interrupted or ambiguous run defaulted to failed because complete, "
            "verifiable finalization proof was absent."
        )
        if proof_error:
            history.at[history_idx, "error_message"] += f" Proof error: {proof_error}"

    reconciliation: pd.DataFrame | None = None
    reconciliation_row: dict[str, str] | None = None
    if reconciliation_path.is_file():
        reconciliation = _read_control_frame(
            reconciliation_path,
            "run_reconciliation_summary.csv",
        )
        reconciliation_idx = _find_run_index(
            reconciliation,
            run_id,
            "run_reconciliation_summary.csv",
        )
        reconciliation.at[reconciliation_idx, "status"] = resolved_status
        reconciliation.at[reconciliation_idx, "completed_at"] = history.at[
            history_idx, "completed_at"
        ]
        reconciliation.at[reconciliation_idx, "failed_agent"] = (
            "" if resolved_status == "succeeded" else "Interrupted Run Resolver"
        )
        reconciliation_row = {
            column: str(reconciliation.at[reconciliation_idx, column])
            for column in reconciliation.columns
        }

    _atomic_write_csv(history, history_path)
    if reconciliation is not None:
        _atomic_write_csv(reconciliation, reconciliation_path)
    history_row = {
        column: str(history.at[history_idx, column]) for column in history.columns
    }
    _upsert_sqlite_rows(
        state_dir / "trading_system.sqlite3",
        history_row,
        reconciliation_row,
    )

    if not any(
        isinstance(row, dict)
        and row.get("run_id") == run_id
        and row.get("resolved_status") == resolved_status
        for row in audit_rows
    ):
        audit_rows.append(
            {
                "record_version": "1.0",
                "run_id": run_id,
                "resolved_at_utc": completed_at,
                "prior_status": prior_status,
                "resolved_status": resolved_status,
                "used_finalization_proof": proof_valid,
                "proof_error": proof_error,
            }
        )
        _atomic_write_json(audit_path, audit_rows)

    return InterruptedRunResolution(
        run_id=run_id,
        prior_status=prior_status,
        resolved_status=resolved_status,
        used_finalization_proof=proof_valid,
        audit_path=audit_path,
    )
