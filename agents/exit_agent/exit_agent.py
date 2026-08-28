from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from pandas.errors import EmptyDataError

from agents.shared.event_log import append_exit_decision_generated_event
from shared.io_utils import write_csv
from shared.paths import DATA_DIR as RUNTIME_STATE_DIR
from shared.portfolio_monitor import merge_authoritative_monitor
from shared.portfolio_state_helpers import (
    ACTIVE_POSITION_STATUSES,
    VALID_POSITION_SIDES,
    VALID_POSITION_STATUSES,
    is_closed_position_status,
    normalise_position_status,
    parse_boolean_flag,
)
from shared.run_context import get_or_create_run_id
from shared.schemas import (
    validate_exit_advice,
    validate_portfolio_monitor,
    validate_portfolio_state,
)


DATA_DIR = str(RUNTIME_STATE_DIR)

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
MONITOR_PATH = os.path.join(DATA_DIR, "portfolio_monitor.csv")
EXIT_ADVICE_PATH = os.path.join(DATA_DIR, "exit_advice.csv")
AGENT_NAME = "Exit Agent"

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_read_csv(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")

    if os.path.getsize(path) == 0:
        raise ValueError(f"Required CSV is zero-byte empty: {path}")

    try:
        return pd.read_csv(path)
    except EmptyDataError as exc:
        raise ValueError(f"Required CSV has no parseable columns: {path}") from exc


def load_portfolio_state() -> pd.DataFrame:
    state_df = validate_portfolio_state(
        safe_read_csv(STATE_PATH),
        keep_extra_columns=False,
    )
    monitor_df = validate_portfolio_monitor(
        safe_read_csv(MONITOR_PATH),
        keep_extra_columns=False,
    )
    df = merge_authoritative_monitor(state_df, monitor_df)
    validate_state_for_exit_decisions(df)
    df["exit_flag"] = df["exit_flag"].apply(parse_boolean_flag)
    return df


def validate_state_for_exit_decisions(df: pd.DataFrame) -> None:
    if "position_id" not in df.columns:
        raise ValueError("portfolio_state.csv is missing position_id")

    if df["position_id"].duplicated().any():
        dupes = df.loc[df["position_id"].duplicated(keep=False), "position_id"].tolist()
        raise ValueError(f"Duplicate position_id values detected: {dupes}")

    invalid_status = sorted(
        set(df.loc[~df["status"].isin(VALID_POSITION_STATUSES), "status"].dropna().astype(str).tolist())
    )
    if invalid_status:
        raise ValueError(f"Invalid status values detected: {invalid_status}")

    invalid_side = sorted(
        set(df.loc[~df["side"].isin(VALID_POSITION_SIDES), "side"].dropna().astype(str).tolist())
    )
    if invalid_side:
        raise ValueError(f"Invalid side values detected: {invalid_side}")

    invalid_exit_flags: list[str] = []
    for value in df["exit_flag"].tolist():
        try:
            parse_boolean_flag(value)
        except ValueError:
            invalid_exit_flags.append(str(value))

    if invalid_exit_flags:
        raise ValueError(f"Invalid exit_flag values detected: {sorted(set(invalid_exit_flags))}")

    active_df = df[df["status"].isin(ACTIVE_POSITION_STATUSES)].copy()

    active_qty = pd.to_numeric(active_df["quantity"], errors="coerce")
    if active_qty.isna().any() or (active_qty <= 0).any():
        bad_ids = active_df.loc[active_qty.isna() | (active_qty <= 0), "position_id"].tolist()
        raise ValueError(f"Invalid quantity for active positions: {bad_ids}")

    active_entry = pd.to_numeric(active_df["entry_price"], errors="coerce")
    if active_entry.isna().any() or (active_entry <= 0).any():
        bad_ids = active_df.loc[active_entry.isna() | (active_entry <= 0), "position_id"].tolist()
        raise ValueError(f"Invalid entry_price for active positions: {bad_ids}")


def exit_decision_for_row(row: pd.Series) -> dict[str, Any]:
    position_id = str(row["position_id"])
    ticker = str(row["ticker"])
    status = normalise_position_status(row["status"])

    if is_closed_position_status(status):
        return {
            "position_id": position_id,
            "ticker": ticker,
            "exit_action": "hold",
            "reason": "Position already closed.",
            "status": "no_action",
            "exit_reason": "already_closed",
            "current_price": row.get("current_price"),
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "pnl_abs": row.get("pnl_abs"),
            "pnl_pct": row.get("pnl_pct"),
            "generated_at": utc_now_iso(),
            "run_id": "",
        }

    current_price = pd.to_numeric(pd.Series([row.get("current_price")]), errors="coerce").iloc[0]
    stop_loss = pd.to_numeric(pd.Series([row.get("stop_loss")]), errors="coerce").iloc[0]
    take_profit = pd.to_numeric(pd.Series([row.get("take_profit")]), errors="coerce").iloc[0]
    pnl_abs = pd.to_numeric(pd.Series([row.get("pnl_abs")]), errors="coerce").iloc[0]
    pnl_pct = pd.to_numeric(pd.Series([row.get("pnl_pct")]), errors="coerce").iloc[0]
    side = str(row["side"]).strip().lower()
    exit_flag = parse_boolean_flag(row.get("exit_flag"))
    existing_exit_reason = str(row.get("exit_reason") if not pd.isna(row.get("exit_reason")) else "").strip()

    if exit_flag:
        return {
            "position_id": position_id,
            "ticker": ticker,
            "exit_action": "review",
            "reason": "Position is flagged for exit.",
            "status": "exit_required",
            "exit_reason": existing_exit_reason or "exit_flagged",
            "current_price": current_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "pnl_abs": pnl_abs,
            "pnl_pct": pnl_pct,
            "generated_at": utc_now_iso(),
            "run_id": "",
        }

    if pd.isna(current_price):
        return {
            "position_id": position_id,
            "ticker": ticker,
            "exit_action": "review",
            "reason": "Current price missing.",
            "status": "review_required",
            "exit_reason": "missing_current_price",
            "current_price": current_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "pnl_abs": pnl_abs,
            "pnl_pct": pnl_pct,
            "generated_at": utc_now_iso(),
            "run_id": "",
        }

    if side == "long":
        if not pd.isna(take_profit) and current_price >= take_profit:
            return {
                "position_id": position_id,
                "ticker": ticker,
                "exit_action": "take_profit",
                "reason": "Take profit level has been reached.",
                "status": "exit_required",
                "exit_reason": "take_profit_triggered",
                "current_price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pnl_abs": pnl_abs,
                "pnl_pct": pnl_pct,
                "generated_at": utc_now_iso(),
                "run_id": "",
            }

        if not pd.isna(stop_loss) and current_price <= stop_loss:
            return {
                "position_id": position_id,
                "ticker": ticker,
                "exit_action": "close",
                "reason": "Stop loss level has been breached.",
                "status": "exit_required",
                "exit_reason": "stop_loss_triggered",
                "current_price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pnl_abs": pnl_abs,
                "pnl_pct": pnl_pct,
                "generated_at": utc_now_iso(),
                "run_id": "",
            }

    elif side == "short":
        if not pd.isna(take_profit) and current_price <= take_profit:
            return {
                "position_id": position_id,
                "ticker": ticker,
                "exit_action": "take_profit",
                "reason": "Take profit level has been reached.",
                "status": "exit_required",
                "exit_reason": "take_profit_triggered",
                "current_price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pnl_abs": pnl_abs,
                "pnl_pct": pnl_pct,
                "generated_at": utc_now_iso(),
                "run_id": "",
            }

        if not pd.isna(stop_loss) and current_price >= stop_loss:
            return {
                "position_id": position_id,
                "ticker": ticker,
                "exit_action": "close",
                "reason": "Stop loss level has been breached.",
                "status": "exit_required",
                "exit_reason": "stop_loss_triggered",
                "current_price": current_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pnl_abs": pnl_abs,
                "pnl_pct": pnl_pct,
                "generated_at": utc_now_iso(),
                "run_id": "",
            }

    return {
        "position_id": position_id,
        "ticker": ticker,
        "exit_action": "hold",
        "reason": "No exit condition met.",
        "status": "hold",
        "exit_reason": "",
        "current_price": current_price,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "pnl_abs": pnl_abs,
        "pnl_pct": pnl_pct,
        "generated_at": utc_now_iso(),
        "run_id": "",
    }


def emit_exit_decision_event(run_id: str, decision: dict[str, Any]) -> None:
    """
    Append one event-log row for an Exit Agent decision.
    """
    append_exit_decision_generated_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        position_id=str(decision.get("position_id", "")).strip() or "unknown_position",
        ticker=str(decision.get("ticker", "")).strip(),
        severity="info",
        message=f"Exit decision generated: {decision.get('exit_action', '')}",
        details={
            "exit_action": decision.get("exit_action"),
            "reason": decision.get("reason"),
            "status": decision.get("status"),
            "exit_reason": decision.get("exit_reason"),
            "current_price": decision.get("current_price"),
            "stop_loss": decision.get("stop_loss"),
            "take_profit": decision.get("take_profit"),
            "pnl_abs": decision.get("pnl_abs"),
            "pnl_pct": decision.get("pnl_pct"),
            "generated_at": decision.get("generated_at"),
        },
    )


def run_exit_agent() -> None:
    run_id = get_or_create_run_id()
    state_df = load_portfolio_state()

    if state_df.empty:
        out_df = pd.DataFrame(
            columns=[
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
        )
        out_df = validate_exit_advice(out_df, keep_extra_columns=False)
        write_csv(out_df, EXIT_ADVICE_PATH)
        print("Exit Agent finished.")
        print(f"Saved exit advice to: {EXIT_ADVICE_PATH}")
        print("")
        print("Run summary:")
        print("Total exit advice rows: 0")
        print("Hold: 0")
        print("Take profit: 0")
        print("Close: 0")
        print("Review immediately: 0")
        print("Raise stop: 0")
        return

    decisions = [exit_decision_for_row(row) for _, row in state_df.iterrows()]
    for decision in decisions:
        decision["run_id"] = run_id
    out_df = pd.DataFrame(decisions)
    out_df = validate_exit_advice(out_df, keep_extra_columns=False)
    write_csv(out_df, EXIT_ADVICE_PATH)
    for decision in decisions:
        emit_exit_decision_event(run_id=run_id, decision=decision)

    hold_count = int((out_df["exit_action"] == "hold").sum())
    take_profit_count = int((out_df["exit_action"] == "take_profit").sum())
    close_count = int((out_df["exit_action"] == "close").sum())
    review_count = int((out_df["exit_action"] == "review").sum())
    raise_stop_count = int((out_df["exit_action"] == "raise_stop").sum()) if "raise_stop" in out_df["exit_action"].values else 0

    print("Exit Agent finished.")
    print(f"Saved exit advice to: {EXIT_ADVICE_PATH}")
    print("")
    print("Run summary:")
    print(f"Total exit advice rows: {len(out_df)}")
    print(f"Hold: {hold_count}")
    print(f"Take profit: {take_profit_count}")
    print(f"Close: {close_count}")
    print(f"Review immediately: {review_count}")
    print(f"Raise stop: {raise_stop_count}")


if __name__ == "__main__":
    run_exit_agent()
