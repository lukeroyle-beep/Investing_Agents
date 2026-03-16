import pandas as pd
from pathlib import Path

from shared.paths import DATA_DIR
from shared.io_utils import read_csv, write_csv

FILLS_FILE = DATA_DIR / "trade_fills.csv"
PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"


def load_trade_fills():
    if not FILLS_FILE.exists():
        return pd.DataFrame()

    df = read_csv(FILLS_FILE)

    if df.empty:
        return df

    required_columns = [
        "timestamp",
        "ticker",
        "action",
        "price",
        "quantity",
    ]

    for col in required_columns:
        if col not in df.columns:
            raise ValueError(f"Missing column in trade_fills.csv: {col}")

    return df


def build_portfolio_state(fills_df):

    if fills_df.empty:
        return pd.DataFrame()

    positions = {}

    for _, row in fills_df.iterrows():

        ticker = row["ticker"]
        action = row["action"].lower()
        price = float(row["price"])
        qty = float(row["quantity"])

        if ticker not in positions:
            positions[ticker] = {
                "ticker": ticker,
                "quantity": 0,
                "avg_price": 0,
                "realised_pnl": 0,
            }

        pos = positions[ticker]

        if action == "buy":

            new_qty = pos["quantity"] + qty

            if new_qty == 0:
                avg_price = 0
            else:
                avg_price = (
                    (pos["quantity"] * pos["avg_price"]) + (qty * price)
                ) / new_qty

            pos["quantity"] = new_qty
            pos["avg_price"] = avg_price

        elif action == "sell":

            sell_qty = min(qty, pos["quantity"])

            pnl = (price - pos["avg_price"]) * sell_qty

            pos["quantity"] -= sell_qty
            pos["realised_pnl"] += pnl

        else:
            raise ValueError(f"Invalid action: {action}")

    state_rows = []

    for ticker, pos in positions.items():

        if pos["quantity"] <= 0:
            continue

        state_rows.append({
            "ticker": ticker,
            "quantity": pos["quantity"],
            "avg_price": round(pos["avg_price"], 4),
            "realised_pnl": round(pos["realised_pnl"], 2),
        })

    return pd.DataFrame(state_rows)


def main():

    print("=== Running Fill Agent ===")

    fills_df = load_trade_fills()

    portfolio_df = build_portfolio_state(fills_df)

    write_csv(portfolio_df, PORTFOLIO_STATE_FILE)

    print("Fill Agent finished.")

    print(f"Saved portfolio state to: {PORTFOLIO_STATE_FILE}")

    print("\nRun summary:")

    print(f"Trades processed: {len(fills_df)}")

    print(f"Open positions: {len(portfolio_df)}")


if __name__ == "__main__":
    main()