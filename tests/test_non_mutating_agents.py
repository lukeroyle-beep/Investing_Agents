from __future__ import annotations

from agents.exit_agent import exit_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from tests.helpers import cash_state_frame, open_position_row, write_csv, write_portfolio_state_csv


def test_exit_agent_does_not_mutate_portfolio_state(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    portfolio_state_path = data_dir / "portfolio_state.csv"

    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXIT_TEST")

    write_portfolio_state_csv(portfolio_state_path, [open_position_row()])
    before_text = portfolio_state_path.read_text(encoding="utf-8")

    exit_agent.run_exit_agent()

    after_text = portfolio_state_path.read_text(encoding="utf-8")

    assert before_text == after_text
    assert (data_dir / "exit_advice.csv").exists()


def test_portfolio_equity_agent_does_not_mutate_state_inputs(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    portfolio_state_path = data_dir / "portfolio_state.csv"
    cash_state_path = data_dir / "cash_state.csv"

    monkeypatch.setattr(portfolio_equity_agent, "get_or_create_run_id", lambda: "RUN_EQUITY_TEST")

    write_portfolio_state_csv(portfolio_state_path, [open_position_row()])
    write_csv(cash_state_path, cash_state_frame(balance=100000.0))

    state_before = portfolio_state_path.read_text(encoding="utf-8")
    cash_before = cash_state_path.read_text(encoding="utf-8")

    portfolio_equity_agent.run_portfolio_equity_agent()

    state_after = portfolio_state_path.read_text(encoding="utf-8")
    cash_after = cash_state_path.read_text(encoding="utf-8")

    assert state_before == state_after
    assert cash_before == cash_after
    assert (data_dir / "portfolio_equity_history.csv").exists()
    assert (data_dir / "performance_summary.csv").exists()
