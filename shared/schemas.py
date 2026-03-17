from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class SchemaSpec:
    name: str
    required_columns: list[str]
    optional_columns: list[str] = field(default_factory=list)
    numeric_columns: list[str] = field(default_factory=list)
    text_columns: list[str] = field(default_factory=list)
    datetime_columns: list[str] = field(default_factory=list)
    default_values: dict[str, Any] = field(default_factory=dict)
    allowed_values: dict[str, set[str]] = field(default_factory=dict)
    uppercase_columns: list[str] = field(default_factory=list)
    lowercase_columns: list[str] = field(default_factory=list)
    column_order: list[str] = field(default_factory=list)
    alias_columns: dict[str, list[str]] = field(default_factory=dict)


ALLOWED_SIDE_VALUES = {"long", "short", "buy", "sell"}
ALLOWED_POSITION_STATUS_VALUES = {
    "open",
    "exit_required",
    "closing",
    "closed",
}
ALLOWED_ADVICE_STATUS_VALUES = {
    "ready_for_manual_review",
    "hold_for_review",
    "blocked",
}
ALLOWED_EXIT_ACTION_VALUES = {
    "hold",
    "take_profit",
    "close",
    "review_immediately",
    "raise_stop",
}
ALLOWED_REVIEW_STATUS_VALUES = {
    "open",
    "reviewed",
    "accepted",
    "rejected",
    "filled",
    "closed",
}


ADVISORY_TRADES_SCHEMA = SchemaSpec(
    name="advisory_trades",
    required_columns=[
        "ticker",
        "action",
        "direction",
        "entry_zone_low",
        "entry_zone_high",
        "suggested_size_pct",
        "suggested_size_cash",
        "stop_loss",
        "take_profit",
        "risk_reward_ratio",
        "estimated_cash_risk",
        "time_in_force_note",
        "manual_review_required",
        "advice_status",
        "advice_notes",
        "advice_generated_at",
        "run_id",
    ],
    numeric_columns=[
        "entry_zone_low",
        "entry_zone_high",
        "suggested_size_pct",
        "suggested_size_cash",
        "stop_loss",
        "take_profit",
        "risk_reward_ratio",
        "estimated_cash_risk",
    ],
    text_columns=[
        "ticker",
        "action",
        "direction",
        "time_in_force_note",
        "advice_status",
        "advice_notes",
        "advice_generated_at",
        "run_id",
    ],
    uppercase_columns=["ticker"],
    lowercase_columns=["direction", "advice_status"],
    allowed_values={"advice_status": ALLOWED_ADVICE_STATUS_VALUES},
    default_values={
        "action": "review_trade",
        "direction": "long",
        "time_in_force_note": "Manual entry only",
        "manual_review_required": True,
        "advice_status": "hold_for_review",
        "advice_notes": "",
    },
    column_order=[
        "ticker",
        "action",
        "direction",
        "entry_zone_low",
        "entry_zone_high",
        "suggested_size_pct",
        "suggested_size_cash",
        "stop_loss",
        "take_profit",
        "risk_reward_ratio",
        "estimated_cash_risk",
        "time_in_force_note",
        "manual_review_required",
        "advice_status",
        "advice_notes",
        "advice_generated_at",
        "run_id",
    ],
)

PORTFOLIO_STATE_SCHEMA = SchemaSpec(
    name="portfolio_state",
    required_columns=[
        "position_id",
        "ticker",
        "side",
        "status",
        "entry_date",
        "entry_price",
        "quantity",
        "capital_allocated",
        "stop_loss",
        "take_profit",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "regime_at_entry",
        "sector",
        "signal_score",
        "highest_price_since_entry",
        "lowest_price_since_entry",
        "exit_reason",
        "last_updated_at",
        "run_id",
    ],
    numeric_columns=[
        "entry_price",
        "quantity",
        "capital_allocated",
        "stop_loss",
        "take_profit",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "signal_score",
        "highest_price_since_entry",
        "lowest_price_since_entry",
    ],
    text_columns=[
        "position_id",
        "ticker",
        "side",
        "status",
        "entry_date",
        "regime_at_entry",
        "sector",
        "exit_reason",
        "last_updated_at",
        "run_id",
    ],
    uppercase_columns=["ticker"],
    lowercase_columns=["side", "status", "exit_reason"],
    allowed_values={
        "side": ALLOWED_SIDE_VALUES,
        "status": ALLOWED_POSITION_STATUS_VALUES,
    },
    default_values={
        "exit_reason": "",
        "run_id": "",
    },
    alias_columns={
        "entry_price": ["average_entry_price"],
        "pnl_abs": ["unrealised_pnl_abs"],
        "pnl_pct": ["unrealised_pnl_pct"],
    },
    column_order=[
        "position_id",
        "ticker",
        "side",
        "status",
        "entry_date",
        "entry_price",
        "quantity",
        "capital_allocated",
        "stop_loss",
        "take_profit",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "regime_at_entry",
        "sector",
        "signal_score",
        "highest_price_since_entry",
        "lowest_price_since_entry",
        "exit_reason",
        "last_updated_at",
        "run_id",
    ],
)

PORTFOLIO_MONITOR_SCHEMA = SchemaSpec(
    name="portfolio_monitor",
    required_columns=PORTFOLIO_STATE_SCHEMA.required_columns,
    numeric_columns=PORTFOLIO_STATE_SCHEMA.numeric_columns,
    text_columns=PORTFOLIO_STATE_SCHEMA.text_columns,
    uppercase_columns=PORTFOLIO_STATE_SCHEMA.uppercase_columns,
    lowercase_columns=PORTFOLIO_STATE_SCHEMA.lowercase_columns,
    allowed_values=PORTFOLIO_STATE_SCHEMA.allowed_values,
    default_values=PORTFOLIO_STATE_SCHEMA.default_values,
    alias_columns=PORTFOLIO_STATE_SCHEMA.alias_columns,
    column_order=PORTFOLIO_STATE_SCHEMA.column_order,
)

POSITION_ALERTS_SCHEMA = SchemaSpec(
    name="position_alerts",
    required_columns=[
        "position_id",
        "ticker",
        "alert_type",
        "message",
        "generated_at",
        "run_id",
    ],
    text_columns=[
        "position_id",
        "ticker",
        "alert_type",
        "message",
        "generated_at",
        "run_id",
    ],
    uppercase_columns=["ticker"],
    lowercase_columns=["alert_type"],
    default_values={"message": ""},
    column_order=[
        "position_id",
        "ticker",
        "alert_type",
        "message",
        "generated_at",
        "run_id",
    ],
)

EXIT_ADVICE_SCHEMA = SchemaSpec(
    name="exit_advice",
    required_columns=[
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
    ],
    numeric_columns=[
        "current_price",
        "stop_loss",
        "take_profit",
        "pnl_abs",
        "pnl_pct",
    ],
    text_columns=[
        "position_id",
        "ticker",
        "exit_action",
        "reason",
        "status",
        "exit_reason",
        "generated_at",
        "run_id",
    ],
    uppercase_columns=["ticker"],
    lowercase_columns=["exit_action", "status", "exit_reason"],
    allowed_values={
        "exit_action": ALLOWED_EXIT_ACTION_VALUES,
        "status": ALLOWED_POSITION_STATUS_VALUES,
    },
    default_values={
        "reason": "",
        "exit_reason": "",
    },
    column_order=[
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
    ],
)

PROCESSED_FILLS_SCHEMA = SchemaSpec(
    name="processed_fills",
    required_columns=["fill_id", "processed_at", "run_id"],
    text_columns=["fill_id", "processed_at", "run_id"],
    default_values={
        "processed_at": "",
        "run_id": "",
    },
    column_order=["fill_id", "processed_at", "run_id"],
)

TRADE_JOURNAL_SCHEMA = SchemaSpec(
    name="trade_journal",
    required_columns=[
        "ticker",
        "name",
        "market_regime",
        "adjusted_setup_score",
        "adjusted_setup_status",
        "risk_decision",
        "risk_notes",
        "run_id",
        "journaled_at",
        "review_status",
        "user_action",
        "outcome",
        "notes",
    ],
    numeric_columns=["adjusted_setup_score"],
    text_columns=[
        "ticker",
        "name",
        "market_regime",
        "adjusted_setup_status",
        "risk_decision",
        "risk_notes",
        "run_id",
        "journaled_at",
        "review_status",
        "user_action",
        "outcome",
        "notes",
    ],
    uppercase_columns=["ticker"],
    lowercase_columns=[
        "adjusted_setup_status",
        "risk_decision",
        "review_status",
    ],
    allowed_values={"review_status": ALLOWED_REVIEW_STATUS_VALUES},
    default_values={
        "risk_notes": "",
        "user_action": "",
        "outcome": "",
        "notes": "",
    },
    column_order=[
        "ticker",
        "name",
        "market_regime",
        "adjusted_setup_score",
        "adjusted_setup_status",
        "risk_decision",
        "risk_notes",
        "run_id",
        "journaled_at",
        "review_status",
        "user_action",
        "outcome",
        "notes",
    ],
)


def _apply_aliases(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for canonical_col, aliases in spec.alias_columns.items():
        if canonical_col not in output_df.columns:
            output_df[canonical_col] = pd.NA

        for alias in aliases:
            if alias in output_df.columns:
                output_df[canonical_col] = output_df[canonical_col].combine_first(output_df[alias])

    return output_df


def _ensure_columns(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for col in spec.required_columns + spec.optional_columns:
        if col not in output_df.columns:
            default = spec.default_values.get(col, pd.NA)
            output_df[col] = default

    return output_df


def _apply_defaults(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for col, default in spec.default_values.items():
        if col in output_df.columns:
            output_df[col] = output_df[col].fillna(default)

    return output_df


def _coerce_numeric(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for col in spec.numeric_columns:
        if col in output_df.columns:
            output_df[col] = pd.to_numeric(output_df[col], errors="coerce")

    return output_df


def _coerce_text(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for col in spec.text_columns:
        if col in output_df.columns:
            output_df[col] = output_df[col].fillna("").astype(str).str.strip()

    return output_df


def _normalise_case(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for col in spec.uppercase_columns:
        if col in output_df.columns:
            output_df[col] = output_df[col].astype(str).str.strip().str.upper()

    for col in spec.lowercase_columns:
        if col in output_df.columns:
            output_df[col] = output_df[col].astype(str).str.strip().str.lower()

    return output_df


def _validate_allowed_values(df: pd.DataFrame, spec: SchemaSpec) -> pd.DataFrame:
    output_df = df.copy()

    for col, allowed in spec.allowed_values.items():
        if col not in output_df.columns:
            continue

        series = output_df[col].fillna("").astype(str).str.strip()
        invalid_mask = (series != "") & (~series.isin(allowed))

        if invalid_mask.any():
            invalid_values = sorted(series[invalid_mask].unique().tolist())
            raise ValueError(
                f"{spec.name}: invalid values in column '{col}': {invalid_values}. "
                f"Allowed values: {sorted(allowed)}"
            )

    return output_df


def _order_columns(df: pd.DataFrame, spec: SchemaSpec, keep_extra_columns: bool) -> pd.DataFrame:
    output_df = df.copy()

    if not spec.column_order:
        return output_df

    ordered = [c for c in spec.column_order if c in output_df.columns]

    if keep_extra_columns:
        extras = [c for c in output_df.columns if c not in ordered]
        return output_df[ordered + extras].copy()

    return output_df[ordered].copy()


def normalise_to_schema(
    df: pd.DataFrame,
    spec: SchemaSpec,
    keep_extra_columns: bool = True,
) -> pd.DataFrame:
    output_df = df.copy()
    output_df = _apply_aliases(output_df, spec)
    output_df = _ensure_columns(output_df, spec)
    output_df = _apply_defaults(output_df, spec)
    output_df = _coerce_numeric(output_df, spec)
    output_df = _coerce_text(output_df, spec)
    output_df = _normalise_case(output_df, spec)
    output_df = _validate_allowed_values(output_df, spec)
    output_df = _order_columns(output_df, spec, keep_extra_columns=keep_extra_columns)

    return output_df


def validate_advisory_trades(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, ADVISORY_TRADES_SCHEMA, keep_extra_columns=keep_extra_columns)


def validate_portfolio_state(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, PORTFOLIO_STATE_SCHEMA, keep_extra_columns=keep_extra_columns)


def validate_portfolio_monitor(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, PORTFOLIO_MONITOR_SCHEMA, keep_extra_columns=keep_extra_columns)


def validate_position_alerts(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, POSITION_ALERTS_SCHEMA, keep_extra_columns=keep_extra_columns)


def validate_exit_advice(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, EXIT_ADVICE_SCHEMA, keep_extra_columns=keep_extra_columns)


def validate_processed_fills(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, PROCESSED_FILLS_SCHEMA, keep_extra_columns=keep_extra_columns)


def validate_trade_journal(df: pd.DataFrame, keep_extra_columns: bool = True) -> pd.DataFrame:
    return normalise_to_schema(df, TRADE_JOURNAL_SCHEMA, keep_extra_columns=keep_extra_columns)