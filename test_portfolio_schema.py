# test_portfolio_schema.py

import pandas as pd

from shared.validation import validate_portfolio_state

sample = pd.DataFrame(
    [
        {
            "position_id": "POS001",
            "ticker": "COST",
            "side": "long",
            "status": "open",
            "quantity": 1,
            "average_entry_price": 700.0,
            "entry_date": "2026-03-16",
            "capital_allocated": 700.0,
            "stop_loss": 650.0,
            "take_profit": 780.0,
            "regime_at_entry": "risk_off",
            "sector": "Consumer Staples",
            "signal_score": 8.5,
            "highest_price_since_entry": 700.0,
            "lowest_price_since_entry": 700.0,
            "realised_pnl_abs": 0.0,
            "last_updated_at": "2026-03-16T12:00:00+00:00",
        }
    ]
)

validated = validate_portfolio_state(sample)

print(validated)
print("\nValidation passed.")