from __future__ import annotations

from pathlib import Path

import pandas as pd
from pandas.testing import assert_frame_equal

import run_pipeline
import shared.event_log as shared_event_log
import shared.run_history as shared_run_history
import shared.run_reconciliation as shared_run_reconciliation
import shared.sqlite_sidecar as sqlite_sidecar
from agents.exit_agent import exit_agent
from agents.fill_agent import fill_agent
from agents.lifecycle_integrity_agent import lifecycle_integrity_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from agents.position_tracking_agent import position_tracking_agent
from shared.invariants import build_invariant_context, validate_all_invariants
from shared.run_context import RUN_ID_ENV_VAR
from shared.schemas import (
    validate_lifecycle_integrity_report,
    validate_portfolio_state,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pipeline_smoke"
PIPELINE_RUN_ID = "RUN_PIPELINE_SMOKE"


def _copy_fixture_csvs(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)

    for fixture_name in ["manual_fills.csv", "portfolio_state.csv", "cash_state.csv"]:
        (data_dir / fixture_name).write_text(
            (FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def _patch_control_paths(isolated_workspace: Path, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"

    monkeypatch.setattr(shared_event_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_event_log, "EVENT_LOG_PATH", data_dir / "event_log.csv")

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


def test_pipeline_smoke_control_integrity(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    _copy_fixture_csvs(data_dir)
    _patch_control_paths(isolated_workspace, monkeypatch)

    monkeypatch.chdir(isolated_workspace)
    monkeypatch.setenv(RUN_ID_ENV_VAR, PIPELINE_RUN_ID)
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: PIPELINE_RUN_ID)
    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: PIPELINE_RUN_ID)
    monkeypatch.setattr(position_tracking_agent, "get_latest_price", lambda ticker, fallback_price: float(fallback_price))

    pipeline_steps = [
        ("Fill Agent", "agents.fill_agent.fill_agent"),
        ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
        ("Position Tracking Agent", "agents.position_tracking_agent.position_tracking_agent"),
        ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
        ("Exit Agent", "agents.exit_agent.exit_agent"),
        ("Portfolio Equity Agent", "agents.portfolio_equity_agent.portfolio_equity_agent"),
    ]
    monkeypatch.setattr(run_pipeline, "PIPELINE_STEPS", pipeline_steps)

    snapshots: dict[str, pd.DataFrame] = {}

    def run_module_in_process(label: str, module_path: str) -> None:
        runners = {
            "agents.fill_agent.fill_agent": fill_agent.run_fill_agent,
            "agents.lifecycle_integrity_agent.lifecycle_integrity_agent": lifecycle_integrity_agent.run_lifecycle_integrity_agent,
            "agents.position_tracking_agent.position_tracking_agent": position_tracking_agent.run_position_tracking_agent,
            "agents.exit_agent.exit_agent": exit_agent.run_exit_agent,
            "agents.portfolio_equity_agent.portfolio_equity_agent": portfolio_equity_agent.run_portfolio_equity_agent,
        }

        runners[module_path]()

        if label == "Fill Agent":
            snapshots["state_after_fill"] = validate_portfolio_state(
                pd.read_csv(data_dir / "portfolio_state.csv"),
                keep_extra_columns=False,
            )
            snapshots["cash_after_fill"] = pd.read_csv(data_dir / "cash_state.csv")
            snapshots["ledger_after_fill"] = pd.read_csv(data_dir / "cash_ledger.csv")
            snapshots["processed_after_fill"] = pd.read_csv(data_dir / "processed_fills.csv")

    monkeypatch.setattr(run_pipeline, "run_module", run_module_in_process)

    run_pipeline.main()

    expected_outputs = [
        "portfolio_state.csv",
        "cash_state.csv",
        "processed_fills.csv",
        "cash_ledger.csv",
        "event_log.csv",
        "run_history.csv",
        "lifecycle_integrity_report.csv",
        "portfolio_state_prev_snapshot.csv",
        "position_alerts.csv",
        "exit_advice.csv",
        "portfolio_equity_history.csv",
        "performance_summary.csv",
        "run_reconciliation_summary.csv",
    ]
    for file_name in expected_outputs:
        assert (data_dir / file_name).exists(), f"Expected output file missing: {file_name}"

    final_state = validate_portfolio_state(
        pd.read_csv(data_dir / "portfolio_state.csv"),
        keep_extra_columns=False,
    )
    final_cash = pd.read_csv(data_dir / "cash_state.csv")
    final_ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    final_processed = pd.read_csv(data_dir / "processed_fills.csv")
    final_report = validate_lifecycle_integrity_report(
        pd.read_csv(data_dir / "lifecycle_integrity_report.csv"),
        keep_extra_columns=False,
    )
    run_history_df = pd.read_csv(data_dir / "run_history.csv", dtype=str, keep_default_na=False)

    assert len(run_history_df) == 1
    assert run_history_df.iloc[0]["run_id"] == PIPELINE_RUN_ID
    assert run_history_df.iloc[0]["status"] == "success"

    assert int(final_report.iloc[0]["failure_count"]) == 0

    assert_frame_equal(snapshots["cash_after_fill"], final_cash, check_dtype=False)
    assert_frame_equal(snapshots["ledger_after_fill"], final_ledger, check_dtype=False)
    assert_frame_equal(snapshots["processed_after_fill"], final_processed, check_dtype=False)

    closed_after_fill = snapshots["state_after_fill"][
        snapshots["state_after_fill"]["position_id"] == "POS_CLOSED_001"
    ].reset_index(drop=True)
    closed_final = final_state[final_state["position_id"] == "POS_CLOSED_001"].reset_index(drop=True)
    assert_frame_equal(closed_after_fill, closed_final, check_dtype=False)

    invariant_failures = validate_all_invariants(
        build_invariant_context(
            current_state=final_state,
            previous_state=snapshots["state_after_fill"],
            cash_state=final_cash,
            equity_history=pd.read_csv(data_dir / "portfolio_equity_history.csv"),
            processed_fills=final_processed,
            cash_ledger=final_ledger,
            run_history=run_history_df,
        )
    )
    assert invariant_failures == []

    reconciliation_df = pd.read_csv(data_dir / "run_reconciliation_summary.csv")
    event_log_df = pd.read_csv(data_dir / "event_log.csv")
    assert len(reconciliation_df) == 1
    assert reconciliation_df.iloc[0]["run_id"] == PIPELINE_RUN_ID
    assert reconciliation_df.iloc[0]["status"] == "success"
    assert (
        (event_log_df["event_type"] == "artifact_written")
        & (event_log_df["agent_name"] == "Position Tracking Agent")
    ).any()

    assert sqlite_sidecar.fetch_row_count("run_history") == len(run_history_df)
    assert sqlite_sidecar.fetch_row_count("run_reconciliation_summary") == len(reconciliation_df)
    assert sqlite_sidecar.fetch_row_count("event_log") == len(event_log_df)
    assert sqlite_sidecar.fetch_row_count("processed_fills") == len(final_processed)
    assert sqlite_sidecar.fetch_row_count("cash_ledger") == len(final_ledger)
    assert sqlite_sidecar.fetch_row_count("portfolio_equity_history") == len(
        pd.read_csv(data_dir / "portfolio_equity_history.csv")
    )
