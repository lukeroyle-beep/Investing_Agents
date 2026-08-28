from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class ScheduleLockError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LockLease:
    token: str
    run_id: str
    acquired_at: datetime


class ScheduleRunLock:
    """An existing lock always fails closed; stale locks require recovery review."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def acquire(self, *, run_id: str, now: datetime | None = None) -> LockLease:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        token = str(uuid4())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {
            "schema_version": "1.0",
            "token": token,
            "run_id": str(run_id),
            "pid": os.getpid(),
            "acquired_at": moment.isoformat(),
        }
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise ScheduleLockError(
                "scheduler lock exists; inspect/recover it instead of overriding"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        return LockLease(token=token, run_id=str(run_id), acquired_at=moment)

    def release(self, lease: LockLease) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScheduleLockError("scheduler lock is missing or corrupt") from exc
        if payload.get("token") != lease.token or payload.get("run_id") != lease.run_id:
            raise ScheduleLockError("scheduler lock ownership mismatch")
        self.path.unlink()
