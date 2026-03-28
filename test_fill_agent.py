import pandas as pd

from agents.fill_agent.fill_agent import (
    append_processed_fill_ids,
    get_unprocessed_fills,
    process_fills,
)
from shared.schemas import validate_portfolio_state


def main() -> None:
    empty_state = validate_portfolio_state(pd.DataFrame())
    empty_processed = pd.DataFrame(columns=["fill_id", "processed_at"])

    fills = pd.DataFrame(
        [
            {
                "fill_id": "FILL001",
                "filled_at": "2026-03-16T12:00:00+00:00",
                "ticker": "COST",
                "side": "buy",
                "quantity": 1,
                "fill_price": 700.0,
                "stop_loss": 650.0,
                "take_profit": 780.0,
                "regime_at_entry": "risk_off",
                "sector": "Consumer Staples",
                "signal_score": 8.5,
            },
            {
                "fill_id": "FILL002",
                "filled_at": "2026-03-16T13:00:00+00:00",
                "ticker": "COST",
                "side": "buy",
                "quantity": 1,
                "fill_price": 720.0,
                "stop_loss": 660.0,
                "take_profit": 800.0,
                "regime_at_entry": "risk_off",
                "sector": "Consumer Staples",
                "signal_score": 8.7,
            },
            {
                "fill_id": "FILL003",
                "filled_at": "2026-03-16T14:00:00+00:00",
                "ticker": "COST",
                "side": "sell",
                "quantity": 1,
                "fill_price": 750.0,
            },
        ]
    )

    first_batch = get_unprocessed_fills(fills, empty_processed)
    result_1 = process_fills(first_batch, empty_state)
    processed_after_first_run = append_processed_fill_ids(empty_processed, first_batch)

    second_batch = get_unprocessed_fills(fills, processed_after_first_run)
    result_2 = process_fills(second_batch, result_1)

    print("First run result:")
    print(result_1)
    print("\nProcessed ledger after first run:")
    print(processed_after_first_run)
    print("\nSecond run unprocessed batch:")
    print(second_batch)
    print("\nSecond run result:")
    print(result_2)
    print("\nValidation passed.")

    assert len(second_batch) == 0, "Second batch should be empty because all fills were already processed."
    assert result_1.equals(result_2), "Portfolio state should not change on the second run."


if __name__ == "__main__":
    main()
