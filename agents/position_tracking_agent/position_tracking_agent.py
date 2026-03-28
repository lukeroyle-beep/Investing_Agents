from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from shared.io_utils import read_csv_with_schema, write_csv_with_schema
from shared.portfolio_state_helpers import (
    ACTIVE_POSITION_STATUSES,
    CLOSED_POSITION_STATUS,
    normalise_position_status,
)
from shared.schemas import (
    PORTFOLIO_STATE_SCHEMA,
    validate_position_alerts,
)


DATA_DIR = "data"

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
ALERTS_PATH = os.path.join(DATA_DIR, "position_alerts.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str:
    return "RUN_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_portfolio_state() -> pd.DataFrame:
    return read_csv_with_schema(
        STATE_PATH,
        schema=PORTFOLIO_STATE_SCHEMA,
        empty_ok=False,
    )


def write_portfolio_state(df: pd.DataFrame) -> None:
    write_csv_with_schema(
        df,
        STATE_PATH,
        schema=PORTFOLIO_STATE_SCHEMA,
        keep_extra_columns=False,
    )


def ensure_alerts_file() -> pd.DataFrame:
    if os.path.exists(ALERTS_PATH):
        return validate_position_alerts(pd.read_csv(ALERTS_PATH), keep_extra_columns=False)

    df = pd.DataFrame(
        columns=[
            "alert_id",
            "run_id",
            "timestamp",
            "position_id",
            "ticker",
            "status",
            "alert_type",
            "message",
        ]
    )
    df = validate_position_alerts(df, keep_extra_columns=False)
    df.to_csv(ALERTS_PATH, index=False)
    return df


def get_latest_price(ticker: str, fallback_price: Optional[float]) -> float:
    """
    Placeholder pricing hook.

    Current behaviour:
    - uses existing current_price if present
    - otherwise falls back to entry_price logic in caller

    Replace this function later with your real market data source.
    """
    if fallback_price is None or pd.isna(fallback_price):
        raise ValueError(f"No fallback price available for {ticker}")
    return float(fallback_price)


def calculate_long_pnl(quantity: float, entry_price: float, current_price: float) -> tuple[float, float]:
    market_value = quantity * current_price
    pnl_abs = (current_price - entry_price) * quantity
    cost_basis = quantity * entry_price
    pnl_pct = (pnl_abs / cost_basis * 100.0) if cost_basis > 0 else 0.0
    return market_value, pnl_abs, pnl_pct


def calculate_short_pnl(quantity: float, entry_price: float, current_price: float) -> tuple[float, float, float]:
    """
    For shorts:
    - market_value here is stored as current notional exposure
    - pnl_abs rises as price falls
    """
    market_value = quantity * current_price
    pnl_abs = (entry_price - current_price) * quantity
    initial_notional = quantity * entry_price
    pnl_pct = (pnl_abs / initial_notional * 100.0) if initial_notional > 0 else 0.0
    return market_value, pnl_abs, pnl_pct


def append_alert(
    alerts_df: pd.DataFrame,
    run_id: str,
    position_id: str,
    ticker: str,
    status: str,
    alert_type: str,
    message: str,
) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [
            {
                "alert_id": f"ALT_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
                "run_id": run_id,
                "timestamp": utc_now_iso(),
                "position_id": position_id,
                "ticker": ticker,
                "status": status,
                "alert_type": alert_type,
                "message": message,
            }
        ]
    )
    out = pd.concat([alerts_df, new_row], ignore_index=True)
    out = validate_position_alerts(out, keep_extra_columns=False)
    out.to_csv(ALERTS_PATH, index=False)
    return out


def validate_tracking_inputs(row: pd.Series) -> None:
    quantity = pd.to_numeric(row.get("quantity"), errors="coerce")
    entry_price = pd.to_numeric(row.get("entry_price"), errors="coerce")

    if pd.isna(quantity) or quantity <= 0:
        raise ValueError(
            f"Invalid active quantity for position_id={row.get('position_id')} ticker={row.get('ticker')}"
        )

    if pd.isna(entry_price) or entry_price <= 0:
        raise ValueError(
            f"Invalid active entry_price for position_id={row.get('position_id')} ticker={row.get('ticker')}"
        )

    side = str(row.get("side")).strip().lower()
    if side not in {"long", "short"}:
        raise ValueError(
            f"Invalid side for position_id={row.get('position_id')} ticker={row.get('ticker')}: {row.get('side')}"
        )


def process_active_position(row: pd.Series, run_id: str) -> pd.Series:
    row = row.copy()
    validate_tracking_inputs(row)

    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).strip().lower()
    quantity = float(row["quantity"])
    entry_price = float(row["entry_price"])

    existing_current_price = pd.to_numeric(pd.Series([row.get("current_price")]), errors="coerce").iloc[0]
    fallback_price = existing_current_price if not pd.isna(existing_current_price) else entry_price
    latest_price = get_latest_price(ticker, fallback_price)

    if side == "long":
        market_value, pnl_abs, pnl_pct = calculate_long_pnl(quantity, entry_price, latest_price)
    else:
        market_value, pnl_abs, pnl_pct = calculate_short_pnl(quantity, entry_price, latest_price)

    prev_high = pd.to_numeric(pd.Series([row.get("highest_price_since_entry")]), errors="coerce").iloc[0]
    prev_low = pd.to_numeric(pd.Series([row.get("lowest_price_since_entry")]), errors="coerce").iloc[0]

    if pd.isna(prev_high):
        new_high = latest_price
    else:
        new_high = max(float(prev_high), latest_price)

    if pd.isna(prev_low):
        new_low = latest_price
    else:
        new_low = min(float(prev_low), latest_price)

    row["current_price"] = latest_price
    row["market_value"] = market_value
    row["pnl_abs"] = pnl_abs
    row["pnl_pct"] = pnl_pct
    row["highest_price_since_entry"] = new_high
    row["lowest_price_since_entry"] = new_low
    row["run_id"] = run_id
    row["last_updated"] = utc_now_iso()

    return row


def preserve_closed_position(row: pd.Series) -> pd.Series:
    """
    Closed positions are intentionally left untouched.

    This prevents Position Tracking from rewriting:
    - quantity
    - entry_price
    - entry_date
    - closed_at
    - exit_price
    - realised_pnl_abs
    - fees_total
    - status
    """
    return row.copy()


def run_position_tracking_agent() -> None:
    run_id = current_run_id()

    state_df = read_portfolio_state()

    alerts_df = ensure_alerts_file()

    if state_df.empty:
        write_portfolio_state(state_df)
        print("Position Tracking Agent finished.")
        print(f"Saved portfolio state to: {STATE_PATH}")
        print(f"Saved position alerts to: {ALERTS_PATH}")
        print("")
        print("Run summary:")
        print("Total positions: 0")
        print("Open positions: 0")
        print("Exit required: 0")
        print(f"Updated at: {utc_now_iso()}")
        return

    output_rows = []

    total_positions = len(state_df)
    active_count = 0
    exit_required_count = 0
    alert_count = 0

    for _, row in state_df.iterrows():
        status = normalise_position_status(row.get("status"))

        if status in ACTIVE_POSITION_STATUSES:
            active_count += 1
            original_status = status

            updated_row = process_active_position(row, run_id)

            output_rows.append(updated_row)

            if original_status == "exit_required":
                exit_required_count += 1
                alerts_df = append_alert(
                    alerts_df=alerts_df,
                    run_id=run_id,
                    position_id=str(updated_row["position_id"]),
                    ticker=str(updated_row["ticker"]),
                    status=str(updated_row["status"]),
                    alert_type="exit_required",
                    message="Position remains in exit_required state and was mark-to-market updated only.",
                )
                alert_count += 1

        elif status == CLOSED_POSITION_STATUS:
            output_rows.append(preserve_closed_position(row))

        else:
            raise ValueError(
                f"Invalid status encountered in Position Tracking Agent for "
                f"position_id={row.get('position_id')} ticker={row.get('ticker')}: {row.get('status')}"
            )

    out_df = pd.DataFrame(output_rows)
    write_portfolio_state(out_df)

    print("Position Tracking Agent finished.")
    print(f"Saved portfolio state to: {STATE_PATH}")
    print(f"Saved position alerts to: {ALERTS_PATH}")
    print("")
    print("Run summary:")
    print(f"Total positions: {total_positions}")
    print(f"Active positions updated: {active_count}")
    print(f"Exit required positions: {exit_required_count}")
    print(f"Alerts added: {alert_count}")
    print(f"Updated at: {utc_now_iso()}")


if __name__ == "__main__":
    run_position_tracking_agent()
