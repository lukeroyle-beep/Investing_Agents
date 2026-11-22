"""NYSE regular-session gate for the host scheduler.

The decision clock is America/New_York. US and UK civil clocks change on
different dates, so a fixed Europe/London window is wrong for part of March
and part of October. This module never reads London wall time.

Session membership, holidays, and early closes come from
``shared.freshness.ExchangeCalendar`` (XNYS). That calendar models the close;
the regular cash open used here is 09:30 America/New_York, matching the
reviewed Nasdaq session snapshot. The open interval is ``[09:30, close)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from shared.freshness import ExchangeCalendar


NEW_YORK = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

REASON_OPEN = "open"
REASON_WEEKEND = "weekend"
REASON_HOLIDAY = "holiday"
REASON_BEFORE_OPEN = "before_open"
REASON_AFTER_CLOSE = "after_close"
REASON_EARLY_CLOSE = "early_close"


class MarketHoursError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MarketDecision:
    is_open: bool
    reason: str
    session_date: str
    local_time: datetime
    opens_at: datetime | None = None
    closes_at: datetime | None = None


def assess_nyse_session(
    now: datetime,
    calendar: ExchangeCalendar | None = None,
) -> MarketDecision:
    """Return whether ``now`` falls inside the NYSE regular session.

    ``now`` must be timezone-aware. It is converted to America/New_York before
    any comparison. A calendar whose zone is not America/New_York is rejected.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise MarketHoursError("now must be a timezone-aware datetime")
    active = calendar or ExchangeCalendar()
    if active.timezone != "America/New_York":
        raise MarketHoursError("NYSE gate requires the America/New_York calendar")
    local = now.astimezone(NEW_YORK)
    session = local.date()
    closed = MarketDecision(
        is_open=False,
        reason=REASON_WEEKEND,
        session_date=session.isoformat(),
        local_time=local,
    )
    if session.weekday() >= 5:
        return closed
    if not active.is_session(session):
        return MarketDecision(
            is_open=False,
            reason=REASON_HOLIDAY,
            session_date=session.isoformat(),
            local_time=local,
        )
    opens_at = datetime.combine(session, REGULAR_OPEN, NEW_YORK)
    closes_at = active.session_close(session)
    if closes_at.tzinfo is None or closes_at.utcoffset() is None:
        raise MarketHoursError("session close must be timezone-aware")
    if local < opens_at:
        reason = REASON_BEFORE_OPEN
        is_open = False
    elif local >= closes_at:
        is_open = False
        reason = REASON_EARLY_CLOSE if closes_at.time() < REGULAR_CLOSE else REASON_AFTER_CLOSE
    else:
        is_open = True
        reason = REASON_OPEN
    return MarketDecision(
        is_open=is_open,
        reason=reason,
        session_date=session.isoformat(),
        local_time=local,
        opens_at=opens_at,
        closes_at=closes_at,
    )
