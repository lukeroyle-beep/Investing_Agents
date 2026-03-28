from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from shared.run_history import RUN_HISTORY_COLUMNS
from shared.schemas import (
    validate_cash_state,
    validate_portfolio_equity_history,
    validate_portfolio_state,
    validate_processed_fills,
)


def open_position_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "position_id": "POS001",
        "ticker": "AAPL",
        "side": "long",
        "status": "open",
        "quantity": 10.0,
        "entry_price": 100.0,
        "entry_date": "2026-03-28T09:00:00+00:00",
        "capital_allocated": 1000.0,
        "stop_loss": 90.0,
        "take_profit": 120.0,
        "regime_at_entry": "risk_on",
        "sector": "technology",
        "signal_score": 8.0,
        "highest_price_since_entry": 105.0,
        "lowest_price_since_entry": 95.0,
        "current_price": 105.0,
        "market_value": 1050.0,
        "pnl_abs": 50.0,
        "pnl_pct": 5.0,
        "exit_flag": "",
        "exit_reason": "",
        "last_updated": "2026-03-28T10:00:00+00:00",
        "run_id": "RUN_BASE",
        "realised_pnl_abs": 0.0,
        "fees_total": 1.0,
        "closed_at": "",
        "exit_price": "",
    }
    row.update(overrides)
    return row


def closed_position_row(**overrides: Any) -> dict[str, Any]:
    row = open_position_row(
        status="closed",
        current_price=110.0,
        market_value=0.0,
        pnl_abs=0.0,
        pnl_pct=0.0,
        exit_flag="",
        exit_reason="position_closed",
        realised_pnl_abs=98.0,
        fees_total=2.0,
        closed_at="2026-03-28T11:00:00+00:00",
        exit_price=110.0,
    )
    row.update(overrides)
    return row


def write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def portfolio_state_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return validate_portfolio_state(pd.DataFrame(rows), keep_extra_columns=False)


def write_portfolio_state_csv(path: Path, rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = portfolio_state_frame(rows)
    write_csv(path, df)
    return df


def cash_state_frame(balance: float = 100000.0, as_of: str = "2026-03-28T10:00:00+00:00") -> pd.DataFrame:
    return validate_cash_state(
        pd.DataFrame([{"as_of": as_of, "cash_balance": balance}]),
        keep_extra_columns=False,
    )


def equity_history_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return validate_portfolio_equity_history(pd.DataFrame(rows), keep_extra_columns=False)


def processed_fills_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return validate_processed_fills(pd.DataFrame(rows), keep_extra_columns=False)


def run_history_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=RUN_HISTORY_COLUMNS).fillna("")
