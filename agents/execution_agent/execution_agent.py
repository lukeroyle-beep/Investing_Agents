import os
from datetime import datetime, UTC

import pandas as pd


PORTFOLIO_ORDERS_FILE = os.path.join("data", "portfolio_orders.csv")
EXECUTION_ORDERS_FILE = os.path.join("data", "execution_orders.csv")


def load_portfolio_orders():
    if not os.path.exists(PORTFOLIO_ORDERS_FILE):
        return pd.DataFrame()

    df = pd.read_csv(PORTFOLIO_ORDERS_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def build_execution_orders(df):
    execution_df = df.copy()

    execution_df["execution_status"] = execution_df["has_news"].apply(
        lambda x: "hold_for_review" if bool(x) else "ready"
    )

    execution_df["execution_notes"] = execution_df["has_news"].apply(
        lambda x: "Recent news present - manual review required" if bool(x) else "Clear for execution"
    )

    execution_df["execution_checked_at"] = datetime.now(UTC).isoformat()

    return execution_df[
        [
            "ticker",
            "name",
            "sector",
            "market_regime",
            "portfolio_action",
            "scaled_target_allocation_pct",
            "risk_budget_pct",
            "stop_loss_pct",
            "headline_count",
            "categories_found",
            "has_news",
            "execution_status",
            "execution_notes",
            "execution_checked_at",
        ]
    ].copy()


def main():
    portfolio_orders_df = load_portfolio_orders()

    if portfolio_orders_df.empty:
        print("No portfolio orders found. Run the Portfolio Agent first.")
        return

    execution_df = build_execution_orders(portfolio_orders_df)

    os.makedirs("data", exist_ok=True)
    execution_df.to_csv(EXECUTION_ORDERS_FILE, index=False)

    total_orders = len(execution_df)
    ready_count = len(execution_df[execution_df["execution_status"] == "ready"])
    hold_count = len(execution_df[execution_df["execution_status"] == "hold_for_review"])

    print("\nExecution Agent finished.")
    print(f"Saved execution orders to: {EXECUTION_ORDERS_FILE}")

    print("\nRun summary:")
    print(f"Total execution orders: {total_orders}")
    print(f"Ready: {ready_count}")
    print(f"Hold for review: {hold_count}")

    print("\nExecution orders preview:")
    print(execution_df)


if __name__ == "__main__":
    main()