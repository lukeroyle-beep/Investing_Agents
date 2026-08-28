from __future__ import annotations

import pandas as pd

from agents.position_tracking_agent import position_tracking_agent
from shared.schemas import validate_portfolio_monitor, validate_portfolio_state
from tests.helpers import (
    closed_position_row,
    open_position_row,
    portfolio_monitor_row,
    write_csv,
    write_portfolio_monitor_csv,
)


def test_position_tracking_writes_monitor_without_rewriting_canonical_state(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    portfolio_state_path = data_dir / "portfolio_state.csv"
    portfolio_monitor_path = data_dir / "portfolio_monitor.csv"

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
    state_before = portfolio_state_path.read_bytes()

    position_tracking_agent.run_position_tracking_agent()

    assert portfolio_state_path.read_bytes() == state_before
    output_df = validate_portfolio_monitor(
        pd.read_csv(portfolio_monitor_path),
        keep_extra_columns=False,
    )
    assert list(output_df.columns) == list(position_tracking_agent.PORTFOLIO_MONITOR_SCHEMA.column_order)

    row = output_df.iloc[0]
    assert row["position_id"] == "POS_ALIAS"
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
    assert row["marked_at"] == "2026-03-28T12:00:00+00:00"


def test_position_tracking_monitor_excludes_closed_rows_and_preserves_state_bytes(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    portfolio_state_path = data_dir / "portfolio_state.csv"

    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_TRACKING_TEST")
    monkeypatch.setattr(position_tracking_agent, "utc_now_iso", lambda: "2026-03-28T12:00:00+00:00")
    monkeypatch.setattr(
        position_tracking_agent,
        "get_latest_price",
        lambda ticker, fallback_price: float(fallback_price),
    )

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
    state_before = portfolio_state_path.read_bytes()

    position_tracking_agent.run_position_tracking_agent()

    assert portfolio_state_path.read_bytes() == state_before
    monitor_df = pd.read_csv(data_dir / "portfolio_monitor.csv")
    assert monitor_df["position_id"].tolist() == ["POS_OPEN"]


def test_position_tracking_carries_forward_monitor_high_low_marks(
    isolated_workspace,
    monkeypatch,
) -> None:
    data_dir = isolated_workspace / "data"
    state_path = data_dir / "portfolio_state.csv"
    monitor_path = data_dir / "portfolio_monitor.csv"
    position = open_position_row(
        current_price=101.0,
        highest_price_since_entry=102.0,
        lowest_price_since_entry=98.0,
    )
    write_csv(
        state_path,
        validate_portfolio_state(pd.DataFrame([position]), keep_extra_columns=False),
    )
    write_portfolio_monitor_csv(
        monitor_path,
        [
            portfolio_monitor_row(
                position,
                current_price=109.0,
                market_value=1090.0,
                pnl_abs=90.0,
                pnl_pct=9.0,
                highest_price_since_entry=115.0,
                lowest_price_since_entry=92.0,
            )
        ],
    )

    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_NEXT")
    monkeypatch.setattr(position_tracking_agent, "utc_now_iso", lambda: "2026-03-28T12:00:00+00:00")
    monkeypatch.setattr(position_tracking_agent, "get_latest_price", lambda ticker, fallback_price: 110.0)

    position_tracking_agent.run_position_tracking_agent()

    row = pd.read_csv(monitor_path).iloc[0]
    assert row["current_price"] == 110.0
    assert row["highest_price_since_entry"] == 115.0
    assert row["lowest_price_since_entry"] == 92.0
    assert row["run_id"] == "RUN_NEXT"
