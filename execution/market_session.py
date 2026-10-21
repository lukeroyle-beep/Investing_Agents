"""Read-only scheduled-session evidence, never order or market-halt authority.

Only the reviewed Nasdaq 2026 annual snapshot and six USD pilot equities are
covered. No session evidence is extrapolated into another year or exchange.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import yaml

from execution.domain import DomainValidationError, _aware_utc
from execution.instruments import Instrument


DEFAULT_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "config/market_sessions/nasdaq_2026.yaml"
APPROVED_CALENDAR_CHECKSUM = "cbd9a72351473b52ef79f370210ffde7e1f17d66467f3107867c54027b4cad3c"
PILOT_SYMBOLS = frozenset({"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"})
_NEW_YORK = ZoneInfo("America/New_York")
_CALENDAR_ID = "nasdaq_regular_2026_verified_2026-10-21"
_SOURCES = [
    "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule",
    "https://www.nasdaqtrader.com/trader.aspx?id=Calendar",
]


@dataclass(frozen=True, slots=True)
class PilotSessionEvidence:
    internal_instrument_id: UUID
    canonical_symbol: str
    exchange: str
    asset_type: str
    currency: str
    assessed_at: datetime
    calendar_checksum: str | None
    session_date: str
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    scheduled_regular_session_open: bool = False
    reasons: tuple[str, ...] = ()
    calendar_id: str | None = None
    submission_available: bool = field(default=False, init=False)
    broker_permission_checked: bool = field(default=False, init=False)
    market_halts_checked: bool = field(default=False, init=False)


def _validate_calendar(raw: bytes) -> tuple[set[date], dict[date, time]]:
    config = yaml.safe_load(raw)
    pinned = {
        "schema_version": 1,
        "calendar_id": _CALENDAR_ID,
        "exchange": "NASDAQ",
        "year": 2026,
        "timezone": "America/New_York",
        "coverage_start": "2026-01-01",
        "coverage_end": "2026-12-31",
        "verified_on": "2026-10-21",
        "sources": _SOURCES,
        "regular_open": "09:30",
        "regular_close": "16:00",
    }
    if not isinstance(config, dict) or set(config) != set(pinned) | {"full_holidays", "early_closes"}:
        raise ValueError("calendar schema is invalid")
    if any(config[key] != expected for key, expected in pinned.items()):
        raise ValueError("calendar metadata does not match the reviewed snapshot")
    if not isinstance(config["full_holidays"], list) or not isinstance(config["early_closes"], dict):
        raise ValueError("calendar exceptions must be explicit")
    holidays = {date.fromisoformat(item) for item in config["full_holidays"]}
    early = {date.fromisoformat(day): time.fromisoformat(close) for day, close in config["early_closes"].items()}
    if len(holidays) != len(config["full_holidays"]) or holidays & early.keys():
        raise ValueError("duplicate or conflicting calendar exceptions")
    if any(day.year != 2026 or day.weekday() >= 5 for day in holidays | early.keys()):
        raise ValueError("calendar exception is outside weekday/year coverage")
    if any(close.tzinfo is not None or not time(9, 30) < close < time(16) for close in early.values()):
        raise ValueError("early close must fall within the regular session")
    if hashlib.sha256(raw).hexdigest() != APPROVED_CALENDAR_CHECKSUM:
        raise ValueError("calendar content is not the approved annual snapshot")
    return holidays, early


def assess_pilot_session(
    instrument: Instrument,
    *,
    now: datetime,
    calendar_path: str | Path | None = None,
) -> PilotSessionEvidence:
    """Assess [open, close) using exact instrument identity and an aware clock.

    Invalid identity/time argument types raise DomainValidationError. Unsupported
    identities, coverage gaps and invalid calendar files yield closed evidence.
    A custom path must contain the exact approved snapshot; this is not a generic
    calendar override. Even open evidence says nothing about broker permission,
    unusual closures, current halts, or whether an order may be submitted.
    """
    if not isinstance(instrument, Instrument):
        raise DomainValidationError("session assessment requires an exact Instrument")
    if not isinstance(now, datetime):
        raise DomainValidationError("now must be a timezone-aware datetime")
    assessed_at = _aware_utc(now, "now")
    session_day = assessed_at.astimezone(_NEW_YORK).date()
    evidence = PilotSessionEvidence(
        internal_instrument_id=instrument.internal_instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        exchange=instrument.exchange,
        asset_type=instrument.asset_type,
        currency=instrument.currency,
        assessed_at=assessed_at,
        calendar_checksum=None,
        session_date=session_day.isoformat(),
    )
    try:
        raw = Path(calendar_path or DEFAULT_CALENDAR_PATH).read_bytes()
    except (OSError, TypeError, ValueError):
        return replace(evidence, reasons=("calendar_unreadable",))
    evidence = replace(evidence, calendar_checksum=hashlib.sha256(raw).hexdigest())
    try:
        holidays, early = _validate_calendar(raw)
    except (yaml.YAMLError, UnicodeError, TypeError, ValueError, OverflowError):
        return replace(evidence, reasons=("calendar_invalid",))
    evidence = replace(evidence, calendar_id=_CALENDAR_ID)
    reasons = []
    if instrument.exchange != "NASDAQ":
        reasons.append("unsupported_exchange")
    if instrument.asset_type != "equity":
        reasons.append("unsupported_asset_type")
    if instrument.currency != "USD":
        reasons.append("unsupported_currency")
    if instrument.canonical_symbol not in PILOT_SYMBOLS:
        reasons.append("instrument_outside_pilot")
    if session_day.year != 2026:
        reasons.append("outside_calendar_coverage")
    if reasons:
        return replace(evidence, reasons=tuple(reasons))
    if session_day.weekday() >= 5:
        return replace(evidence, reasons=("weekend",))
    if session_day in holidays:
        return replace(evidence, reasons=("full_holiday",))
    opens_at = datetime.combine(session_day, time(9, 30), _NEW_YORK).astimezone(timezone.utc)
    closes_at = datetime.combine(session_day, early.get(session_day, time(16)), _NEW_YORK).astimezone(timezone.utc)
    is_open = opens_at <= assessed_at < closes_at
    reason = "scheduled_regular_session_open" if is_open else "outside_regular_session"
    return replace(
        evidence,
        opens_at=opens_at,
        closes_at=closes_at,
        scheduled_regular_session_open=is_open,
        reasons=(reason, "early_close") if session_day in early else (reason,),
    )
