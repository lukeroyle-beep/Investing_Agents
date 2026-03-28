from __future__ import annotations

import pandas as pd

import shared.analytics_reads as analytics_reads
import shared.run_reconciliation as run_reconciliation
import shared.sqlite_sidecar as sqlite_sidecar
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


def test_run_reconciliation_can_read_dual_written_inputs_from_sqlite(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    _patch_reconciliation_paths(isolated_workspace, monkeypatch)
    monkeypatch.setattr(analytics_reads, "PREFER_SQLITE_ANALYTICS_READS", True)

    run_id = "RUN_SQLITE_RECON"
    sqlite_sidecar.initialise_db()
    sqlite_sidecar.upsert_run_history_row(
        {
            "run_id": run_id,
            "started_at": "2026-03-28T10:00:00+00:00",
            "completed_at": "2026-03-28T10:05:00+00:00",
            "status": "success",
            "failed_agent": "",
            "error_message": "",
            "notes": "sqlite-backed analytics read",
        }
    )
    sqlite_sidecar.append_processed_fill_row(
        {
            "fill_id": "FILL_SQLITE_001",
            "processed_at": "2026-03-28T10:01:00+00:00",
            "run_id": run_id,
        }
    )
    sqlite_sidecar.append_event_log_row(
        {
            "event_id": "EVT_SQLITE_OPEN",
            "run_id": run_id,
            "event_time": "2026-03-28T10:02:00+00:00",
            "agent_name": "Fill Agent",
            "event_type": "position_opened",
            "entity_type": "position",
            "entity_id": "POS_SQLITE_001",
            "ticker": "AAPL",
            "position_id": "POS_SQLITE_001",
            "order_id": "",
            "severity": "info",
            "message": "Opened position",
            "before_json": "",
            "after_json": "",
            "metadata_json": "{}",
        }
    )
    sqlite_sidecar.append_event_log_row(
        {
            "event_id": "EVT_SQLITE_VALIDATE",
            "run_id": run_id,
            "event_time": "2026-03-28T10:03:00+00:00",
            "agent_name": "Lifecycle Integrity Agent",
            "event_type": "validation_passed",
            "entity_type": "system",
            "entity_id": "portfolio_state",
            "ticker": "",
            "position_id": "",
            "order_id": "",
            "severity": "info",
            "message": "Validation passed",
            "before_json": "",
            "after_json": "",
            "metadata_json": '{"details":{"warning_check_count":0,"critical_issue_count":0}}',
        }
    )
    sqlite_sidecar.append_cash_ledger_row(
        {
            "ledger_id": "LEDGER_SQLITE_001",
            "run_id": run_id,
            "timestamp": "2026-03-28T10:01:00+00:00",
            "event_type": "position_open",
            "position_id": "POS_SQLITE_001",
            "ticker": "AAPL",
            "side": "long",
            "action": "buy",
            "amount": -1000.0,
            "fees": 2.5,
            "cash_balance_after": 98997.5,
            "notes": "sqlite-only ledger row",
        }
    )

    write_csv(data_dir / "position_alerts.csv", pd.DataFrame(columns=["run_id", "alert_type"]))

    row = run_reconciliation.write_run_reconciliation_summary(run_id)

    assert row["run_id"] == run_id
    assert row["status"] == "success"
    assert int(row["fills_processed"]) == 1
    assert int(row["positions_opened"]) == 1
    assert int(row["positions_closed"]) == 0
    assert int(row["validation_warning_count"]) == 0
    assert int(row["validation_failure_count"]) == 0
    assert float(row["cash_delta"]) == -1002.5
    assert "cash_ledger.csv" in str(row["notes"])
