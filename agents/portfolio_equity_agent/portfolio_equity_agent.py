from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd

from agents.shared.event_log import append_event
from shared.run_context import get_or_create_run_id


DATA_DIR = "data"

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
CASH_STATE_PATH = os.path.join(DATA_DIR, "cash_state.csv")
EQUITY_SNAPSHOT_PATH = os.path.join(DATA_DIR, "portfolio_equity.csv")
EQUITY_HISTORY_PATH = os.path.join(DATA_DIR, "portfolio_equity_history.csv")
PERFORMANCE_SUMMARY_PATH = os.path.join(DATA_DIR, "performance_summary.csv")
AGENT_NAME = "Portfolio Equity Agent"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def emit_portfolio_equity_snapshot_event(run_id: str, snapshot_row: pd.Series) -> None:
    """
    Append one event-log row for a portfolio equity snapshot.
    """
    append_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        event_type="portfolio_equity_snapshot",
        entity_type="portfolio",
        entity_id="portfolio_equity",
        severity="info",
        message="Portfolio equity snapshot generated",
        metadata={
            "timestamp": snapshot_row.get("timestamp"),
            "cash_balance": snapshot_row.get("cash_balance"),
            "open_market_value": snapshot_row.get("open_market_value"),
            "gross_exposure": snapshot_row.get("gross_exposure"),
            "net_exposure": snapshot_row.get("net_exposure"),
            "unrealised_pnl_abs": snapshot_row.get("unrealised_pnl_abs"),
            "realised_pnl_abs": snapshot_row.get("realised_pnl_abs"),
            "total_equity": snapshot_row.get("total_equity"),
            "open_positions": snapshot_row.get("open_positions"),
            "closed_positions": snapshot_row.get("closed_positions"),
        },
    )


def apply_drawdown_metrics(history_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add peak equity and drawdown fields based on total equity history.
    """
    output_df = history_df.copy()
    if "timestamp" in output_df.columns:
        output_df["timestamp"] = pd.to_datetime(output_df["timestamp"], errors="coerce")
        output_df = output_df.sort_values(by="timestamp", kind="stable").reset_index(drop=True)
        output_df["timestamp"] = output_df["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S.%f%z")
        output_df["timestamp"] = output_df["timestamp"].str.replace(
            r"(\+|-)(\d{2})(\d{2})$",
            r"\1\2:\3",
            regex=True,
        )

    total_equity = pd.to_numeric(output_df["total_equity"], errors="coerce").fillna(0.0)
    output_df["total_equity"] = total_equity

    output_df["peak_equity"] = total_equity.cummax()
    output_df["drawdown_abs"] = output_df["peak_equity"] - total_equity
    output_df["drawdown_pct"] = output_df.apply(
        lambda row: 0.0
        if float(row["peak_equity"]) <= 0
        else (float(row["drawdown_abs"]) / float(row["peak_equity"])) * 100.0,
        axis=1,
    )

    output_df["peak_equity"] = output_df["peak_equity"].round(6)
    output_df["drawdown_abs"] = output_df["drawdown_abs"].round(6)
    output_df["drawdown_pct"] = output_df["drawdown_pct"].round(6)

    return output_df


def build_performance_summary(history_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build a one-row performance summary from the full equity history.
    """
    latest_row = history_df.iloc[-1]
    peak_idx = pd.to_numeric(history_df["peak_equity"], errors="coerce").idxmax()
    max_drawdown_idx = pd.to_numeric(history_df["drawdown_abs"], errors="coerce").idxmax()

    peak_row = history_df.loc[peak_idx]
    max_drawdown_row = history_df.loc[max_drawdown_idx]

    return pd.DataFrame(
        [
            {
                "latest_timestamp": latest_row.get("timestamp"),
                "latest_run_id": latest_row.get("run_id"),
                "current_total_equity": latest_row.get("total_equity"),
                "peak_equity": latest_row.get("peak_equity"),
                "peak_equity_timestamp": peak_row.get("timestamp"),
                "current_drawdown_abs": latest_row.get("drawdown_abs"),
                "current_drawdown_pct": latest_row.get("drawdown_pct"),
                "max_drawdown_abs": max_drawdown_row.get("drawdown_abs"),
                "max_drawdown_pct": max_drawdown_row.get("drawdown_pct"),
                "max_drawdown_timestamp": max_drawdown_row.get("timestamp"),
                "observation_count": len(history_df),
            }
        ]
    )


def run_portfolio_equity_agent() -> None:
    run_id = get_or_create_run_id()

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

    if os.path.exists(EQUITY_HISTORY_PATH):
        history_df = pd.read_csv(EQUITY_HISTORY_PATH)
        history_df = pd.concat([history_df, snapshot], ignore_index=True)
    else:
        history_df = snapshot.copy()

    history_df = apply_drawdown_metrics(history_df)
    snapshot = history_df.tail(1).copy()
    performance_summary_df = build_performance_summary(history_df)

    snapshot.to_csv(EQUITY_SNAPSHOT_PATH, index=False)
    history_df.to_csv(EQUITY_HISTORY_PATH, index=False)
    performance_summary_df.to_csv(PERFORMANCE_SUMMARY_PATH, index=False)
    emit_portfolio_equity_snapshot_event(run_id=run_id, snapshot_row=snapshot.iloc[0])

    print("Portfolio Equity Agent finished.")
    print(f"Saved equity snapshot to: {EQUITY_SNAPSHOT_PATH}")
    print(f"Saved equity history to: {EQUITY_HISTORY_PATH}")
    print(f"Saved performance summary to: {PERFORMANCE_SUMMARY_PATH}")
    print("")
    print("Run summary:")
    print(f"Cash balance: {cash_balance:.2f}")
    print(f"Open market value: {open_market_value:.2f}")
    print(f"Unrealised PnL: {unrealised_pnl:.2f}")
    print(f"Realised PnL: {realised_pnl:.2f}")
    print(f"Total equity: {total_equity:.2f}")


if __name__ == "__main__":
    run_portfolio_equity_agent()
