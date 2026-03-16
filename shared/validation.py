# shared/validation.py

from __future__ import annotations

from typing import Iterable

import pandas as pd

from shared.schemas import (
    ALLOWED_POSITION_SIDES,
    ALLOWED_POSITION_STATUSES,
    PORTFOLIO_STATE_COLUMNS,
    PORTFOLIO_STATE_DEFAULTS,
    PORTFOLIO_STATE_NUMERIC_COLUMNS,
    REQUIRED_PORTFOLIO_STATE_COLUMNS,
)


def ensure_columns(df: pd.DataFrame, required_columns: Iterable[str], df_name: str) -> None:
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"{df_name} is missing required columns: {missing}")


def add_missing_columns_with_defaults(df: pd.DataFrame, all_columns: Iterable[str], defaults: dict) -> pd.DataFrame:
    output = df.copy()
    for col in all_columns:
        if col not in output.columns:
            output[col] = defaults.get(col, "")
    return output


def coerce_numeric_columns(df: pd.DataFrame, numeric_columns: Iterable[str], df_name: str) -> pd.DataFrame:
    output = df.copy()
    for col in numeric_columns:
        if col in output.columns:
            output[col] = pd.to_numeric(output[col], errors="coerce")
    return output


def validate_portfolio_state(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return add_missing_columns_with_defaults(
            df=df,
            all_columns=PORTFOLIO_STATE_COLUMNS,
            defaults=PORTFOLIO_STATE_DEFAULTS,
        )[PORTFOLIO_STATE_COLUMNS]

    output = df.copy()

    output = add_missing_columns_with_defaults(
        df=output,
        all_columns=PORTFOLIO_STATE_COLUMNS,
        defaults=PORTFOLIO_STATE_DEFAULTS,
    )

    ensure_columns(
        df=output,
        required_columns=REQUIRED_PORTFOLIO_STATE_COLUMNS,
        df_name="portfolio_state",
    )

    output = coerce_numeric_columns(
        df=output,
        numeric_columns=PORTFOLIO_STATE_NUMERIC_COLUMNS,
        df_name="portfolio_state",
    )

    if output["position_id"].isna().any() or (output["position_id"].astype(str).str.strip() == "").any():
        raise ValueError("portfolio_state contains blank position_id values")

    if output["ticker"].isna().any() or (output["ticker"].astype(str).str.strip() == "").any():
        raise ValueError("portfolio_state contains blank ticker values")

    if not output["side"].astype(str).isin(ALLOWED_POSITION_SIDES).all():
        bad_values = sorted(output.loc[~output["side"].astype(str).isin(ALLOWED_POSITION_SIDES), "side"].astype(str).unique())
        raise ValueError(f"portfolio_state contains invalid side values: {bad_values}")

    if not output["status"].astype(str).isin(ALLOWED_POSITION_STATUSES).all():
        bad_values = sorted(output.loc[~output["status"].astype(str).isin(ALLOWED_POSITION_STATUSES), "status"].astype(str).unique())
        raise ValueError(f"portfolio_state contains invalid status values: {bad_values}")

    if output["quantity"].isna().any():
        raise ValueError("portfolio_state contains non-numeric quantity values")

    if (output["quantity"] < 0).any():
        raise ValueError("portfolio_state contains negative quantity values")

    open_like = output["status"].astype(str).isin({"open", "exit_required"})
    if open_like.any():
        for col in ["average_entry_price", "stop_loss", "take_profit", "capital_allocated"]:
            if output.loc[open_like, col].isna().any():
                raise ValueError(f"portfolio_state has open or exit_required rows with invalid {col}")

    duplicate_open_ids = output.loc[open_like, "position_id"].duplicated()
    if duplicate_open_ids.any():
        raise ValueError("portfolio_state contains duplicate active position_id values")

    return output[PORTFOLIO_STATE_COLUMNS]