from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution.domain import Environment, _aware_utc, canonical_json


KILL_SWITCH_SCHEMA_VERSION = "1.0"
RESET_ACKNOWLEDGEMENT = "I UNDERSTAND DEMO WRITES MAY RESUME"


class KillSwitchError(RuntimeError):
    """Kill-switch state cannot be safely changed."""


@dataclass(frozen=True, slots=True)
class KillSwitchStatus:
    engaged: bool
    integrity_valid: bool
    environment: Environment
    revision: int
    reason: str
    updated_at: datetime | None

    @property
    def writes_allowed(self) -> bool:
        return self.integrity_valid and not self.engaged


class KillSwitch:
    """Persistent fail-closed switch with a hash-chained append-only audit."""

    def __init__(
        self,
        *,
        state_path: Path | str,
        audit_path: Path | str,
        environment: Environment | str = Environment.DEMO,
    ) -> None:
        self.state_path = Path(state_path)
        self.audit_path = Path(audit_path)
        self.environment = Environment(environment)
        if self.environment != Environment.DEMO:
            raise KillSwitchError("only the Demo kill switch is authorized")

    def _ensure_directory(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in {self.state_path.parent, self.audit_path.parent}:
            try:
                directory.chmod(0o700)
            except OSError:
                pass

    def _atomic_write_state(self, payload: dict[str, Any]) -> None:
        self._ensure_directory()
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.state_path)
            self.state_path.chmod(0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _read_audit(self) -> tuple[list[dict[str, Any]], str]:
        if not self.audit_path.exists():
            return [], ""
        events: list[dict[str, Any]] = []
        previous_hash = ""
        with self.audit_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise KillSwitchError(
                        f"kill-switch audit is corrupt at line {line_number}"
                    ) from exc
                recorded_hash = str(event.pop("event_hash", ""))
                if event.get("previous_hash", "") != previous_hash:
                    raise KillSwitchError("kill-switch audit chain is broken")
                calculated = hashlib.sha256(
                    canonical_json(event).encode("utf-8")
                ).hexdigest()
                if recorded_hash != calculated:
                    raise KillSwitchError("kill-switch audit checksum mismatch")
                event["event_hash"] = recorded_hash
                events.append(event)
                previous_hash = recorded_hash
        return events, previous_hash

    def _append_audit(self, event: dict[str, Any], previous_hash: str) -> str:
        self._ensure_directory()
        record = dict(event)
        record["previous_hash"] = previous_hash
        record["event_hash"] = hashlib.sha256(
            canonical_json(record).encode("utf-8")
        ).hexdigest()
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.audit_path.chmod(0o600)
        return str(record["event_hash"])

    @staticmethod
    def _failsafe(reason: str, environment: Environment) -> KillSwitchStatus:
        return KillSwitchStatus(
            engaged=True,
            integrity_valid=False,
            environment=environment,
            revision=0,
            reason=reason,
            updated_at=None,
        )

    def status(self) -> KillSwitchStatus:
        if not self.state_path.exists() or not self.audit_path.exists():
            return self._failsafe("kill-switch state is not initialized", self.environment)
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            events, head_hash = self._read_audit()
            if not events:
                raise KillSwitchError("kill-switch audit is empty")
            if raw.get("schema_version") != KILL_SWITCH_SCHEMA_VERSION:
                raise KillSwitchError("kill-switch schema is unsupported")
            environment = Environment(raw["environment"])
            updated_at = datetime.fromisoformat(
                str(raw["updated_at"]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            revision = int(raw["revision"])
            engaged = raw["engaged"] is True
            last_event = events[-1]
            if raw.get("audit_head_hash") != head_hash:
                raise KillSwitchError("kill-switch state/audit head mismatch")
            if int(last_event["revision"]) != revision:
                raise KillSwitchError("kill-switch state/audit revision mismatch")
            if (last_event["resulting_engaged"] is True) != engaged:
                raise KillSwitchError("kill-switch state/audit outcome mismatch")
            if environment != self.environment:
                raise KillSwitchError("kill-switch environment mismatch")
            return KillSwitchStatus(
                engaged=engaged,
                integrity_valid=True,
                environment=environment,
                revision=revision,
                reason=str(raw["reason"]),
                updated_at=updated_at,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, KillSwitchError) as exc:
            return self._failsafe(str(exc), self.environment)

    def initialize_engaged(
        self,
        *,
        operator_id: str,
        reason: str,
        now: datetime,
    ) -> KillSwitchStatus:
        if self.state_path.exists() or self.audit_path.exists():
            raise KillSwitchError("kill switch is already initialized")
        return self._change(
            engaged=True,
            action="initialize_engaged",
            operator_id=operator_id,
            reason=reason,
            now=now,
            prior_revision=0,
            previous_hash="",
        )

    def engage(
        self,
        *,
        operator_id: str,
        reason: str,
        now: datetime,
    ) -> KillSwitchStatus:
        current = self.status()
        if not current.integrity_valid:
            raise KillSwitchError(
                "kill switch is already fail-closed but its audit must be repaired before mutation"
            )
        _, previous_hash = self._read_audit()
        return self._change(
            engaged=True,
            action="engage",
            operator_id=operator_id,
            reason=reason,
            now=now,
            prior_revision=current.revision,
            previous_hash=previous_hash,
        )

    def reset(
        self,
        *,
        operator_id: str,
        reason: str,
        acknowledgement: str,
        now: datetime,
    ) -> KillSwitchStatus:
        if acknowledgement != RESET_ACKNOWLEDGEMENT:
            raise KillSwitchError("interactive reset acknowledgement did not match")
        current = self.status()
        if not current.integrity_valid or not current.engaged:
            raise KillSwitchError("kill switch is corrupt, missing, or already reset")
        _, previous_hash = self._read_audit()
        return self._change(
            engaged=False,
            action="reset",
            operator_id=operator_id,
            reason=reason,
            now=now,
            prior_revision=current.revision,
            previous_hash=previous_hash,
        )

    def _change(
        self,
        *,
        engaged: bool,
        action: str,
        operator_id: str,
        reason: str,
        now: datetime,
        prior_revision: int,
        previous_hash: str,
    ) -> KillSwitchStatus:
        operator = str(operator_id).strip()
        rationale = str(reason).strip()
        if not operator or not rationale:
            raise KillSwitchError("operator identity and reason are required")
        moment = _aware_utc(now, "now")
        revision = prior_revision + 1
        event = {
            "schema_version": KILL_SWITCH_SCHEMA_VERSION,
            "environment": str(self.environment),
            "revision": revision,
            "action": action,
            "operator_id": operator,
            "reason": rationale,
            "timestamp": moment.isoformat(),
            "resulting_engaged": engaged,
        }
        head_hash = self._append_audit(event, previous_hash)
        self._atomic_write_state(
            {
                "schema_version": KILL_SWITCH_SCHEMA_VERSION,
                "environment": str(self.environment),
                "revision": revision,
                "engaged": engaged,
                "reason": rationale,
                "operator_id": operator,
                "updated_at": moment.isoformat(),
                "audit_head_hash": head_hash,
            }
        )
        status = self.status()
        if not status.integrity_valid:
            raise KillSwitchError("kill-switch mutation could not be verified")
        return status
