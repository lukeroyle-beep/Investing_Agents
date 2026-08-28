from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Callable
from uuid import UUID

from scheduler.config import SchedulerConfig
from scheduler.lock import ScheduleRunLock
from scheduler.store import ScheduleStore


class ScheduledRunError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedApprovalWait:
    command_id: UUID
    intent_hash: str


@dataclass(frozen=True, slots=True)
class PreparationResult:
    finalization_passed: bool
    approval_waits: tuple[PreparedApprovalWait, ...] = ()


@dataclass(frozen=True, slots=True)
class ReconciliationStatus:
    unresolved_count: int
    duplicate_order_count: int


class ScheduledDemoRunner:
    """Prepares/reconciles runs and intentionally has no broker-submit callback."""

    def __init__(
        self,
        *,
        config: SchedulerConfig,
        store: ScheduleStore,
        run_lock: ScheduleRunLock,
        prepare: Callable[[], PreparationResult],
        reconcile: Callable[[], ReconciliationStatus],
        nightly_check: Callable[[], bool],
    ) -> None:
        self.config = config
        self.store = store
        self.run_lock = run_lock
        self.prepare = prepare
        self.reconcile = reconcile
        self.nightly_check = nightly_check

    def run(
        self,
        *,
        trading_session: date | str,
        pipeline_run_id: str,
        now: datetime | None = None,
    ) -> UUID:
        if not self.config.enabled:
            raise ScheduledRunError("scheduled Demo preparation is disabled")
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        lease = self.run_lock.acquire(run_id=pipeline_run_id, now=moment)
        schedule_run_id: UUID | None = None
        try:
            schedule_run_id = self.store.start_run(
                trading_session=trading_session,
                pipeline_run_id=pipeline_run_id,
                now=moment,
            )
            self.store.heartbeat(schedule_run_id, phase="preparing", now=moment)
            preparation = self.prepare()
            for wait in preparation.approval_waits:
                self.store.request_approval(
                    schedule_run_id=schedule_run_id,
                    command_id=wait.command_id,
                    intent_hash=wait.intent_hash,
                    requested_at=moment,
                    expires_at=moment
                    + timedelta(seconds=self.config.approval_ttl_seconds),
                )
            self.store.heartbeat(schedule_run_id, phase="reconciling", now=moment)
            reconciliation = self.reconcile()
            nightly_passed = self.nightly_check()
            succeeded = (
                preparation.finalization_passed
                and nightly_passed
                and reconciliation.unresolved_count == 0
                and reconciliation.duplicate_order_count == 0
            )
            self.store.finish_run(
                schedule_run_id,
                succeeded=succeeded,
                finalization_passed=preparation.finalization_passed,
                nightly_passed=nightly_passed,
                duplicate_orders=reconciliation.duplicate_order_count,
                unresolved_reconciliation=reconciliation.unresolved_count,
                now=moment,
                failure="" if succeeded else "scheduled validation did not pass",
            )
            return schedule_run_id
        except Exception as exc:
            if schedule_run_id is not None:
                try:
                    self.store.finish_run(
                        schedule_run_id,
                        succeeded=False,
                        finalization_passed=False,
                        nightly_passed=False,
                        duplicate_orders=0,
                        unresolved_reconciliation=1,
                        now=moment,
                        failure=exc.__class__.__name__,
                    )
                except Exception:
                    pass
            raise
        finally:
            self.run_lock.release(lease)
