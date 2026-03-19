from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd


DATA_DIR = "data"

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
CASH_STATE_PATH = os.path.join(DATA_DIR, "cash_state.csv")
EQUITY_SNAPSHOT_PATH = os.path.join(DATA_DIR, "portfolio_equity.csv")
EQUITY_HISTORY_PATH = os.path.join(DATA_DIR, "portfolio_equity_history.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str:
    return "RUN_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def read_csv_required(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    return pd.read_csv(path)


def normalise_state_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    aliases = {
        "average_entry_price": "entry_price",
        "current_qty": "quantity",
    }

    for old_col, new_col in aliases.items():
        if old_col in df.columns and new_col not in df.columns:
            df[new_col] = df[old_col]

    required_columns = [
        "position_id",
        "ticker",
        "side",
        "status",
        "quantity",
        "entry_price",
        "entry_date",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "realised_pnl_abs",
        "fees_total",
        "exit_flag",
        "exit_reason",
        "last_updated",
        "run_id",
        "closed_at",
        "exit_price",
    ]

    for col in required_columns:
        if col not in df.columns:
            if col in {"market_value", "pnl_abs", "pnl_pct", "realised_pnl_abs", "fees_total"}:
                df[col] = 0.0
            else:
                df[col] = pd.NA

    return df


def ensure_cash_state() -> pd.DataFrame:
    if os.path.exists(CASH_STATE_PATH):
        return pd.read_csv(CASH_STATE_PATH)

    df = pd.DataFrame(
        [{"as_of": utc_now_iso(), "cash_balance": 100000.0}]
    )
    df.to_csv(CASH_STATE_PATH, index=False)
    return df


def run_portfolio_equity_agent() -> None:
    run_id = current_run_id()

    state_df = read_csv_required(STATE_PATH)
    state_df = normalise_state_columns(state_df)

    cash_state_df = ensure_cash_state()

    active_df = state_df[state_df["status"].astype(str).isin(["open", "exit_required"])].copy()
    closed_df = state_df[state_df["status"].astype(str) == "closed"].copy()

    cash_balance = float(cash_state_df.iloc[-1]["cash_balance"]) if not cash_state_df.empty else 0.0
    open_market_value = pd.to_numeric(active_df["market_value"], errors="coerce").fillna(0.0).sum()
    unrealised_pnl = pd.to_numeric(active_df["pnl_abs"], errors="coerce").fillna(0.0).sum()
    realised_pnl = pd.to_numeric(closed_df["realised_pnl_abs"], errors="coerce").fillna(0.0).sum()

    gross_exposure = open_market_value
    net_exposure = open_market_value
    total_equity = cash_balance + open_market_value

    snapshot = pd.DataFrame(
        [
            {
                "timestamp": utc_now_iso(),
                "run_id": run_id,
                "cash_balance": cash_balance,
                "open_market_value": open_market_value,
                "gross_exposure": gross_exposure,
                "net_exposure": net_exposure,
                "unrealised_pnl_abs": unrealised_pnl,
                "realised_pnl_abs": realised_pnl,
                "total_equity": total_equity,
                "open_positions": len(active_df),
                "closed_positions": len(closed_df),
            }
        ]
    )

    snapshot.to_csv(EQUITY_SNAPSHOT_PATH, index=False)

    if os.path.exists(EQUITY_HISTORY_PATH):
        history_df = pd.read_csv(EQUITY_HISTORY_PATH)
        history_df = pd.concat([history_df, snapshot], ignore_index=True)
    else:
        history_df = snapshot.copy()

    history_df.to_csv(EQUITY_HISTORY_PATH, index=False)

    print("Portfolio Equity Agent finished.")
    print(f"Saved equity snapshot to: {EQUITY_SNAPSHOT_PATH}")
    print(f"Saved equity history to: {EQUITY_HISTORY_PATH}")
    print("")
    print("Run summary:")
    print(f"Cash balance: {cash_balance:.2f}")
    print(f"Open market value: {open_market_value:.2f}")
    print(f"Unrealised PnL: {unrealised_pnl:.2f}")
    print(f"Realised PnL: {realised_pnl:.2f}")
    print(f"Total equity: {total_equity:.2f}")


if __name__ == "__main__":
    run_portfolio_equity_agent()