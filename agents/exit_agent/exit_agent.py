from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"
EXIT_ADVICE_FILE = DATA_DIR / "exit_advice.csv"


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

EXIT_ADVICE_COLUMNS = [
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
    "run_id",
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


def build_exit_row(row: pd.Series, generated_at: str, run_id: str) -> dict:
    exit_action = determine_exit_action(row)
    reason = determine_reason_text(row, exit_action)

    return {
        "position_id": row["position_id"],
        "ticker": row["ticker"],
        "exit_action": exit_action,
        "reason": reason,
        "status": row["status"],
        "exit_reason": row["exit_reason"],
        "current_price": row["current_price"],
        "stop_loss": row["stop_loss"],
        "take_profit": row["take_profit"],
        "pnl_abs": row["pnl_abs"],
        "pnl_pct": row["pnl_pct"],
        "generated_at": generated_at,
        "run_id": run_id,
    }


def determine_exit_action(row: pd.Series) -> str:
    if row["status"] != "open":
        return "hold"

    exit_flag = row["exit_flag"]
    exit_reason = row["exit_reason"]

    if exit_flag == "review":
        return "review_immediately"

    if exit_flag == "exit_required":
        if exit_reason == "stop_loss_triggered":
            return "close"
        if exit_reason == "take_profit_triggered":
            return "take_profit"
        return "close"

    return "hold"


def determine_reason_text(row: pd.Series, exit_action: str) -> str:
    exit_reason = row["exit_reason"]

    if exit_action == "review_immediately":
        if exit_reason == "missing_price":
            return "Live price missing. Review immediately."
        return "Position requires review."

    if exit_action == "close":
        if exit_reason == "stop_loss_triggered":
            return "Stop loss level has been reached."
        if exit_reason:
            return f"Exit required: {exit_reason}."
        return "Exit required."

    if exit_action == "take_profit":
        return "Take profit level has been reached."

    return "No exit action required."


def run_exit_agent() -> None:
    run_id = generate_run_id()
    generated_at = utc_now_iso()

    state_df = load_portfolio_state()

    open_state_df = state_df[state_df["status"] == "open"].copy()

    advice_rows: List[dict] = []
    for _, row in open_state_df.iterrows():
        advice_rows.append(build_exit_row(row, generated_at, run_id))

    exit_advice_df = pd.DataFrame(advice_rows, columns=EXIT_ADVICE_COLUMNS)

    if exit_advice_df.empty:
        exit_advice_df = pd.DataFrame(columns=EXIT_ADVICE_COLUMNS)

    atomic_write_csv(exit_advice_df, EXIT_ADVICE_FILE)

    total_rows = len(exit_advice_df)
    hold_count = int((exit_advice_df["exit_action"] == "hold").sum()) if not exit_advice_df.empty else 0
    take_profit_count = int((exit_advice_df["exit_action"] == "take_profit").sum()) if not exit_advice_df.empty else 0
    close_count = int((exit_advice_df["exit_action"] == "close").sum()) if not exit_advice_df.empty else 0
    review_count = int((exit_advice_df["exit_action"] == "review_immediately").sum()) if not exit_advice_df.empty else 0

    print("Exit Agent finished.")
    print(f"Saved exit advice to: {EXIT_ADVICE_FILE}")
    print()
    print("Run summary:")
    print(f"Run ID: {run_id}")
    print(f"Total exit advice rows: {total_rows}")
    print(f"Hold: {hold_count}")
    print(f"Take profit: {take_profit_count}")
    print(f"Close: {close_count}")
    print(f"Review immediately: {review_count}")


if __name__ == "__main__":
    run_exit_agent()