from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"

MANUAL_FILLS_FILE = DATA_DIR / "manual_fills.csv"
PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"
PROCESSED_FILLS_FILE = DATA_DIR / "processed_fills.csv"


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

FILL_COLUMNS = [
    "fill_id",
    "ticker",
    "side",
    "quantity",
    "price",
    "filled_at",
]

PROCESSED_FILLS_COLUMNS = [
    "fill_id",
    "ticker",
    "side",
    "quantity",
    "price",
    "filled_at",
    "processed_at",
    "run_id",
]

ALLOWED_FILL_SIDES = {"buy", "sell"}
ALLOWED_POSITION_SIDES = {"long", "short"}
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


def ensure_state_file_exists() -> None:
    if not PORTFOLIO_STATE_FILE.exists():
        empty_df = pd.DataFrame(columns=STATE_COLUMNS)
        atomic_write_csv(empty_df, PORTFOLIO_STATE_FILE)


def ensure_processed_fills_file_exists() -> None:
    if not PROCESSED_FILLS_FILE.exists():
        empty_df = pd.DataFrame(columns=PROCESSED_FILLS_COLUMNS)
        atomic_write_csv(empty_df, PROCESSED_FILLS_FILE)


def load_portfolio_state() -> pd.DataFrame:
    ensure_state_file_exists()
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


def load_processed_fills() -> pd.DataFrame:
    ensure_processed_fills_file_exists()
    df = pd.read_csv(PROCESSED_FILLS_FILE)

    for column in PROCESSED_FILLS_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[PROCESSED_FILLS_COLUMNS].copy()

    for column in ["fill_id", "ticker", "side", "filled_at", "processed_at", "run_id"]:
        df[column] = df[column].fillna("").astype(str).str.strip()

    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()

    for column in ["quantity", "price"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    return df


def load_manual_fills() -> pd.DataFrame:
    if not MANUAL_FILLS_FILE.exists():
        raise FileNotFoundError(f"Missing file: {MANUAL_FILLS_FILE}")

    df = pd.read_csv(MANUAL_FILLS_FILE)

    missing = [col for col in FILL_COLUMNS if col not in df.columns]
    if missing:
        raise ValueError(f"manual_fills.csv is missing required columns: {missing}")

    df = df[FILL_COLUMNS].copy()

    for column in ["fill_id", "ticker", "side", "filled_at"]:
        df[column] = df[column].fillna("").astype(str).str.strip()

    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()

    for column in ["quantity", "price"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    validate_manual_fills(df)
    return df


def validate_portfolio_state(df: pd.DataFrame) -> None:
    if df["position_id"].duplicated().any():
        duplicates = df.loc[df["position_id"].duplicated(), "position_id"].tolist()
        raise ValueError(f"Duplicate position_id values detected: {duplicates}")

    invalid_sides = sorted(set(df.loc[~df["side"].isin(ALLOWED_POSITION_SIDES), "side"]) - {""})
    if invalid_sides:
        raise ValueError(f"Invalid position side values detected: {invalid_sides}")

    invalid_statuses = sorted(set(df.loc[~df["status"].isin(ALLOWED_STATUSES), "status"]) - {""})
    if invalid_statuses:
        raise ValueError(f"Invalid status values detected: {invalid_statuses}")

    invalid_exit_flags = sorted(set(df.loc[~df["exit_flag"].isin(ALLOWED_EXIT_FLAGS), "exit_flag"]) - {""})
    if invalid_exit_flags:
        raise ValueError(f"Invalid exit_flag values detected: {invalid_exit_flags}")


def validate_manual_fills(df: pd.DataFrame) -> None:
    if df.empty:
        return

    if df["fill_id"].eq("").any():
        bad_rows = df[df["fill_id"].eq("")]
        raise ValueError(f"Blank fill_id detected in rows: {bad_rows.index.tolist()}")

    if df["fill_id"].duplicated().any():
        duplicates = df.loc[df["fill_id"].duplicated(), "fill_id"].tolist()
        raise ValueError(f"Duplicate fill_id values in manual_fills.csv: {duplicates}")

    invalid_sides = sorted(set(df.loc[~df["side"].isin(ALLOWED_FILL_SIDES), "side"]) - {""})
    if invalid_sides:
        raise ValueError(f"Invalid fill side values detected: {invalid_sides}")

    if df["ticker"].eq("").any():
        bad_rows = df[df["ticker"].eq("")]
        raise ValueError(f"Blank ticker detected in rows: {bad_rows.index.tolist()}")

    if df["quantity"].isna().any():
        bad_rows = df[df["quantity"].isna()]
        raise ValueError(f"Invalid quantity detected in rows: {bad_rows.index.tolist()}")

    if (df["quantity"] <= 0).any():
        bad_rows = df[df["quantity"] <= 0]
        raise ValueError(f"Non-positive quantity detected in rows: {bad_rows.index.tolist()}")

    if df["price"].isna().any():
        bad_rows = df[df["price"].isna()]
        raise ValueError(f"Invalid price detected in rows: {bad_rows.index.tolist()}")

    if (df["price"] <= 0).any():
        bad_rows = df[df["price"] <= 0]
        raise ValueError(f"Non-positive price detected in rows: {bad_rows.index.tolist()}")


def next_position_id(state_df: pd.DataFrame) -> str:
    if state_df.empty:
        return "POS001"

    existing = (
        state_df["position_id"]
        .fillna("")
        .astype(str)
        .str.extract(r"POS(\d+)", expand=False)
        .dropna()
    )

    if existing.empty:
        return "POS001"

    max_id = existing.astype(int).max()
    return f"POS{max_id + 1:03d}"


def find_open_position(state_df: pd.DataFrame, ticker: str, side: str = "long") -> Optional[int]:
    matches = state_df[
        (state_df["ticker"] == ticker) &
        (state_df["side"] == side) &
        (state_df["status"] == "open")
    ]

    if matches.empty:
        return None

    if len(matches) > 1:
        raise ValueError(f"Multiple open positions found for ticker {ticker}. State integrity violated.")

    return matches.index[0]


def create_new_long_position(
    state_df: pd.DataFrame,
    ticker: str,
    quantity: float,
    price: float,
    filled_at: str,
    run_id: str,
) -> pd.DataFrame:
    position_id = next_position_id(state_df)
    now = utc_now_iso()

    new_row = {
        "position_id": position_id,
        "ticker": ticker,
        "side": "long",
        "status": "open",
        "quantity": quantity,
        "entry_price": price,
        "entry_date": filled_at,
        "capital_allocated": quantity * price,
        "stop_loss": pd.NA,
        "take_profit": pd.NA,
        "regime_at_entry": "",
        "sector": "",
        "signal_score": pd.NA,
        "highest_price_since_entry": price,
        "lowest_price_since_entry": price,
        "current_price": price,
        "market_value": quantity * price,
        "pnl_abs": 0.0,
        "pnl_pct": 0.0,
        "exit_flag": "none",
        "exit_reason": "",
        "last_updated": now,
        "run_id": run_id,
    }

    return pd.concat([state_df, pd.DataFrame([new_row], columns=STATE_COLUMNS)], ignore_index=True)


def apply_buy_fill(
    state_df: pd.DataFrame,
    fill: pd.Series,
    run_id: str,
) -> pd.DataFrame:
    ticker = fill["ticker"]
    quantity = float(fill["quantity"])
    price = float(fill["price"])
    filled_at = fill["filled_at"]

    existing_idx = find_open_position(state_df, ticker=ticker, side="long")

    if existing_idx is None:
        return create_new_long_position(
            state_df=state_df,
            ticker=ticker,
            quantity=quantity,
            price=price,
            filled_at=filled_at,
            run_id=run_id,
        )

    row = state_df.loc[existing_idx].copy()

    old_quantity = float(row["quantity"])
    old_entry_price = float(row["entry_price"])
    new_quantity = old_quantity + quantity
    new_entry_price = ((old_quantity * old_entry_price) + (quantity * price)) / new_quantity

    row["quantity"] = new_quantity
    row["entry_price"] = new_entry_price
    row["capital_allocated"] = new_quantity * new_entry_price
    row["current_price"] = price
    row["market_value"] = new_quantity * price
    row["pnl_abs"] = (price - new_entry_price) * new_quantity
    row["pnl_pct"] = 0.0 if new_quantity * new_entry_price == 0 else (row["pnl_abs"] / (new_quantity * new_entry_price)) * 100.0
    row["highest_price_since_entry"] = price if pd.isna(row["highest_price_since_entry"]) else max(float(row["highest_price_since_entry"]), price)
    row["lowest_price_since_entry"] = price if pd.isna(row["lowest_price_since_entry"]) else min(float(row["lowest_price_since_entry"]), price)
    row["exit_flag"] = "none"
    row["exit_reason"] = ""
    row["last_updated"] = utc_now_iso()
    row["run_id"] = run_id

    state_df.loc[existing_idx] = row
    return state_df


def apply_sell_fill(
    state_df: pd.DataFrame,
    fill: pd.Series,
    run_id: str,
) -> pd.DataFrame:
    ticker = fill["ticker"]
    sell_quantity = float(fill["quantity"])
    price = float(fill["price"])

    existing_idx = find_open_position(state_df, ticker=ticker, side="long")

    if existing_idx is None:
        raise ValueError(f"Cannot process sell fill for {ticker}: no open long position exists.")

    row = state_df.loc[existing_idx].copy()
    old_quantity = float(row["quantity"])

    if sell_quantity > old_quantity:
        raise ValueError(
            f"Cannot process sell fill for {ticker}: sell quantity {sell_quantity} exceeds open quantity {old_quantity}."
        )

    remaining_quantity = old_quantity - sell_quantity

    if remaining_quantity == 0:
        row["quantity"] = 0.0
        row["status"] = "closed"
        row["current_price"] = price
        row["market_value"] = 0.0
        row["pnl_abs"] = (price - float(row["entry_price"])) * old_quantity
        capital_allocated = float(row["capital_allocated"]) if pd.notna(row["capital_allocated"]) else float(row["entry_price"]) * old_quantity
        row["pnl_pct"] = 0.0 if capital_allocated == 0 else (row["pnl_abs"] / capital_allocated) * 100.0
        row["exit_flag"] = "none"
        row["exit_reason"] = ""
        row["last_updated"] = utc_now_iso()
        row["run_id"] = run_id
        state_df.loc[existing_idx] = row
        return state_df

    entry_price = float(row["entry_price"])
    row["quantity"] = remaining_quantity
    row["capital_allocated"] = remaining_quantity * entry_price
    row["current_price"] = price
    row["market_value"] = remaining_quantity * price
    row["pnl_abs"] = (price - entry_price) * remaining_quantity
    row["pnl_pct"] = 0.0 if row["capital_allocated"] == 0 else (row["pnl_abs"] / row["capital_allocated"]) * 100.0
    row["exit_flag"] = "none"
    row["exit_reason"] = ""
    row["last_updated"] = utc_now_iso()
    row["run_id"] = run_id

    state_df.loc[existing_idx] = row
    return state_df


def process_fill(
    state_df: pd.DataFrame,
    fill: pd.Series,
    run_id: str,
) -> pd.DataFrame:
    if fill["side"] == "buy":
        return apply_buy_fill(state_df, fill, run_id)

    if fill["side"] == "sell":
        return apply_sell_fill(state_df, fill, run_id)

    raise ValueError(f"Unsupported fill side: {fill['side']}")


def build_processed_fill_row(fill: pd.Series, run_id: str) -> dict:
    return {
        "fill_id": fill["fill_id"],
        "ticker": fill["ticker"],
        "side": fill["side"],
        "quantity": float(fill["quantity"]),
        "price": float(fill["price"]),
        "filled_at": fill["filled_at"],
        "processed_at": utc_now_iso(),
        "run_id": run_id,
    }


def run_fill_agent() -> None:
    run_id = generate_run_id()

    state_df = load_portfolio_state()
    processed_df = load_processed_fills()
    manual_fills_df = load_manual_fills()

    already_processed = set(processed_df["fill_id"].dropna().astype(str).tolist())
    pending_fills_df = manual_fills_df[~manual_fills_df["fill_id"].isin(already_processed)].copy()

    if pending_fills_df.empty:
        print("Fill Agent finished.")
        print("No new fills to process.")
        print(f"Run ID: {run_id}")
        return

    processed_now: List[dict] = []

    for _, fill in pending_fills_df.iterrows():
        print(f"Processing fill {fill['fill_id']} for {fill['ticker']}")
        state_df = process_fill(state_df, fill, run_id)
        processed_now.append(build_processed_fill_row(fill, run_id))

    state_df = state_df[STATE_COLUMNS].copy()
    validate_portfolio_state(state_df)

    processed_append_df = pd.DataFrame(processed_now, columns=PROCESSED_FILLS_COLUMNS)
    processed_df = pd.concat([processed_df, processed_append_df], ignore_index=True)
    processed_df = processed_df[PROCESSED_FILLS_COLUMNS].copy()

    atomic_write_csv(state_df, PORTFOLIO_STATE_FILE)
    atomic_write_csv(processed_df, PROCESSED_FILLS_FILE)

    open_positions = int((state_df["status"] == "open").sum())
    closed_positions = int((state_df["status"] == "closed").sum())

    print("Fill Agent finished.")
    print(f"Saved updated portfolio state to: {PORTFOLIO_STATE_FILE}")
    print(f"Saved processed fills to: {PROCESSED_FILLS_FILE}")
    print()
    print("Run summary:")
    print(f"Run ID: {run_id}")
    print(f"New fills processed: {len(processed_now)}")
    print(f"Open positions: {open_positions}")
    print(f"Closed positions: {closed_positions}")


if __name__ == "__main__":
    run_fill_agent()