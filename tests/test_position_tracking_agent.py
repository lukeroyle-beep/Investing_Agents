from __future__ import annotations

import pandas as pd

from agents.position_tracking_agent import position_tracking_agent
from shared.schemas import validate_portfolio_state
from tests.helpers import closed_position_row, open_position_row, write_csv


def test_position_tracking_agent_reads_and_writes_portfolio_state_via_shared_schema(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    portfolio_state_path = data_dir / "portfolio_state.csv"

    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_TRACKING_TEST")
    monkeypatch.setattr(position_tracking_agent, "utc_now_iso", lambda: "2026-03-28T12:00:00+00:00")
    monkeypatch.setattr(position_tracking_agent, "get_latest_price", lambda ticker, fallback_price: 111.0)

    raw_state = pd.DataFrame(
        [
            {
                "position_id": "POS_ALIAS",
                "ticker": "aapl",
                "side": "LONG",
                "status": "OPEN",
                "current_qty": 10.0,
                "average_entry_price": 100.0,
                "entry_date": "2026-03-28T09:00:00+00:00",
                "capital_allocated": 1000.0,
                "stop_loss": 90.0,
                "take_profit": 120.0,
                "current_price": 105.0,
                "market_value": 1050.0,
                "unrealised_pnl_abs": 50.0,
                "unrealised_pnl_pct": 5.0,
                "regime_at_entry": "risk_on",
                "sector": "technology",
                "signal_score": 8.0,
                "highest_price_since_entry": 105.0,
                "lowest_price_since_entry": 95.0,
                "exit_reason": "",
                "last_updated_at": "2026-03-28T10:00:00+00:00",
                "run_id": "RUN_OLD",
            }
        ]
    )
    write_csv(portfolio_state_path, raw_state)

    position_tracking_agent.run_position_tracking_agent()

    output_df = pd.read_csv(portfolio_state_path)

    assert "average_entry_price" not in output_df.columns
    assert "current_qty" not in output_df.columns
    assert "unrealised_pnl_abs" not in output_df.columns
    assert "unrealised_pnl_pct" not in output_df.columns
    assert "last_updated_at" not in output_df.columns

    row = output_df.iloc[0]
    assert row["ticker"] == "AAPL"
    assert row["side"] == "long"
    assert row["status"] == "open"
    assert row["quantity"] == 10.0
    assert row["entry_price"] == 100.0
    assert row["current_price"] == 111.0
    assert row["market_value"] == 1110.0
    assert row["pnl_abs"] == 110.0
    assert row["pnl_pct"] == 11.0
    assert row["highest_price_since_entry"] == 111.0
    assert row["lowest_price_since_entry"] == 95.0
    assert row["run_id"] == "RUN_TRACKING_TEST"
    assert row["last_updated"] == "2026-03-28T12:00:00+00:00"


def test_position_tracking_agent_preserves_closed_rows(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    portfolio_state_path = data_dir / "portfolio_state.csv"

    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_TRACKING_TEST")
    monkeypatch.setattr(position_tracking_agent, "utc_now_iso", lambda: "2026-03-28T12:00:00+00:00")
    monkeypatch.setattr(position_tracking_agent, "get_latest_price", lambda ticker, fallback_price: float(fallback_price))

    starting_df = validate_portfolio_state(
        pd.DataFrame(
            [
                open_position_row(position_id="POS_OPEN", run_id="RUN_OPEN"),
                closed_position_row(position_id="POS_CLOSED", run_id="RUN_CLOSED"),
            ]
        ),
        keep_extra_columns=False,
    )
    write_csv(portfolio_state_path, starting_df)

    before_closed = validate_portfolio_state(
        pd.read_csv(portfolio_state_path),
        keep_extra_columns=False,
    )
    before_closed = before_closed[before_closed["position_id"] == "POS_CLOSED"].reset_index(drop=True)

    position_tracking_agent.run_position_tracking_agent()

    after_df = validate_portfolio_state(
        pd.read_csv(portfolio_state_path),
        keep_extra_columns=False,
    )
    after_closed = after_df[after_df["position_id"] == "POS_CLOSED"].reset_index(drop=True)

    pd.testing.assert_frame_equal(after_closed, before_closed)
