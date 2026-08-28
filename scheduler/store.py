from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID, uuid4


class ScheduleStoreError(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleStoreError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ApprovalWait:
    wait_id: UUID
    schedule_run_id: UUID
    command_id: UUID
    intent_hash: str
    state: str
    requested_at: datetime
    expires_at: datetime
    approval_id: UUID | None


class ScheduleStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    schedule_run_id TEXT PRIMARY KEY,
                    trading_session TEXT NOT NULL,
                    pipeline_run_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    finalization_passed INTEGER NOT NULL DEFAULT 0,
                    nightly_passed INTEGER NOT NULL DEFAULT 0,
                    duplicate_orders INTEGER NOT NULL DEFAULT 0,
                    unresolved_reconciliation INTEGER NOT NULL DEFAULT 0,
                    failure TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS approval_waits (
                    wait_id TEXT PRIMARY KEY,
                    schedule_run_id TEXT NOT NULL,
                    command_id TEXT NOT NULL UNIQUE,
                    intent_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    approval_id TEXT,
                    operator_id TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT,
                    FOREIGN KEY(schedule_run_id) REFERENCES schedule_runs(schedule_run_id)
                );
                CREATE TABLE IF NOT EXISTS heartbeats (
                    heartbeat_id TEXT PRIMARY KEY,
                    schedule_run_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    FOREIGN KEY(schedule_run_id) REFERENCES schedule_runs(schedule_run_id)
                );
                CREATE TABLE IF NOT EXISTS operator_actions (
                    action_id TEXT PRIMARY KEY,
                    schedule_run_id TEXT,
                    operator_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mutation_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    trading_session TEXT NOT NULL,
                    command_id TEXT NOT NULL UNIQUE,
                    manually_approved INTEGER NOT NULL,
                    terminal_state TEXT NOT NULL,
                    duplicate_detected INTEGER NOT NULL,
                    reconciliation_resolved INTEGER NOT NULL,
                    recovery_verified INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fault_drills (
                    drill_id TEXT PRIMARY KEY,
                    trading_session TEXT NOT NULL,
                    drill_name TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    evidence_checksum TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(trading_session, drill_name)
                );
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def start_run(
        self,
        *,
        trading_session: date | str,
        pipeline_run_id: str,
        now: datetime,
    ) -> UUID:
        run_id = uuid4()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO schedule_runs
                    (schedule_run_id, trading_session, pipeline_run_id, state, started_at)
                    VALUES (?, ?, ?, 'started', ?)
                    """,
                    (
                        str(run_id),
                        date.fromisoformat(str(trading_session)).isoformat(),
                        str(pipeline_run_id),
                        _utc(now).isoformat(),
                    ),
                )
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise ScheduleStoreError("schedule run identity already exists or is invalid") from exc
        return run_id

    def heartbeat(self, schedule_run_id: UUID | str, *, phase: str, now: datetime) -> None:
        if not str(phase).strip():
            raise ScheduleStoreError("heartbeat phase is required")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO heartbeats VALUES (?, ?, ?, ?)",
                (str(uuid4()), str(schedule_run_id), _utc(now).isoformat(), str(phase)),
            )

    def finish_run(
        self,
        schedule_run_id: UUID | str,
        *,
        succeeded: bool,
        finalization_passed: bool,
        nightly_passed: bool,
        duplicate_orders: int,
        unresolved_reconciliation: int,
        now: datetime,
        failure: str = "",
    ) -> None:
        if min(duplicate_orders, unresolved_reconciliation) < 0:
            raise ScheduleStoreError("run counters must not be negative")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE schedule_runs SET state = ?, completed_at = ?,
                    finalization_passed = ?, nightly_passed = ?, duplicate_orders = ?,
                    unresolved_reconciliation = ?, failure = ?
                WHERE schedule_run_id = ? AND state = 'started'
                """,
                (
                    "succeeded" if succeeded else "failed",
                    _utc(now).isoformat(),
                    int(finalization_passed),
                    int(nightly_passed),
                    duplicate_orders,
                    unresolved_reconciliation,
                    str(failure)[:500],
                    str(schedule_run_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ScheduleStoreError("schedule run is missing or already terminal")

    def request_approval(
        self,
        *,
        schedule_run_id: UUID | str,
        command_id: UUID | str,
        intent_hash: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> ApprovalWait:
        requested = _utc(requested_at)
        expiry = _utc(expires_at)
        if expiry <= requested or (expiry - requested).total_seconds() > 300:
            raise ScheduleStoreError("approval wait expiry is invalid")
        wait_id = uuid4()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO approval_waits
                    (wait_id, schedule_run_id, command_id, intent_hash, state,
                     requested_at, expires_at)
                    VALUES (?, ?, ?, ?, 'awaiting', ?, ?)
                    """,
                    (
                        str(wait_id),
                        str(schedule_run_id),
                        str(command_id),
                        str(intent_hash),
                        requested.isoformat(),
                        expiry.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ScheduleStoreError("command already has an approval wait") from exc
        return ApprovalWait(
            wait_id=wait_id,
            schedule_run_id=UUID(str(schedule_run_id)),
            command_id=UUID(str(command_id)),
            intent_hash=str(intent_hash),
            state="awaiting",
            requested_at=requested,
            expires_at=expiry,
            approval_id=None,
        )

    def approve_wait(
        self,
        wait_id: UUID | str,
        *,
        approval_id: UUID | str,
        operator_id: str,
        now: datetime,
        human_confirmed: bool,
    ) -> None:
        operator = str(operator_id).strip()
        if not human_confirmed or not operator:
            raise ScheduleStoreError("approval requires explicit human operator identity")
        moment = _utc(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_waits SET state = 'approved', approval_id = ?,
                    operator_id = ?, resolved_at = ?
                WHERE wait_id = ? AND state = 'awaiting' AND expires_at > ?
                """,
                (
                    str(approval_id),
                    operator,
                    moment.isoformat(),
                    str(wait_id),
                    moment.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ScheduleStoreError("approval wait is stale, missing, or already resolved")

    def expire_approvals(self, *, now: datetime) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE approval_waits SET state = 'expired', resolved_at = ?
                WHERE state = 'awaiting' AND expires_at <= ?
                """,
                (_utc(now).isoformat(), _utc(now).isoformat()),
            )
            return int(cursor.rowcount)

    def record_operator_action(
        self,
        *,
        operator_id: str,
        action: str,
        reason: str,
        now: datetime,
        schedule_run_id: UUID | str | None = None,
    ) -> None:
        if not all(str(item).strip() for item in (operator_id, action, reason)):
            raise ScheduleStoreError("operator action identity/reason is incomplete")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO operator_actions VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    str(schedule_run_id) if schedule_run_id else None,
                    str(operator_id),
                    str(action),
                    str(reason),
                    _utc(now).isoformat(),
                ),
            )

    def record_mutation_cycle(
        self,
        *,
        trading_session: date | str,
        command_id: UUID | str,
        manually_approved: bool,
        terminal_state: str,
        duplicate_detected: bool,
        reconciliation_resolved: bool,
        recovery_verified: bool,
        now: datetime,
    ) -> UUID:
        if terminal_state not in {"filled", "partially_filled", "rejected", "cancelled"}:
            raise ScheduleStoreError("mutation cycle is not terminal")
        cycle_id = uuid4()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO mutation_cycles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(cycle_id),
                        date.fromisoformat(str(trading_session)).isoformat(),
                        str(command_id),
                        int(manually_approved),
                        terminal_state,
                        int(duplicate_detected),
                        int(reconciliation_resolved),
                        int(recovery_verified),
                        _utc(now).isoformat(),
                    ),
                )
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise ScheduleStoreError("mutation cycle identity already exists or is invalid") from exc
        return cycle_id

    def record_fault_drill(
        self,
        *,
        trading_session: date | str,
        drill_name: str,
        passed: bool,
        evidence: str,
        now: datetime,
    ) -> UUID:
        if not str(drill_name).strip() or not str(evidence).strip():
            raise ScheduleStoreError("fault drill requires named evidence")
        drill_id = uuid4()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO fault_drills VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(drill_id),
                        date.fromisoformat(str(trading_session)).isoformat(),
                        str(drill_name),
                        int(passed),
                        hashlib.sha256(str(evidence).encode("utf-8")).hexdigest(),
                        _utc(now).isoformat(),
                    ),
                )
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise ScheduleStoreError("fault drill evidence already exists or is invalid") from exc
        return drill_id

    def rows(self, table: str) -> list[dict[str, object]]:
        if table not in {
            "schedule_runs",
            "approval_waits",
            "heartbeats",
            "operator_actions",
            "mutation_cycles",
            "fault_drills",
        }:
            raise ScheduleStoreError("unknown schedule table")
        with closing(self._connect()) as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
