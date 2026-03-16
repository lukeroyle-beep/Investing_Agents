from __future__ import annotations

import os
from datetime import datetime, UTC

import pandas as pd


INPUT_PORTFOLIO_MONITOR_FILE = os.path.join("data", "portfolio_monitor.csv")
OUTPUT_EXIT_ADVICE_FILE = os.path.join("data", "exit_advice.csv")


REQUIRED_COLUMNS = [
    "position_id",
    "ticker",
    "side",
    "status",
    "entry_date",
    "entry_price",
    "quantity",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "current_price",
    "market_value",
    "pnl_abs",
    "pnl_pct",
    "regime_at_entry",
    "sector",
    "signal_score",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "exit_reason",
    "last_updated_at",
]


NUMERIC_COLUMNS = [
    "entry_price",
    "quantity",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "current_price",
    "market_value",
    "pnl_abs",
    "pnl_pct",
    "signal_score",
    "highest_price_since_entry",
    "lowest_price_since_entry",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def load_portfolio_monitor() -> pd.DataFrame:
    if not os.path.exists(INPUT_PORTFOLIO_MONITOR_FILE):
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    df = pd.read_csv(INPUT_PORTFOLIO_MONITOR_FILE)

    if df.empty:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[REQUIRED_COLUMNS].copy()

    for column in NUMERIC_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    text_columns = [c for c in REQUIRED_COLUMNS if c not in NUMERIC_COLUMNS]
    for column in text_columns:
        df[column] = df[column].astype("object")

    df["ticker"] = df["ticker"].fillna("").astype(str).str.strip().str.upper()
    df["side"] = df["side"].fillna("").astype(str).str.strip().str.lower()
    df["status"] = df["status"].fillna("").astype(str).str.strip().str.lower()
    df["exit_reason"] = df["exit_reason"].fillna("").astype(str).str.strip().str.lower()

    return df


def decide_exit_action(row: pd.Series) -> tuple[str, str]:
    status = row["status"]
    exit_reason = row["exit_reason"]
    side = row["side"]
    current_price = row["current_price"]
    take_profit = row["take_profit"]
    stop_loss = row["stop_loss"]
    highest_price = row["highest_price_since_entry"]
    lowest_price = row["lowest_price_since_entry"]

    if status == "exit_required":
        if exit_reason == "take_profit_triggered":
            return "take_profit", "Take profit level has been reached."
        if exit_reason == "stop_loss_triggered":
            return "close", "Stop loss level has been triggered."
        return "review_immediately", "Exit required but exit reason is unclear."

    if pd.notna(current_price) and pd.notna(take_profit):
        if side == "long" and current_price >= take_profit:
            return "take_profit", "Current price is at or above take profit."
        if side == "short" and current_price <= take_profit:
            return "take_profit", "Current price is at or below take profit."

    if pd.notna(current_price) and pd.notna(stop_loss):
        if side == "long" and current_price <= stop_loss:
            return "close", "Current price is at or below stop loss."
        if side == "short" and current_price >= stop_loss:
            return "close", "Current price is at or above stop loss."

    if side == "long" and pd.notna(highest_price) and pd.notna(stop_loss) and pd.notna(current_price):
        if current_price > stop_loss and highest_price > current_price:
            return "raise_stop", "Consider raising stop loss to protect gains."

    if side == "short" and pd.notna(lowest_price) and pd.notna(stop_loss) and pd.notna(current_price):
        if current_price < stop_loss and lowest_price < current_price:
            return "raise_stop", "Consider lowering stop loss to protect gains."

    return "hold", "No exit condition triggered."


def build_exit_advice(df: pd.DataFrame) -> pd.DataFrame:
    output_rows: list[dict] = []

    if df.empty:
        return pd.DataFrame(
            columns=[
                "position_id",
                "ticker",
                "exit_action",
                "reason",
                "status",
                "exit_reason",
                "current_price",
                "stop_loss",
                "take_profit",
                "pnl_abs",
                "pnl_pct",
                "generated_at",
            ]
        )

    for _, row in df.iterrows():
        ticker = row["ticker"]
        if not ticker:
            continue

        exit_action, reason = decide_exit_action(row)

        output_rows.append(
            {
                "position_id": row["position_id"],
                "ticker": ticker,
                "exit_action": exit_action,
                "reason": reason,
                "status": row["status"],
                "exit_reason": row["exit_reason"],
                "current_price": row["current_price"],
                "stop_loss": row["stop_loss"],
                "take_profit": row["take_profit"],
                "pnl_abs": row["pnl_abs"],
                "pnl_pct": row["pnl_pct"],
                "generated_at": utc_now_iso(),
            }
        )

    return pd.DataFrame(output_rows)


def run() -> None:
    portfolio_df = load_portfolio_monitor()
    out_df = build_exit_advice(portfolio_df)
    out_path = OUTPUT_EXIT_ADVICE_FILE
    out_df.to_csv(out_path, index=False)

    hold_count = int((out_df["exit_action"] == "hold").sum()) if not out_df.empty else 0
    take_profit_count = int((out_df["exit_action"] == "take_profit").sum()) if not out_df.empty else 0
    close_count = int((out_df["exit_action"] == "close").sum()) if not out_df.empty else 0
    review_count = int((out_df["exit_action"] == "review_immediately").sum()) if not out_df.empty else 0
    raise_stop_count = int((out_df["exit_action"] == "raise_stop").sum()) if not out_df.empty else 0

    print("Exit Agent finished.")
    print(f"Saved exit advice to: {out_path}")
    print()
    print("Run summary:")
    print(f"Total exit advice rows: {len(out_df)}")
    print(f"Hold: {hold_count}")
    print(f"Take profit: {take_profit_count}")
    print(f"Close: {close_count}")
    print(f"Review immediately: {review_count}")
    print(f"Raise stop: {raise_stop_count}")


if __name__ == "__main__":
    run()