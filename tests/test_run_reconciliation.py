from __future__ import annotations

import pandas as pd

import shared.run_reconciliation as run_reconciliation
from tests.helpers import (
    event_log_frame,
    event_log_row,
    processed_fills_frame,
    run_history_frame,
    write_csv,
)


def _patch_reconciliation_paths(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(run_reconciliation, "RUN_HISTORY_PATH", data_dir / "run_history.csv")
    monkeypatch.setattr(run_reconciliation, "RUN_RECONCILIATION_SUMMARY_PATH", data_dir / "run_reconciliation_summary.csv")
    monkeypatch.setattr(run_reconciliation, "EVENT_LOG_PATH", data_dir / "event_log.csv")
    monkeypatch.setattr(run_reconciliation, "EQUITY_HISTORY_PATH", data_dir / "portfolio_equity_history.csv")
    monkeypatch.setattr(run_reconciliation, "CASH_LEDGER_PATH", data_dir / "cash_ledger.csv")
    monkeypatch.setattr(run_reconciliation, "PROCESSED_FILLS_PATH", data_dir / "processed_fills.csv")
    monkeypatch.setattr(run_reconciliation, "POSITION_ALERTS_PATH", data_dir / "position_alerts.csv")


def test_run_reconciliation_summary_counts_and_csv_write(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _patch_reconciliation_paths(isolated_workspace, monkeypatch)

    run_id = "RUN_RECON_TEST"

    write_csv(
        data_dir / "run_history.csv",
        run_history_frame(
            [
                {
                    "run_id": run_id,
                    "started_at": "2026-03-28T10:00:00+00:00",
                    "completed_at": "2026-03-28T10:05:00+00:00",
                    "status": "failed",
                    "failed_agent": "Lifecycle Integrity Agent",
                    "error_message": "validation failure",
                    "notes": "operator note",
                }
            ]
        ),
    )
    write_csv(
        data_dir / "processed_fills.csv",
        processed_fills_frame(
            [
                {"fill_id": "FILL001", "processed_at": "2026-03-28T10:01:00+00:00", "run_id": run_id},
                {"fill_id": "FILL002", "processed_at": "2026-03-28T10:02:00+00:00", "run_id": run_id},
            ]
        ),
    )
    write_csv(
        data_dir / "event_log.csv",
        event_log_frame(
            [
                event_log_row(
                    event_id="EVT_OPEN",
                    run_id=run_id,
                    event_type="position_opened",
                    entity_type="position",
                    entity_id="POS001",
                    ticker="AAPL",
                    position_id="POS001",
                    message="Opened position",
                ),
                event_log_row(
                    event_id="EVT_CLOSE",
                    run_id=run_id,
                    event_type="position_closed",
                    entity_type="position",
                    entity_id="POS002",
                    ticker="MSFT",
                    position_id="POS002",
                    message="Closed position",
                ),
                event_log_row(
                    event_id="EVT_VALIDATE",
                    run_id=run_id,
                    event_type="validation_failed",
                    agent_name="Lifecycle Integrity Agent",
                    message="Validation failed",
                    severity="error",
                    metadata={
                        "schema_version": "1.0",
                        "entity": {"entity_type": "system", "entity_id": "portfolio_state"},
                        "details": {
                            "warning_check_count": 2,
                            "critical_issue_count": 1,
                        },
                    },
                ),
            ]
        ),
    )

    row = run_reconciliation.write_run_reconciliation_summary(run_id)
    summary_df = pd.read_csv(data_dir / "run_reconciliation_summary.csv")

    assert row["run_id"] == run_id
    assert int(row["fills_processed"]) == 2
    assert int(row["positions_opened"]) == 1
    assert int(row["positions_closed"]) == 1
    assert int(row["validation_warning_count"]) == 2
    assert int(row["validation_failure_count"]) == 1

    assert len(summary_df) == 1
    assert summary_df.iloc[0]["run_id"] == run_id
    assert int(summary_df.iloc[0]["validation_warning_count"]) == 2
    assert int(summary_df.iloc[0]["validation_failure_count"]) == 1
    assert summary_df.iloc[0]["status"] == "failed"


def test_run_reconciliation_accepts_legacy_validation_metadata_shape() -> None:
    event_rows = event_log_frame(
        [
            event_log_row(
                event_id="EVT_LEGACY_VALIDATE",
                run_id="RUN_LEGACY",
                event_type="validation_failed",
                metadata={
                    "warning_check_count": 3,
                    "critical_issue_count": 2,
                },
            )
        ]
    )

    warning_count, failure_count = run_reconciliation._extract_validation_counts(event_rows)

    assert warning_count == 3
    assert failure_count == 2
