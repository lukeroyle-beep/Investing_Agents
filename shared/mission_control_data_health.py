from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from shared.market_data import HEALTH_COLUMNS

STATUS_OK = "OK"
STATUS_HOLD = "Hold"
STATUS_MISSING = "Missing"


def _parse_bool(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def build_data_source_health_card(
    path: str | Path = "data/data_source_health.csv",
) -> dict[str, Any]:
    """Build a compact Mission Control card from the provider-health artifact.

    Missing, error, or stale provider evidence is intentionally visible as a
    hold condition because advisory outputs must not silently trust partial or
    stale market data.
    """
    health_path = Path(path)
    if not health_path.exists():
        return {
            "status": STATUS_MISSING,
            "total_checks": 0,
            "error_checks": 0,
            "stale_checks": 0,
            "affected_tickers": [],
            "message": "No data_source_health.csv artifact found.",
        }

    df = pd.read_csv(health_path, keep_default_na=False)
    missing_columns = [column for column in HEALTH_COLUMNS if column not in df.columns]
    if missing_columns:
        return {
            "status": STATUS_HOLD,
            "total_checks": len(df),
            "error_checks": 0,
            "stale_checks": 0,
            "affected_tickers": [],
            "message": f"data_source_health.csv missing columns: {missing_columns}",
        }

    error_mask = df["error"].astype(str).str.strip() != ""
    stale_mask = df["stale"].map(_parse_bool)
    affected_tickers = sorted(
        set(df.loc[error_mask | stale_mask, "ticker"].astype(str).str.upper())
    )

    error_checks = int(error_mask.sum())
    stale_checks = int(stale_mask.sum())
    status = STATUS_HOLD if error_checks or stale_checks else STATUS_OK

    if status == STATUS_OK:
        message = f"Market data healthy across {len(df)} provider checks."
    else:
        message = (
            f"Market data hold: {error_checks} error checks, "
            f"{stale_checks} stale checks across {len(affected_tickers)} tickers."
        )

    return {
        "status": status,
        "total_checks": int(len(df)),
        "error_checks": error_checks,
        "stale_checks": stale_checks,
        "affected_tickers": affected_tickers,
        "message": message,
    }
