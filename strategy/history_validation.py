"""Strict observed-input checks, without claiming exchange-calendar coverage."""
from __future__ import annotations

from datetime import date, datetime
from math import isfinite

import pandas as pd


def session_dates(values, label: str) -> pd.DatetimeIndex:
    """Read explicit daily labels; timezone/session conversion belongs upstream."""
    parsed = []
    for value in values:
        try:
            if isinstance(value, str):
                value = date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
            if not isinstance(value, (date, datetime, pd.Timestamp)):
                raise ValueError("not a date label")
            stamp = pd.Timestamp(value)
            if pd.isna(stamp) or stamp.tzinfo is not None or stamp != stamp.normalize():
                raise ValueError("not a naive midnight date label")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{label}: dates must be explicit naive midnight daily labels") from exc
        parsed.append(stamp)
    result = pd.DatetimeIndex(parsed)
    if result.empty or result.has_duplicates or not result.is_monotonic_increasing:
        raise ValueError(f"{label}: daily labels must be nonempty, unique and chronological")
    return result


def _symbols(config, key):
    values = config.get(key)
    if not isinstance(values, list) or not values or any(
        not isinstance(value, str) or not value or value != value.strip().upper()
        for value in values
    ) or len(values) != len(set(values)):
        raise ValueError(f"{key}: configured symbols must be nonempty, unique and normalized")
    return set(values)


def _requested_date(value, label):
    if value is None or value == "":
        return None
    return session_dates([value], label)[0].date().isoformat()


def audit_histories(histories, benchmarks, config) -> dict:
    """Require every configured instrument and valid, aligned observed bars.

    Matching observed dates cannot detect a session missing from every source.
    This audit deliberately grants no calendar or corporate-action qualification.
    """
    expected = {"strategy": _symbols(config, "tickers"),
                "benchmark": _symbols(config, "benchmark_tickers")}
    if expected["strategy"] & expected["benchmark"]:
        raise ValueError("configured strategy and benchmark symbols must be disjoint")
    records = []
    reference_dates = None
    for role, frames in (("strategy", histories), ("benchmark", benchmarks)):
        if set(frames) != expected[role]:
            missing = sorted(expected[role] - set(frames))
            extra = sorted(set(frames) - expected[role])
            raise ValueError(f"{role} membership mismatch: missing={missing}, unexpected={extra}")
        for ticker, history in sorted(frames.items()):
            label = f"{role} {ticker}"
            if not history.columns.is_unique or not {"Date", "Open", "High", "Low", "Close"}.issubset(history.columns):
                raise ValueError(f"{label}: complete daily OHLC columns are required")
            dates = session_dates(history["Date"], label)
            if any(isinstance(value, bool) for column in ("Open", "High", "Low", "Close")
                   for value in history[column].tolist()):
                raise ValueError(f"{label}: OHLC prices must not be booleans")
            try:
                prices = history[["Open", "High", "Low", "Close"]].astype(float)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{label}: OHLC prices must be finite and positive") from exc
            if not all(isfinite(value) and value > 0 for value in prices.to_numpy().flat):
                raise ValueError(f"{label}: OHLC prices must be finite and positive")
            if ((prices["High"] < prices[["Open", "Close", "Low"]].max(axis=1)) |
                    (prices["Low"] > prices[["Open", "Close", "High"]].min(axis=1))).any():
                raise ValueError(f"{label}: OHLC ranges are inconsistent")
            if reference_dates is None:
                reference_dates = dates
            elif not dates.equals(reference_dates):
                missing = reference_dates.difference(dates).strftime("%Y-%m-%d").tolist()
                extra = dates.difference(reference_dates).strftime("%Y-%m-%d").tolist()
                raise ValueError(f"{label}: observed-session coverage mismatch: missing={missing}, unexpected={extra}")
            records.append({"role": role, "ticker": ticker, "sessions": len(dates),
                            "start": dates[0].date().isoformat(), "end": dates[-1].date().isoformat()})
    requested = {"start": _requested_date(config.get("start_date"), "requested start"),
                 "end": _requested_date(config.get("end_date"), "requested end")}
    if requested["start"] and requested["end"] and requested["start"] > requested["end"]:
        raise ValueError("requested historical range must be ordered")
    return {
        "result": "pass", "strategy_symbols": sorted(expected["strategy"]),
        "benchmark_symbols": sorted(expected["benchmark"]), "instruments": records,
        "requested_range": requested,
        "actual_range": {"start": reference_dates[0].date().isoformat(),
                         "end": reference_dates[-1].date().isoformat()},
        "range_policy": "replay_all_supplied_sessions; requested_range_is_recorded_not_enforced",
        "date_semantics": "naive_midnight_daily_labels; no_timezone_conversion",
        "observed_sessions_aligned": True, "exchange_calendar_qualified": False,
        "corporate_action_treatment_qualified": False,
        "limitations": ["sessions_missing_from_every_series_cannot_be_detected_without_an_independent_calendar",
                        "identical_observed_dates_do_not_establish_exchange_sessions_or_price_adjustment_semantics"],
    }
