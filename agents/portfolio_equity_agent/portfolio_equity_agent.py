from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd

from agents.shared.event_log import append_equity_snapshot_recorded_event
from shared.io_utils import write_managed_csv_with_schema
from shared.paths import DATA_DIR as RUNTIME_STATE_DIR
from shared.portfolio_monitor import merge_authoritative_monitor
from shared.portfolio_state_helpers import ACTIVE_POSITION_STATUSES, CLOSED_POSITION_STATUS
from shared.schema_registry import get_file_schema
from shared.schemas import (
    PERFORMANCE_SUMMARY_SCHEMA,
    PORTFOLIO_EQUITY_HISTORY_SCHEMA,
    validate_cash_state,
    validate_performance_summary,
    validate_portfolio_equity_history,
    validate_portfolio_monitor,
    validate_portfolio_state,
)
from shared.run_context import get_or_create_run_id
from shared.sqlite_sidecar import upsert_portfolio_equity_history_row


DATA_DIR = str(RUNTIME_STATE_DIR)

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
MONITOR_PATH = os.path.join(DATA_DIR, "portfolio_monitor.csv")
CASH_STATE_PATH = os.path.join(DATA_DIR, "cash_state.csv")
EQUITY_HISTORY_PATH = os.path.join(DATA_DIR, "portfolio_equity_history.csv")
PERFORMANCE_SUMMARY_PATH = os.path.join(DATA_DIR, "performance_summary.csv")
AGENT_NAME = "Portfolio Equity Agent"
PORTFOLIO_EQUITY_HISTORY_FILE_SCHEMA = get_file_schema("portfolio_equity_history.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_csv_required(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")
    return pd.read_csv(path)


def read_cash_state() -> pd.DataFrame:
    return validate_cash_state(
        read_csv_required(CASH_STATE_PATH),
        keep_extra_columns=False,
    )


def emit_portfolio_equity_snapshot_event(run_id: str, snapshot_row: pd.Series) -> None:
    """
    Append one event-log row for a portfolio equity snapshot.
    """
    append_equity_snapshot_recorded_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        severity="info",
        message="Portfolio equity snapshot generated",
        details={
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

    state_df = validate_portfolio_state(
        read_csv_required(STATE_PATH),
        keep_extra_columns=False,
    )
    monitor_df = validate_portfolio_monitor(
        read_csv_required(MONITOR_PATH),
        keep_extra_columns=False,
    )
    state_df = merge_authoritative_monitor(state_df, monitor_df)

    cash_state_df = read_cash_state()

    active_df = state_df[state_df["status"].astype(str).isin(ACTIVE_POSITION_STATUSES)].copy()
    closed_df = state_df[state_df["status"].astype(str) == CLOSED_POSITION_STATUS].copy()

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
        ],
        columns=PORTFOLIO_EQUITY_HISTORY_FILE_SCHEMA.canonical_column_order[:11],
    )

    if os.path.exists(EQUITY_HISTORY_PATH):
        history_df = pd.read_csv(EQUITY_HISTORY_PATH)
        history_df = pd.concat([history_df, snapshot], ignore_index=True)
    else:
        history_df = snapshot.copy()

    history_df = apply_drawdown_metrics(history_df)
    history_df = validate_portfolio_equity_history(history_df, keep_extra_columns=False)
    snapshot = history_df.tail(1).copy()
    performance_summary_df = build_performance_summary(history_df)
    performance_summary_df = validate_performance_summary(performance_summary_df, keep_extra_columns=False)

    write_managed_csv_with_schema(
        history_df,
        EQUITY_HISTORY_PATH,
        schema=PORTFOLIO_EQUITY_HISTORY_SCHEMA,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    write_managed_csv_with_schema(
        performance_summary_df,
        PERFORMANCE_SUMMARY_PATH,
        schema=PERFORMANCE_SUMMARY_SCHEMA,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    upsert_portfolio_equity_history_row(snapshot.iloc[0].to_dict())
    emit_portfolio_equity_snapshot_event(run_id=run_id, snapshot_row=snapshot.iloc[0])

    print("Portfolio Equity Agent finished.")
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
