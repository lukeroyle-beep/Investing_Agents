from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from typing import Iterable

from shared.event_log import append_run_lifecycle_event
from shared.artifact_manifest import capture_pre_economic_state
from shared.run_finalizer import (
    finalize_run,
    record_failed_finalization,
    redact_failure_message,
)
from shared.run_context import get_or_create_run_id
import shared.run_history as run_history


PIPELINE_STEPS: list[tuple[str, str]] = [
    ("Universe Agent", "agents.universe_agent.universe_agent"),
    ("Macro Agent", "agents.macro_agent.macro_agent"),
    ("Signal Agent", "agents.signal_agent.signal_agent"),
    ("Risk Agent", "agents.risk_agent.risk_agent"),
    ("News Agent", "agents.news_agent.news_agent"),
    ("Data Freshness Gate", "agents.data_freshness_agent.data_freshness_agent"),
    ("Portfolio Agent", "agents.portfolio_agent.portfolio_agent"),
    ("Advisory Agent", "agents.advisory_agent.advisory_agent"),
    ("Fill Agent", "agents.fill_agent.fill_agent"),
    ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
    ("Position Tracking Agent", "agents.position_tracking_agent.position_tracking_agent"),
    ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
    ("Exit Agent", "agents.exit_agent.exit_agent"),
    ("Portfolio Equity Agent", "agents.portfolio_equity_agent.portfolio_equity_agent"),
    ("Journal Agent", "agents.journal_agent.journal_agent"),
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_module(label: str, module_path: str) -> None:
    print(f"\n=== Running {label} ===\n")

    result = subprocess.run(
        [sys.executable, "-m", module_path],
        capture_output=True,
        text=True,
    )

    if result.stdout:
        print(result.stdout)

    if result.returncode != 0:
        if result.stderr:
            print(result.stderr)
        raise RuntimeError(f"{label} failed.")


def run_pipeline(steps: Iterable[tuple[str, str]]) -> None:
    for label, module_path in steps:
        run_module(label, module_path)


def _persist_failure(
    *,
    run_id: str,
    error: object,
    state_dir,
    runs_dir,
    failed_agent: str,
    event_message: str,
) -> list[str]:
    """Best-effort layered failure recording with a direct history fallback."""
    message = redact_failure_message(error)
    recording_errors: list[str] = []
    try:
        record_failed_finalization(
            run_id=run_id,
            error=message,
            state_dir=state_dir,
            runs_dir=runs_dir,
            failed_agent=failed_agent,
        )
    except Exception as exc:
        recording_errors.append(f"finalization_record={redact_failure_message(exc)}")
        try:
            run_history.force_fail_run_record(
                run_id=run_id,
                completed_at=utc_now_iso(),
                failed_agent=failed_agent,
                error_message=message,
            )
        except Exception as fallback_exc:
            recording_errors.append(
                f"run_history_fallback={redact_failure_message(fallback_exc)}"
            )
    try:
        append_run_lifecycle_event(
            run_id=run_id,
            event_type="run_failed",
            message=event_message,
            severity="error",
            details={
                "completed_at": utc_now_iso(),
                "failed_agent": failed_agent,
                "error_message": message,
                "recording_errors": recording_errors,
            },
        )
    except Exception as exc:
        recording_errors.append(f"event_log={redact_failure_message(exc)}")
    return recording_errors


def main() -> None:
    run_id = get_or_create_run_id()
    started_at = utc_now_iso()
    state_dir = run_history.RUN_HISTORY_PATH.parent
    runs_dir = state_dir.parent / "runs"
    run_history.start_run_record(run_id=run_id, started_at=started_at)

    try:
        capture_pre_economic_state(
            run_id,
            state_dir=state_dir,
            runs_dir=runs_dir,
        )
        append_run_lifecycle_event(
            run_id=run_id,
            event_type="run_started",
            message="Pipeline run started",
            details={"started_at": started_at},
        )
        run_pipeline(PIPELINE_STEPS)
    except Exception as exc:
        failed_agent = "Run Finalizer"
        message = redact_failure_message(exc)

        if message.endswith(" failed."):
            failed_agent = message[:-8]

        recording_errors = _persist_failure(
            run_id=run_id,
            error=message,
            state_dir=state_dir,
            runs_dir=runs_dir,
            failed_agent=failed_agent,
            event_message="Pipeline run failed",
        )
        if recording_errors:
            raise RuntimeError(
                f"{message}; failure recording encountered: {'; '.join(recording_errors)}"
            ) from exc
        raise

    try:
        result = finalize_run(
            run_id,
            state_dir=state_dir,
            runs_dir=runs_dir,
        )
    except Exception as exc:
        recording_errors = _persist_failure(
            run_id=run_id,
            error=exc,
            state_dir=state_dir,
            runs_dir=runs_dir,
            failed_agent="Run Finalizer",
            event_message="Pipeline finalization failed",
        )
        if recording_errors:
            raise RuntimeError(
                f"{redact_failure_message(exc)}; failure recording encountered: "
                f"{'; '.join(recording_errors)}"
            ) from exc
        raise

    print(
        "\nPipeline finalized successfully: "
        f"finalization_id={result.finalization_id}, manifest={result.manifest_path}"
    )


if __name__ == "__main__":
    main()
