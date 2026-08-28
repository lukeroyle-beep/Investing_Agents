from __future__ import annotations

import pytest

import shared.event_log as shared_event_log
import shared.sqlite_sidecar as shared_sqlite_sidecar
from agents.exit_agent import exit_agent
from agents.fill_agent import fill_agent
from agents.lifecycle_integrity_agent import lifecycle_integrity_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from agents.position_tracking_agent import position_tracking_agent


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shared_event_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_event_log, "EVENT_LOG_PATH", data_dir / "event_log.csv")
    monkeypatch.setattr(shared_sqlite_sidecar, "SQLITE_DB_PATH", data_dir / "trading_system.sqlite3")

    monkeypatch.setattr(fill_agent, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(fill_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(fill_agent, "PROCESSED_FILLS_PATH", str(data_dir / "processed_fills.csv"))
    monkeypatch.setattr(fill_agent, "TRADE_FILLS_PATH", str(data_dir / "trade_fills.csv"))
    monkeypatch.setattr(fill_agent, "MANUAL_FILLS_PATH", str(data_dir / "manual_fills.csv"))
    monkeypatch.setattr(fill_agent, "BROKER_FILLS_PATH", str(data_dir / "broker_fills.csv"))
    monkeypatch.setattr(fill_agent, "CASH_STATE_PATH", str(data_dir / "cash_state.csv"))
    monkeypatch.setattr(fill_agent, "CASH_LEDGER_PATH", str(data_dir / "cash_ledger.csv"))

    monkeypatch.setattr(position_tracking_agent, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(position_tracking_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(
        position_tracking_agent,
        "MONITOR_PATH",
        str(data_dir / "portfolio_monitor.csv"),
    )
    monkeypatch.setattr(position_tracking_agent, "ALERTS_PATH", str(data_dir / "position_alerts.csv"))

    monkeypatch.setattr(exit_agent, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(exit_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(exit_agent, "MONITOR_PATH", str(data_dir / "portfolio_monitor.csv"))
    monkeypatch.setattr(exit_agent, "EXIT_ADVICE_PATH", str(data_dir / "exit_advice.csv"))

    monkeypatch.setattr(portfolio_equity_agent, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(portfolio_equity_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(
        portfolio_equity_agent,
        "MONITOR_PATH",
        str(data_dir / "portfolio_monitor.csv"),
    )
    monkeypatch.setattr(portfolio_equity_agent, "CASH_STATE_PATH", str(data_dir / "cash_state.csv"))
    monkeypatch.setattr(
        portfolio_equity_agent,
        "EQUITY_HISTORY_PATH",
        str(data_dir / "portfolio_equity_history.csv"),
    )
    monkeypatch.setattr(
        portfolio_equity_agent,
        "PERFORMANCE_SUMMARY_PATH",
        str(data_dir / "performance_summary.csv"),
    )

    monkeypatch.setattr(lifecycle_integrity_agent, "STATE_PATH", str(data_dir / "portfolio_state.csv"))
    monkeypatch.setattr(
        lifecycle_integrity_agent,
        "REPORT_PATH",
        str(data_dir / "lifecycle_integrity_report.csv"),
    )
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

    return tmp_path
