from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from pandas.errors import EmptyDataError

from agents.shared.event_log import (
    append_cash_adjusted_event,
    append_fill_processed_event,
    append_position_closed_event,
    append_position_opened_event,
    ensure_event_log_exists,
)
from shared.schema_registry import get_file_schema
from shared.schemas import (
    validate_cash_ledger,
    validate_cash_state,
    validate_portfolio_state,
    validate_processed_fills,
)


DATA_DIR = "data"

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
PROCESSED_FILLS_PATH = os.path.join(DATA_DIR, "processed_fills.csv")
MANUAL_FILLS_PATH = os.path.join(DATA_DIR, "manual_fills.csv")
CASH_STATE_PATH = os.path.join(DATA_DIR, "cash_state.csv")
CASH_LEDGER_PATH = os.path.join(DATA_DIR, "cash_ledger.csv")

DEFAULT_STARTING_CASH = 100000.0
AGENT_NAME = "Fill Agent"
PORTFOLIO_STATE_SCHEMA = get_file_schema("portfolio_state.csv")
CASH_STATE_SCHEMA = get_file_schema("cash_state.csv")
CASH_LEDGER_SCHEMA = get_file_schema("cash_ledger.csv")
PROCESSED_FILLS_SCHEMA = get_file_schema("processed_fills.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str:
    return "RUN_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_read_csv_or_default(
    path: str,
    columns: list[str],
    default_df: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Read CSV safely.
    If file does not exist, is zero-byte, or is structurally empty, create/reset it.
    """
    if not os.path.exists(path):
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        df.to_csv(path, index=False)
        return df

    if os.path.getsize(path) == 0:
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        df.to_csv(path, index=False)
        return df

    try:
        df = pd.read_csv(path)
    except EmptyDataError:
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        df.to_csv(path, index=False)
        return df

    if df.empty and len(df.columns) == 0:
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        df.to_csv(path, index=False)
        return df

    return df


def ensure_cash_files() -> tuple[pd.DataFrame, pd.DataFrame]:
    cash_state_df = safe_read_csv_or_default(
        CASH_STATE_PATH,
        columns=CASH_STATE_SCHEMA.canonical_column_order,
        default_df=pd.DataFrame(
            [{"as_of": utc_now_iso(), "cash_balance": DEFAULT_STARTING_CASH}]
        ),
    )
    cash_state_df = validate_cash_state(cash_state_df, keep_extra_columns=False)

    cash_ledger_df = safe_read_csv_or_default(
        CASH_LEDGER_PATH,
        columns=CASH_LEDGER_SCHEMA.canonical_column_order,
    )
    cash_ledger_df = validate_cash_ledger(cash_ledger_df, keep_extra_columns=False)

    return cash_state_df, cash_ledger_df


def ensure_state_file() -> pd.DataFrame:
    df = safe_read_csv_or_default(
        STATE_PATH,
        columns=PORTFOLIO_STATE_SCHEMA.canonical_column_order,
    )
    return validate_portfolio_state(df, keep_extra_columns=False)


def ensure_processed_fills_file() -> pd.DataFrame:
    df = safe_read_csv_or_default(
        PROCESSED_FILLS_PATH,
        columns=PROCESSED_FILLS_SCHEMA.canonical_column_order,
    )
    return validate_processed_fills(df, keep_extra_columns=False)


def ensure_manual_fills_file() -> pd.DataFrame:
    return safe_read_csv_or_default(
        MANUAL_FILLS_PATH,
        columns=[
            "fill_id",
            "ticker",
            "side",
            "action",
            "quantity",
            "fill_price",
            "fees",
            "fill_timestamp",
        ],
    )


def read_fill_input() -> pd.DataFrame:
    df = ensure_manual_fills_file()

    required = [
        "fill_id",
        "ticker",
        "side",
        "action",
        "quantity",
        "fill_price",
        "fees",
        "fill_timestamp",
    ]
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required fill input columns in {MANUAL_FILLS_PATH}: {missing}")

    return df


def validate_fill_row(row: pd.Series) -> None:
    if pd.isna(row["fill_id"]) or str(row["fill_id"]).strip() == "":
        raise ValueError("fill_id is required")

    if pd.isna(row["ticker"]) or str(row["ticker"]).strip() == "":
        raise ValueError("ticker is required")

    if str(row["side"]).strip().lower() not in {"long", "short"}:
        raise ValueError(f"Invalid side: {row['side']}")

    if str(row["action"]).strip().lower() not in {"buy", "sell"}:
        raise ValueError(f"Invalid action: {row['action']}")

    quantity = pd.to_numeric(row["quantity"], errors="coerce")
    fill_price = pd.to_numeric(row["fill_price"], errors="coerce")
    fees = pd.to_numeric(row["fees"], errors="coerce")

    if pd.isna(quantity) or quantity <= 0:
        raise ValueError("quantity must be positive")

    if pd.isna(fill_price) or fill_price <= 0:
        raise ValueError("fill_price must be positive")

    if pd.isna(fees) or fees < 0:
        raise ValueError("fees must be zero or positive")

    if pd.isna(row["fill_timestamp"]) or str(row["fill_timestamp"]).strip() == "":
        raise ValueError("fill_timestamp is required")


def get_cash_balance(cash_state_df: pd.DataFrame) -> float:
    if cash_state_df.empty:
        return DEFAULT_STARTING_CASH
    return float(cash_state_df.iloc[-1]["cash_balance"])


def serialise_row_state(row: pd.Series | dict) -> dict[str, object]:
    if isinstance(row, pd.Series):
        payload = row.to_dict()
    else:
        payload = dict(row)

    clean_payload: dict[str, object] = {}
    for key, value in payload.items():
        clean_payload[str(key)] = None if pd.isna(value) else value
    return clean_payload


def write_cash_balance(balance: float) -> None:
    cash_state_df = pd.DataFrame(
        [{"as_of": utc_now_iso(), "cash_balance": balance}]
    )
    cash_state_df = validate_cash_state(cash_state_df, keep_extra_columns=False)
    cash_state_df.to_csv(CASH_STATE_PATH, index=False)


def append_cash_ledger_row(
    ledger_df: pd.DataFrame,
    run_id: str,
    event_type: str,
    position_id: str,
    ticker: str,
    side: str,
    action: str,
    amount: float,
    fees: float,
    cash_balance_after: float,
    notes: str,
) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [
            {
                "ledger_id": str(uuid.uuid4()),
                "run_id": run_id,
                "timestamp": utc_now_iso(),
                "event_type": event_type,
                "position_id": position_id,
                "ticker": ticker,
                "side": side,
                "action": action,
                "amount": amount,
                "fees": fees,
                "cash_balance_after": cash_balance_after,
                "notes": notes,
            }
        ],
        columns=CASH_LEDGER_SCHEMA.canonical_column_order,
    )
    out = pd.concat([ledger_df, new_row], ignore_index=True)
    out = validate_cash_ledger(out, keep_extra_columns=False)
    out.to_csv(CASH_LEDGER_PATH, index=False)
    return out


def find_open_position(state_df: pd.DataFrame, ticker: str, side: str) -> Optional[int]:
    matches = state_df[
        (state_df["ticker"].astype(str).str.upper() == str(ticker).upper()) &
        (state_df["side"].astype(str).str.lower() == str(side).lower()) &
        (state_df["status"].astype(str).isin(["open", "exit_required"]))
    ]
    if matches.empty:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"More than one active position found for ticker={ticker}, side={side}")
    return matches.index[0]


def open_long_position(
    state_df: pd.DataFrame,
    cash_balance: float,
    ledger_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> tuple[pd.DataFrame, float, pd.DataFrame]:
    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).lower()
    quantity = float(row["quantity"])
    fill_price = float(row["fill_price"])
    fees = float(row["fees"])
    gross_cost = quantity * fill_price
    total_cash_out = gross_cost + fees

    if cash_balance < total_cash_out:
        raise RuntimeError(
            f"Insufficient cash to open position in {ticker}. "
            f"Required={total_cash_out:.2f}, Available={cash_balance:.2f}"
        )

    existing_idx = find_open_position(state_df, ticker, side)
    if existing_idx is not None:
        raise RuntimeError(f"Active position already exists for {ticker} {side}")

    position_id = "POS_" + uuid.uuid4().hex[:10].upper()

    new_row = pd.DataFrame(
        [
            {
                "position_id": position_id,
                "ticker": ticker,
                "side": side,
                "status": "open",
                "quantity": quantity,
                "entry_price": fill_price,
                "entry_date": row["fill_timestamp"],
                "current_price": fill_price,
                "market_value": quantity * fill_price,
                "pnl_abs": -fees,
                "pnl_pct": (-fees / gross_cost * 100) if gross_cost > 0 else 0.0,
                "realised_pnl_abs": 0.0,
                "fees_total": fees,
                "exit_flag": False,
                "exit_reason": "",
                "last_updated": utc_now_iso(),
                "run_id": run_id,
                "closed_at": pd.NA,
                "exit_price": pd.NA,
            }
        ]
    )

    state_df = pd.concat([state_df, new_row], ignore_index=True)

    position_after = serialise_row_state(new_row.iloc[0])
    cash_balance_before = cash_balance
    cash_balance -= total_cash_out
    write_cash_balance(cash_balance)

    ledger_df = append_cash_ledger_row(
        ledger_df=ledger_df,
        run_id=run_id,
        event_type="position_open",
        position_id=position_id,
        ticker=ticker,
        side=side,
        action="buy",
        amount=-gross_cost,
        fees=fees,
        cash_balance_after=cash_balance,
        notes=f"Opened {quantity} shares at {fill_price}",
    )

    append_position_opened_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        position_id=position_id,
        ticker=ticker,
        message=f"Opened long position for {ticker}",
        before_state={},
        after_state=position_after,
        details={
            "fill_id": str(row["fill_id"]),
            "action": "buy",
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "fees": fees,
        },
    )
    append_cash_adjusted_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        ticker=ticker,
        position_id=position_id,
        message=f"Cash debited for opening {ticker}",
        before_state={"cash_balance": round(cash_balance_before, 2)},
        after_state={"cash_balance": round(cash_balance, 2)},
        details={
            "fill_id": str(row["fill_id"]),
            "action": "buy",
            "gross_cost": round(gross_cost, 2),
            "fees": fees,
            "net_cash_change": round(-total_cash_out, 2),
        },
    )

    return state_df, cash_balance, ledger_df


def close_long_position(
    state_df: pd.DataFrame,
    cash_balance: float,
    ledger_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> tuple[pd.DataFrame, float, pd.DataFrame]:
    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).lower()
    quantity = float(row["quantity"])
    fill_price = float(row["fill_price"])
    fees = float(row["fees"])

    idx = find_open_position(state_df, ticker, side)
    if idx is None:
        raise RuntimeError(f"No active position found to close for {ticker} {side}")

    position = state_df.loc[idx].copy()
    position_before = serialise_row_state(position)

    if float(position["quantity"]) != quantity:
        raise RuntimeError(
            f"Partial closes are not supported yet for {ticker}. "
            f"Expected quantity={position['quantity']}, received={quantity}"
        )

    entry_price = float(position["entry_price"])
    entry_fees = float(position.get("fees_total", 0.0))

    gross_proceeds = quantity * fill_price
    net_proceeds = gross_proceeds - fees
    realised_pnl = net_proceeds - (quantity * entry_price) - entry_fees

    state_df.at[idx, "status"] = "closed"
    state_df.at[idx, "current_price"] = fill_price
    state_df.at[idx, "market_value"] = 0.0
    state_df.at[idx, "pnl_abs"] = 0.0
    state_df.at[idx, "pnl_pct"] = 0.0
    state_df.at[idx, "realised_pnl_abs"] = realised_pnl
    state_df.at[idx, "fees_total"] = entry_fees + fees
    state_df.at[idx, "exit_flag"] = False
    state_df.at[idx, "exit_reason"] = "position_closed"
    state_df.at[idx, "last_updated"] = utc_now_iso()
    state_df.at[idx, "run_id"] = run_id
    state_df.at[idx, "closed_at"] = row["fill_timestamp"]
    state_df.at[idx, "exit_price"] = fill_price

    position_after = serialise_row_state(state_df.loc[idx])
    cash_balance_before = cash_balance
    cash_balance += net_proceeds
    write_cash_balance(cash_balance)

    ledger_df = append_cash_ledger_row(
        ledger_df=ledger_df,
        run_id=run_id,
        event_type="position_close",
        position_id=str(position["position_id"]),
        ticker=ticker,
        side=side,
        action="sell",
        amount=net_proceeds,
        fees=fees,
        cash_balance_after=cash_balance,
        notes=f"Closed {quantity} shares at {fill_price}; realised_pnl={realised_pnl:.2f}",
    )

    append_position_closed_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        position_id=str(position["position_id"]),
        ticker=ticker,
        message=f"Closed long position for {ticker}",
        before_state=position_before,
        after_state=position_after,
        details={
            "fill_id": str(row["fill_id"]),
            "action": "sell",
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "fees": fees,
            "realised_pnl_abs": round(realised_pnl, 2),
        },
    )
    append_cash_adjusted_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        ticker=ticker,
        position_id=str(position["position_id"]),
        message=f"Cash credited for closing {ticker}",
        before_state={"cash_balance": round(cash_balance_before, 2)},
        after_state={"cash_balance": round(cash_balance, 2)},
        details={
            "fill_id": str(row["fill_id"]),
            "action": "sell",
            "gross_proceeds": round(gross_proceeds, 2),
            "fees": fees,
            "net_cash_change": round(net_proceeds, 2),
        },
    )

    return state_df, cash_balance, ledger_df


def append_processed_fill(processed_df: pd.DataFrame, fill_id: str, run_id: str) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [{"fill_id": fill_id, "processed_at": utc_now_iso(), "run_id": run_id}],
        columns=PROCESSED_FILLS_SCHEMA.canonical_column_order,
    )
    out = pd.concat([processed_df, new_row], ignore_index=True)
    out = validate_processed_fills(out, keep_extra_columns=False)
    out.to_csv(PROCESSED_FILLS_PATH, index=False)
    return out


def run_fill_agent() -> None:
    run_id = current_run_id()
    ensure_event_log_exists()

    state_df = ensure_state_file()
    processed_df = ensure_processed_fills_file()
    fills_df = read_fill_input()
    cash_state_df, ledger_df = ensure_cash_files()

    cash_balance = get_cash_balance(cash_state_df)
    processed_ids = set(processed_df["fill_id"].astype(str).tolist()) if not processed_df.empty else set()

    if fills_df.empty:
        print("Fill Agent finished.")
        print(f"No fills found in {MANUAL_FILLS_PATH}")
        print(f"Ending cash balance: {cash_balance:.2f}")
        return

    for _, row in fills_df.iterrows():
        validate_fill_row(row)

        fill_id = str(row["fill_id"])
        if fill_id in processed_ids:
            continue

        action = str(row["action"]).strip().lower()
        side = str(row["side"]).strip().lower()

        print(f"Processing fill {fill_id} for {row['ticker']}")

        if side != "long":
            raise NotImplementedError("This version currently supports long positions only")

        if action == "buy":
            state_df, cash_balance, ledger_df = open_long_position(
                state_df=state_df,
                cash_balance=cash_balance,
                ledger_df=ledger_df,
                row=row,
                run_id=run_id,
            )
        elif action == "sell":
            state_df, cash_balance, ledger_df = close_long_position(
                state_df=state_df,
                cash_balance=cash_balance,
                ledger_df=ledger_df,
                row=row,
                run_id=run_id,
            )
        else:
            raise ValueError(f"Unsupported action: {action}")

        processed_df = append_processed_fill(processed_df, fill_id, run_id)
        processed_ids.add(fill_id)
        append_fill_processed_event(
            run_id=run_id,
            agent_name=AGENT_NAME,
            fill_id=fill_id,
            ticker=str(row["ticker"]).upper(),
            message=f"Processed fill {fill_id}",
            details={
                "side": side,
                "action": action,
                "quantity": float(row["quantity"]),
                "fill_price": float(row["fill_price"]),
                "fees": float(row["fees"]),
                "fill_timestamp": str(row["fill_timestamp"]),
            },
        )

    state_df = validate_portfolio_state(state_df, keep_extra_columns=False)
    state_df.to_csv(STATE_PATH, index=False)

    print("Fill Agent finished.")
    print(f"Saved state to: {STATE_PATH}")
    print(f"Saved processed fills to: {PROCESSED_FILLS_PATH}")
    print(f"Saved cash state to: {CASH_STATE_PATH}")
    print(f"Saved cash ledger to: {CASH_LEDGER_PATH}")
    print(f"Ending cash balance: {cash_balance:.2f}")


if __name__ == "__main__":
    run_fill_agent()
