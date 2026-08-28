from __future__ import annotations

from dataclasses import dataclass

from execution.domain import CommandState
from execution.store import ExecutionStore
from scheduler.config import SchedulerConfig
from scheduler.store import ScheduleStore


@dataclass(frozen=True, slots=True)
class QualificationReport:
    qualified: bool
    trading_sessions: int
    approved_mutation_cycles: int
    blockers: tuple[str, ...]


def evaluate_gate_c(
    *,
    schedule_store: ScheduleStore,
    execution_store: ExecutionStore,
    config: SchedulerConfig,
) -> QualificationReport:
    runs = schedule_store.rows("schedule_runs")
    cycles = schedule_store.rows("mutation_cycles")
    drills = schedule_store.rows("fault_drills")
    blockers: list[str] = []

    clean_runs = [
        row
        for row in runs
        if row["state"] == "succeeded"
        and int(row["finalization_passed"]) == 1
        and int(row["nightly_passed"]) == 1
        and int(row["duplicate_orders"]) == 0
        and int(row["unresolved_reconciliation"]) == 0
    ]
    sessions = len({str(row["trading_session"]) for row in clean_runs})
    if sessions < config.minimum_trading_sessions:
        blockers.append(
            f"trading_sessions={sessions}/{config.minimum_trading_sessions}"
        )
    if len(clean_runs) != len(runs):
        blockers.append("one_or_more_schedule_runs_not_clean")

    approved_cycles = [row for row in cycles if int(row["manually_approved"]) == 1]
    if len(approved_cycles) < config.minimum_approved_mutation_cycles:
        blockers.append(
            "approved_mutation_cycles="
            f"{len(approved_cycles)}/{config.minimum_approved_mutation_cycles}"
        )
    if any(int(row["duplicate_detected"]) for row in cycles):
        blockers.append("duplicate_order_evidence")
    if any(not int(row["reconciliation_resolved"]) for row in cycles):
        blockers.append("unresolved_cycle_reconciliation")
    if any(not int(row["recovery_verified"]) for row in cycles):
        blockers.append("unverified_cycle_recovery")

    passed_drills = {
        str(row["drill_name"]) for row in drills if int(row["passed"]) == 1
    }
    missing_drills = sorted(set(config.required_fault_drills) - passed_drills)
    if missing_drills:
        blockers.append("missing_fault_drills=" + ",".join(missing_drills))
    failed_required = sorted(
        {
            str(row["drill_name"])
            for row in drills
            if str(row["drill_name"]) in config.required_fault_drills
            and int(row["passed"]) == 0
        }
    )
    if failed_required:
        blockers.append("failed_fault_drills=" + ",".join(failed_required))

    unresolved_states = tuple(
        state
        for state in CommandState
        if state not in {CommandState.RECONCILED, CommandState.RISK_REJECTED}
    )
    if execution_store.list_commands(unresolved_states):
        blockers.append("execution_commands_unresolved")
    return QualificationReport(
        qualified=not blockers,
        trading_sessions=sessions,
        approved_mutation_cycles=len(approved_cycles),
        blockers=tuple(blockers),
    )
