from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from shared.io_utils import read_csv, write_csv
from shared.paths import DATA_DIR
from shared.validation import validate_portfolio_state


TRADE_FILLS_FILE = DATA_DIR / "trade_fills.csv"
PROCESSED_FILLS_FILE = DATA_DIR / "processed_fills.csv"
PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_trade_fills() -> pd.DataFrame:
    if not TRADE_FILLS_FILE.exists():
        return pd.DataFrame()

    fills = read_csv(TRADE_FILLS_FILE)

    if fills.empty:
        return fills

    required = {"fill_id", "filled_at", "ticker", "side", "quantity", "fill_price"}
    missing = [col for col in required if col not in fills.columns]
    if missing:
        raise ValueError(f"trade_fills.csv is missing required columns: {missing}")

    fills = fills.copy()
    fills["fill_id"] = fills["fill_id"].astype(str).str.strip()
    fills["ticker"] = fills["ticker"].astype(str).str.upper().str.strip()
    fills["side"] = fills["side"].astype(str).str.lower().str.strip()
    fills["quantity"] = pd.to_numeric(fills["quantity"], errors="coerce")
    fills["fill_price"] = pd.to_numeric(fills["fill_price"], errors="coerce")

    if fills["fill_id"].eq("").any():
        raise ValueError("trade_fills.csv contains blank fill_id values")

    if fills["fill_id"].duplicated().any():
        duplicates = sorted(fills.loc[fills["fill_id"].duplicated(), "fill_id"].unique())
        raise ValueError(f"trade_fills.csv contains duplicate fill_id values: {duplicates}")

    if fills["quantity"].isna().any() or (fills["quantity"] <= 0).any():
        raise ValueError("trade_fills.csv contains invalid quantity values")

    if fills["fill_price"].isna().any() or (fills["fill_price"] <= 0).any():
        raise ValueError("trade_fills.csv contains invalid fill_price values")

    if not fills["side"].isin({"buy", "sell"}).all():
        bad = sorted(fills.loc[~fills["side"].isin({"buy", "sell"}), "side"].unique())
        raise ValueError(f"trade_fills.csv contains invalid side values: {bad}")

    return fills


def load_processed_fills() -> pd.DataFrame:
    if not PROCESSED_FILLS_FILE.exists():
        return pd.DataFrame(columns=["fill_id", "processed_at"])

    processed = read_csv(PROCESSED_FILLS_FILE)

    if processed.empty:
        return pd.DataFrame(columns=["fill_id", "processed_at"])

    required = {"fill_id", "processed_at"}
    missing = [col for col in required if col not in processed.columns]
    if missing:
        raise ValueError(f"processed_fills.csv is missing required columns: {missing}")

    processed = processed.copy()
    processed["fill_id"] = processed["fill_id"].astype(str).str.strip()
    processed["processed_at"] = processed["processed_at"].astype(str).str.strip()

    return processed[["fill_id", "processed_at"]]


def load_portfolio_state() -> pd.DataFrame:
    if not PORTFOLIO_STATE_FILE.exists():
        return validate_portfolio_state(pd.DataFrame())

    state = read_csv(PORTFOLIO_STATE_FILE)
    return validate_portfolio_state(state)


def save_portfolio_state(state: pd.DataFrame) -> None:
    write_csv(state, PORTFOLIO_STATE_FILE)


def save_processed_fills(processed_fills: pd.DataFrame) -> None:
    write_csv(processed_fills, PROCESSED_FILLS_FILE)


def get_unprocessed_fills(all_fills: pd.DataFrame, processed_fills: pd.DataFrame) -> pd.DataFrame:
    if all_fills.empty:
        return all_fills.copy()

    if processed_fills.empty:
        return all_fills.copy()

    processed_ids = set(processed_fills["fill_id"].astype(str))
    unprocessed = all_fills.loc[~all_fills["fill_id"].astype(str).isin(processed_ids)].copy()
    return unprocessed


def append_processed_fill_ids(
    processed_fills: pd.DataFrame,
    newly_processed_fills: pd.DataFrame,
) -> pd.DataFrame:
    if newly_processed_fills.empty:
        return processed_fills.copy()

    now = utc_now_iso()
    new_rows = newly_processed_fills[["fill_id"]].copy()
    new_rows["processed_at"] = now

    combined = pd.concat([processed_fills, new_rows], ignore_index=True)
    combined = combined.drop_duplicates(subset=["fill_id"], keep="first").reset_index(drop=True)

    return combined[["fill_id", "processed_at"]]


def generate_position_id(existing_state: pd.DataFrame) -> str:
    if existing_state.empty or "position_id" not in existing_state.columns:
        return "POS001"

    existing_ids = (
        existing_state["position_id"]
        .dropna()
        .astype(str)
        .str.extract(r"POS(\d+)", expand=False)
        .dropna()
    )

    if existing_ids.empty:
        return "POS001"

    next_id = int(existing_ids.astype(int).max()) + 1
    return f"POS{next_id:03d}"


def get_optional_fill_value(fill: pd.Series, column: str, default):
    value = fill[column] if column in fill.index else default
    if pd.isna(value):
        return default
    return value


def find_open_position(state: pd.DataFrame, ticker: str) -> Optional[int]:
    if state.empty:
        return None

    matches = state[
        (state["ticker"].astype(str) == ticker)
        & (state["status"].astype(str).isin({"open", "exit_required"}))
        & (state["side"].astype(str) == "long")
    ]

    if matches.empty:
        return None

    if len(matches) > 1:
        raise ValueError(
            f"Multiple open positions found for ticker {ticker}. Fill Agent expects one open long position per ticker."
        )

    return matches.index[0]


def build_new_position(fill: pd.Series, existing_state: pd.DataFrame) -> dict:
    quantity = float(fill["quantity"])
    fill_price = float(fill["fill_price"])
    capital_allocated = get_optional_fill_value(fill, "capital_allocated", quantity * fill_price)
    stop_loss = get_optional_fill_value(fill, "stop_loss", 0.0)
    take_profit = get_optional_fill_value(fill, "take_profit", 0.0)
    regime_at_entry = get_optional_fill_value(fill, "regime_at_entry", "")
    sector = get_optional_fill_value(fill, "sector", "")
    signal_score = float(get_optional_fill_value(fill, "signal_score", 0.0))
    filled_at = str(fill["filled_at"])

    return {
        "position_id": generate_position_id(existing_state),
        "ticker": str(fill["ticker"]).upper(),
        "side": "long",
        "status": "open",
        "quantity": quantity,
        "average_entry_price": fill_price,
        "entry_date": filled_at,
        "capital_allocated": float(capital_allocated),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "regime_at_entry": str(regime_at_entry),
        "sector": str(sector),
        "signal_score": signal_score,
        "highest_price_since_entry": fill_price,
        "lowest_price_since_entry": fill_price,
        "current_price": fill_price,
        "market_value": quantity * fill_price,
        "unrealised_pnl_abs": 0.0,
        "unrealised_pnl_pct": 0.0,
        "realised_pnl_abs": 0.0,
        "exit_reason": "",
        "last_updated_at": utc_now_iso(),
    }


def apply_buy_fill_to_existing_position(state: pd.DataFrame, row_idx: int, fill: pd.Series) -> pd.DataFrame:
    output = state.copy()

    old_qty = float(output.at[row_idx, "quantity"])
    old_avg = float(output.at[row_idx, "average_entry_price"])
    add_qty = float(fill["quantity"])
    add_price = float(fill["fill_price"])

    new_qty = old_qty + add_qty
    new_avg = ((old_qty * old_avg) + (add_qty * add_price)) / new_qty

    output.at[row_idx, "quantity"] = new_qty
    output.at[row_idx, "average_entry_price"] = new_avg
    output.at[row_idx, "capital_allocated"] = new_qty * new_avg
    output.at[row_idx, "highest_price_since_entry"] = max(
        float(output.at[row_idx, "highest_price_since_entry"]),
        add_price,
    )
    output.at[row_idx, "lowest_price_since_entry"] = min(
        float(output.at[row_idx, "lowest_price_since_entry"]),
        add_price,
    )
    output.at[row_idx, "current_price"] = add_price
    output.at[row_idx, "market_value"] = new_qty * add_price
    output.at[row_idx, "unrealised_pnl_abs"] = (add_price - new_avg) * new_qty
    output.at[row_idx, "unrealised_pnl_pct"] = ((add_price - new_avg) / new_avg) * 100 if new_avg else 0.0
    output.at[row_idx, "last_updated_at"] = utc_now_iso()

    for col in ["stop_loss", "take_profit", "regime_at_entry", "sector", "signal_score"]:
        if col in fill.index and not pd.isna(fill[col]) and str(fill[col]).strip() != "":
            output.at[row_idx, col] = fill[col]

    return output


def apply_sell_fill_to_existing_position(state: pd.DataFrame, row_idx: int, fill: pd.Series) -> pd.DataFrame:
    output = state.copy()

    old_qty = float(output.at[row_idx, "quantity"])
    avg_entry = float(output.at[row_idx, "average_entry_price"])
    sell_qty = float(fill["quantity"])
    sell_price = float(fill["fill_price"])

    if sell_qty > old_qty:
        ticker = output.at[row_idx, "ticker"]
        raise ValueError(f"Sell quantity {sell_qty} exceeds open quantity {old_qty} for {ticker}")

    realised_increment = (sell_price - avg_entry) * sell_qty
    remaining_qty = old_qty - sell_qty
    existing_realised = float(output.at[row_idx, "realised_pnl_abs"])

    output.at[row_idx, "realised_pnl_abs"] = existing_realised + realised_increment
    output.at[row_idx, "current_price"] = sell_price
    output.at[row_idx, "last_updated_at"] = utc_now_iso()

    if remaining_qty > 0:
        output.at[row_idx, "quantity"] = remaining_qty
        output.at[row_idx, "capital_allocated"] = remaining_qty * avg_entry
        output.at[row_idx, "market_value"] = remaining_qty * sell_price
        output.at[row_idx, "unrealised_pnl_abs"] = (sell_price - avg_entry) * remaining_qty
        output.at[row_idx, "unrealised_pnl_pct"] = ((sell_price - avg_entry) / avg_entry) * 100 if avg_entry else 0.0
        output.at[row_idx, "status"] = "open"
    else:
        output.at[row_idx, "quantity"] = 0.0
        output.at[row_idx, "capital_allocated"] = 0.0
        output.at[row_idx, "market_value"] = 0.0
        output.at[row_idx, "unrealised_pnl_abs"] = 0.0
        output.at[row_idx, "unrealised_pnl_pct"] = 0.0
        output.at[row_idx, "status"] = "closed"
        output.at[row_idx, "exit_reason"] = "manual_sell_fill"

    return output


def process_fills(fills: pd.DataFrame, portfolio_state: pd.DataFrame) -> pd.DataFrame:
    state = portfolio_state.copy()

    if fills.empty:
        return validate_portfolio_state(state)

    fills = fills.sort_values(by="filled_at").reset_index(drop=True)

    for _, fill in fills.iterrows():
        ticker = str(fill["ticker"]).upper()
        side = str(fill["side"]).lower()

        open_position_idx = find_open_position(state, ticker)

        if side == "buy":
            if open_position_idx is None:
                new_position = build_new_position(fill, state)
                state = pd.concat([state, pd.DataFrame([new_position])], ignore_index=True)
            else:
                state = apply_buy_fill_to_existing_position(state, open_position_idx, fill)

        elif side == "sell":
            if open_position_idx is None:
                raise ValueError(f"No open long position found for sell fill in {ticker}")
            state = apply_sell_fill_to_existing_position(state, open_position_idx, fill)

        else:
            raise ValueError(f"Unsupported fill side: {side}")

        state = validate_portfolio_state(state)

    return validate_portfolio_state(state)


def main() -> None:
    all_fills = load_trade_fills()
    processed_fills = load_processed_fills()
    unprocessed_fills = get_unprocessed_fills(all_fills, processed_fills)

    portfolio_state = load_portfolio_state()
    updated_state = process_fills(unprocessed_fills, portfolio_state)

    updated_processed_fills = append_processed_fill_ids(processed_fills, unprocessed_fills)

    save_portfolio_state(updated_state)
    save_processed_fills(updated_processed_fills)

    open_positions = int((updated_state["status"] == "open").sum()) if not updated_state.empty else 0
    closed_positions = int((updated_state["status"] == "closed").sum()) if not updated_state.empty else 0

    print("Fill Agent finished.")
    print(f"Saved portfolio state to: {PORTFOLIO_STATE_FILE}")
    print(f"Saved processed fills ledger to: {PROCESSED_FILLS_FILE}")
    print("\nRun summary:")
    print(f"Total fills loaded: {len(all_fills)}")
    print(f"New fills processed: {len(unprocessed_fills)}")
    print(f"Total positions in state: {len(updated_state)}")
    print(f"Open positions: {open_positions}")
    print(f"Closed positions: {closed_positions}")


if __name__ == "__main__":
    main()