from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

import run_pipeline
import shared.run_history as shared_run_history
import shared.run_reconciliation as shared_run_reconciliation
import shared.sqlite_parity as sqlite_parity
from agents.fill_agent import fill_agent
from agents.lifecycle_integrity_agent import lifecycle_integrity_agent
from shared.run_context import RUN_ID_ENV_VAR
from shared.sqlite_parity import ParityIssue, ParityReport
from tests.helpers import (
    cash_state_frame,
    closed_position_row,
    portfolio_state_frame,
    processed_fills_frame,
    write_csv,
)


def _patch_lifecycle_paths(isolated_workspace: Path, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(lifecycle_integrity_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "REPORT_PATH", str(data_dir / "lifecycle_integrity_report.csv"))
    monkeypatch.setattr(
        lifecycle_integrity_agent,
        "SNAPSHOT_PATH",
        str(data_dir / "portfolio_state_prev_snapshot.csv"),
    )
    monkeypatch.setattr(lifecycle_integrity_agent, "CASH_STATE_PATH", str(data_dir / "cash_state.csv"))
    monkeypatch.setattr(
        lifecycle_integrity_agent,
        "EQUITY_HISTORY_PATH",
        str(data_dir / "portfolio_equity_history.csv"),
    )
    monkeypatch.setattr(
        lifecycle_integrity_agent,
        "PROCESSED_FILLS_PATH",
        str(data_dir / "processed_fills.csv"),
    )
    monkeypatch.setattr(lifecycle_integrity_agent, "CASH_LEDGER_PATH", str(data_dir / "cash_ledger.csv"))
    monkeypatch.setattr(lifecycle_integrity_agent, "RUN_HISTORY_PATH", str(data_dir / "run_history.csv"))


def _manual_fill_row(**overrides):
    row = {
        "fill_id": "FILL_FAULT_001",
        "ticker": "AAPL",
        "side": "long",
        "action": "buy",
        "quantity": 2,
        "fill_price": 100.0,
        "fees": 1.0,
        "fill_timestamp": "2026-03-28T10:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_fill_agent_rejects_malformed_fill_schema_before_state_mutation(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_BAD_FILL_SCHEMA")

    initial_state = portfolio_state_frame([closed_position_row(position_id="POS_SCHEMA_GUARD")])
    initial_cash = cash_state_frame(balance=100000.0)
    write_csv(data_dir / "portfolio_state.csv", initial_state)
    write_csv(data_dir / "cash_state.csv", initial_cash)
    write_csv(data_dir / "processed_fills.csv", processed_fills_frame([]))
    write_csv(data_dir / "manual_fills.csv", pd.DataFrame([_manual_fill_row()]).drop(columns=["action"]))

    with pytest.raises(ValueError, match="Missing required fill input columns"):
        fill_agent.run_fill_agent()

    assert_frame_equal(
        pd.read_csv(data_dir / "portfolio_state.csv", keep_default_na=False),
        initial_state,
        check_dtype=False,
    )
    assert_frame_equal(
        pd.read_csv(data_dir / "cash_state.csv", keep_default_na=False),
        initial_cash,
        check_dtype=False,
    )
    assert pd.read_csv(data_dir / "processed_fills.csv").empty
    assert not (data_dir / "cash_ledger.csv").exists()


def test_fill_agent_rejects_duplicate_manual_fill_ids_before_partial_mutation(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_DUPLICATE_FILL")

    initial_state = portfolio_state_frame([])
    initial_cash = cash_state_frame(balance=100000.0)
    write_csv(data_dir / "portfolio_state.csv", initial_state)
    write_csv(data_dir / "cash_state.csv", initial_cash)
    write_csv(data_dir / "processed_fills.csv", processed_fills_frame([]))
    write_csv(
        data_dir / "manual_fills.csv",
        pd.DataFrame(
            [
                _manual_fill_row(fill_id="FILL_DUPLICATE", ticker="AAPL", quantity=1),
                _manual_fill_row(fill_id="FILL_DUPLICATE", ticker="MSFT", quantity=3),
            ]
        ),
    )

    with pytest.raises(ValueError, match="Duplicate fill_id values"):
        fill_agent.run_fill_agent()

    assert_frame_equal(
        pd.read_csv(data_dir / "portfolio_state.csv", keep_default_na=False),
        initial_state,
        check_dtype=False,
    )
    assert_frame_equal(
        pd.read_csv(data_dir / "cash_state.csv", keep_default_na=False),
        initial_cash,
        check_dtype=False,
    )
    assert pd.read_csv(data_dir / "processed_fills.csv").empty
    assert pd.read_csv(data_dir / "cash_ledger.csv").empty


def test_lifecycle_agent_rejects_invalid_transition_without_overwriting_snapshot(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _patch_lifecycle_paths(isolated_workspace, monkeypatch)
    monkeypatch.setattr(lifecycle_integrity_agent, "get_or_create_run_id", lambda: "RUN_BAD_TRANSITION")

    previous_snapshot = portfolio_state_frame([closed_position_row(position_id="POS_REOPENED", status="closed")])
    reopened_state = portfolio_state_frame([closed_position_row(position_id="POS_REOPENED", status="open")])
    write_csv(data_dir / "portfolio_state_prev_snapshot.csv", previous_snapshot)
    write_csv(data_dir / "portfolio_state.csv", reopened_state)

    with pytest.raises(RuntimeError, match="Lifecycle Integrity Agent hard-failed"):
        lifecycle_integrity_agent.run_lifecycle_integrity_agent()

    report = pd.read_csv(data_dir / "lifecycle_integrity_report.csv")
    assert "Invalid lifecycle transition: closed -> open" in "\n".join(report["detail"].astype(str))
    assert_frame_equal(
        pd.read_csv(data_dir / "portfolio_state_prev_snapshot.csv", keep_default_na=False),
        previous_snapshot,
        check_dtype=False,
    )


def test_lifecycle_agent_rejects_duplicate_processed_fills_without_snapshot_mutation(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _patch_lifecycle_paths(isolated_workspace, monkeypatch)
    monkeypatch.setattr(lifecycle_integrity_agent, "get_or_create_run_id", lambda: "RUN_DUP_PROCESSED")

    state = portfolio_state_frame([])
    previous_snapshot = portfolio_state_frame([])
    write_csv(data_dir / "portfolio_state.csv", state)
    write_csv(data_dir / "portfolio_state_prev_snapshot.csv", previous_snapshot)
    write_csv(
        data_dir / "processed_fills.csv",
        processed_fills_frame(
            [
                {"fill_id": "FILL_SEEN_TWICE", "processed_at": "2026-03-28T10:00:00+00:00", "run_id": "RUN_A"},
                {"fill_id": "FILL_SEEN_TWICE", "processed_at": "2026-03-28T10:01:00+00:00", "run_id": "RUN_B"},
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="Lifecycle Integrity Agent hard-failed"):
        lifecycle_integrity_agent.run_lifecycle_integrity_agent()

    report = pd.read_csv(data_dir / "lifecycle_integrity_report.csv")
    assert "duplicate fill_id=FILL_SEEN_TWICE" in "\n".join(report["detail"].astype(str))
    assert_frame_equal(
        pd.read_csv(data_dir / "portfolio_state_prev_snapshot.csv", keep_default_na=False),
        previous_snapshot,
        check_dtype=False,
    )


def test_lifecycle_agent_fails_closed_when_required_portfolio_state_is_missing(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _patch_lifecycle_paths(isolated_workspace, monkeypatch)

    with pytest.raises(FileNotFoundError, match="Required file not found"):
        lifecycle_integrity_agent.run_lifecycle_integrity_agent()

    assert not (data_dir / "lifecycle_integrity_report.csv").exists()
    assert not (data_dir / "portfolio_state_prev_snapshot.csv").exists()


def test_pipeline_parity_failure_is_terminal_and_fail_closed(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setenv(RUN_ID_ENV_VAR, "RUN_PARITY_ADVISORY")
    monkeypatch.setattr(run_pipeline, "PIPELINE_STEPS", [])
    monkeypatch.setattr(shared_run_history, "RUN_HISTORY_PATH", data_dir / "run_history.csv")
    monkeypatch.setattr(shared_run_reconciliation, "RUN_HISTORY_PATH", data_dir / "run_history.csv")
    monkeypatch.setattr(
        shared_run_reconciliation,
        "RUN_RECONCILIATION_SUMMARY_PATH",
        data_dir / "run_reconciliation_summary.csv",
    )
    monkeypatch.setattr(sqlite_parity, "RUN_HISTORY_PATH", data_dir / "run_history.csv")

    def fail_parity_before_success(*_args, **_kwargs):
        in_progress = pd.read_csv(
            data_dir / "run_history.csv",
            dtype=str,
            keep_default_na=False,
        )
        assert in_progress.iloc[0]["status"] == "started"
        raise RuntimeError("synthetic parity mismatch")

    monkeypatch.setattr(run_pipeline, "finalize_run", fail_parity_before_success)

    with pytest.raises(RuntimeError, match="synthetic parity mismatch"):
        run_pipeline.main()

    run_history_df = pd.read_csv(data_dir / "run_history.csv", dtype=str, keep_default_na=False)
    assert len(run_history_df) == 1
    assert run_history_df.iloc[0]["run_id"] == "RUN_PARITY_ADVISORY"
    assert run_history_df.iloc[0]["status"] == "failed"
    assert run_history_df.iloc[0]["failed_agent"] == "Run Finalizer"
