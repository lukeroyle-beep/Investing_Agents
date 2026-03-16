# shared/schemas.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set


PORTFOLIO_STATE_COLUMNS: List[str] = [
    "position_id",
    "ticker",
    "side",
    "status",
    "quantity",
    "average_entry_price",
    "entry_date",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "regime_at_entry",
    "sector",
    "signal_score",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "current_price",
    "market_value",
    "unrealised_pnl_abs",
    "unrealised_pnl_pct",
    "realised_pnl_abs",
    "exit_reason",
    "last_updated_at",
]

REQUIRED_PORTFOLIO_STATE_COLUMNS: Set[str] = {
    "position_id",
    "ticker",
    "side",
    "status",
    "quantity",
    "average_entry_price",
    "entry_date",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "realised_pnl_abs",
    "last_updated_at",
}

PORTFOLIO_STATE_NUMERIC_COLUMNS: List[str] = [
    "quantity",
    "average_entry_price",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "signal_score",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "current_price",
    "market_value",
    "unrealised_pnl_abs",
    "unrealised_pnl_pct",
    "realised_pnl_abs",
]

ALLOWED_POSITION_SIDES: Set[str] = {"long", "short"}
ALLOWED_POSITION_STATUSES: Set[str] = {"open", "closed", "exit_required"}

PORTFOLIO_STATE_DEFAULTS: Dict[str, object] = {
    "regime_at_entry": "",
    "sector": "",
    "signal_score": 0.0,
    "current_price": 0.0,
    "market_value": 0.0,
    "unrealised_pnl_abs": 0.0,
    "unrealised_pnl_pct": 0.0,
    "exit_reason": "",
}