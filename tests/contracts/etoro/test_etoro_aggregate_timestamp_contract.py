"""Synthetic offline timestamp regressions; these do not qualify an account.

No offset-free interpretation is authorized. Tests exercise the existing Demo
aggregate parser and account gates without opening a socket or running a launcher.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from brokers.base import BrokerDataUnavailable
from brokers.etoro import account_evidence
from brokers.etoro.account_evidence import capture_pretrade_account_evidence
from brokers.etoro.schemas import EtoroPortfolioSnapshot, EtoroSchemaError
from tests.contracts.etoro.test_etoro_account_evidence import (
    NOW, _aggregate, _capture, _client, _detail, _page, _security,
)


@pytest.mark.parametrize("timestamp", [
    "2026-10-21T14:30:00Z",
    "2026-10-21T14:30:00+00:00",
    "2026-10-21T15:30:00+01:00",
    "2026-10-21T10:30:00-04:00",
    "2026-10-21T20:00:00+05:30",
    "2026-10-22T04:30:00+14:00",
])
def test_explicit_aggregate_offsets_normalize_to_the_same_utc_instant(timestamp):
    body = _aggregate()
    body["timestamp"] = timestamp
    snapshot = EtoroPortfolioSnapshot.parse(body)
    assert snapshot.observed_at == NOW
    assert snapshot.observed_at.tzinfo is timezone.utc


@pytest.mark.parametrize("timestamp,microseconds", [
    ("2026-10-21T14:30:00.1Z", 100000),
    ("2026-10-21T14:30:00.123456+00:00", 123456),
    ("2026-10-21T10:30:00.123456-04:00", 123456),
    ("2026-10-21T14:30:00.1234567Z", 123456),
])
def test_fractional_seconds_follow_existing_python_microsecond_precision(timestamp, microseconds):
    body = _aggregate()
    body["timestamp"] = timestamp
    original = deepcopy(body)
    snapshot = EtoroPortfolioSnapshot.parse(body)
    assert snapshot.observed_at == NOW.replace(microsecond=microseconds)
    assert body == original  # Parsing must not rewrite the source timestamp.


@pytest.mark.parametrize("timestamp", [
    "2026-10-21T14:30:00",
    "2026-10-21T14:30:00.1",
    "2026-10-21T14:30:00.1234567",
    "2026-10-21 14:30:00.123456",
    "2026-10-21",
])
def test_unsupported_offset_free_values_remain_ambiguous(timestamp):
    body = _aggregate()
    body["timestamp"] = timestamp
    with pytest.raises(EtoroSchemaError, match="^timestamp must include a timezone$"):
        EtoroPortfolioSnapshot.parse(body)


@pytest.mark.parametrize("timestamp", [
    None, "", "not-a-time", "2026-02-30T14:30:00Z",
    "2026-10-21T24:30:00Z", "2026-10-21T14:30:60Z",
    "2026-10-21T14:30:00+24:00", "2026-10-21T14:30:00+00:00Z",
    "2026-10-21T14:30:00 UTC", "2026-10-21T14:30:00 Europe/London",
])
def test_malformed_timestamp_and_unsupported_timezone_labels_are_rejected(timestamp):
    body = _aggregate()
    body["timestamp"] = timestamp
    with pytest.raises(EtoroSchemaError, match="^timestamp must be an ISO timestamp$"):
        EtoroPortfolioSnapshot.parse(body)


def test_timestamp_text_remains_bound_to_snapshot_identity_after_normalization():
    texts = (
        "2026-10-21T14:30:00.123456Z",
        "2026-10-21T14:30:00.123456+00:00",
        "2026-10-21T14:30:00.1234567Z",
    )
    snapshots = []
    for timestamp in texts:
        body = _aggregate()
        body["timestamp"] = timestamp
        snapshots.append(EtoroPortfolioSnapshot.parse(body))
        assert body["timestamp"] == timestamp
    assert len({item.observed_at for item in snapshots}) == 1
    assert len({item.snapshot_id for item in snapshots}) == len(texts)


@pytest.mark.parametrize("timestamp", [
    "2026-10-21T14:30:00.1234567", "invalid-timestamp",
])
def test_timestamp_rejection_stops_before_metadata_and_closing_snapshot(timestamp):
    aggregate = _aggregate()
    aggregate["timestamp"] = timestamp
    client, transport = _client([_detail(), aggregate, _page(_security()), _detail()])
    with pytest.raises(EtoroSchemaError):
        capture_pretrade_account_evidence(client, expected_demo_customer_id="9001", now=NOW)
    assert [request.method for request in transport.requests] == ["GET", "POST"]
    assert len(transport.responses) == 2


@pytest.mark.parametrize("age_microseconds,accepted", [
    (-1, False), (0, True), (1, True), (14999999, True),
    (15000000, True), (15000001, False),
])
def test_aggregate_freshness_has_closed_zero_to_fifteen_second_boundaries(age_microseconds, accepted):
    aggregate = _aggregate()
    aggregate["timestamp"] = (NOW - timedelta(microseconds=age_microseconds)).isoformat()
    if not accepted:
        with pytest.raises(BrokerDataUnavailable, match="^aggregate_account_snapshot_stale_or_future$"):
            _capture(aggregate=aggregate)
    else:
        evidence, _ = _capture(aggregate=aggregate)
        assert (NOW - evidence.snapshot.observed_at).total_seconds() == age_microseconds / 1000000
        assert evidence.daily_pnl is None and evidence.peak_equity is None
        assert evidence.snapshot.executions_complete is False


def _capture_with_clock(monkeypatch, *, started, completed, aggregate_timestamp):
    moments = iter((started, completed))

    class Clock:
        @staticmethod
        def now(zone):
            assert zone is timezone.utc
            return next(moments)

    monkeypatch.setattr(account_evidence, "datetime", Clock)
    aggregate = _aggregate()
    aggregate["timestamp"] = aggregate_timestamp
    client, transport = _client([_detail(), aggregate, _page(_security()), _detail()])
    evidence = capture_pretrade_account_evidence(client, expected_demo_customer_id="9001")
    return evidence, transport


@pytest.mark.parametrize("duration_microseconds,accepted", [
    (-1, False), (0, True), (14999999, True), (15000000, True), (15000001, False),
])
def test_capture_window_has_closed_zero_to_fifteen_second_boundaries(monkeypatch, duration_microseconds, accepted):
    completed = NOW + timedelta(microseconds=duration_microseconds)
    args = dict(started=NOW, completed=completed, aggregate_timestamp=completed.isoformat())
    if not accepted:
        with pytest.raises(BrokerDataUnavailable, match="^account_capture_timing_not_valid$"):
            _capture_with_clock(monkeypatch, **args)
    else:
        evidence, transport = _capture_with_clock(monkeypatch, **args)
        assert evidence.capture_started_at == NOW
        assert evidence.capture_completed_at == completed
        assert len(transport.requests) == 4


def test_freshness_is_measured_at_capture_completion(monkeypatch):
    # Fresh at opening, one microsecond too old by an otherwise valid close.
    started = NOW
    completed = NOW + timedelta(seconds=1)
    observed = NOW - timedelta(seconds=14, microseconds=1)
    with pytest.raises(BrokerDataUnavailable, match="^aggregate_account_snapshot_stale_or_future$"):
        _capture_with_clock(monkeypatch, started=started, completed=completed,
                            aggregate_timestamp=observed.isoformat())


@pytest.mark.parametrize("naive_clock", ["started", "completed"])
def test_naive_capture_clock_is_not_a_timezone_fallback(monkeypatch, naive_clock):
    moments = {"started": NOW, "completed": NOW}
    moments[naive_clock] = NOW.replace(tzinfo=None)
    with pytest.raises(BrokerDataUnavailable, match="^account_capture_time_requires_timezone$"):
        _capture_with_clock(monkeypatch, **moments, aggregate_timestamp=NOW.isoformat())
