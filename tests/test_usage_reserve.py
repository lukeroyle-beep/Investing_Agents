"""Synthetic usage admission; never consumes credits or resets allowance."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.usage_reserve import UsageAdmission, UsageEvidenceError, UsageWindow

D = Decimal
NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def admission(hour="50", week="50", **kwargs):
    return UsageAdmission(NOW, (UsageWindow("five_hour", None if hour is None else D(hour), True),
                                UsageWindow("weekly", None if week is None else D(week))),
                          ("five_hour", "weekly"), "synthetic-usage-tool", "a" * 64, **kwargs)


@pytest.mark.parametrize("remaining,submit,work", [("25", True, True), ("24.999", False, True),
                                                   ("20.001", False, True), ("20", False, False),
                                                   ("0", False, False)])
def test_submission_preserves_recovery_reserve_and_work_stops_at_floor(remaining, submit, work):
    value = admission(week=remaining)
    assert (not value.validate_for_submission(NOW)) is submit
    assert (not value.validate_for_work(NOW)) is work
    assert value.minimum_remaining_percent == D(remaining)


@pytest.mark.parametrize("age", [-1, 61])
def test_stale_or_future_usage_cannot_authorize_work_or_submission(age):
    value = admission()
    at = NOW + timedelta(seconds=age)
    assert "usage_snapshot_stale_or_future" in value.validate_for_submission(at)
    assert "usage_snapshot_stale_or_future" in value.validate_for_work(at)


def test_exact_sixty_second_boundary_and_every_available_quota_window():
    value = admission()
    assert not value.validate_for_submission(NOW + timedelta(seconds=60))
    extra = replace(value, windows=(*value.windows, UsageWindow("extra_model", D("24"))),
                    available_window_ids=(*value.available_window_ids, "extra_model"))
    assert extra.minimum_remaining_percent == D("24")
    assert extra.validate_for_submission(NOW) == ("usage_recovery_reserve_insufficient",)


@pytest.mark.parametrize("value", [admission(hour=None), admission(week=None),
                                    replace(admission(), windows=(UsageWindow("weekly", D("90")),)),
                                    replace(admission(), windows=())])
def test_unknown_or_missing_core_allowance_fails_closed(value):
    assert value.minimum_remaining_percent is None
    assert value.validate_for_submission(NOW)
    assert value.validate_for_work(NOW)


@pytest.mark.parametrize("changes", [{"floor_percent": D("19.99")}, {"recovery_reserve_percent": D("4.99")},
                                      {"floor_percent": D("99")}, {"floor_percent": 20.0}])
def test_user_floor_and_recovery_reserve_cannot_be_relaxed(changes):
    with pytest.raises(UsageEvidenceError):
        admission(**changes)


def test_higher_floor_and_reserve_are_supported_without_bypasses():
    value = admission(week="30", floor_percent=D("25"), recovery_reserve_percent=D("10"))
    assert value.required_submission_percent == D("35")
    assert value.validate_for_submission(NOW)
    assert not value.validate_for_work(NOW)


@pytest.mark.parametrize("value", [D("NaN"), D("Infinity"), D("-1"), D("101"), 50.0])
def test_malformed_allowance_is_never_treated_as_available(value):
    with pytest.raises(UsageEvidenceError):
        UsageWindow("weekly", value)


def test_duplicate_windows_and_naive_time_are_rejected():
    with pytest.raises(UsageEvidenceError, match="duplicate"):
        replace(admission(), windows=(UsageWindow("weekly", D("80")), UsageWindow("weekly", D("10"))))
    with pytest.raises(UsageEvidenceError, match="timezone"):
        replace(admission(), observed_at=NOW.replace(tzinfo=None))


def test_source_weekly_only_primary_does_not_invent_missing_five_hour_bucket():
    value = UsageAdmission(NOW, (UsageWindow("weekly", D("53"), True),),
                           ("weekly",), "synthetic-usage-tool", "a" * 64)
    assert value.minimum_remaining_percent == D("53")
    assert not value.validate_for_submission(NOW)


def test_every_available_window_and_provenance_must_be_retained():
    omitted = replace(admission(), windows=(UsageWindow("five_hour", D("90"), True),))
    assert "available_usage_window_coverage_mismatch" in omitted.validate_for_submission(NOW)
    with pytest.raises(UsageEvidenceError, match="source_provenance"):
        replace(admission(), source_sha256="unbound")


def test_unknown_available_secondary_cannot_be_assumed_unlimited():
    value = replace(admission(), windows=(*admission().windows, UsageWindow("extra", None)),
                    available_window_ids=(*admission().available_window_ids, "extra"))
    assert "usage_window_allowance_unknown" in value.validate_for_submission(NOW)
