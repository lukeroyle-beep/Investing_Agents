from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from shared.market_data import HEALTH_COLUMNS
from shared.freshness import assess_freshness
from shared.paths import data_path

STATUS_OK = "OK"
STATUS_HOLD = "Hold"
STATUS_MISSING = "Missing"
STATUS_DEGRADED = "Degraded"
STATUS_NO_TRADE = "No Trade"


def _parse_bool(value: Any) -> bool:
    if value is None or pd.isna(value):
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def build_data_source_health_card(
    path: str | Path = data_path("data_source_health.csv"),
    *,
    now: datetime | None = None,
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
            "mode": "no_trade",
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
            "mode": "no_trade",
            "total_checks": len(df),
            "error_checks": 0,
            "stale_checks": 0,
            "affected_tickers": [],
            "message": f"data_source_health.csv missing columns: {missing_columns}",
        }

    error_mask = df["error"].astype(str).str.strip() != ""
    stale_mask = df["stale"].map(_parse_bool)
    evaluated_modes: list[str] = []
    resolved_now = now or datetime.now(UTC)
    for _, row in df.iterrows():
        try:
            assessment = assess_freshness(
                source=str(row.get("source", "")),
                data_kind=str(row.get("data_kind", "")),
                observation_time=row.get("observation_time", ""),
                retrieval_time=row.get("retrieval_time", ""),
                now=resolved_now,
                provider_error=row.get("error", ""),
            )
            mode = assessment.mode
        except Exception:
            mode = "no_trade"
        recorded_mode = str(row.get("mode", "")).strip().lower()
        if recorded_mode == "no_trade" or str(
            row.get("contradiction_status", "")
        ).strip().lower() == "material":
            mode = "no_trade"
        elif recorded_mode == "degraded" and mode == "normal":
            mode = "degraded"
        evaluated_modes.append(mode)
    modes = pd.Series(evaluated_modes, index=df.index)
    no_trade_mask = modes == "no_trade"
    degraded_mask = modes == "degraded"
    affected_tickers = sorted(
        set(
            df.loc[
                error_mask | stale_mask | no_trade_mask | degraded_mask,
                "ticker",
            ].astype(str).str.upper()
        )
    )

    error_checks = int(error_mask.sum())
    stale_checks = int(stale_mask.sum())
    if no_trade_mask.any() or error_checks or stale_checks:
        status = STATUS_NO_TRADE
        mode = "no_trade"
    elif degraded_mask.any():
        status = STATUS_DEGRADED
        mode = "degraded"
    else:
        status = STATUS_OK
        mode = "normal"

    if status == STATUS_OK:
        message = f"Market data healthy across {len(df)} provider checks."
    else:
        message = (
            f"Market data {mode}: {error_checks} error checks, "
            f"{stale_checks} stale checks across {len(affected_tickers)} tickers."
        )

    return {
        "status": status,
        "mode": mode,
        "total_checks": int(len(df)),
        "error_checks": error_checks,
        "stale_checks": stale_checks,
        "affected_tickers": affected_tickers,
        "message": message,
    }
