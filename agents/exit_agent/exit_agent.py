from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from shared.io_utils import (
    load_yaml,
    normalise_columns,
    parse_bool,
    read_csv_required,
    safe_float,
    safe_str,
    write_csv,
)
from shared.paths import config_path, data_path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_alert_lookup(alerts: pd.DataFrame) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}

    if alerts.empty or "ticker" not in alerts.columns:
        return lookup

    for _, row in alerts.iterrows():
        ticker = safe_str(row.get("ticker")).upper()
        if not ticker:
            continue
        lookup[ticker] = {col: row.get(col) for col in alerts.columns}

    return lookup


def choose_exit_action(
    current_price: float,
    stop_loss: float,
    take_profit: float,
    unrealised_pnl: float,
    alert_row: dict[str, Any] | None
) -> tuple[str, str, str]:
    if current_price > 0 and take_profit > 0 and current_price >= take_profit:
        return "take_profit", "high", "Current price is at or above take profit"

    if current_price > 0 and stop_loss > 0 and current_price <= stop_loss:
        return "close", "high", "Current price is at or below stop loss"

    if alert_row:
        exit_required = parse_bool(alert_row.get("exit_required", False))
        alert_type = safe_str(alert_row.get("alert_type")).lower()
        alert_message = safe_str(alert_row.get("alert_message"))
        severity = safe_str(alert_row.get("severity")).lower()

        if exit_required:
            return "review_immediately", "high", alert_message or "Position alert requires exit review"

        if alert_type in {"take_profit", "target_hit"}:
            return "take_profit", "high", alert_message or "Position alert indicates take profit"

        if alert_type in {"stop_loss", "stop_hit"}:
            return "close", "high", alert_message or "Position alert indicates stop loss"

        if alert_type in {"trail_stop", "raise_stop"}:
            return "raise_stop", "medium", alert_message or "Position alert indicates stop should be raised"

        if severity in {"high", "critical"}:
            return "review_immediately", "high", alert_message or "High-severity position alert"

    if unrealised_pnl > 0:
        return "hold", "low", "Position is open and within normal range"

    return "hold", "low", "No exit condition triggered"


def run() -> None:
    governance = load_yaml(config_path("governance.yaml"))

    if governance.get("execution_mode") != "advisory_only":
        raise ValueError("Governance breach: execution_mode must be advisory_only")

    portfolio_state = read_csv_required(data_path("portfolio_state.csv"))
    position_alerts = read_csv_required(data_path("position_alerts.csv"))

    portfolio_state = normalise_columns(portfolio_state)
    position_alerts = normalise_columns(position_alerts)

    required_portfolio_cols = {"ticker"}
    if not required_portfolio_cols.issubset(set(portfolio_state.columns)):
        raise ValueError("portfolio_state.csv must contain at least: ticker")

    alert_lookup = build_alert_lookup(position_alerts)

    output_rows: list[dict[str, Any]] = []

    for _, row in portfolio_state.iterrows():
        ticker = safe_str(row.get("ticker")).upper()
        if not ticker:
            continue

        status = safe_str(row.get("status")).lower()
        quantity = safe_float(row.get("quantity"))
        current_price = safe_float(row.get("current_price"))
        avg_price = safe_float(row.get("average_price"))
        unrealised_pnl = safe_float(row.get("unrealised_pnl"))

        if status != "open" or quantity <= 0:
            continue

        stop_loss = safe_float(row.get("stop_loss"))
        take_profit = safe_float(row.get("take_profit"))

        if stop_loss <= 0 and avg_price > 0:
            default_stop_loss_pct = safe_float(governance.get("default_stop_loss_pct", 5.0))
            stop_loss = round(avg_price * (1 - default_stop_loss_pct / 100.0), 4)

        if take_profit <= 0 and avg_price > 0:
            default_take_profit_pct = safe_float(governance.get("default_take_profit_pct", 12.0))
            take_profit = round(avg_price * (1 + default_take_profit_pct / 100.0), 4)

        alert_row = alert_lookup.get(ticker)

        exit_action, urgency, exit_reason = choose_exit_action(
            current_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            unrealised_pnl=unrealised_pnl,
            alert_row=alert_row,
        )

        suggested_exit_price = current_price if current_price > 0 else 0.0
        suggested_stop_update = ""
        suggested_take_profit_update = ""

        if exit_action == "raise_stop" and current_price > 0:
            suggested_stop_update = round(current_price * 0.98, 4)

        output_rows.append(
            {
                "ticker": ticker,
                "current_position": quantity,
                "exit_action": exit_action,
                "exit_reason": exit_reason,
                "suggested_exit_price": round(suggested_exit_price, 4) if suggested_exit_price else 0.0,
                "suggested_stop_update": suggested_stop_update,
                "suggested_take_profit_update": suggested_take_profit_update,
                "urgency": urgency,
                "manual_review_required": True,
                "exit_generated_at": utc_now_iso(),
            }
        )

    out_df = pd.DataFrame(output_rows)
    out_path = data_path("exit_advice.csv")
    write_csv(out_df, out_path)

    hold_count = int((out_df["exit_action"] == "hold").sum()) if not out_df.empty else 0
    take_profit_count = int((out_df["exit_action"] == "take_profit").sum()) if not out_df.empty else 0
    close_count = int((out_df["exit_action"] == "close").sum()) if not out_df.empty else 0
    review_count = int((out_df["exit_action"] == "review_immediately").sum()) if not out_df.empty else 0
    raise_stop_count = int((out_df["exit_action"] == "raise_stop").sum()) if not out_df.empty else 0

    print("Exit Agent finished.")
    print(f"Saved exit advice to: {out_path}")
    print()
    print("Run summary:")
    print(f"Total exit advice rows: {len(out_df)}")
    print(f"Hold: {hold_count}")
    print(f"Take profit: {take_profit_count}")
    print(f"Close: {close_count}")
    print(f"Review immediately: {review_count}")
    print(f"Raise stop: {raise_stop_count}")


if __name__ == "__main__":
    run()