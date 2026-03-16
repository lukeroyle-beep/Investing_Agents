from __future__ import annotations

from datetime import datetime, timezone
import pandas as pd

from shared.id_utils import generate_fill_id, assert_valid_fill_id
from shared.paths import DATA_DIR


TRADE_FILLS_PATH = DATA_DIR / "trade_fills.csv"
PROCESSED_FILLS_PATH = DATA_DIR / "processed_fills.csv"

REQUIRED_COLUMNS = [
    "fill_id",
    "ticker",
    "side",
    "quantity",
    "fill_price",
    "filled_at",
]


def prompt_non_empty(prompt_text: str) -> str:
    while True:
        value = input(prompt_text).strip()
        if value:
            return value
        print("Value is required.")


def prompt_side() -> str:
    while True:
        value = input("Side (buy/sell): ").strip().lower()
        if value in {"buy", "sell"}:
            return value
        print("Side must be 'buy' or 'sell'.")


def prompt_positive_float(prompt_text: str) -> float:
    while True:
        raw = input(prompt_text).strip()
        try:
            value = float(raw)
            if value <= 0:
                raise ValueError
            return value
        except ValueError:
            print("Enter a number greater than 0.")


def prompt_timestamp() -> str:
    raw = input("Filled at UTC (press Enter for now): ").strip()
    if not raw:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        print("Invalid timestamp. Use ISO 8601 format, for example 2026-03-16T22:30:00Z")
        return prompt_timestamp()


def load_or_create_trade_fills() -> pd.DataFrame:
    if TRADE_FILLS_PATH.exists():
        df = pd.read_csv(TRADE_FILLS_PATH)

        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = pd.NA

        return df[REQUIRED_COLUMNS].copy()

    return pd.DataFrame(columns=REQUIRED_COLUMNS)


def load_processed_fill_ids() -> set[str]:
    if not PROCESSED_FILLS_PATH.exists():
        return set()

    df = pd.read_csv(PROCESSED_FILLS_PATH)
    if "fill_id" not in df.columns:
        return set()

    return set(df["fill_id"].dropna().astype(str).str.strip())


def generate_unique_fill_id(existing_fill_ids: set[str], processed_fill_ids: set[str]) -> str:
    for _ in range(10):
        fill_id = generate_fill_id(source="MANUAL")
        assert_valid_fill_id(fill_id)

        if fill_id not in existing_fill_ids and fill_id not in processed_fill_ids:
            return fill_id

    raise RuntimeError("Failed to generate a unique fill_id after 10 attempts.")


def main() -> None:
    print("\n=== Manual Fill Entry ===\n")

    ticker = prompt_non_empty("Ticker: ").upper()
    side = prompt_side()
    quantity = prompt_positive_float("Quantity: ")
    fill_price = prompt_positive_float("Fill price: ")
    filled_at = prompt_timestamp()

    df = load_or_create_trade_fills()
    existing_fill_ids = set(df["fill_id"].dropna().astype(str).str.strip())
    processed_fill_ids = load_processed_fill_ids()

    fill_id = generate_unique_fill_id(existing_fill_ids, processed_fill_ids)

    new_row = {
        "fill_id": fill_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "fill_price": fill_price,
        "filled_at": filled_at,
    }

    new_df = pd.DataFrame([new_row], columns=REQUIRED_COLUMNS)
    df = pd.concat([df, new_df], ignore_index=True)
    df.to_csv(TRADE_FILLS_PATH, index=False)

    print("\nFill recorded successfully.")
    print(f"fill_id: {fill_id}")
    print(f"ticker: {ticker}")
    print(f"side: {side}")
    print(f"quantity: {quantity}")
    print(f"fill_price: {fill_price}")
    print(f"filled_at: {filled_at}")
    print(f"\nSaved to: {TRADE_FILLS_PATH}")


if __name__ == "__main__":
    main()