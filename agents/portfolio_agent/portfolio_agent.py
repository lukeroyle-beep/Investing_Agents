import os
from datetime import datetime, UTC

import pandas as pd


FINAL_SHORTLIST_FILE = os.path.join("data", "final_shortlist.csv")
MACRO_REGIME_FILE = os.path.join("data", "macro_regime.csv")
NEWS_FLAGS_FILE = os.path.join("data", "news_flags.csv")

PORTFOLIO_CANDIDATES_FILE = os.path.join("data", "portfolio_candidates.csv")
PORTFOLIO_ORDERS_FILE = os.path.join("data", "portfolio_orders.csv")
PORTFOLIO_POSITIONS_FILE = os.path.join("data", "portfolio_positions.csv")


SECTOR_MAP = {
    "AAPL": "Technology",
    "ABBV": "Healthcare",
    "ABNB": "Consumer Discretionary",
    "AMAT": "Technology",
    "AMZN": "Consumer Discretionary",
    "ARM": "Technology",
    "BA": "Industrials",
    "BKNG": "Consumer Discretionary",
    "BRK-B": "Financials",
    "COST": "Consumer Staples",
    "CRM": "Technology",
    "CRWD": "Technology",
    "CVX": "Energy",
    "DDOG": "Technology",
    "DOCU": "Technology",
    "HIMS": "Healthcare",
    "INTC": "Technology",
    "JNJ": "Healthcare",
    "KO": "Consumer Staples",
    "LMT": "Industrials",
    "MCD": "Consumer Discretionary",
    "MU": "Technology",
    "NET": "Technology",
    "NFLX": "Communication Services",
    "NOC": "Industrials",
    "ORCL": "Technology",
    "PANW": "Technology",
    "PFE": "Healthcare",
    "PLTR": "Technology",
    "PYPL": "Financials",
    "RTX": "Industrials",
    "SBUX": "Consumer Discretionary",
    "SNOW": "Technology",
    "T": "Communication Services",
    "UBER": "Consumer Discretionary",
    "VZ": "Communication Services",
    "WMT": "Consumer Staples",
    "XOM": "Energy",
}


def load_final_shortlist():
    if not os.path.exists(FINAL_SHORTLIST_FILE):
        return pd.DataFrame()

    df = pd.read_csv(FINAL_SHORTLIST_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def load_macro_regime():
    if not os.path.exists(MACRO_REGIME_FILE):
        return "neutral"

    df = pd.read_csv(MACRO_REGIME_FILE)

    if df.empty or "market_regime" not in df.columns:
        return "neutral"

    return str(df.iloc[0]["market_regime"]).strip().lower()


def load_news_flags():
    if not os.path.exists(NEWS_FLAGS_FILE):
        return pd.DataFrame()

    df = pd.read_csv(NEWS_FLAGS_FILE)

    if df.empty:
        return pd.DataFrame()

    return df


def get_regime_settings(market_regime):
    if market_regime == "risk_on":
        return {
            "max_positions": 5,
            "capital_deployment_pct": 100.0,
            "risk_budget_per_position_pct": 1.0,
            "max_positions_per_sector": 2,
        }
    if market_regime == "neutral":
        return {
            "max_positions": 3,
            "capital_deployment_pct": 60.0,
            "risk_budget_per_position_pct": 0.6,
            "max_positions_per_sector": 1,
        }

    return {
        "max_positions": 2,
        "capital_deployment_pct": 25.0,
        "risk_budget_per_position_pct": 0.3,
        "max_positions_per_sector": 1,
    }


def risk_priority_value(risk_decision):
    decision = str(risk_decision).strip().lower()

    if decision == "approved":
        return 0
    if decision == "caution":
        return 1

    return 2


def calculate_position_risk_fields(row, risk_budget_pct):
    atr_pct = float(row["atr_pct"])

    stop_loss_pct = round(atr_pct * 2, 2)

    if stop_loss_pct <= 0:
        raw_position_size_pct = 0.0
    else:
        raw_position_size_pct = round((risk_budget_pct / stop_loss_pct) * 100, 2)

    return {
        "risk_budget_pct": risk_budget_pct,
        "stop_loss_pct": stop_loss_pct,
        "raw_position_size_pct": raw_position_size_pct,
    }


def assign_sector(ticker):
    ticker = str(ticker).strip().upper()
    return SECTOR_MAP.get(ticker, "Unknown")


def apply_sector_limits(candidates_df, max_positions, max_positions_per_sector):
    selected_rows = []
    sector_counts = {}

    for _, row in candidates_df.iterrows():
        sector = row["sector"]

        if sector not in sector_counts:
            sector_counts[sector] = 0

        if sector_counts[sector] >= max_positions_per_sector:
            continue

        selected_rows.append(row)
        sector_counts[sector] += 1

        if len(selected_rows) >= max_positions:
            break

    if not selected_rows:
        return pd.DataFrame()

    return pd.DataFrame(selected_rows).reset_index(drop=True)


def main():
    shortlist_df = load_final_shortlist()

    if shortlist_df.empty:
        print("No final shortlist found. Run the pipeline first.")
        return

    market_regime = load_macro_regime()
    regime_settings = get_regime_settings(market_regime)

    news_flags_df = load_news_flags()

    candidates_df = shortlist_df.copy()
    candidates_df = candidates_df[
        candidates_df["risk_decision"].isin(["approved", "caution"])
    ].copy()

    if candidates_df.empty:
        print("No portfolio candidates available after shortlist filtering.")
        return

    candidates_df["risk_priority"] = candidates_df["risk_decision"].apply(risk_priority_value)
    candidates_df["sector"] = candidates_df["ticker"].apply(assign_sector)

    candidates_df = candidates_df.sort_values(
        by=["adjusted_setup_score", "risk_priority", "ticker"],
        ascending=[False, True, True]
    ).reset_index(drop=True)

    if not news_flags_df.empty and "ticker" in news_flags_df.columns:
        candidates_df = candidates_df.merge(
            news_flags_df[["ticker", "headline_count", "categories_found", "has_news"]],
            on="ticker",
            how="left"
        )
    else:
        candidates_df["headline_count"] = 0
        candidates_df["categories_found"] = ""
        candidates_df["has_news"] = False

    max_positions = regime_settings["max_positions"]
    capital_deployment_pct = regime_settings["capital_deployment_pct"]
    risk_budget_per_position_pct = regime_settings["risk_budget_per_position_pct"]
    max_positions_per_sector = regime_settings["max_positions_per_sector"]

    selected_df = apply_sector_limits(
        candidates_df=candidates_df,
        max_positions=max_positions,
        max_positions_per_sector=max_positions_per_sector,
    )

    if selected_df.empty:
        print("No positions selected for the portfolio.")
        return

    risk_rows = []
    for _, row in selected_df.iterrows():
        risk_rows.append(calculate_position_risk_fields(row, risk_budget_per_position_pct))

    risk_df = pd.DataFrame(risk_rows)
    selected_df = pd.concat([selected_df.reset_index(drop=True), risk_df], axis=1)

    total_raw_position_size = selected_df["raw_position_size_pct"].sum()

    if total_raw_position_size > 0:
        selected_df["scaled_target_allocation_pct"] = (
            selected_df["raw_position_size_pct"] / total_raw_position_size
        ) * capital_deployment_pct
    else:
        selected_df["scaled_target_allocation_pct"] = 0.0

    selected_df["scaled_target_allocation_pct"] = selected_df["scaled_target_allocation_pct"].round(2)
    selected_df["market_regime"] = market_regime
    selected_df["capital_deployment_pct"] = capital_deployment_pct
    selected_df["portfolio_action"] = "buy"
    selected_df["portfolio_checked_at"] = datetime.now(UTC).isoformat()

    orders_df = selected_df[
        [
            "ticker",
            "name",
            "sector",
            "market_regime",
            "adjusted_setup_score",
            "adjusted_setup_status",
            "risk_decision",
            "risk_notes",
            "headline_count",
            "categories_found",
            "has_news",
            "risk_budget_pct",
            "stop_loss_pct",
            "raw_position_size_pct",
            "scaled_target_allocation_pct",
            "portfolio_action",
            "portfolio_checked_at",
        ]
    ].copy()

    positions_df = selected_df[
        [
            "ticker",
            "name",
            "sector",
            "market_regime",
            "adjusted_setup_score",
            "risk_decision",
            "risk_notes",
            "risk_budget_pct",
            "stop_loss_pct",
            "raw_position_size_pct",
            "scaled_target_allocation_pct",
            "headline_count",
            "categories_found",
            "has_news",
            "portfolio_checked_at",
        ]
    ].copy()

    os.makedirs("data", exist_ok=True)

    candidates_df.to_csv(PORTFOLIO_CANDIDATES_FILE, index=False)
    orders_df.to_csv(PORTFOLIO_ORDERS_FILE, index=False)
    positions_df.to_csv(PORTFOLIO_POSITIONS_FILE, index=False)

    print("\nPortfolio Agent finished.")
    print(f"Saved portfolio candidates to: {PORTFOLIO_CANDIDATES_FILE}")
    print(f"Saved portfolio orders to: {PORTFOLIO_ORDERS_FILE}")
    print(f"Saved portfolio positions to: {PORTFOLIO_POSITIONS_FILE}")

    print("\nRun summary:")
    print(f"Market regime: {market_regime}")
    print(f"Max positions allowed: {max_positions}")
    print(f"Capital deployment percent: {capital_deployment_pct}")
    print(f"Risk budget per position percent: {risk_budget_per_position_pct}")
    print(f"Max positions per sector: {max_positions_per_sector}")
    print(f"Total eligible candidates: {len(candidates_df)}")
    print(f"Selected positions: {len(selected_df)}")

    print("\nPortfolio orders preview:")
    print(orders_df)


if __name__ == "__main__":
    main()