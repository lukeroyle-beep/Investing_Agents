from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.store import ExecutionStore
from scheduler.config import SchedulerConfig
from scheduler.qualification import evaluate_gate_c
from scheduler.store import ScheduleStore
from shared.paths import (
    EXECUTION_STORE_PATH,
    SCHEDULE_STORE_PATH,
    SCHEDULER_CONFIG_PATH,
)


def main() -> None:
    report = evaluate_gate_c(
        schedule_store=ScheduleStore(SCHEDULE_STORE_PATH),
        execution_store=ExecutionStore(EXECUTION_STORE_PATH),
        config=SchedulerConfig.load(SCHEDULER_CONFIG_PATH),
    )
    print(
        json.dumps(
            {
                "qualified": report.qualified,
                "trading_sessions": report.trading_sessions,
                "approved_mutation_cycles": report.approved_mutation_cycles,
                "blockers": list(report.blockers),
            },
            sort_keys=True,
            indent=2,
        )
    )
    raise SystemExit(0 if report.qualified else 1)


if __name__ == "__main__":
    main()
