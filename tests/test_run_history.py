from __future__ import annotations

import pandas as pd
import pytest

import shared.run_history as run_history


def _patch_run_history_path(isolated_workspace, monkeypatch):
    path = isolated_workspace / "data" / "run_history.csv"
    monkeypatch.setattr(run_history, "RUN_HISTORY_PATH", path)
    return path


def test_start_run_record_rejects_blank_identifiers(isolated_workspace, monkeypatch) -> None:
    _patch_run_history_path(isolated_workspace, monkeypatch)

    with pytest.raises(ValueError, match="run_id must be non-blank"):
        run_history.start_run_record(run_id=" ", started_at="2026-03-28T10:00:00+00:00")

    with pytest.raises(ValueError, match="started_at must be non-blank"):
        run_history.start_run_record(run_id="RUN_START", started_at=" ")


def test_complete_run_record_rejects_blank_completed_at(isolated_workspace, monkeypatch) -> None:
    path = _patch_run_history_path(isolated_workspace, monkeypatch)
    run_history.start_run_record(run_id="RUN_COMPLETE", started_at="2026-03-28T10:00:00+00:00")

    with pytest.raises(ValueError, match="completed_at must be non-blank"):
        run_history.complete_run_record(run_id="RUN_COMPLETE", completed_at=" ")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert df.iloc[0]["status"] == "running"
    assert df.iloc[0]["completed_at"] == ""


def test_run_history_disallows_second_terminal_transition(isolated_workspace, monkeypatch) -> None:
    path = _patch_run_history_path(isolated_workspace, monkeypatch)
    run_history.start_run_record(run_id="RUN_TERMINAL", started_at="2026-03-28T10:00:00+00:00")
    run_history.complete_run_record(run_id="RUN_TERMINAL", completed_at="2026-03-28T10:05:00+00:00")

    with pytest.raises(ValueError, match="already terminal or invalid"):
        run_history.fail_run_record(
            run_id="RUN_TERMINAL",
            completed_at="2026-03-28T10:06:00+00:00",
            failed_agent="Lifecycle Integrity Agent",
            error_message="should not overwrite terminal status",
        )

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert df.iloc[0]["status"] == "success"
    assert df.iloc[0]["completed_at"] == "2026-03-28T10:05:00+00:00"


def test_fail_run_record_allows_single_running_to_failed_transition(isolated_workspace, monkeypatch) -> None:
    path = _patch_run_history_path(isolated_workspace, monkeypatch)
    run_history.start_run_record(run_id="RUN_FAIL", started_at="2026-03-28T10:00:00+00:00")

    run_history.fail_run_record(
        run_id="RUN_FAIL",
        completed_at="2026-03-28T10:04:00+00:00",
        failed_agent="Exit Agent",
        error_message="synthetic failure",
    )

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert df.iloc[0]["status"] == "failed"
    assert df.iloc[0]["completed_at"] == "2026-03-28T10:04:00+00:00"
    assert df.iloc[0]["failed_agent"] == "Exit Agent"


def test_start_run_record_fails_closed_when_previous_run_is_still_running(isolated_workspace, monkeypatch) -> None:
    path = _patch_run_history_path(isolated_workspace, monkeypatch)
    run_history.start_run_record(run_id="RUN_INTERRUPTED", started_at="2026-03-28T10:00:00+00:00")

    with pytest.raises(RuntimeError, match="previous run-history records remain running: RUN_INTERRUPTED"):
        run_history.start_run_record(run_id="RUN_NEXT", started_at="2026-03-28T10:05:00+00:00")

    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert df["run_id"].tolist() == ["RUN_INTERRUPTED"]
    assert df.iloc[0]["status"] == "running"


def test_running_run_detection_ignores_current_run_id(isolated_workspace, monkeypatch) -> None:
    _patch_run_history_path(isolated_workspace, monkeypatch)
    run_history.start_run_record(run_id="RUN_CURRENT", started_at="2026-03-28T10:00:00+00:00")

    run_history.assert_no_unresolved_running_runs(new_run_id="RUN_CURRENT")

    running = run_history.find_running_run_records()
    assert [row["run_id"] for row in running] == ["RUN_CURRENT"]
