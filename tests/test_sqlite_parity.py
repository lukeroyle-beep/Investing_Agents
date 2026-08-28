from __future__ import annotations

import pandas as pd

import shared.event_log as shared_event_log
import shared.run_history as shared_run_history
import shared.run_reconciliation as shared_run_reconciliation
import shared.sqlite_parity as sqlite_parity
from agents.fill_agent import fill_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from agents.position_tracking_agent import position_tracking_agent
from shared.sqlite_parity import format_parity_report, validate_sqlite_dual_write_parity
from shared.sqlite_sidecar import get_connection, initialise_db
from tests.helpers import cash_state_frame, write_csv


def _patch_reconciliation_paths(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(shared_run_history, "RUN_HISTORY_PATH", data_dir / "run_history.csv")
    monkeypatch.setattr(shared_run_reconciliation, "RUN_HISTORY_PATH", data_dir / "run_history.csv")
    monkeypatch.setattr(
        shared_run_reconciliation,
        "RUN_RECONCILIATION_SUMMARY_PATH",
        data_dir / "run_reconciliation_summary.csv",
    )
    monkeypatch.setattr(shared_run_reconciliation, "EVENT_LOG_PATH", data_dir / "event_log.csv")
    monkeypatch.setattr(shared_run_reconciliation, "EQUITY_HISTORY_PATH", data_dir / "portfolio_equity_history.csv")
    monkeypatch.setattr(shared_run_reconciliation, "CASH_LEDGER_PATH", data_dir / "cash_ledger.csv")
    monkeypatch.setattr(shared_run_reconciliation, "PROCESSED_FILLS_PATH", data_dir / "processed_fills.csv")
    monkeypatch.setattr(shared_run_reconciliation, "POSITION_ALERTS_PATH", data_dir / "position_alerts.csv")
    monkeypatch.setattr(sqlite_parity, "EVENT_LOG_PATH", data_dir / "event_log.csv")
    monkeypatch.setattr(sqlite_parity, "RUN_HISTORY_PATH", data_dir / "run_history.csv")
    monkeypatch.setattr(
        sqlite_parity,
        "RUN_RECONCILIATION_SUMMARY_PATH",
        data_dir / "run_reconciliation_summary.csv",
    )
    monkeypatch.setattr(sqlite_parity, "CASH_LEDGER_PATH", data_dir / "cash_ledger.csv")
    monkeypatch.setattr(sqlite_parity, "CASH_STATE_PATH", data_dir / "cash_state.csv")
    monkeypatch.setattr(sqlite_parity, "PROCESSED_FILLS_PATH", data_dir / "processed_fills.csv")
    monkeypatch.setattr(sqlite_parity, "TRADE_FILLS_PATH", data_dir / "trade_fills.csv")
    monkeypatch.setattr(sqlite_parity, "PORTFOLIO_STATE_PATH", data_dir / "portfolio_state.csv")
    monkeypatch.setattr(
        sqlite_parity,
        "PORTFOLIO_EQUITY_HISTORY_PATH",
        data_dir / "portfolio_equity_history.csv",
    )


def test_sqlite_parity_passes_for_dual_written_outputs(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"
    _patch_reconciliation_paths(isolated_workspace, monkeypatch)

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_PARITY_FILL")
    monkeypatch.setattr(portfolio_equity_agent, "get_or_create_run_id", lambda: "RUN_PARITY_FILL")
    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_PARITY_FILL")
    monkeypatch.setattr(
        position_tracking_agent,
        "get_latest_price",
        lambda ticker, fallback_price: float(fallback_price),
    )

    pd.DataFrame(
        [
            {
                "fill_id": "FILL_PARITY_001",
                "ticker": "AAPL",
                "side": "long",
                "action": "buy",
                "quantity": 2,
                "fill_price": 100.0,
                "fees": 1.0,
                "fill_timestamp": "2026-03-28T10:00:00+00:00",
            }
        ]
    ).to_csv(manual_fills_path, index=False)

    shared_run_history.start_run_record("RUN_PARITY_FILL", "2026-03-28T10:00:00+00:00")
    shared_event_log.append_run_lifecycle_event(
        run_id="RUN_PARITY_FILL",
        event_type="run_started",
        message="Parity test run started",
        details={"started_at": "2026-03-28T10:00:00+00:00"},
    )
    fill_agent.run_fill_agent()
    shared_run_history.begin_run_validation("RUN_PARITY_FILL")
    shared_run_history.complete_run_record("RUN_PARITY_FILL", "2026-03-28T10:05:00+00:00")
    shared_event_log.append_run_lifecycle_event(
        run_id="RUN_PARITY_FILL",
        event_type="run_completed",
        message="Parity test run completed",
        details={"completed_at": "2026-03-28T10:05:00+00:00"},
    )
    position_tracking_agent.run_position_tracking_agent()
    portfolio_equity_agent.run_portfolio_equity_agent()
    shared_run_reconciliation.write_run_reconciliation_summary("RUN_PARITY_FILL")

    report = validate_sqlite_dual_write_parity(run_id="RUN_PARITY_FILL")

    assert report.passed
    assert "passed" in format_parity_report(report)


def test_sqlite_parity_reports_explicit_mismatch(isolated_workspace, monkeypatch) -> None:
    _patch_reconciliation_paths(isolated_workspace, monkeypatch)
    shared_run_history.start_run_record("RUN_PARITY_MISMATCH", "2026-03-28T10:00:00+00:00")
    initialise_db()

    with get_connection() as connection:
        connection.execute("DELETE FROM run_history")
        connection.execute(
            """
            INSERT INTO run_history (
                run_id, started_at, completed_at, status, failed_agent, error_message, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "RUN_PARITY_MISMATCH",
                "2026-03-28T10:00:00+00:00",
                "",
                "failed",
                "",
                "",
                "",
            ),
        )
        connection.commit()

    report = validate_sqlite_dual_write_parity(run_id="RUN_PARITY_MISMATCH")
    message = format_parity_report(report)

    assert not report.passed
    assert any(issue.table_name == "run_history" for issue in report.issues)
    assert "value_mismatch" in message
    assert "run_history" in message
