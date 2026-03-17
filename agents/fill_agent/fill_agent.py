from __future__ import annotations

from datetime import datetime, UTC

import pandas as pd

from shared.id_utils import validate_fill_id
from shared.io_utils import read_csv, write_csv, write_csv_with_run_id
from shared.paths import (
    PORTFOLIO_STATE_PATH,
    PROCESSED_FILLS_PATH,
    TRADE_FILLS_PATH,
)
from shared.run_context import get_or_create_run_id
from shared.schemas import validate_portfolio_state, validate_processed_fills


TRADE_FILLS_FILE = TRADE_FILLS_PATH
PROCESSED_FILLS_FILE = PROCESSED_FILLS_PATH
PORTFOLIO_STATE_FILE = PORTFOLIO_STATE_PATH


REQUIRED_FILL_COLUMNS = [
    "fill_id",
    "ticker",
    "side",
    "quantity",
    "fill_price",
    "filled_at",
]

ALLOWED_FILL_SIDES = {"buy", "sell"}

PROCESSED_FILLS_REQUIRED_COLUMNS = [
    "fill_id",
    "processed_at",
    "run_id",
]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def validate_trade_fills_input(trade_fills_df: pd.DataFrame) -> pd.DataFrame:
    missing_columns = [col for col in REQUIRED_FILL_COLUMNS if col not in trade_fills_df.columns]

    if missing_columns:
        raise ValueError(
            f"trade_fills.csv is missing required columns: {missing_columns}"
        )

    df = trade_fills_df.copy()

    for col in REQUIRED_FILL_COLUMNS:
        df[col] = df[col].astype(str).str.strip()

    if df.empty:
        return df

    if (df["fill_id"] == "").any():
        rows = df.index[df["fill_id"] == ""].tolist()
        raise ValueError(f"Blank fill_id found in rows: {rows}")

    invalid_ids = df.loc[~df["fill_id"].apply(validate_fill_id), "fill_id"].tolist()
    if invalid_ids:
        raise ValueError(f"Invalid fill_id values: {invalid_ids}")

    duplicates = df["fill_id"][df["fill_id"].duplicated()].unique().tolist()
    if duplicates:
        raise ValueError(f"Duplicate fill_id values found: {duplicates}")

    if (df["ticker"] == "").any():
        rows = df.index[df["ticker"] == ""].tolist()
        raise ValueError(f"Blank ticker values found in rows: {rows}")

    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()

    invalid_sides = df.loc[~df["side"].isin(ALLOWED_FILL_SIDES), "side"].unique().tolist()
    if invalid_sides:
        raise ValueError(
            f"Invalid side values: {invalid_sides}. Allowed values are buy/sell."
        )

    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    if df["quantity"].isna().any():
        rows = df.index[df["quantity"].isna()].tolist()
        raise ValueError(f"Non-numeric quantity values found in rows: {rows}")

    if (df["quantity"] <= 0).any():
        rows = df.index[df["quantity"] <= 0].tolist()
        raise ValueError(f"Quantity must be greater than zero. Bad rows: {rows}")

    df["fill_price"] = pd.to_numeric(df["fill_price"], errors="coerce")
    if df["fill_price"].isna().any():
        rows = df.index[df["fill_price"].isna()].tolist()
        raise ValueError(f"Non-numeric fill_price values found in rows: {rows}")

    if (df["fill_price"] <= 0).any():
        rows = df.index[df["fill_price"] <= 0].tolist()
        raise ValueError(f"fill_price must be greater than zero. Bad rows: {rows}")

    if (df["filled_at"] == "").any():
        rows = df.index[df["filled_at"] == ""].tolist()
        raise ValueError(f"Blank filled_at values found in rows: {rows}")

    return df


def load_processed_fills_df() -> pd.DataFrame:
    df = read_csv(PROCESSED_FILLS_FILE)

    if df.empty:
        return pd.DataFrame(columns=PROCESSED_FILLS_REQUIRED_COLUMNS)

    return validate_processed_fills(df)


def load_processed_fills() -> set[str]:
    processed_df = load_processed_fills_df()

    if processed_df.empty:
        return set()

    return set(processed_df["fill_id"].tolist())


def append_processed_fills(new_ids: list[str], run_id: str) -> None:
    if not new_ids:
        return

    existing_df = load_processed_fills_df()

    processed_at = utc_now_iso()
    new_df = pd.DataFrame(
        {
            "fill_id": new_ids,
            "processed_at": processed_at,
            "run_id": run_id,
        }
    )
    new_df = validate_processed_fills(new_df)

    combined_df = pd.concat([existing_df, new_df], ignore_index=True)
    combined_df = validate_processed_fills(combined_df)
    combined_df = combined_df[combined_df["fill_id"] != ""].copy()
    combined_df = combined_df.drop_duplicates(subset=["fill_id"], keep="first")

    write_csv(combined_df, PROCESSED_FILLS_FILE)


def run_fill_agent() -> None:
    run_id = get_or_create_run_id()
    print(f"Run ID: {run_id}")

    fills = read_csv(TRADE_FILLS_FILE)
    fills = validate_trade_fills_input(fills)

    if fills.empty:
        print("No fills to process.")
        return

    processed_ids = load_processed_fills()
    new_fills = fills[~fills["fill_id"].isin(processed_ids)].copy()

    if new_fills.empty:
        print("No new fills detected.")
        return

    portfolio_state = read_csv(PORTFOLIO_STATE_FILE)
    portfolio_state = validate_portfolio_state(portfolio_state)

    processed_now: list[str] = []

    for _, fill in new_fills.iterrows():
        fill_id = fill["fill_id"]
        ticker = fill["ticker"]

        print(f"Processing fill {fill_id} for {ticker}")

        processed_now.append(fill_id)

    append_processed_fills(processed_now, run_id=run_id)

    portfolio_state = validate_portfolio_state(portfolio_state)

    write_csv_with_run_id(
        portfolio_state,
        PORTFOLIO_STATE_FILE,
        run_id=run_id,
    )

    print("Fill Agent finished.")
    print(f"Processed fills: {len(processed_now)}")


if __name__ == "__main__":
    run_fill_agent()