from __future__ import annotations

import pandas as pd
from pandas.testing import assert_frame_equal

from agents.fill_agent import fill_agent


def test_fill_agent_is_idempotent(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_FILL_TEST")

    pd.DataFrame(
        [
            {
                "fill_id": "FILL001",
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

    fill_agent.run_fill_agent()

    first_state = pd.read_csv(data_dir / "portfolio_state.csv")
    first_processed = pd.read_csv(data_dir / "processed_fills.csv")
    first_cash = pd.read_csv(data_dir / "cash_state.csv")
    first_ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    first_event_log = pd.read_csv(data_dir / "event_log.csv")

    fill_agent.run_fill_agent()

    second_state = pd.read_csv(data_dir / "portfolio_state.csv")
    second_processed = pd.read_csv(data_dir / "processed_fills.csv")
    second_cash = pd.read_csv(data_dir / "cash_state.csv")
    second_ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    second_event_log = pd.read_csv(data_dir / "event_log.csv")

    assert len(first_processed) == 1
    assert len(first_ledger) == 1
    assert len(first_event_log) == 3

    assert_frame_equal(first_state, second_state, check_dtype=False)
    assert_frame_equal(first_processed, second_processed, check_dtype=False)
    assert_frame_equal(first_cash, second_cash, check_dtype=False)
    assert_frame_equal(first_ledger, second_ledger, check_dtype=False)
    assert_frame_equal(first_event_log, second_event_log, check_dtype=False)
