from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"
PORTFOLIO_EQUITY_FILE = DATA_DIR / "portfolio_equity.csv"


STATE_COLUMNS = [
    "position_id",
    "ticker",
    "side",
    "status",
    "quantity",
    "entry_price",
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
    "pnl_abs",
    "pnl_pct",
    "exit_flag",
    "exit_reason",
    "last_updated",
    "run_id",
]

PORTFOLIO_EQUITY_COLUMNS = [
    "generated_at",
    "run_id",
    "total_positions",
    "open_positions",
    "closed_positions",
    "long_open_positions",
    "short_open_positions",
    "total_market_value",
    "total_capital_allocated_open",
    "total_unrealised_pnl_abs",
    "average_open_pnl_pct",
    "largest_open_position_value",
    "smallest_open_position_value",
    "largest_open_winner_ticker",
    "largest_open_winner_pnl_abs",
    "largest_open_loser_ticker",
    "largest_open_loser_pnl_abs",
    "positions_flagged_review",
    "positions_flagged_exit_required",
    "gross_long_exposure",
    "gross_short_exposure",
    "gross_exposure",
    "net_exposure",
    "cash_estimate",
    "equity_estimate",
    "portfolio_return_pct_on_open_capital",
    "drawdown_pct_from_high_capital",
    "sector_exposure_top_1_name",
    "sector_exposure_top_1_value",
    "sector_exposure_top_1_pct",
    "sector_exposure_top_2_name",
    "sector_exposure_top_2_value",
    "sector_exposure_top_2_pct",
    "sector_exposure_top_3_name",
    "sector_exposure_top_3_value",
    "sector_exposure_top_3_pct",
]

ALLOWED_SIDES = {"long", "short"}
ALLOWED_STATUSES = {"open", "closed"}
ALLOWED_EXIT_FLAGS = {"none", "review", "exit_required"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_run_id() -> str:
    return datetime.now(timezone.utc).strftime("RUN_%Y%m%dT%H%M%SZ")


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(temp_path, index=False)
    temp_path.replace(path)


def load_portfolio_state() -> pd.DataFrame:
    if not PORTFOLIO_STATE_FILE.exists():
        raise FileNotFoundError(f"Missing file: {PORTFOLIO_STATE_FILE}")

    df = pd.read_csv(PORTFOLIO_STATE_FILE)

    for column in STATE_COLUMNS:
        if column not in df.columns:
            if column == "status":
                df[column] = "open"
            elif column == "side":
                df[column] = "long"
            elif column == "exit_flag":
                df[column] = "none"
            elif column == "exit_reason":
                df[column] = ""
            elif column == "run_id":
                df[column] = ""
            else:
                df[column] = pd.NA

    df = df[STATE_COLUMNS].copy()

    string_columns = [
        "position_id",
        "ticker",
        "side",
        "status",
        "entry_date",
        "regime_at_entry",
        "sector",
        "exit_flag",
        "exit_reason",
        "last_updated",
        "run_id",
    ]
    for column in string_columns:
        df[column] = df[column].fillna("").astype(str).str.strip()

    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()
    df["status"] = df["status"].str.lower()
    df["exit_flag"] = df["exit_flag"].str.lower()

    numeric_columns = [
        "quantity",
        "entry_price",
        "capital_allocated",
        "stop_loss",
        "take_profit",
        "signal_score",
        "highest_price_since_entry",
        "lowest_price_since_entry",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
    ]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    validate_portfolio_state(df)
    return df


def validate_portfolio_state(df: pd.DataFrame) -> None:
    if df["position_id"].duplicated().any():
        duplicates = df.loc[df["position_id"].duplicated(), "position_id"].tolist()
        raise ValueError(f"Duplicate position_id values detected: {duplicates}")

    invalid_sides = sorted(set(df.loc[~df["side"].isin(ALLOWED_SIDES), "side"]) - {""})
    if invalid_sides:
        raise ValueError(f"Invalid side values detected: {invalid_sides}")

    invalid_statuses = sorted(set(df.loc[~df["status"].isin(ALLOWED_STATUSES), "status"]) - {""})
    if invalid_statuses:
        raise ValueError(f"Invalid status values detected: {invalid_statuses}")

    invalid_exit_flags = sorted(set(df.loc[~df["exit_flag"].isin(ALLOWED_EXIT_FLAGS), "exit_flag"]) - {""})
    if invalid_exit_flags:
        raise ValueError(f"Invalid exit_flag values detected: {invalid_exit_flags}")


def safe_float(value: object) -> float:
    if pd.isna(value):
        return 0.0
    return float(value)


def calculate_sector_exposure(open_df: pd.DataFrame, total_market_value: float) -> List[dict]:
    if open_df.empty:
        return []

    working_df = open_df.copy()
    working_df["sector"] = working_df["sector"].fillna("").astype(str).str.strip()
    working_df["sector"] = working_df["sector"].replace("", "Unclassified")
    working_df["market_value"] = pd.to_numeric(working_df["market_value"], errors="coerce").fillna(0.0)

    sector_df = (
        working_df.groupby("sector", dropna=False)["market_value"]
        .sum()
        .reset_index()
        .sort_values(by="market_value", ascending=False)
        .reset_index(drop=True)
    )

    exposures: List[dict] = []
    for _, row in sector_df.iterrows():
        sector_value = float(row["market_value"])
        sector_pct = 0.0 if total_market_value == 0 else (sector_value / total_market_value) * 100.0
        exposures.append(
            {
                "sector": str(row["sector"]),
                "value": sector_value,
                "pct": sector_pct,
            }
        )

    return exposures


def build_portfolio_equity_row(state_df: pd.DataFrame, generated_at: str, run_id: str) -> dict:
    open_df = state_df[state_df["status"] == "open"].copy()
    closed_df = state_df[state_df["status"] == "closed"].copy()

    total_positions = len(state_df)
    open_positions = len(open_df)
    closed_positions = len(closed_df)

    long_open_positions = int(((open_df["side"] == "long") & (open_df["status"] == "open")).sum())
    short_open_positions = int(((open_df["side"] == "short") & (open_df["status"] == "open")).sum())

    total_market_value = safe_float(open_df["market_value"].sum()) if not open_df.empty else 0.0
    total_capital_allocated_open = safe_float(open_df["capital_allocated"].sum()) if not open_df.empty else 0.0
    total_unrealised_pnl_abs = safe_float(open_df["pnl_abs"].sum()) if not open_df.empty else 0.0

    average_open_pnl_pct = (
        float(open_df["pnl_pct"].dropna().mean())
        if not open_df.empty and not open_df["pnl_pct"].dropna().empty
        else 0.0
    )

    largest_open_position_value = (
        float(open_df["market_value"].fillna(0.0).max()) if not open_df.empty else 0.0
    )
    smallest_open_position_value = (
        float(open_df["market_value"].fillna(0.0).min()) if not open_df.empty else 0.0
    )

    if not open_df.empty and not open_df["pnl_abs"].dropna().empty:
        winner_idx = open_df["pnl_abs"].fillna(float("-inf")).idxmax()
        loser_idx = open_df["pnl_abs"].fillna(float("inf")).idxmin()

        largest_open_winner_ticker = str(open_df.loc[winner_idx, "ticker"])
        largest_open_winner_pnl_abs = safe_float(open_df.loc[winner_idx, "pnl_abs"])

        largest_open_loser_ticker = str(open_df.loc[loser_idx, "ticker"])
        largest_open_loser_pnl_abs = safe_float(open_df.loc[loser_idx, "pnl_abs"])
    else:
        largest_open_winner_ticker = ""
        largest_open_winner_pnl_abs = 0.0
        largest_open_loser_ticker = ""
        largest_open_loser_pnl_abs = 0.0

    positions_flagged_review = int((open_df["exit_flag"] == "review").sum()) if not open_df.empty else 0
    positions_flagged_exit_required = int((open_df["exit_flag"] == "exit_required").sum()) if not open_df.empty else 0

    gross_long_exposure = safe_float(open_df.loc[open_df["side"] == "long", "market_value"].sum()) if not open_df.empty else 0.0
    gross_short_exposure = safe_float(open_df.loc[open_df["side"] == "short", "market_value"].sum()) if not open_df.empty else 0.0
    gross_exposure = gross_long_exposure + gross_short_exposure
    net_exposure = gross_long_exposure - gross_short_exposure

    cash_estimate = 0.0
    equity_estimate = total_market_value + cash_estimate

    portfolio_return_pct_on_open_capital = (
        0.0 if total_capital_allocated_open == 0 else (total_unrealised_pnl_abs / total_capital_allocated_open) * 100.0
    )

    open_high_water_capital = total_capital_allocated_open + max(total_unrealised_pnl_abs, 0.0)
    drawdown_pct_from_high_capital = (
        0.0
        if open_high_water_capital == 0
        else ((open_high_water_capital - equity_estimate) / open_high_water_capital) * 100.0
    )

    sector_exposures = calculate_sector_exposure(open_df, total_market_value)

    def top_sector_value(index: int, key: str, default: object) -> object:
        if index < len(sector_exposures):
            return sector_exposures[index][key]
        return default

    return {
        "generated_at": generated_at,
        "run_id": run_id,
        "total_positions": total_positions,
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "long_open_positions": long_open_positions,
        "short_open_positions": short_open_positions,
        "total_market_value": total_market_value,
        "total_capital_allocated_open": total_capital_allocated_open,
        "total_unrealised_pnl_abs": total_unrealised_pnl_abs,
        "average_open_pnl_pct": average_open_pnl_pct,
        "largest_open_position_value": largest_open_position_value,
        "smallest_open_position_value": smallest_open_position_value,
        "largest_open_winner_ticker": largest_open_winner_ticker,
        "largest_open_winner_pnl_abs": largest_open_winner_pnl_abs,
        "largest_open_loser_ticker": largest_open_loser_ticker,
        "largest_open_loser_pnl_abs": largest_open_loser_pnl_abs,
        "positions_flagged_review": positions_flagged_review,
        "positions_flagged_exit_required": positions_flagged_exit_required,
        "gross_long_exposure": gross_long_exposure,
        "gross_short_exposure": gross_short_exposure,
        "gross_exposure": gross_exposure,
        "net_exposure": net_exposure,
        "cash_estimate": cash_estimate,
        "equity_estimate": equity_estimate,
        "portfolio_return_pct_on_open_capital": portfolio_return_pct_on_open_capital,
        "drawdown_pct_from_high_capital": drawdown_pct_from_high_capital,
        "sector_exposure_top_1_name": top_sector_value(0, "sector", ""),
        "sector_exposure_top_1_value": top_sector_value(0, "value", 0.0),
        "sector_exposure_top_1_pct": top_sector_value(0, "pct", 0.0),
        "sector_exposure_top_2_name": top_sector_value(1, "sector", ""),
        "sector_exposure_top_2_value": top_sector_value(1, "value", 0.0),
        "sector_exposure_top_2_pct": top_sector_value(1, "pct", 0.0),
        "sector_exposure_top_3_name": top_sector_value(2, "sector", ""),
        "sector_exposure_top_3_value": top_sector_value(2, "value", 0.0),
        "sector_exposure_top_3_pct": top_sector_value(2, "pct", 0.0),
    }


def run_portfolio_equity_agent() -> None:
    run_id = generate_run_id()
    generated_at = utc_now_iso()

    state_df = load_portfolio_state()
    equity_row = build_portfolio_equity_row(state_df, generated_at, run_id)

    portfolio_equity_df = pd.DataFrame([equity_row], columns=PORTFOLIO_EQUITY_COLUMNS)
    atomic_write_csv(portfolio_equity_df, PORTFOLIO_EQUITY_FILE)

    print("Portfolio Equity Agent finished.")
    print(f"Saved portfolio equity to: {PORTFOLIO_EQUITY_FILE}")
    print()
    print("Run summary:")
    print(f"Run ID: {run_id}")
    print(f"Total positions: {equity_row['total_positions']}")
    print(f"Open positions: {equity_row['open_positions']}")
    print(f"Closed positions: {equity_row['closed_positions']}")
    print(f"Total market value: {equity_row['total_market_value']:.2f}")
    print(f"Total unrealised PnL: {equity_row['total_unrealised_pnl_abs']:.2f}")
    print(f"Gross exposure: {equity_row['gross_exposure']:.2f}")
    print(f"Net exposure: {equity_row['net_exposure']:.2f}")
    print(f"Equity estimate: {equity_row['equity_estimate']:.2f}")


if __name__ == "__main__":
    run_portfolio_equity_agent()