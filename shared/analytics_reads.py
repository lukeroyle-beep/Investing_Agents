from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

import shared.sqlite_sidecar as sqlite_sidecar


PREFER_SQLITE_ANALYTICS_READS = True


@dataclass(frozen=True)
class AnalyticsTableSpec:
    csv_path: Path
    table_name: str | None = None
    csv_read_kwargs: dict[str, Any] = field(default_factory=dict)
    sqlite_order_by: list[str] = field(default_factory=list)
    required: bool = False


def _read_csv_table(spec: AnalyticsTableSpec) -> pd.DataFrame:
    if not spec.csv_path.exists():
        if spec.required:
            raise FileNotFoundError(f"Missing required analytics CSV: {spec.csv_path}")
        return pd.DataFrame()
    return pd.read_csv(spec.csv_path, **spec.csv_read_kwargs)


def _read_sqlite_table(spec: AnalyticsTableSpec) -> pd.DataFrame | None:
    if not PREFER_SQLITE_ANALYTICS_READS or not spec.table_name or not sqlite_sidecar.SQLITE_DB_PATH.exists():
        return None

    try:
        return sqlite_sidecar.fetch_table_df(spec.table_name, order_by=spec.sqlite_order_by)
    except Exception as exc:
        warnings.warn(
            f"SQLite analytics read fallback for {spec.table_name}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return None


def read_analytics_table(spec: AnalyticsTableSpec) -> pd.DataFrame:
    sqlite_df = _read_sqlite_table(spec)
    if sqlite_df is not None:
        if not sqlite_df.empty or not spec.csv_path.exists():
            return sqlite_df

    return _read_csv_table(spec)
