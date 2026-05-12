from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from shared.mission_control_status import (
    MISSION_CONTROL_BLOCKED,
    MISSION_CONTROL_BUSY,
    MISSION_CONTROL_IDLE,
    build_mission_control_status_frame,
    map_mission_control_status,
    mission_control_rollup,
)

NOW = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)


def test_closed_lifecycle_rows_are_idle_even_when_flags_are_set() -> None:
    status = map_mission_control_status(
        {"status": "closed", "agent_status": "running", "work_in_progress": True, "blocked_flag": True},
        now=NOW,
    )

    assert status.status == MISSION_CONTROL_IDLE
    assert status.cause == ""
    assert status.is_busy is False


def test_blocked_flag_maps_to_blocked_and_counts_as_wip() -> None:
    status = map_mission_control_status(
        {
            "status": "open",
            "blocked_flag": "true",
            "blocked_reason": "waiting_on_board",
            "blocked_since": "2026-04-24T09:30:00Z",
        },
        now=NOW,
    )

    assert status.status == MISSION_CONTROL_BLOCKED
    assert status.cause == "waiting_on_board"
    assert status.blocked_since == "2026-04-24T09:30:00Z"
    assert status.is_busy is True


def test_manual_approval_data_news_and_risk_waits_are_blocked() -> None:
    cases = [
        ({"status": "open", "manual_signoff_required": True}, "manual_signoff_wait"),
        ({"status": "open", "approval_status": "pending_approval"}, "external_approval_wait"),
        ({"status": "open", "data_status": "waiting_for_data"}, "external_data_wait"),
        ({"status": "open", "news_review_status": "review_required"}, "news_review_wait"),
        ({"status": "open", "risk_review_status": "hold_for_review"}, "risk_review_wait"),
    ]

    for row, cause in cases:
        assert map_mission_control_status(row, now=NOW).status == MISSION_CONTROL_BLOCKED
        assert map_mission_control_status(row, now=NOW).cause == cause


def test_open_or_exit_required_busy_agent_or_wip_maps_to_busy() -> None:
    assert map_mission_control_status({"status": "open", "agent_status": "running"}, now=NOW).status == MISSION_CONTROL_BUSY
    assert map_mission_control_status({"status": "exit_required", "work_in_progress": "yes"}, now=NOW).status == MISSION_CONTROL_BUSY


def test_exit_required_without_owner_engagement_after_grace_is_blocked() -> None:
    status = map_mission_control_status(
        {"status": "exit_required", "last_updated": "2026-04-22T11:00:00Z"},
        now=NOW,
        owner_grace_hours=24,
    )

    assert status.status == MISSION_CONTROL_BLOCKED
    assert status.cause == "exit_required_owner_grace_elapsed"
    assert status.blocked_since == "2026-04-22T11:00:00Z"


def test_exit_required_with_recent_update_or_owner_is_idle_when_not_busy() -> None:
    assert (
        map_mission_control_status(
            {"status": "exit_required", "last_updated": "2026-04-24T11:00:00Z"},
            now=NOW,
            owner_grace_hours=24,
        ).status
        == MISSION_CONTROL_IDLE
    )
    assert (
        map_mission_control_status(
            {"status": "exit_required", "owner_agent": "Nova", "last_updated": "2026-04-22T11:00:00Z"},
            now=NOW,
            owner_grace_hours=24,
        ).status
        == MISSION_CONTROL_IDLE
    )


def test_frame_builder_and_rollup_emit_dashboard_columns_and_chips() -> None:
    frame = pd.DataFrame(
        [
            {"position_id": "P1", "status": "open", "agent_status": "running"},
            {"position_id": "P2", "status": "open", "blocked_flag": True},
            {"position_id": "P3", "status": "closed", "blocked_flag": True},
        ]
    )

    mapped = build_mission_control_status_frame(frame, now=NOW)
    assert list(mapped["mission_control_status"]) == [MISSION_CONTROL_BUSY, MISSION_CONTROL_BLOCKED, MISSION_CONTROL_IDLE]
    assert list(mapped["mission_control_counts_as_wip"]) == [True, True, False]

    assert mission_control_rollup(mapped) == {
        "busy": 1,
        "blocked": 1,
        "idle": 1,
        "chips": "Busy 1 | Blocked 1 | Idle 1",
    }
