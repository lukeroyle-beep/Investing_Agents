from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from typing import Iterable

from shared.run_context import get_or_create_run_id
from shared.run_history import complete_run_record, fail_run_record, start_run_record


PIPELINE_STEPS: list[tuple[str, str]] = [
    ("Universe Agent", "agents.universe_agent.universe_agent"),
    ("Signal Agent", "agents.signal_agent.signal_agent"),
    ("Macro Agent", "agents.macro_agent.macro_agent"),
    ("News Agent", "agents.news_agent.news_agent"),
    ("Risk Agent", "agents.risk_agent.risk_agent"),
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


def main() -> None:
    run_id = get_or_create_run_id()
    start_run_record(run_id=run_id, started_at=utc_now_iso())

    try:
        run_pipeline(PIPELINE_STEPS)
    except Exception as exc:
        failed_agent = "Unknown"
        message = str(exc).strip() or exc.__class__.__name__

        if message.endswith(" failed."):
            failed_agent = message[:-8]

        fail_run_record(
            run_id=run_id,
            completed_at=utc_now_iso(),
            failed_agent=failed_agent,
            error_message=message,
        )
        raise

    complete_run_record(
        run_id=run_id,
        completed_at=utc_now_iso(),
    )
    print("\nPipeline completed successfully.")


if __name__ == "__main__":
    main()
