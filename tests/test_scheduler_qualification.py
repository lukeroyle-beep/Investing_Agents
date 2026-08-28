from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from execution.store import ExecutionStore
from scheduler.config import SchedulerConfig
from scheduler.lock import ScheduleLockError, ScheduleRunLock
from scheduler.qualification import evaluate_gate_c
from scheduler.runner import (
    PreparationResult,
    PreparedApprovalWait,
    ReconciliationStatus,
    ScheduledDemoRunner,
    ScheduledRunError,
)
from scheduler.store import ScheduleStore, ScheduleStoreError
from shared.paths import SCHEDULER_CONFIG_PATH


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=UTC)


def _config(*, enabled: bool = True) -> SchedulerConfig:
    checked = SchedulerConfig.load(SCHEDULER_CONFIG_PATH)
    return SchedulerConfig(
        enabled=enabled,
        environment=checked.environment,
        broker_submission_enabled=False,
        per_order_human_approval_required=True,
        approval_ttl_seconds=checked.approval_ttl_seconds,
        heartbeat_interval_seconds=checked.heartbeat_interval_seconds,
        minimum_trading_sessions=checked.minimum_trading_sessions,
        minimum_approved_mutation_cycles=checked.minimum_approved_mutation_cycles,
        required_fault_drills=checked.required_fault_drills,
    )


def test_checked_in_scheduler_is_disabled_and_has_no_submit_capability():
    config = SchedulerConfig.load(SCHEDULER_CONFIG_PATH)
    assert not config.enabled
    assert not config.broker_submission_enabled
    assert config.per_order_human_approval_required
    assert config.minimum_trading_sessions == 30
    assert config.minimum_approved_mutation_cycles == 20


def test_schedule_lock_rejects_concurrency_and_wrong_owner(tmp_path):
    lock = ScheduleRunLock(tmp_path / "control" / "schedule.lock")
    lease = lock.acquire(run_id="RUN_1", now=NOW)
    with pytest.raises(ScheduleLockError, match="exists"):
        lock.acquire(run_id="RUN_2", now=NOW)
    wrong = type(lease)(token=str(uuid4()), run_id=lease.run_id, acquired_at=lease.acquired_at)
    with pytest.raises(ScheduleLockError, match="ownership"):
        lock.release(wrong)
    lock.release(lease)
    assert not lock.path.exists()


def test_runner_prepares_approval_waits_but_never_submits(tmp_path):
    calls = {"prepare": 0, "reconcile": 0, "nightly": 0}
    command_id = uuid4()

    def prepare():
        calls["prepare"] += 1
        return PreparationResult(
            finalization_passed=True,
            approval_waits=(
                PreparedApprovalWait(command_id=command_id, intent_hash="a" * 64),
            ),
        )

    def reconcile():
        calls["reconcile"] += 1
        return ReconciliationStatus(unresolved_count=0, duplicate_order_count=0)

    def nightly():
        calls["nightly"] += 1
        return True

    store = ScheduleStore(tmp_path / "control" / "schedule.sqlite3")
    runner = ScheduledDemoRunner(
        config=_config(),
        store=store,
        run_lock=ScheduleRunLock(tmp_path / "control" / "schedule.lock"),
        prepare=prepare,
        reconcile=reconcile,
        nightly_check=nightly,
    )
    runner.run(
        trading_session="2026-08-28", pipeline_run_id="RUN_SCHEDULE", now=NOW
    )
    assert calls == {"prepare": 1, "reconcile": 1, "nightly": 1}
    waits = store.rows("approval_waits")
    assert len(waits) == 1 and waits[0]["state"] == "awaiting"
    runs = store.rows("schedule_runs")
    assert runs[0]["state"] == "succeeded"


def test_disabled_runner_and_stale_approval_fail_closed(tmp_path):
    runner = ScheduledDemoRunner(
        config=_config(enabled=False),
        store=ScheduleStore(tmp_path / "schedule.sqlite3"),
        run_lock=ScheduleRunLock(tmp_path / "schedule.lock"),
        prepare=lambda: PreparationResult(finalization_passed=True),
        reconcile=lambda: ReconciliationStatus(0, 0),
        nightly_check=lambda: True,
    )
    with pytest.raises(ScheduledRunError, match="disabled"):
        runner.run(trading_session="2026-08-28", pipeline_run_id="RUN_DISABLED")

    store = runner.store
    schedule_run_id = store.start_run(
        trading_session="2026-08-28", pipeline_run_id="RUN_APPROVAL", now=NOW
    )
    wait = store.request_approval(
        schedule_run_id=schedule_run_id,
        command_id=uuid4(),
        intent_hash="b" * 64,
        requested_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    with pytest.raises(ScheduleStoreError, match="stale"):
        store.approve_wait(
            wait.wait_id,
            approval_id=uuid4(),
            operator_id="operator@example.test",
            now=NOW + timedelta(minutes=5),
            human_confirmed=True,
        )
    assert store.expire_approvals(now=NOW + timedelta(minutes=5)) == 1


def test_gate_c_cannot_qualify_without_real_accumulated_evidence(tmp_path):
    report = evaluate_gate_c(
        schedule_store=ScheduleStore(tmp_path / "schedule.sqlite3"),
        execution_store=ExecutionStore(tmp_path / "execution.sqlite3"),
        config=_config(),
    )
    assert not report.qualified
    assert report.trading_sessions == 0
    assert any("trading_sessions=0/30" in item for item in report.blockers)
    assert any("approved_mutation_cycles=0/20" in item for item in report.blockers)


def test_gate_c_qualifies_only_after_all_thresholds_and_fault_drills(tmp_path):
    schedule = ScheduleStore(tmp_path / "schedule.sqlite3")
    execution = ExecutionStore(tmp_path / "execution.sqlite3")
    config = _config()
    start = date(2026, 1, 2)
    for index in range(30):
        session = start + timedelta(days=index)
        schedule_run_id = schedule.start_run(
            trading_session=session,
            pipeline_run_id=f"RUN_{index:02d}",
            now=NOW + timedelta(days=index),
        )
        schedule.finish_run(
            schedule_run_id,
            succeeded=True,
            finalization_passed=True,
            nightly_passed=True,
            duplicate_orders=0,
            unresolved_reconciliation=0,
            now=NOW + timedelta(days=index, minutes=1),
        )
    for index in range(20):
        schedule.record_mutation_cycle(
            trading_session=start + timedelta(days=index),
            command_id=uuid4(),
            manually_approved=True,
            terminal_state="filled",
            duplicate_detected=False,
            reconciliation_resolved=True,
            recovery_verified=True,
            now=NOW + timedelta(days=index),
        )
    for drill in config.required_fault_drills:
        schedule.record_fault_drill(
            trading_session=start,
            drill_name=drill,
            passed=True,
            evidence=f"verified evidence for {drill}",
            now=NOW,
        )
    report = evaluate_gate_c(
        schedule_store=schedule,
        execution_store=execution,
        config=config,
    )
    assert report.qualified
    assert report.trading_sessions == 30
    assert report.approved_mutation_cycles == 20
    assert report.blockers == ()
