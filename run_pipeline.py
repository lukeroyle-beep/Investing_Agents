from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from typing import Iterable

from shared.event_log import append_run_lifecycle_event
from shared.sqlite_parity import format_parity_report, validate_sqlite_dual_write_parity
from shared.run_reconciliation import print_run_reconciliation_summary, write_run_reconciliation_summary
from shared.run_context import get_or_create_run_id
from shared.run_history import complete_run_record, fail_run_record, start_run_record


PIPELINE_STEPS: list[tuple[str, str]] = [
    ("Universe Agent", "agents.universe_agent.universe_agent"),
    ("Macro Agent", "agents.macro_agent.macro_agent"),
    ("Signal Agent", "agents.signal_agent.signal_agent"),
    ("Risk Agent", "agents.risk_agent.risk_agent"),
    ("News Agent", "agents.news_agent.news_agent"),
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


def emit_reconciliation_summary(run_id: str) -> None:
    try:
        row = write_run_reconciliation_summary(run_id)
        print_run_reconciliation_summary(row)
        print()
        print(format_parity_report(validate_sqlite_dual_write_parity(run_id=run_id)))
    except Exception as exc:
        print("\nRun reconciliation summary could not be generated.")
        print(f"Reason: {exc}")


def main() -> None:
    run_id = get_or_create_run_id()
    started_at = utc_now_iso()
    start_run_record(run_id=run_id, started_at=started_at)
    append_run_lifecycle_event(
        run_id=run_id,
        event_type="run_started",
        message="Pipeline run started",
        details={"started_at": started_at},
    )

    try:
        run_pipeline(PIPELINE_STEPS)
    except Exception as exc:
        failed_agent = "Unknown"
        message = str(exc).strip() or exc.__class__.__name__

        if message.endswith(" failed."):
            failed_agent = message[:-8]

        completed_at = utc_now_iso()
        fail_run_record(
            run_id=run_id,
            completed_at=completed_at,
            failed_agent=failed_agent,
            error_message=message,
        )
        append_run_lifecycle_event(
            run_id=run_id,
            event_type="run_failed",
            message="Pipeline run failed",
            severity="error",
            details={
                "completed_at": completed_at,
                "failed_agent": failed_agent,
                "error_message": message,
            },
        )
        emit_reconciliation_summary(run_id)
        raise

    completed_at = utc_now_iso()
    complete_run_record(
        run_id=run_id,
        completed_at=completed_at,
    )
    append_run_lifecycle_event(
        run_id=run_id,
        event_type="run_completed",
        message="Pipeline run completed successfully",
        details={"completed_at": completed_at},
    )
    emit_reconciliation_summary(run_id)
    print("\nPipeline completed successfully.")


if __name__ == "__main__":
    main()
