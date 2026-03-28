from __future__ import annotations

import pandas as pd

from agents.lifecycle_integrity_agent import lifecycle_integrity_agent
from tests.helpers import (
    cash_state_frame,
    closed_position_row,
    open_position_row,
    portfolio_state_frame,
    processed_fills_frame,
    run_history_frame,
    write_csv,
)


def _patch_lifecycle_paths(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(lifecycle_integrity_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "REPORT_PATH", str(data_dir / "lifecycle_integrity_report.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "SNAPSHOT_PATH", str(data_dir / "portfolio_state_prev_snapshot.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "CASH_STATE_PATH", str(data_dir / "cash_state.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "EQUITY_HISTORY_PATH", str(data_dir / "portfolio_equity_history.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "PROCESSED_FILLS_PATH", str(data_dir / "processed_fills.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "CASH_LEDGER_PATH", str(data_dir / "cash_ledger.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "RUN_HISTORY_PATH", str(data_dir / "run_history.csv"))


def test_lifecycle_integrity_report_rows_on_failure(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _patch_lifecycle_paths(isolated_workspace, monkeypatch)
    monkeypatch.setattr(lifecycle_integrity_agent, "get_or_create_run_id", lambda: "RUN_LIFECYCLE_FAIL")

    current_state = portfolio_state_frame([closed_position_row(exit_price=111.0, run_id="RUN_LIFECYCLE_FAIL")])
    previous_state = portfolio_state_frame([closed_position_row(exit_price=110.0, run_id="RUN_PREV")])

    write_csv(data_dir / "portfolio_state.csv", current_state)
    write_csv(data_dir / "portfolio_state_prev_snapshot.csv", previous_state)
    write_csv(data_dir / "cash_state.csv", cash_state_frame())
    write_csv(data_dir / "processed_fills.csv", processed_fills_frame([]))
    write_csv(
        data_dir / "run_history.csv",
        run_history_frame(
            [
                {
                    "run_id": "RUN_LIFECYCLE_FAIL",
                    "started_at": "2026-03-28T10:00:00+00:00",
                    "completed_at": "",
                    "status": "running",
                    "failed_agent": "",
                    "error_message": "",
                    "notes": "",
                }
            ]
        ),
    )

    try:
        lifecycle_integrity_agent.run_lifecycle_integrity_agent()
        assert False, "Lifecycle Integrity Agent should hard-fail on invariant breaches."
    except RuntimeError as exc:
        assert "hard-failed" in str(exc)

    report_df = pd.read_csv(data_dir / "lifecycle_integrity_report.csv")

    assert report_df.iloc[0]["record_type"] == "summary"
    assert int(report_df.iloc[0]["failure_count"]) >= 1
    assert (
        report_df["invariant_name"] == "closed_positions_cannot_mutate_economic_fields"
    ).any()
    assert (
        (report_df["record_type"] == "check")
        & (report_df["invariant_name"] == "closed_positions_cannot_mutate_economic_fields")
        & (report_df["severity"] == "critical")
    ).any()
    assert (
        (report_df["record_type"] == "detail")
        & (report_df["invariant_name"] == "closed_positions_cannot_mutate_economic_fields")
    ).any()


def test_lifecycle_integrity_report_rows_on_success(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _patch_lifecycle_paths(isolated_workspace, monkeypatch)
    monkeypatch.setattr(lifecycle_integrity_agent, "get_or_create_run_id", lambda: "RUN_LIFECYCLE_PASS")

    current_state = portfolio_state_frame([open_position_row(run_id="RUN_LIFECYCLE_PASS")])

    write_csv(data_dir / "portfolio_state.csv", current_state)
    write_csv(data_dir / "cash_state.csv", cash_state_frame())
    write_csv(data_dir / "processed_fills.csv", processed_fills_frame([]))
    write_csv(
        data_dir / "run_history.csv",
        run_history_frame(
            [
                {
                    "run_id": "RUN_LIFECYCLE_PASS",
                    "started_at": "2026-03-28T10:00:00+00:00",
                    "completed_at": "",
                    "status": "running",
                    "failed_agent": "",
                    "error_message": "",
                    "notes": "",
                }
            ]
        ),
    )

    lifecycle_integrity_agent.run_lifecycle_integrity_agent()

    report_df = pd.read_csv(data_dir / "lifecycle_integrity_report.csv")

    assert report_df.iloc[0]["record_type"] == "summary"
    assert int(report_df.iloc[0]["failure_count"]) == 0
    assert int(report_df.iloc[0]["passed_checks"]) == int(report_df.iloc[0]["total_checks"])
    assert (report_df["record_type"] == "check").any()
    assert not (report_df["record_type"] == "detail").any()
    assert (data_dir / "portfolio_state_prev_snapshot.csv").exists()
