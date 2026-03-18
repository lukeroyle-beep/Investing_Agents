from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd
import yfinance as yf


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"
PORTFOLIO_MONITOR_FILE = DATA_DIR / "portfolio_monitor.csv"
POSITION_ALERTS_FILE = DATA_DIR / "position_alerts.csv"


CANONICAL_COLUMNS = [
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

COLUMN_ALIASES = {
    "average_entry_price": "entry_price",
    "unrealised_pnl_abs": "pnl_abs",
    "unrealised_pnl_pct": "pnl_pct",
    "last_updated_at": "last_updated",
}

ALLOWED_SIDES = {"long", "short"}
ALLOWED_STATUSES = {"open", "closed"}
ALLOWED_EXIT_FLAGS = {"none", "review", "exit_required"}

OPEN_PRICE_FIELDS = [
    "quantity",
    "entry_price",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "current_price",
    "market_value",
    "pnl_abs",
    "pnl_pct",
    "signal_score",
]

ALERT_COLUMNS = [
    "position_id",
    "ticker",
    "alert_type",
    "severity",
    "message",
    "current_price",
    "stop_loss",
    "take_profit",
    "status",
    "exit_flag",
    "generated_at",
    "run_id",
]


@dataclass
class PriceResult:
    ticker: str
    price: Optional[float]
    error: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_run_id() -> str:
    return datetime.now(timezone.utc).strftime("RUN_%Y%m%dT%H%M%SZ")


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(temp_path, index=False)
    temp_path.replace(path)


def resolve_aliases(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    for old_name, new_name in COLUMN_ALIASES.items():
        if old_name in df.columns and new_name not in df.columns:
            rename_map[old_name] = new_name
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


def ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:
    for column in CANONICAL_COLUMNS:
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
    return df[CANONICAL_COLUMNS].copy()


def normalise_strings(df: pd.DataFrame) -> pd.DataFrame:
    for column in ["position_id", "ticker", "side", "status", "regime_at_entry", "sector", "exit_flag", "exit_reason", "run_id"]:
        df[column] = df[column].fillna("").astype(str).str.strip()

    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()
    df["status"] = df["status"].str.lower()
    df["exit_flag"] = df["exit_flag"].str.lower()

    return df


def coerce_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
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
    return df


def validate_portfolio_state(df: pd.DataFrame) -> None:
    if df["position_id"].eq("").any():
        bad_rows = df[df["position_id"].eq("")]
        raise ValueError(f"Blank position_id detected in rows: {bad_rows.index.tolist()}")

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


def load_portfolio_state() -> pd.DataFrame:
    if not PORTFOLIO_STATE_FILE.exists():
        raise FileNotFoundError(f"Missing file: {PORTFOLIO_STATE_FILE}")

    df = pd.read_csv(PORTFOLIO_STATE_FILE)
    df = resolve_aliases(df)
    df = ensure_required_columns(df)
    df = normalise_strings(df)
    df = coerce_numeric_columns(df)
    validate_portfolio_state(df)
    return df


def fetch_latest_price(ticker: str) -> PriceResult:
    try:
        history = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False)
        if history.empty or "Close" not in history.columns:
            return PriceResult(ticker=ticker, price=None, error="No price history returned")
        latest_close = history["Close"].dropna()
        if latest_close.empty:
            return PriceResult(ticker=ticker, price=None, error="No close price available")
        price = float(latest_close.iloc[-1])
        if math.isnan(price):
            return PriceResult(ticker=ticker, price=None, error="Latest price is NaN")
        return PriceResult(ticker=ticker, price=price)
    except Exception as exc:
        return PriceResult(ticker=ticker, price=None, error=str(exc))


def calculate_market_value(side: str, quantity: float, current_price: float) -> float:
    return quantity * current_price


def calculate_pnl_abs(side: str, quantity: float, entry_price: float, current_price: float) -> float:
    if side == "long":
        return (current_price - entry_price) * quantity
    if side == "short":
        return (entry_price - current_price) * quantity
    raise ValueError(f"Unsupported side: {side}")


def calculate_pnl_pct(capital_allocated: float, pnl_abs: float) -> Optional[float]:
    if capital_allocated in (None, 0) or pd.isna(capital_allocated):
        return None
    return (pnl_abs / capital_allocated) * 100.0


def determine_exit_signal(side: str, current_price: float, stop_loss: float, take_profit: float) -> tuple[str, str]:
    if pd.isna(current_price):
        return "review", "missing_price"

    if side == "long":
        if pd.notna(stop_loss) and current_price <= stop_loss:
            return "exit_required", "stop_loss_triggered"
        if pd.notna(take_profit) and current_price >= take_profit:
            return "exit_required", "take_profit_triggered"

    elif side == "short":
        if pd.notna(stop_loss) and current_price >= stop_loss:
            return "exit_required", "stop_loss_triggered"
        if pd.notna(take_profit) and current_price <= take_profit:
            return "exit_required", "take_profit_triggered"

    return "none", ""


def update_open_position(row: pd.Series, price_map: dict[str, PriceResult], generated_at: str, run_id: str) -> pd.Series:
    ticker = row["ticker"]
    price_result = price_map.get(ticker)

    if price_result is None or price_result.price is None:
        row["exit_flag"] = "review"
        row["exit_reason"] = "missing_price"
        row["last_updated"] = generated_at
        row["run_id"] = run_id
        return row

    current_price = float(price_result.price)
    quantity = float(row["quantity"])
    entry_price = float(row["entry_price"])

    capital_allocated = row["capital_allocated"]
    if pd.isna(capital_allocated) or capital_allocated == 0:
        capital_allocated = quantity * entry_price
        row["capital_allocated"] = capital_allocated

    row["current_price"] = current_price
    row["market_value"] = calculate_market_value(row["side"], quantity, current_price)

    pnl_abs = calculate_pnl_abs(row["side"], quantity, entry_price, current_price)
    row["pnl_abs"] = pnl_abs
    row["pnl_pct"] = calculate_pnl_pct(float(capital_allocated), pnl_abs)

    existing_high = row["highest_price_since_entry"]
    existing_low = row["lowest_price_since_entry"]

    if pd.isna(existing_high):
        row["highest_price_since_entry"] = current_price
    else:
        row["highest_price_since_entry"] = max(float(existing_high), current_price)

    if pd.isna(existing_low):
        row["lowest_price_since_entry"] = current_price
    else:
        row["lowest_price_since_entry"] = min(float(existing_low), current_price)

    exit_flag, exit_reason = determine_exit_signal(
        side=row["side"],
        current_price=current_price,
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
    )
    row["exit_flag"] = exit_flag
    row["exit_reason"] = exit_reason
    row["last_updated"] = generated_at
    row["run_id"] = run_id

    return row


def build_alerts(df: pd.DataFrame, generated_at: str, run_id: str) -> pd.DataFrame:
    alerts: List[dict] = []

    for _, row in df.iterrows():
        if row["status"] != "open":
            continue

        if row["exit_flag"] == "exit_required":
            alert_type = str(row["exit_reason"])
            if alert_type == "stop_loss_triggered":
                severity = "high"
                message = "Stop loss has been triggered."
            elif alert_type == "take_profit_triggered":
                severity = "medium"
                message = "Take profit has been triggered."
            else:
                severity = "medium"
                message = "Exit required."
            alerts.append(
                {
                    "position_id": row["position_id"],
                    "ticker": row["ticker"],
                    "alert_type": alert_type,
                    "severity": severity,
                    "message": message,
                    "current_price": row["current_price"],
                    "stop_loss": row["stop_loss"],
                    "take_profit": row["take_profit"],
                    "status": row["status"],
                    "exit_flag": row["exit_flag"],
                    "generated_at": generated_at,
                    "run_id": run_id,
                }
            )

        elif row["exit_flag"] == "review" and row["exit_reason"] == "missing_price":
            alerts.append(
                {
                    "position_id": row["position_id"],
                    "ticker": row["ticker"],
                    "alert_type": "missing_price",
                    "severity": "high",
                    "message": "Live price could not be refreshed.",
                    "current_price": row["current_price"],
                    "stop_loss": row["stop_loss"],
                    "take_profit": row["take_profit"],
                    "status": row["status"],
                    "exit_flag": row["exit_flag"],
                    "generated_at": generated_at,
                    "run_id": run_id,
                }
            )

    alerts_df = pd.DataFrame(alerts, columns=ALERT_COLUMNS)
    if alerts_df.empty:
        alerts_df = pd.DataFrame(columns=ALERT_COLUMNS)

    return alerts_df


def build_monitor_view(df: pd.DataFrame) -> pd.DataFrame:
    monitor_columns = [
        "position_id",
        "ticker",
        "side",
        "status",
        "quantity",
        "entry_price",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "stop_loss",
        "take_profit",
        "highest_price_since_entry",
        "lowest_price_since_entry",
        "exit_flag",
        "exit_reason",
        "last_updated",
        "run_id",
    ]
    return df[monitor_columns].copy()


def save_outputs(state_df: pd.DataFrame, monitor_df: pd.DataFrame, alerts_df: pd.DataFrame) -> None:
    state_df = state_df[CANONICAL_COLUMNS].copy()
    atomic_write_csv(state_df, PORTFOLIO_STATE_FILE)
    atomic_write_csv(monitor_df, PORTFOLIO_MONITOR_FILE)
    atomic_write_csv(alerts_df, POSITION_ALERTS_FILE)


def run_position_tracking_agent() -> None:
    run_id = generate_run_id()
    generated_at = utc_now_iso()

    state_df = load_portfolio_state()

    open_mask = state_df["status"] == "open"
    open_positions = state_df.loc[open_mask].copy()

    unique_tickers = sorted(open_positions["ticker"].dropna().unique().tolist())
    price_map = {ticker: fetch_latest_price(ticker) for ticker in unique_tickers}

    updated_rows = []
    for _, row in state_df.iterrows():
        if row["status"] == "open":
            updated_row = update_open_position(row.copy(), price_map, generated_at, run_id)
        else:
            row["last_updated"] = row["last_updated"] if str(row["last_updated"]).strip() else generated_at
            row["run_id"] = run_id
            updated_row = row.copy()
        updated_rows.append(updated_row)

    updated_state_df = pd.DataFrame(updated_rows, columns=CANONICAL_COLUMNS)
    updated_state_df = normalise_strings(updated_state_df)
    updated_state_df = coerce_numeric_columns(updated_state_df)
    validate_portfolio_state(updated_state_df)

    monitor_df = build_monitor_view(updated_state_df)
    alerts_df = build_alerts(updated_state_df, generated_at, run_id)

    save_outputs(updated_state_df, monitor_df, alerts_df)

    total_positions = len(updated_state_df)
    open_positions_count = int((updated_state_df["status"] == "open").sum())
    exit_required_count = int((updated_state_df["exit_flag"] == "exit_required").sum())
    review_count = int((updated_state_df["exit_flag"] == "review").sum())

    print("Position Tracking Agent finished.")
    print(f"Saved updated portfolio state to: {PORTFOLIO_STATE_FILE}")
    print(f"Saved portfolio monitor view to: {PORTFOLIO_MONITOR_FILE}")
    print(f"Saved position alerts to: {POSITION_ALERTS_FILE}")
    print()
    print("Run summary:")
    print(f"Run ID: {run_id}")
    print(f"Total positions: {total_positions}")
    print(f"Open positions: {open_positions_count}")
    print(f"Exit required: {exit_required_count}")
    print(f"Review flags: {review_count}")
    print(f"Updated at: {generated_at}")


if __name__ == "__main__":
    run_position_tracking_agent()