from __future__ import annotations

import pandas as pd

from shared.portfolio_state_helpers import ACTIVE_POSITION_STATUSES
from shared.schemas import validate_portfolio_monitor, validate_portfolio_state


MARK_TO_MARKET_COLUMNS = [
    "current_price",
    "market_value",
    "pnl_abs",
    "pnl_pct",
    "highest_price_since_entry",
    "lowest_price_since_entry",
]

_CONTEXT_COLUMNS = [
    "ticker",
    "side",
    "status",
    "quantity",
    "entry_price",
]


def _normalised_scalar(value: object) -> object:
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if not pd.isna(numeric):
        return round(float(numeric), 10)
    if pd.isna(value):
        return None
    return str(value).strip().lower()


def merge_authoritative_monitor(
    state_df: pd.DataFrame,
    monitor_df: pd.DataFrame,
) -> pd.DataFrame:
    """Join current marks onto Fill-owned state without changing economic fields."""
    state = validate_portfolio_state(state_df, keep_extra_columns=False)
    monitor = validate_portfolio_monitor(monitor_df, keep_extra_columns=False)

    if state["position_id"].duplicated().any():
        duplicates = sorted(
            state.loc[state["position_id"].duplicated(keep=False), "position_id"]
            .astype(str)
            .unique()
        )
        raise ValueError(f"portfolio_state.csv has duplicate position_id values: {duplicates}")

    if monitor["position_id"].duplicated().any():
        duplicates = sorted(
            monitor.loc[monitor["position_id"].duplicated(keep=False), "position_id"]
            .astype(str)
            .unique()
        )
        raise ValueError(f"portfolio_monitor.csv has duplicate position_id values: {duplicates}")

    active_state = state[
        state["status"].astype(str).isin(ACTIVE_POSITION_STATUSES)
    ].copy()
    active_ids = set(active_state["position_id"].astype(str))
    monitor_ids = set(monitor["position_id"].astype(str))

    if active_ids != monitor_ids:
        missing = sorted(active_ids - monitor_ids)
        unexpected = sorted(monitor_ids - active_ids)
        raise ValueError(
            "portfolio_monitor.csv does not exactly cover active canonical positions: "
            f"missing={missing}, unexpected={unexpected}"
        )

    state_by_id = active_state.set_index("position_id", drop=False)
    for _, monitor_row in monitor.iterrows():
        position_id = str(monitor_row["position_id"])
        state_row = state_by_id.loc[position_id]
        for column in _CONTEXT_COLUMNS:
            if _normalised_scalar(state_row.get(column)) != _normalised_scalar(
                monitor_row.get(column)
            ):
                raise ValueError(
                    "portfolio_monitor.csv contradicts Fill-owned state for "
                    f"position_id={position_id}, field={column}"
                )

    merged = state.copy()
    for column in MARK_TO_MARKET_COLUMNS:
        merged[column] = pd.NA

    monitor_by_id = monitor.set_index("position_id", drop=False)
    for index, state_row in merged.iterrows():
        position_id = str(state_row["position_id"])
        if position_id not in monitor_by_id.index:
            continue
        monitor_row = monitor_by_id.loc[position_id]
        for column in MARK_TO_MARKET_COLUMNS:
            merged.at[index, column] = monitor_row[column]

    return merged
