from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from agents.shared.event_log import append_artifact_written_event
from shared.io_utils import (
    load_yaml,
    normalise_columns,
    read_csv_optional,
    read_csv_required,
    safe_float,
    write_csv_with_run_id,
)
from shared.paths import ADVISORY_TRADES_PATH, config_path, data_path
from shared.run_context import get_or_create_run_id
from shared.schemas import validate_advisory_trades

AGENT_NAME = "Advisory Agent"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_entry_zone(entry_price: float, buffer_pct: float) -> tuple[float, float]:
    half_buffer = buffer_pct / 100.0
    low = entry_price * (1.0 - half_buffer)
    high = entry_price * (1.0 + half_buffer)
    return round(low, 4), round(high, 4)


def calculate_cash_risk(capital_allocated: float, stop_loss_pct: float) -> float:
    return round(capital_allocated * (stop_loss_pct / 100.0), 2)


def calculate_rr(entry_price: float, stop_loss_price: float, take_profit_price: float) -> float:
    risk = entry_price - stop_loss_price
    reward = take_profit_price - entry_price
    if risk <= 0:
        return 0.0
    return round(reward / risk, 2)


def determine_news_block(
    has_news: Any,
    news_severity: Any,
    governance: dict[str, Any],
) -> tuple[bool, str]:
    high_news_severity_block = bool(governance.get("high_news_severity_block", True))
    has_news_bool = str(has_news).strip().lower() in {"true", "1", "yes", "y"}
    severity = str(news_severity).strip().lower()

    if has_news_bool and high_news_severity_block and severity in {"high", "critical"}:
        return True, f"Blocked due to news severity: {severity}"

    if has_news_bool and severity in {"medium"}:
        return False, f"Hold for review due to news severity: {severity}"

    return False, ""


def determine_open_position_block(
    ticker: str,
    portfolio_state: pd.DataFrame,
    governance: dict[str, Any],
) -> tuple[bool, str]:
    if not bool(governance.get("block_if_open_position_exists", True)):
        return False, ""

    if portfolio_state.empty:
        return False, ""

    state = normalise_columns(portfolio_state.copy())

    required_cols = {"ticker", "status"}
    if not required_cols.issubset(set(state.columns)):
        return False, ""

    open_rows = state[
        (state["ticker"].astype(str).str.upper() == ticker.upper())
        & (state["status"].astype(str).str.lower() == "open")
    ]

    if not open_rows.empty:
        return True, "Blocked because ticker already has an open position"

    return False, ""


def run() -> None:
    run_id = get_or_create_run_id()
    print(f"Run ID: {run_id}")

    governance = load_yaml(config_path("governance.yaml"))

    if governance.get("execution_mode") != "advisory_only":
        raise ValueError("Governance breach: execution_mode must be advisory_only")

    if bool(governance.get("allow_order_submission", False)):
        raise ValueError("Governance breach: allow_order_submission must be false")

    recommendations = read_csv_required(data_path("portfolio_recommendations.csv"))
    portfolio_state = read_csv_optional(data_path("portfolio_state.csv"))
    news_review = read_csv_optional(data_path("news_review.csv"))

    recommendations = normalise_columns(recommendations)
    portfolio_state = normalise_columns(portfolio_state)
    news_review = normalise_columns(news_review)

    news_lookup: dict[str, dict[str, Any]] = {}
    if not news_review.empty and "ticker" in news_review.columns:
        for _, row in news_review.iterrows():
            news_lookup[str(row.get("ticker", "")).upper()] = {
                "has_news": row.get("has_news", False),
                "news_severity": row.get("news_severity", ""),
                "news_pass": row.get("news_pass", True),
                "news_notes": row.get("news_notes", ""),
            }

    blocked_tickers = {str(x).upper() for x in governance.get("blocked_tickers", [])}
    blocked_asset_types = {str(x).lower() for x in governance.get("blocked_asset_types", [])}
    max_position_pct = safe_float(governance.get("max_position_pct", 10.0), 10.0)
    default_entry_buffer_pct = safe_float(governance.get("default_entry_buffer_pct", 0.5), 0.5)

    output_rows: list[dict[str, Any]] = []

    for _, row in recommendations.iterrows():
        ticker = str(row.get("ticker", "")).upper().strip()
        direction = str(row.get("direction", "long")).strip().lower()
        asset_type = str(row.get("asset_type", "")).strip().lower()

        entry_price = safe_float(row.get("entry_price"))
        suggested_size_pct = min(safe_float(row.get("position_size_pct")), max_position_pct)
        capital_allocated = safe_float(row.get("capital_allocated"))
        stop_loss_price = safe_float(row.get("stop_loss_price"))
        take_profit_price = safe_float(row.get("take_profit_price"))
        recommendation_status = str(row.get("recommendation_status", "candidate")).strip().lower()
        recommendation_notes = str(row.get("recommendation_notes", "")).strip()

        if not ticker:
            continue

        advice_status = "ready_for_manual_review"
        advice_notes: list[str] = []
        manual_review_required = True

        if recommendation_status not in {"candidate", "approved", "ready", "ready_for_review"}:
            advice_status = "hold_for_review"
            advice_notes.append(f"Recommendation status is {recommendation_status}")

        if ticker in blocked_tickers:
            advice_status = "blocked"
            advice_notes.append("Ticker is blocked by governance")

        if asset_type and asset_type in blocked_asset_types:
            advice_status = "blocked"
            advice_notes.append("Asset type is blocked by governance")

        open_block, open_note = determine_open_position_block(ticker, portfolio_state, governance)
        if open_block:
            advice_status = "blocked"
            advice_notes.append(open_note)

        news = news_lookup.get(ticker, {})
        news_block, news_note = determine_news_block(
            news.get("has_news", False),
            news.get("news_severity", ""),
            governance,
        )
        if news_block:
            advice_status = "blocked"
            advice_notes.append(news_note)
        elif news_note and advice_status != "blocked":
            advice_status = "hold_for_review"
            advice_notes.append(news_note)

        if entry_price <= 0:
            advice_status = "blocked"
            advice_notes.append("Missing or invalid entry price")

        if stop_loss_price <= 0:
            advice_status = "blocked"
            advice_notes.append("Missing or invalid stop loss price")

        if take_profit_price <= 0:
            advice_status = "blocked"
            advice_notes.append("Missing or invalid take profit price")

        if capital_allocated <= 0:
            advice_status = "blocked"
            advice_notes.append("Missing or invalid capital allocation")

        if suggested_size_pct <= 0:
            advice_status = "blocked"
            advice_notes.append("Missing or invalid position size percent")

        entry_zone_low, entry_zone_high = build_entry_zone(entry_price, default_entry_buffer_pct)
        stop_loss_pct = ((entry_price - stop_loss_price) / entry_price) * 100 if entry_price > 0 else 0
        cash_risk = calculate_cash_risk(capital_allocated, stop_loss_pct)
        risk_reward_ratio = calculate_rr(entry_price, stop_loss_price, take_profit_price)

        output_rows.append(
            {
                "ticker": ticker,
                "action": "review_trade",
                "direction": direction,
                "entry_zone_low": entry_zone_low,
                "entry_zone_high": entry_zone_high,
                "suggested_size_pct": round(suggested_size_pct, 2),
                "suggested_size_cash": round(capital_allocated, 2),
                "stop_loss": round(stop_loss_price, 4),
                "take_profit": round(take_profit_price, 4),
                "risk_reward_ratio": risk_reward_ratio,
                "estimated_cash_risk": cash_risk,
                "time_in_force_note": "Manual entry only",
                "manual_review_required": manual_review_required,
                "advice_status": advice_status,
                "advice_notes": " | ".join(filter(None, [recommendation_notes] + advice_notes)).strip(),
                "advice_generated_at": utc_now_iso(),
                "run_id": run_id,
            }
        )

    out_df = pd.DataFrame(output_rows)
    out_df = validate_advisory_trades(out_df)

    write_csv_with_run_id(
        out_df,
        ADVISORY_TRADES_PATH,
        run_id=run_id,
    )

    ready_count = int((out_df["advice_status"] == "ready_for_manual_review").sum()) if not out_df.empty else 0
    hold_count = int((out_df["advice_status"] == "hold_for_review").sum()) if not out_df.empty else 0
    blocked_count = int((out_df["advice_status"] == "blocked").sum()) if not out_df.empty else 0
    append_artifact_written_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        entity_type="advisory",
        entity_id="advisory_trades",
        message="Advisory trade output generated.",
        details={
            "output_path": str(ADVISORY_TRADES_PATH),
            "row_count": len(out_df),
            "ready_for_manual_review_count": ready_count,
            "hold_for_review_count": hold_count,
            "blocked_count": blocked_count,
        },
    )

    print("Advisory Agent finished.")
    print(f"Saved trade advice to: {ADVISORY_TRADES_PATH}")
    print()
    print("Run summary:")
    print(f"Run ID: {run_id}")
    print(f"Total trade advice rows: {len(out_df)}")
    print(f"Ready for manual review: {ready_count}")
    print(f"Hold for review: {hold_count}")
    print(f"Blocked: {blocked_count}")


if __name__ == "__main__":
    run()
