from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from execution.domain import CommandState
from execution.store import ExecutionStore
from scheduler.config import SchedulerConfig
from scheduler.lock import ScheduleRunLock
from scheduler.runner import (
    PreparationResult,
    ReconciliationStatus,
    ScheduledDemoRunner,
)
from scheduler.store import ScheduleStore
from shared.paths import (
    EXECUTION_STORE_PATH,
    PROJECT_ROOT,
    SCHEDULE_LOCK_PATH,
    SCHEDULE_STORE_PATH,
    SCHEDULER_CONFIG_PATH,
)


def _run_module(module: str, *, run_id: str) -> bool:
    environment = dict(os.environ)
    environment["TRADING_PIPELINE_RUN_ID"] = run_id
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare/reconcile a scheduled Demo run; never submit orders."
    )
    parser.add_argument("--session", default=date.today().isoformat())
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    config = SchedulerConfig.load(SCHEDULER_CONFIG_PATH)
    execution_store = ExecutionStore(EXECUTION_STORE_PATH)

    def prepare() -> PreparationResult:
        pipeline_ok = _run_module("run_pipeline", run_id=args.run_id)
        if pipeline_ok:
            _run_module("agents.execution_agent.execution_agent", run_id=args.run_id)
        return PreparationResult(finalization_passed=pipeline_ok)

    def reconcile() -> ReconciliationStatus:
        unresolved_states = tuple(
            state
            for state in CommandState
            if state not in {CommandState.RECONCILED, CommandState.RISK_REJECTED}
        )
        return ReconciliationStatus(
            unresolved_count=len(execution_store.list_commands(unresolved_states)),
            duplicate_order_count=0,
        )

    def nightly() -> bool:
        return _run_module("scripts.nightly_checklist", run_id=args.run_id)

    runner = ScheduledDemoRunner(
        config=config,
        store=ScheduleStore(SCHEDULE_STORE_PATH),
        run_lock=ScheduleRunLock(SCHEDULE_LOCK_PATH),
        prepare=prepare,
        reconcile=reconcile,
        nightly_check=nightly,
    )
    schedule_run_id = runner.run(
        trading_session=args.session,
        pipeline_run_id=args.run_id,
        now=datetime.now(UTC),
    )
    print(f"Scheduled Demo preparation completed: {schedule_run_id}")
    print("No broker submission capability was available to the scheduler.")


if __name__ == "__main__":
    main()
