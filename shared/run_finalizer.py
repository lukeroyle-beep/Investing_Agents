from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import shared.run_history as run_history
from shared.freshness import assert_actionable_health_frame
from shared.artifact_manifest import (
    ArtifactValidationError,
    build_artifact_manifest,
    load_pre_economic_checksums,
    sha256_file,
    validate_artifact_manifest,
    validate_run_id,
    write_artifact_manifest,
)
from shared.run_reconciliation import (
    validate_run_reconciliation,
    write_run_reconciliation_summary,
)
from shared.sqlite_parity import format_parity_report, validate_sqlite_dual_write_parity


FINALIZATION_RECORD_VERSION = "1.0"
FINALIZATION_RECORD_FILE = "run_finalization.json"
_SAFE_MESSAGE = re.compile(
    r"(?i)([\"']?(?:x-api-key|x-user-key|authorization|api[_-]?key|token|secret|password)[\"']?)"
    r"\s*[:=]\s*[\"']?[^\s,;}]+"
)


class RunFinalizationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FinalizationResult:
    run_id: str
    finalization_id: str
    outcome: str
    manifest_path: Path
    record_path: Path
    validation_checks: tuple[str, ...]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".json.tmp",
    )
    temp_path = Path(raw_temp_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def redact_failure_message(value: object) -> str:
    message = str(value).strip() or value.__class__.__name__
    message = _SAFE_MESSAGE.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    return message[:1000]


def _validate_lifecycle_report(state_dir: Path) -> None:
    path = state_dir / "lifecycle_integrity_report.csv"
    try:
        frame = pd.read_csv(path, keep_default_na=False)
    except Exception as exc:
        raise RunFinalizationError(f"Lifecycle report cannot be read: {exc}") from exc
    if frame.empty:
        raise RunFinalizationError("Lifecycle report has no validation rows")
    if "failure_count" not in frame.columns:
        raise RunFinalizationError("Lifecycle report lacks failure_count")
    if "record_type" in frame.columns:
        summary_rows = frame[
            frame["record_type"].astype(str).str.strip().str.lower() == "summary"
        ]
    else:
        summary_rows = frame[
            frame["failure_count"].astype(str).str.strip().ne("")
        ]
    if summary_rows.empty:
        raise RunFinalizationError("Lifecycle report lacks a summary validation row")
    failure_counts = pd.to_numeric(summary_rows["failure_count"], errors="coerce")
    if failure_counts.isna().any() or (failure_counts > 0).any():
        raise RunFinalizationError("Lifecycle integrity validation did not pass")


def _validate_health_modes(
    state_dir: Path,
    *,
    now_func: Callable[[], str] = utc_now_iso,
) -> None:
    path = state_dir / "data_source_health.csv"
    frame = pd.read_csv(path, keep_default_na=False)
    try:
        assert_actionable_health_frame(
            frame,
            now=datetime.fromisoformat(now_func()),
        )
    except Exception as exc:
        raise RunFinalizationError(f"Data-source health did not pass: {exc}") from exc


def _read_csv_for_finalization(state_dir: Path, file_name: str) -> pd.DataFrame:
    try:
        return pd.read_csv(state_dir / file_name, keep_default_na=False)
    except Exception as exc:
        raise RunFinalizationError(f"Cannot validate current-run {file_name}: {exc}") from exc


def _validate_current_run_artifacts(run_id: str, state_dir: Path) -> None:
    run = run_history.get_run_record(run_id)
    try:
        started_at = pd.Timestamp(run["started_at"])
    except Exception as exc:
        raise RunFinalizationError("Run history has malformed started_at") from exc
    if started_at.tzinfo is None:
        raise RunFinalizationError("Run history started_at must be timezone-aware")

    events = _read_csv_for_finalization(state_dir, "event_log.csv")
    started_events = events[
        events["run_id"].astype(str).str.strip().eq(run_id)
        & events["event_type"].astype(str).str.strip().eq("run_started")
    ]
    if len(started_events) != 1:
        raise RunFinalizationError("Current run must have exactly one run_started event")

    lifecycle = _read_csv_for_finalization(state_dir, "lifecycle_integrity_report.csv")
    summary = lifecycle[
        lifecycle["record_type"].astype(str).str.strip().str.lower().eq("summary")
    ]
    checked = pd.to_datetime(summary.get("checked_at"), utc=True, errors="coerce")
    if summary.empty or checked.isna().any() or (checked < started_at).any():
        raise RunFinalizationError("Lifecycle summary was not produced by the current run")

    health = _read_csv_for_finalization(state_dir, "data_source_health.csv")
    retrieved = pd.to_datetime(health.get("retrieval_time"), utc=True, errors="coerce")
    if health.empty or retrieved.isna().any() or (retrieved < started_at).any():
        raise RunFinalizationError("Data-source health was not produced by the current run")

    equity = _read_csv_for_finalization(state_dir, "portfolio_equity_history.csv")
    if not equity["run_id"].astype(str).str.strip().eq(run_id).any():
        raise RunFinalizationError("Current run lacks an equity snapshot")

    performance = _read_csv_for_finalization(state_dir, "performance_summary.csv")
    if len(performance) != 1 or str(performance.iloc[0].get("latest_run_id", "")).strip() != run_id:
        raise RunFinalizationError("Performance summary does not identify the current run")

    monitor = _read_csv_for_finalization(state_dir, "portfolio_monitor.csv")
    if not monitor.empty and not monitor["run_id"].astype(str).str.strip().eq(run_id).all():
        raise RunFinalizationError("Portfolio monitor contains non-current run projections")

    for file_name in ("portfolio_orders.csv", "advisory_trades.csv", "exit_advice.csv"):
        frame = _read_csv_for_finalization(state_dir, file_name)
        if frame.empty:
            continue
        if "run_id" not in frame.columns or not frame["run_id"].astype(str).str.strip().eq(run_id).all():
            raise RunFinalizationError(f"{file_name} contains stale or unscoped rows")


def validate_economic_changes(
    pre_checksums: dict[str, str | None],
    post_checksums: dict[str, str | None],
    reconciliation: dict[str, object],
) -> None:
    changed = {
        name
        for name in sorted(set(pre_checksums) | set(post_checksums))
        if pre_checksums.get(name) != post_checksums.get(name)
    }
    if not changed:
        return

    fills = int(float(reconciliation.get("fills_processed", 0) or 0))
    opened = int(float(reconciliation.get("positions_opened", 0) or 0))
    closed = int(float(reconciliation.get("positions_closed", 0) or 0))
    errors: list[str] = []
    if fills == 0:
        errors.append(f"economic artifacts changed without processed fills: {sorted(changed)}")
    if "portfolio_state.csv" in changed and opened + closed == 0:
        errors.append("portfolio_state.csv changed without a position lifecycle event")
    if "cash_state.csv" in changed and "cash_ledger.csv" not in changed:
        errors.append("cash_state.csv changed without a cash-ledger change")
    if "processed_fills.csv" in changed and "trade_fills.csv" not in changed:
        errors.append("processed_fills.csv changed without a trade-fill ledger change")
    if any(value is None for value in post_checksums.values()):
        errors.append("one or more post-run economic artifacts are missing")
    if errors:
        raise RunFinalizationError("Unexplained economic delta: " + "; ".join(errors))


def _record_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / validate_run_id(run_id) / FINALIZATION_RECORD_FILE


def validate_finalization_record(
    path: Path,
    *,
    state_dir: Path,
) -> FinalizationResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunFinalizationError(f"Finalization record is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunFinalizationError("Finalization record must be a JSON object")
    if payload.get("record_version") != FINALIZATION_RECORD_VERSION:
        raise RunFinalizationError("Unsupported finalization record version")
    if payload.get("state") != "complete" or payload.get("outcome") != "succeeded":
        raise RunFinalizationError("Finalization record does not prove successful completion")
    run_id = validate_run_id(payload.get("run_id", ""))
    if path.parent.name != run_id:
        raise RunFinalizationError("Finalization record directory does not match run_id")

    manifest_path = path.parent / str(payload.get("manifest_file", ""))
    if manifest_path.parent != path.parent or not manifest_path.is_file():
        raise RunFinalizationError("Finalization record references an unsafe or missing manifest")
    if sha256_file(manifest_path) != payload.get("manifest_sha256"):
        raise RunFinalizationError("Finalization manifest checksum does not match its record")
    try:
        validate_artifact_manifest(manifest_path, state_dir=state_dir)
    except ArtifactValidationError as exc:
        raise RunFinalizationError(redact_failure_message(exc)) from exc
    for file_name in ("run_history.csv", "run_reconciliation_summary.csv"):
        try:
            frame = pd.read_csv(state_dir / file_name, dtype=str, keep_default_na=False)
        except Exception as exc:
            raise RunFinalizationError(f"Cannot validate terminal {file_name}: {exc}") from exc
        matches = frame[frame["run_id"].astype(str).str.strip() == run_id]
        if len(matches) != 1:
            raise RunFinalizationError(
                f"Terminal {file_name} must contain exactly one row for run_id={run_id}"
            )
        status = str(matches.iloc[0].get("status", "")).strip().lower()
        status = "succeeded" if status == "success" else status
        if status != "succeeded" or not str(matches.iloc[0].get("completed_at", "")).strip():
            raise RunFinalizationError(
                f"Terminal {file_name} does not prove succeeded completion"
            )
    return FinalizationResult(
        run_id=run_id,
        finalization_id=str(payload["finalization_id"]),
        outcome="succeeded",
        manifest_path=manifest_path,
        record_path=path,
        validation_checks=tuple(payload.get("validation_checks", [])),
    )


def finalize_run(
    run_id: str,
    *,
    state_dir: Path,
    runs_dir: Path,
    now_func: Callable[[], str] = utc_now_iso,
) -> FinalizationResult:
    run_id = validate_run_id(run_id)
    record_path = _record_path(runs_dir, run_id)
    if record_path.exists():
        return validate_finalization_record(record_path, state_dir=state_dir)

    finalization_id = str(uuid.uuid4())
    checks: list[str] = []
    try:
        run_history.begin_run_validation(run_id)
        checks.append("run_transitioned_to_validating")

        reconciliation = write_run_reconciliation_summary(run_id)
        validate_run_reconciliation(reconciliation)
        checks.append("economic_deltas_reconciled")

        _validate_current_run_artifacts(run_id, state_dir)
        checks.append("current_run_artifacts_validated")

        pre_checksums = load_pre_economic_checksums(run_id, runs_dir=runs_dir)
        provisional_manifest = build_artifact_manifest(
            run_id,
            pre_economic_checksums=pre_checksums,
            state_dir=state_dir,
            validation_checks=checks,
            now_func=now_func,
        )
        checks.append("required_artifacts_and_schemas_validated")

        _validate_lifecycle_report(state_dir)
        checks.append("lifecycle_integrity_passed")
        _validate_health_modes(state_dir, now_func=now_func)
        checks.append("freshness_gate_passed")
        validate_economic_changes(
            pre_checksums,
            provisional_manifest["post_economic_state_checksums"],
            reconciliation,
        )
        checks.append("economic_checksums_explained")

        preliminary_parity = validate_sqlite_dual_write_parity(
            run_id=run_id,
            state_dir=state_dir,
        )
        if not preliminary_parity.passed:
            raise RunFinalizationError(format_parity_report(preliminary_parity))
        checks.append("preterminal_csv_sqlite_parity_passed")

        completed_at = now_func()
        run_history.complete_run_record(run_id=run_id, completed_at=completed_at)
        terminal_reconciliation = write_run_reconciliation_summary(run_id)
        validate_run_reconciliation(terminal_reconciliation)

        terminal_parity = validate_sqlite_dual_write_parity(
            run_id=run_id,
            state_dir=state_dir,
        )
        if not terminal_parity.passed:
            raise RunFinalizationError(format_parity_report(terminal_parity))
        checks.append("terminal_csv_sqlite_parity_passed")

        manifest = build_artifact_manifest(
            run_id,
            pre_economic_checksums=pre_checksums,
            state_dir=state_dir,
            validation_checks=checks,
            now_func=now_func,
        )
        manifest_path = write_artifact_manifest(manifest, runs_dir=runs_dir)
        validate_artifact_manifest(manifest_path, state_dir=state_dir)
        checks.append("artifact_manifest_verified")

        record = {
            "record_version": FINALIZATION_RECORD_VERSION,
            "finalization_id": finalization_id,
            "run_id": run_id,
            "state": "complete",
            "outcome": "succeeded",
            "completed_at_utc": completed_at,
            "manifest_file": manifest_path.name,
            "manifest_sha256": sha256_file(manifest_path),
            "validation_checks": checks,
        }
        _atomic_write_json(record_path, record)
        return validate_finalization_record(record_path, state_dir=state_dir)
    except Exception as exc:
        raise RunFinalizationError(redact_failure_message(exc)) from exc


def record_failed_finalization(
    run_id: str,
    error: object,
    *,
    state_dir: Path,
    runs_dir: Path,
    failed_agent: str = "Run Finalizer",
    now_func: Callable[[], str] = utc_now_iso,
) -> Path:
    run_id = validate_run_id(run_id)
    message = redact_failure_message(error)
    completed_at = now_func()
    recording_errors: list[str] = []
    try:
        run_history.force_fail_run_record(
            run_id=run_id,
            completed_at=completed_at,
            failed_agent=failed_agent,
            error_message=message,
        )
    except Exception as exc:
        recording_errors.append(f"run_history={redact_failure_message(exc)}")
    try:
        write_run_reconciliation_summary(run_id)
    except Exception as exc:
        recording_errors.append(f"reconciliation={redact_failure_message(exc)}")

    record_path = _record_path(runs_dir, run_id)
    payload = {
        "record_version": FINALIZATION_RECORD_VERSION,
        "finalization_id": str(uuid.uuid4()),
        "run_id": run_id,
        "state": "complete",
        "outcome": "failed",
        "completed_at_utc": completed_at,
        "failed_agent": failed_agent,
        "error_message": message,
        "recording_errors": recording_errors,
    }
    _atomic_write_json(record_path, payload)
    return record_path
