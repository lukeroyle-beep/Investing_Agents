from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, UTC

import pandas as pd


PORTFOLIO_STATE_FILE = os.path.join("data", "portfolio_state.csv")
POSITION_ALERTS_FILE = os.path.join("data", "position_alerts.csv")
UNIVERSE_SNAPSHOT_FILE = os.path.join("data", "universe_snapshot.csv")


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


@dataclass
class RunResult:
    total_positions: int
    open_positions: int
    exit_required: int
    updated_at: str


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def ensure_portfolio_state_file() -> pd.DataFrame:
    os.makedirs("data", exist_ok=True)

    if not os.path.exists(PORTFOLIO_STATE_FILE):
        df = pd.DataFrame(columns=REQUIRED_COLUMNS)
        df.to_csv(PORTFOLIO_STATE_FILE, index=False)
        return df

    df = pd.read_csv(PORTFOLIO_STATE_FILE)

    for column in REQUIRED_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[REQUIRED_COLUMNS].copy()
    return df


def normalize_portfolio_state(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    df = df.copy()

    for column in NUMERIC_COLUMNS:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["side"] = df["side"].astype(str).str.strip().str.lower()
    df["status"] = df["status"].astype(str).str.strip().str.lower()

    return df


def load_latest_prices() -> pd.DataFrame:
    if not os.path.exists(UNIVERSE_SNAPSHOT_FILE):
        return pd.DataFrame(columns=["ticker", "latest_price"])

    df = pd.read_csv(UNIVERSE_SNAPSHOT_FILE)

    if df.empty:
        return pd.DataFrame(columns=["ticker", "latest_price"])

    if "ticker" not in df.columns or "latest_close" not in df.columns:
        return pd.DataFrame(columns=["ticker", "latest_price"])

    prices_df = df[["ticker", "latest_close"]].copy()
    prices_df["ticker"] = prices_df["ticker"].astype(str).str.strip().str.upper()
    prices_df["latest_close"] = pd.to_numeric(prices_df["latest_close"], errors="coerce")
    prices_df = prices_df.rename(columns={"latest_close": "latest_price"})

    prices_df = prices_df.dropna(subset=["ticker"]).drop_duplicates(subset=["ticker"], keep="last")
    return prices_df


def refresh_position_prices(portfolio_df: pd.DataFrame, prices_df: pd.DataFrame) -> pd.DataFrame:
    if portfolio_df.empty:
        return portfolio_df.copy()

    merged_df = portfolio_df.merge(prices_df, on="ticker", how="left")

    merged_df["current_price"] = merged_df["latest_price"].combine_first(merged_df["current_price"])
    merged_df = merged_df.drop(columns=["latest_price"])

    return merged_df


def calculate_position_metrics(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()

    df = df.copy()

    long_mask = df["side"] == "long"
    short_mask = df["side"] == "short"

    df["market_value"] = df["current_price"] * df["quantity"]

    df.loc[long_mask, "pnl_abs"] = (
        (df.loc[long_mask, "current_price"] - df.loc[long_mask, "entry_price"])
        * df.loc[long_mask, "quantity"]
    )

    df.loc[short_mask, "pnl_abs"] = (
        (df.loc[short_mask, "entry_price"] - df.loc[short_mask, "current_price"])
        * df.loc[short_mask, "quantity"]
    )

    df.loc[long_mask, "pnl_pct"] = (
        (df.loc[long_mask, "current_price"] - df.loc[long_mask, "entry_price"])
        / df.loc[long_mask, "entry_price"]
    ) * 100.0

    df.loc[short_mask, "pnl_pct"] = (
        (df.loc[short_mask, "entry_price"] - df.loc[short_mask, "current_price"])
        / df.loc[short_mask, "entry_price"]
    ) * 100.0

    df["highest_price_since_entry"] = df[
        ["highest_price_since_entry", "current_price"]
    ].max(axis=1, skipna=True)

    df["lowest_price_since_entry"] = df[
        ["lowest_price_since_entry", "current_price"]
    ].min(axis=1, skipna=True)

    df["last_updated_at"] = utc_now_iso()

    return df


def create_alerts(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if df.empty:
        alerts_df = pd.DataFrame(
            columns=["position_id", "ticker", "alert_type", "message", "generated_at"]
        )
        return df.copy(), alerts_df

    df = df.copy()
    alerts = []
    now = utc_now_iso()

    for idx, row in df.iterrows():
        if row["status"] != "open":
            continue

        ticker = row["ticker"]
        side = row["side"]
        current_price = row["current_price"]
        stop_loss = row["stop_loss"]
        take_profit = row["take_profit"]

        if pd.isna(current_price):
            alerts.append(
                {
                    "position_id": row["position_id"],
                    "ticker": ticker,
                    "alert_type": "missing_price",
                    "message": f"No fresh price available for {ticker}",
                    "generated_at": now,
                }
            )
            continue

        stop_triggered = False
        take_profit_triggered = False

        if side == "long":
            stop_triggered = pd.notna(stop_loss) and current_price <= stop_loss
            take_profit_triggered = pd.notna(take_profit) and current_price >= take_profit
        elif side == "short":
            stop_triggered = pd.notna(stop_loss) and current_price >= stop_loss
            take_profit_triggered = pd.notna(take_profit) and current_price <= take_profit

        if stop_triggered:
            df.at[idx, "status"] = "exit_required"
            df.at[idx, "exit_reason"] = "stop_loss_triggered"
            alerts.append(
                {
                    "position_id": row["position_id"],
                    "ticker": ticker,
                    "alert_type": "stop_loss_triggered",
                    "message": f"{ticker} hit stop loss at {current_price}",
                    "generated_at": now,
                }
            )
        elif take_profit_triggered:
            df.at[idx, "status"] = "exit_required"
            df.at[idx, "exit_reason"] = "take_profit_triggered"
            alerts.append(
                {
                    "position_id": row["position_id"],
                    "ticker": ticker,
                    "alert_type": "take_profit_triggered",
                    "message": f"{ticker} hit take profit at {current_price}",
                    "generated_at": now,
                }
            )

    alerts_df = pd.DataFrame(alerts)
    if alerts_df.empty:
        alerts_df = pd.DataFrame(
            columns=["position_id", "ticker", "alert_type", "message", "generated_at"]
        )

    return df, alerts_df


def save_outputs(portfolio_df: pd.DataFrame, alerts_df: pd.DataFrame) -> None:
    portfolio_df.to_csv(PORTFOLIO_STATE_FILE, index=False)
    alerts_df.to_csv(POSITION_ALERTS_FILE, index=False)


def main() -> None:
    portfolio_df = ensure_portfolio_state_file()
    portfolio_df = normalize_portfolio_state(portfolio_df)

    prices_df = load_latest_prices()
    portfolio_df = refresh_position_prices(portfolio_df, prices_df)
    portfolio_df = calculate_position_metrics(portfolio_df)
    portfolio_df, alerts_df = create_alerts(portfolio_df)

    save_outputs(portfolio_df, alerts_df)

    result = RunResult(
        total_positions=len(portfolio_df),
        open_positions=int((portfolio_df["status"] == "open").sum()) if not portfolio_df.empty else 0,
        exit_required=int((portfolio_df["status"] == "exit_required").sum()) if not portfolio_df.empty else 0,
        updated_at=utc_now_iso(),
    )

    print("\nPosition Tracking Agent finished.")
    print(f"Saved portfolio state to: {PORTFOLIO_STATE_FILE}")
    print(f"Saved position alerts to: {POSITION_ALERTS_FILE}")

    print("\nRun summary:")
    print(f"Total positions: {result.total_positions}")
    print(f"Open positions: {result.open_positions}")
    print(f"Exit required: {result.exit_required}")
    print(f"Updated at: {result.updated_at}")

    if not portfolio_df.empty:
        print("\nPortfolio state preview:")
        print(portfolio_df)


if __name__ == "__main__":
    main()