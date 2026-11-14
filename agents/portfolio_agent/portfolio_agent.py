import os
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path

import pandas as pd
import yaml

from shared.paths import (
    FINAL_SHORTLIST_PATH,
    MACRO_REGIME_PATH,
    NEWS_FLAGS_PATH,
    PORTFOLIO_CANDIDATES_PATH,
    PORTFOLIO_ORDERS_PATH,
    PORTFOLIO_POSITIONS_PATH,
    PORTFOLIO_STATE_PATH,
    config_path,
)
from shared.run_context import get_or_create_run_id


FINAL_SHORTLIST_FILE = FINAL_SHORTLIST_PATH
MACRO_REGIME_FILE = MACRO_REGIME_PATH
NEWS_FLAGS_FILE = NEWS_FLAGS_PATH
PORTFOLIO_STATE_FILE = PORTFOLIO_STATE_PATH
GOVERNANCE_FILE = config_path("governance.yaml")

PORTFOLIO_CANDIDATES_FILE = PORTFOLIO_CANDIDATES_PATH
PORTFOLIO_ORDERS_FILE = PORTFOLIO_ORDERS_PATH
PORTFOLIO_POSITIONS_FILE = PORTFOLIO_POSITIONS_PATH

PORTFOLIO_ORDER_COLUMNS = [
    "run_id",
    "internal_instrument_id",
    "ticker",
    "name",
    "exchange",
    "currency",
    "direction",
    "asset_type",
    "execution_environment",
    "order_type",
    "sizing_method",
    "sizing_value",
    "entry_price",
    "position_size_pct",
    "capital_allocated",
    "stop_loss_price",
    "take_profit_price",
    "recommendation_status",
    "recommendation_notes",
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

PORTFOLIO_POSITION_COLUMNS = [
    "run_id",
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


def load_governance():
    if not os.path.exists(GOVERNANCE_FILE):
        return {}

    with open(GOVERNANCE_FILE, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


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


def load_open_tickers():
    if not os.path.exists(PORTFOLIO_STATE_FILE):
        return set()

    df = pd.read_csv(PORTFOLIO_STATE_FILE)

    if df.empty:
        return set()

    if "ticker" not in df.columns or "status" not in df.columns:
        return set()

    open_df = df[df["status"].astype(str).str.strip().str.lower() == "open"].copy()

    if open_df.empty:
        return set()

    return set(open_df["ticker"].astype(str).str.strip().str.upper().tolist())


def write_empty_portfolio_outputs():
    Path(PORTFOLIO_CANDIDATES_FILE).parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame().to_csv(PORTFOLIO_CANDIDATES_FILE, index=False)
    pd.DataFrame(columns=PORTFOLIO_ORDER_COLUMNS).to_csv(PORTFOLIO_ORDERS_FILE, index=False)
    pd.DataFrame(columns=PORTFOLIO_POSITION_COLUMNS).to_csv(PORTFOLIO_POSITIONS_FILE, index=False)


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


def _float_noise(spelling):
    token = spelling.strip().lower()
    if token[:1] in {"+", "-"}:
        token = token[1:]
    coefficient = token.split("e", 1)[0]
    if "." not in coefficient:
        return False
    fraction = coefficient.split(".", 1)[1]
    return len(fraction) >= 10 and ("000000" in fraction or "999999" in fraction)


def _finite_decimal(value, name):
    """Finite decimal. Floats use their shortest spelling; binary residue is rejected."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite decimal")
    if isinstance(value, Decimal):
        parsed = value
        shown = format(parsed, "f")
    elif isinstance(value, int):
        parsed = Decimal(value)
        shown = None
    elif isinstance(value, float):
        shown = str(value)
        try:
            parsed = Decimal(shown)
        except InvalidOperation as exc:
            raise ValueError(f"{name} must be a finite decimal") from exc
    else:
        shown = str(value).strip()
        try:
            parsed = Decimal(shown)
        except InvalidOperation as exc:
            raise ValueError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    if shown is not None and _float_noise(shown):
        raise ValueError(f"{name} contains binary float noise")
    return parsed


def calculate_position_risk_fields(row, risk_budget_pct):
    atr_pct = _finite_decimal(row["atr_pct"], "atr_pct")
    # ROUND_HALF_EVEN is the mode already used for fill-agent percents.
    # Python's round() is the same mode, but it was applied to a binary float.
    stop_loss_pct = (atr_pct * Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    if stop_loss_pct <= 0:
        raw_position_size_pct = Decimal("0.00")
    else:
        budget = _finite_decimal(risk_budget_pct, "risk_budget_pct")
        raw_position_size_pct = (budget / stop_loss_pct * Decimal("100")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )

    return {
        "risk_budget_pct": risk_budget_pct,
        "stop_loss_pct": float(stop_loss_pct),
        "raw_position_size_pct": float(raw_position_size_pct),
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


def build_portfolio_orders(selected_df, governance, run_id):
    output_df = selected_df.copy()
    default_take_profit_pct = _finite_decimal(
        governance.get("default_take_profit_pct", 12.0), "default_take_profit_pct"
    )
    notional_portfolio_value = _finite_decimal(
        governance.get("notional_portfolio_value", 10000.0), "notional_portfolio_value"
    )
    hundred = Decimal("100")
    price_quantum = Decimal("0.0001")
    cent = Decimal("0.01")

    output_df["run_id"] = run_id
    output_df["internal_instrument_id"] = (
        output_df["internal_instrument_id"]
        if "internal_instrument_id" in output_df.columns
        else ""
    )
    output_df["exchange"] = output_df["exchange"] if "exchange" in output_df.columns else ""
    output_df["currency"] = output_df["currency"] if "currency" in output_df.columns else ""
    output_df["direction"] = "long"
    output_df["asset_type"] = output_df["asset_class"] if "asset_class" in output_df.columns else "equity"
    output_df["execution_environment"] = "demo"
    output_df["order_type"] = "market"
    output_df["sizing_method"] = "fixed_notional"

    entries = []
    position_sizes = []
    capitals = []
    stop_prices = []
    take_prices = []
    for _, row in output_df.iterrows():
        entry = _finite_decimal(row["latest_close"], "latest_close").quantize(
            price_quantum, rounding=ROUND_HALF_EVEN
        )
        position_size = _finite_decimal(
            row["scaled_target_allocation_pct"], "scaled_target_allocation_pct"
        ).quantize(cent, rounding=ROUND_HALF_EVEN)
        capital = (notional_portfolio_value * position_size / hundred).quantize(
            cent, rounding=ROUND_HALF_EVEN
        )
        stop_pct = _finite_decimal(row["stop_loss_pct"], "stop_loss_pct")
        # Subtract the stop distance from entry before adding the take-profit distance.
        stop_price = (entry - (entry * stop_pct / hundred)).quantize(
            price_quantum, rounding=ROUND_HALF_EVEN
        )
        take_price = (entry + (entry * default_take_profit_pct / hundred)).quantize(
            price_quantum, rounding=ROUND_HALF_EVEN
        )
        entries.append(float(entry))
        position_sizes.append(float(position_size))
        capitals.append(float(capital))
        stop_prices.append(float(stop_price))
        take_prices.append(float(take_price))

    output_df["entry_price"] = pd.Series(entries, index=output_df.index, dtype="float64")
    output_df["position_size_pct"] = pd.Series(position_sizes, index=output_df.index, dtype="float64")
    output_df["capital_allocated"] = pd.Series(capitals, index=output_df.index, dtype="float64")
    output_df["sizing_value"] = output_df["capital_allocated"]
    output_df["stop_loss_price"] = pd.Series(stop_prices, index=output_df.index, dtype="float64")
    output_df["take_profit_price"] = pd.Series(take_prices, index=output_df.index, dtype="float64")
    output_df["recommendation_status"] = "ready_for_review"
    output_df["recommendation_notes"] = output_df["risk_notes"].fillna("").astype(str)

    return output_df[PORTFOLIO_ORDER_COLUMNS].copy()


def main():
    run_id = get_or_create_run_id()
    shortlist_df = load_final_shortlist()

    if shortlist_df.empty:
        print("No final shortlist found. Run the pipeline first.")
        write_empty_portfolio_outputs()
        return

    governance = load_governance()
    market_regime = load_macro_regime()
    regime_settings = get_regime_settings(market_regime)

    news_flags_df = load_news_flags()
    open_tickers = load_open_tickers()

    candidates_df = shortlist_df.copy()
    candidates_df = candidates_df[
        candidates_df["risk_decision"].isin(["approved", "caution"])
    ].copy()

    if candidates_df.empty:
        print("No portfolio candidates available after shortlist filtering.")
        write_empty_portfolio_outputs()
        return

    candidates_df["ticker"] = candidates_df["ticker"].astype(str).str.strip().str.upper()
    candidates_df = candidates_df[~candidates_df["ticker"].isin(open_tickers)].copy()

    if candidates_df.empty:
        print("No portfolio candidates available after excluding open positions.")
        write_empty_portfolio_outputs()
        return

    candidates_df["risk_priority"] = candidates_df["risk_decision"].apply(risk_priority_value)
    candidates_df["sector"] = candidates_df["ticker"].apply(assign_sector)

    candidates_df = candidates_df.sort_values(
        by=["adjusted_setup_score", "risk_priority", "ticker"],
        ascending=[False, True, True]
    ).reset_index(drop=True)

    if not news_flags_df.empty and "ticker" in news_flags_df.columns:
        news_flags_df = news_flags_df.copy()
        news_flags_df["ticker"] = news_flags_df["ticker"].astype(str).str.strip().str.upper()

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
        write_empty_portfolio_outputs()
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

    orders_df = build_portfolio_orders(selected_df, governance, run_id)

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
    positions_df.insert(0, "run_id", run_id)

    Path(PORTFOLIO_CANDIDATES_FILE).parent.mkdir(parents=True, exist_ok=True)

    candidates_df.to_csv(PORTFOLIO_CANDIDATES_FILE, index=False)
    orders_df.to_csv(PORTFOLIO_ORDERS_FILE, index=False)
    positions_df.to_csv(PORTFOLIO_POSITIONS_FILE, index=False)

    print("\nPortfolio Agent finished.")
    print(f"Saved portfolio candidates to: {PORTFOLIO_CANDIDATES_FILE}")
    print(f"Saved portfolio orders to: {PORTFOLIO_ORDERS_FILE}")
    print(f"Saved portfolio positions to: {PORTFOLIO_POSITIONS_FILE}")

    print("\nRun summary:")
    print(f"Run ID: {run_id}")
    print(f"Market regime: {market_regime}")
    print(f"Max positions allowed: {max_positions}")
    print(f"Capital deployment percent: {capital_deployment_pct}")
    print(f"Risk budget per position percent: {risk_budget_per_position_pct}")
    print(f"Max positions per sector: {max_positions_per_sector}")
    print(f"Open tickers excluded: {len(open_tickers)}")
    print(f"Total eligible candidates: {len(candidates_df)}")
    print(f"Selected positions: {len(selected_df)}")

    print("\nPortfolio orders preview:")
    print(orders_df)


if __name__ == "__main__":
    main()
