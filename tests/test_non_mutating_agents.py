from __future__ import annotations

import pandas as pd
import pytest

from agents.exit_agent import exit_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from agents.position_tracking_agent import position_tracking_agent
from tests.helpers import (
    cash_ledger_frame,
    cash_state_frame,
    open_position_row,
    portfolio_monitor_row,
    processed_fills_frame,
    write_csv,
    write_portfolio_monitor_csv,
    write_portfolio_state_csv,
)


def write_economic_inputs(data_dir) -> dict[str, bytes]:
    position = open_position_row()
    write_portfolio_state_csv(data_dir / "portfolio_state.csv", [position])
    write_portfolio_monitor_csv(
        data_dir / "portfolio_monitor.csv",
        [portfolio_monitor_row(position)],
    )
    write_csv(data_dir / "cash_state.csv", cash_state_frame(balance=100000.0))
    write_csv(data_dir / "cash_ledger.csv", cash_ledger_frame([]))
    write_csv(data_dir / "processed_fills.csv", processed_fills_frame([]))
    return {
        name: (data_dir / name).read_bytes()
        for name in [
            "portfolio_state.csv",
            "cash_state.csv",
            "cash_ledger.csv",
            "processed_fills.csv",
        ]
    }


def assert_economic_inputs_unchanged(data_dir, before: dict[str, bytes]) -> None:
    assert {name: (data_dir / name).read_bytes() for name in before} == before


def test_position_tracking_agent_does_not_mutate_economic_inputs(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    before = write_economic_inputs(data_dir)
    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_TRACKING_TEST")
    monkeypatch.setattr(
        position_tracking_agent,
        "get_latest_price",
        lambda ticker, fallback_price: 111.0,
    )

    position_tracking_agent.run_position_tracking_agent()

    assert_economic_inputs_unchanged(data_dir, before)


def test_exit_agent_does_not_mutate_portfolio_state(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXIT_TEST")

    before = write_economic_inputs(data_dir)

    exit_agent.run_exit_agent()

    assert_economic_inputs_unchanged(data_dir, before)
    assert (data_dir / "exit_advice.csv").exists()


def test_exit_agent_uses_monitor_price_instead_of_legacy_state_mark(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    position = open_position_row(current_price=105.0, stop_loss=90.0)
    write_portfolio_state_csv(data_dir / "portfolio_state.csv", [position])
    write_portfolio_monitor_csv(
        data_dir / "portfolio_monitor.csv",
        [
            portfolio_monitor_row(
                position,
                current_price=89.0,
                market_value=890.0,
                pnl_abs=-110.0,
                pnl_pct=-11.0,
            )
        ],
    )
    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_MONITOR_EXIT")

    exit_agent.run_exit_agent()

    row = pd.read_csv(data_dir / "exit_advice.csv").iloc[0]
    assert row["exit_action"] == "close"
    assert row["exit_reason"] == "stop_loss_triggered"
    assert row["current_price"] == 89.0


def test_portfolio_equity_agent_does_not_mutate_state_inputs(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    equity_snapshot_path = data_dir / "portfolio_equity.csv"

    monkeypatch.setattr(portfolio_equity_agent, "get_or_create_run_id", lambda: "RUN_EQUITY_TEST")

    before = write_economic_inputs(data_dir)

    portfolio_equity_agent.run_portfolio_equity_agent()

    assert_economic_inputs_unchanged(data_dir, before)
    assert (data_dir / "portfolio_equity_history.csv").exists()
    assert (data_dir / "performance_summary.csv").exists()
    assert not equity_snapshot_path.exists()


def test_portfolio_equity_uses_monitor_valuation(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    position = open_position_row(current_price=105.0, market_value=1050.0)
    write_portfolio_state_csv(data_dir / "portfolio_state.csv", [position])
    write_portfolio_monitor_csv(
        data_dir / "portfolio_monitor.csv",
        [
            portfolio_monitor_row(
                position,
                current_price=111.0,
                market_value=1110.0,
                pnl_abs=110.0,
                pnl_pct=11.0,
            )
        ],
    )
    write_csv(data_dir / "cash_state.csv", cash_state_frame(balance=100000.0))
    monkeypatch.setattr(portfolio_equity_agent, "get_or_create_run_id", lambda: "RUN_MONITOR_EQUITY")

    portfolio_equity_agent.run_portfolio_equity_agent()

    history = pd.read_csv(data_dir / "portfolio_equity_history.csv")
    row = history.iloc[-1]
    assert row["open_market_value"] == 1110.0
    assert row["unrealised_pnl_abs"] == 110.0
    assert row["total_equity"] == 101110.0


def test_portfolio_equity_missing_cash_fails_closed_without_bootstrap(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    position = open_position_row()
    write_portfolio_state_csv(data_dir / "portfolio_state.csv", [position])
    write_portfolio_monitor_csv(
        data_dir / "portfolio_monitor.csv",
        [portfolio_monitor_row(position)],
    )
    cash_path = data_dir / "cash_state.csv"
    monkeypatch.setattr(portfolio_equity_agent, "get_or_create_run_id", lambda: "RUN_NO_CASH")

    with pytest.raises(FileNotFoundError, match="cash_state.csv"):
        portfolio_equity_agent.run_portfolio_equity_agent()

    assert not cash_path.exists()
