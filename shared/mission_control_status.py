from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

import pandas as pd

from shared.portfolio_state_helpers import is_closed_position_status, normalise_position_status, parse_boolean_flag

MISSION_CONTROL_BUSY = "Busy"
MISSION_CONTROL_IDLE = "Idle"
MISSION_CONTROL_BLOCKED = "Blocked"
MISSION_CONTROL_STATUSES = {MISSION_CONTROL_BUSY, MISSION_CONTROL_IDLE, MISSION_CONTROL_BLOCKED}

_BUSY_AGENT_STATUSES = {
    "active",
    "busy",
    "executing",
    "in_progress",
    "running",
    "working",
}
_BLOCKING_REVIEW_STATUSES = {
    "blocked",
    "hold_for_review",
    "manual_review_required",
    "pending_review",
    "review_required",
    "waiting",
    "waiting_for_review",
}
_TRUTHY_WAIT_TEXT = {
    "approval_required",
    "awaiting_approval",
    "awaiting_data",
    "blocked",
    "hold",
    "hold_for_review",
    "manual_review_required",
    "pending",
    "pending_approval",
    "pending_data",
    "review_required",
    "waiting",
    "waiting_for_approval",
    "waiting_for_data",
    "waiting_for_review",
}


@dataclass(frozen=True)
class MissionControlStatus:
    """Compact dashboard status for one lifecycle row."""

    status: str
    cause: str = ""
    blocked_since: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.status == MISSION_CONTROL_BLOCKED

    @property
    def is_busy(self) -> bool:
        # Blocked rows still count inside WIP/utilisation, but render separately.
        return self.status in {MISSION_CONTROL_BUSY, MISSION_CONTROL_BLOCKED}

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "mission_control_status": self.status,
            "mission_control_blocked_cause": self.cause,
            "mission_control_blocked_since": self.blocked_since,
            "mission_control_counts_as_wip": self.is_busy,
        }


def _first_present(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in row and not pd.isna(row[name]) and str(row[name]).strip() != "":
            return row[name]
    return None


def _normalise_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip().lower()


def _truthyish(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    try:
        return parse_boolean_flag(value)
    except ValueError:
        return _normalise_text(value) in _TRUTHY_WAIT_TEXT


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or pd.isna(value):
        return None
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime()


def _format_timestamp(value: Any) -> str:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return ""
    return timestamp.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _blocking_cause(row: Mapping[str, Any], *, now: datetime, owner_grace: timedelta) -> tuple[str, str]:
    explicit_blocked_since = _format_timestamp(_first_present(row, ["blocked_since", "wait_started_at", "blocked_at"]))

    if _truthyish(_first_present(row, ["blocked_flag", "is_blocked"])):
        cause = _normalise_text(_first_present(row, ["blocked_cause", "blocked_reason", "wait_reason"]))
        return (cause or "blocked_flag", explicit_blocked_since)

    if _truthyish(_first_present(row, ["manual_signoff_required", "manual_sign_off_required", "manual_review_required"])):
        return ("manual_signoff_wait", explicit_blocked_since)

    if _truthyish(_first_present(row, ["external_approval_required", "waiting_on_external_approval", "approval_wait", "approval_status"])):
        return ("external_approval_wait", explicit_blocked_since)

    if _truthyish(_first_present(row, ["waiting_on_data", "data_wait", "data_status"])):
        return ("external_data_wait", explicit_blocked_since)

    for name, cause in [("news_review_status", "news_review_wait"), ("risk_review_status", "risk_review_wait")]:
        if _normalise_text(row.get(name)) in _BLOCKING_REVIEW_STATUSES:
            return (cause, explicit_blocked_since)

    lifecycle = normalise_position_status(_first_present(row, ["lifecycle", "status", "position_status"]))
    if lifecycle == "exit_required":
        owner_engaged = _truthyish(_first_present(row, ["owner_engaged", "owner_active", "owner_acknowledged"]))
        owner = _first_present(row, ["owner", "owner_agent", "assignee", "assignee_agent", "assigneeAgentId"])
        last_engagement = _parse_timestamp(
            _first_present(row, ["owner_engaged_at", "last_owner_engagement_at", "last_agent_engagement_at", "last_updated"])
        )
        if not owner_engaged and owner is None and last_engagement is not None and now - last_engagement >= owner_grace:
            return ("exit_required_owner_grace_elapsed", _format_timestamp(last_engagement))

    return ("", "")


def map_mission_control_status(
    row: Mapping[str, Any],
    *,
    now: datetime | None = None,
    owner_grace_hours: int = 24,
) -> MissionControlStatus:
    """Map lifecycle and operational flags into Busy, Idle, or Blocked.

    Precedence is intentionally deterministic:
    1. closed lifecycle rows are Idle
    2. blocking waits are Blocked
    3. open/exit_required rows with busy agent or WIP flag are Busy
    4. everything else is Idle
    """

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    lifecycle = normalise_position_status(_first_present(row, ["lifecycle", "status", "position_status"]))
    if is_closed_position_status(lifecycle):
        return MissionControlStatus(MISSION_CONTROL_IDLE)

    blocked_cause, blocked_since = _blocking_cause(row, now=current_time, owner_grace=timedelta(hours=owner_grace_hours))
    if blocked_cause:
        return MissionControlStatus(MISSION_CONTROL_BLOCKED, blocked_cause, blocked_since)

    agent_status = _normalise_text(_first_present(row, ["agent_status", "worker_status", "execution_status"]))
    work_in_progress = _truthyish(_first_present(row, ["work_in_progress", "wip", "in_progress"]))
    if lifecycle in {"open", "exit_required"} and (agent_status in _BUSY_AGENT_STATUSES or work_in_progress):
        return MissionControlStatus(MISSION_CONTROL_BUSY)

    return MissionControlStatus(MISSION_CONTROL_IDLE)


def build_mission_control_status_frame(
    lifecycle_rows: pd.DataFrame,
    *,
    now: datetime | None = None,
    owner_grace_hours: int = 24,
) -> pd.DataFrame:
    """Append deterministic Mission Control status columns to lifecycle rows."""

    rows = []
    for _, row in lifecycle_rows.iterrows():
        rows.append(
            map_mission_control_status(row.to_dict(), now=now, owner_grace_hours=owner_grace_hours).as_dict()
        )
    status_frame = pd.DataFrame(rows)
    return pd.concat([lifecycle_rows.reset_index(drop=True), status_frame], axis=1)


def mission_control_rollup(lifecycle_rows: pd.DataFrame) -> dict[str, int | str]:
    """Return roll-up counts and chip text for Mission Control."""

    if "mission_control_status" not in lifecycle_rows.columns:
        lifecycle_rows = build_mission_control_status_frame(lifecycle_rows)

    counts = {status: 0 for status in [MISSION_CONTROL_BUSY, MISSION_CONTROL_BLOCKED, MISSION_CONTROL_IDLE]}
    raw_counts = lifecycle_rows["mission_control_status"].value_counts().to_dict()
    for status in counts:
        counts[status] = int(raw_counts.get(status, 0))

    return {
        "busy": counts[MISSION_CONTROL_BUSY],
        "blocked": counts[MISSION_CONTROL_BLOCKED],
        "idle": counts[MISSION_CONTROL_IDLE],
        "chips": f"Busy {counts[MISSION_CONTROL_BUSY]} | Blocked {counts[MISSION_CONTROL_BLOCKED]} | Idle {counts[MISSION_CONTROL_IDLE]}",
    }
