from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import yaml

from execution.domain import DomainValidationError, canonical_json
from execution.instruments import Instrument
from execution.market_session import (
    APPROVED_CALENDAR_CHECKSUM,
    DEFAULT_CALENDAR_PATH,
    PILOT_SYMBOLS,
    assess_pilot_session,
)


@pytest.fixture
def instrument():
    return Instrument.create(
        canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity", currency="USD"
    )


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-21T13:29:59", False),
    ("2026-10-21T13:30:00", True),
    ("2026-10-21T19:59:59", True),
    ("2026-10-21T20:00:00", False),
    ("2026-10-24T16:00:00", False),
    ("2026-10-25T16:00:00", False),
])
def test_regular_session_boundaries_and_weekends(instrument, timestamp, expected):
    evidence = assess_pilot_session(instrument, now=at(timestamp))
    assert evidence.scheduled_regular_session_open is expected
    assert evidence.calendar_checksum == APPROVED_CALENDAR_CHECKSUM
    if timestamp[:10] in {"2026-10-24", "2026-10-25"}:
        assert evidence.reasons == ("weekend",)
        assert evidence.opens_at is evidence.closes_at is None


@pytest.mark.parametrize("holiday", [
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
])
def test_full_holidays(instrument, holiday):
    evidence = assess_pilot_session(instrument, now=at(f"{holiday}T16:00:00"))
    assert not evidence.scheduled_regular_session_open
    assert evidence.reasons == ("full_holiday",)
    assert evidence.opens_at is evidence.closes_at is None


@pytest.mark.parametrize("day", ["2026-11-27", "2026-12-24"])
@pytest.mark.parametrize("clock,expected", [("17:59:59", True), ("18:00:00", False)])
def test_early_close_is_exclusive(instrument, day, clock, expected):
    evidence = assess_pilot_session(instrument, now=at(f"{day}T{clock}"))
    assert evidence.scheduled_regular_session_open is expected
    assert evidence.opens_at == at(f"{day}T14:30:00")
    assert evidence.closes_at == at(f"{day}T18:00:00")
    assert "early_close" in evidence.reasons


@pytest.mark.parametrize("day,utc_open,uk_open,uk_close", [
    ("2026-03-09", "13:30", "13:30", "20:00"),
    ("2026-03-30", "13:30", "14:30", "21:00"),
    ("2026-10-26", "13:30", "13:30", "20:00"),
    ("2026-11-02", "14:30", "14:30", "21:00"),
])
def test_us_uk_dst_mismatch_weeks(instrument, day, utc_open, uk_open, uk_close):
    evidence = assess_pilot_session(instrument, now=at(f"{day}T{utc_open}:00"))
    assert evidence.scheduled_regular_session_open
    assert evidence.opens_at == evidence.assessed_at
    assert evidence.opens_at.astimezone(ZoneInfo("Europe/London")).strftime("%H:%M") == uk_open
    assert evidence.closes_at.astimezone(ZoneInfo("Europe/London")).strftime("%H:%M") == uk_close


@pytest.mark.parametrize("changes,reason", [
    ({"exchange": "NYSE"}, "unsupported_exchange"),
    ({"asset_type": "cfd"}, "unsupported_asset_type"),
    ({"currency": "GBP"}, "unsupported_currency"),
    ({"canonical_symbol": "GOOG"}, "instrument_outside_pilot"),
])
def test_wrong_instrument_fails_closed(instrument, changes, reason):
    evidence = assess_pilot_session(replace(instrument, **changes), now=at("2026-10-21T15:00:00"))
    assert not evidence.scheduled_regular_session_open
    assert evidence.reasons == (reason,)
    assert evidence.opens_at is evidence.closes_at is None


@pytest.mark.parametrize("day", ["2025-10-21", "2027-10-21"])
def test_no_annual_extrapolation(instrument, day):
    evidence = assess_pilot_session(instrument, now=at(f"{day}T15:00:00"))
    assert not evidence.scheduled_regular_session_open
    assert evidence.reasons == ("outside_calendar_coverage",)


def test_requires_exact_identity_and_aware_clock(instrument):
    with pytest.raises(DomainValidationError, match="timezone-aware"):
        assess_pilot_session(instrument, now=datetime(2026, 10, 21, 15))
    with pytest.raises(DomainValidationError, match="exact Instrument"):
        assess_pilot_session("AAPL", now=at("2026-10-21T15:00:00"))


@pytest.mark.parametrize("symbol", sorted(PILOT_SYMBOLS))
def test_evidence_is_immutable_bound_and_not_order_authority(instrument, symbol):
    instrument = replace(instrument, canonical_symbol=symbol)
    now = datetime(2026, 10, 21, 16, tzinfo=ZoneInfo("Europe/London"))
    evidence = assess_pilot_session(instrument, now=now)
    assert evidence.scheduled_regular_session_open
    for name in ("internal_instrument_id", "canonical_symbol", "exchange", "asset_type", "currency"):
        assert getattr(evidence, name) == getattr(instrument, name)
    assert evidence.assessed_at == at("2026-10-21T15:00:00")
    assert evidence.session_date == "2026-10-21"
    assert not evidence.submission_available
    assert not evidence.broker_permission_checked
    assert not evidence.market_halts_checked
    assert '"assessed_at":"2026-10-21T15:00:00Z"' in canonical_json(evidence)
    with pytest.raises(FrozenInstanceError):
        evidence.scheduled_regular_session_open = False


@pytest.mark.parametrize("mutation", ["overlap", "missing_holiday", "weekend", "year", "metadata", "hours"])
def test_malformed_or_changed_snapshot_fails_closed(instrument, tmp_path, mutation):
    config = yaml.safe_load(DEFAULT_CALENDAR_PATH.read_bytes())
    if mutation == "overlap":
        config["early_closes"]["2026-12-25"] = "13:00"
    elif mutation == "missing_holiday":
        config["full_holidays"].remove("2026-07-03")
    elif mutation == "weekend":
        config["full_holidays"].append("2026-07-04")
    elif mutation == "year":
        config["full_holidays"].append("2027-01-01")
    elif mutation == "metadata":
        config["sources"] = []
    else:
        config["early_closes"]["2026-12-24"] = "18:00"
    path = tmp_path / "calendar.yaml"
    path.write_text(yaml.safe_dump(config))
    evidence = assess_pilot_session(instrument, now=at("2026-10-21T15:00:00"), calendar_path=path)
    assert not evidence.scheduled_regular_session_open
    assert evidence.reasons == ("calendar_invalid",)


@pytest.mark.parametrize("content", [b"[broken:", b"\xff", b"[]", b"", b"full_holidays: null"])
def test_unparseable_config_fails_closed(instrument, tmp_path, content):
    path = tmp_path / "calendar.yaml"
    path.write_bytes(content)
    evidence = assess_pilot_session(instrument, now=at("2026-10-21T15:00:00"), calendar_path=path)
    assert not evidence.scheduled_regular_session_open
    assert evidence.reasons == ("calendar_invalid",)


def test_missing_snapshot_fails_closed(instrument, tmp_path):
    evidence = assess_pilot_session(instrument, now=at("2026-10-21T15:00:00"), calendar_path=tmp_path / "missing")
    assert not evidence.scheduled_regular_session_open
    assert evidence.reasons == ("calendar_unreadable",)
    assert evidence.calendar_checksum is None
