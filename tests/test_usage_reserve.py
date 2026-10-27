"""Synthetic usage admission; never consumes credits or resets allowance."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.usage_reserve import PilotUsageAdmission, UsageAdmission, UsageEvidenceError, UsageWindow

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


def pilot_admission(hour="50", week="50", **kwargs):
    options = {"floor_percent": D("10"),
               "authorization_reference": "Synthetic explicit Demo-pilot floor authorization"}
    options.update(kwargs)
    return PilotUsageAdmission(NOW,
        (UsageWindow("five_hour", None if hour is None else D(hour), True),
         UsageWindow("weekly", None if week is None else D(week))),
        ("five_hour", "weekly"), "synthetic-usage-tool", "a" * 64, **options)


@pytest.mark.parametrize("remaining,submit,work", [
    ("15", True, True), ("14.999", False, True),
    ("10.001", False, True), ("10", False, False), ("9.999", False, False)])
def test_explicit_pilot_floor_retains_five_percent_recovery_reserve(remaining, submit, work):
    value = pilot_admission(week=remaining)
    assert isinstance(value, UsageAdmission)
    assert value.required_submission_percent == D("15")
    assert (not value.validate_for_submission(NOW)) is submit
    assert (not value.validate_for_work(NOW)) is work


@pytest.mark.parametrize("remaining,submit,work", [
    ("10", True, True), ("9.999", False, True),
    ("5.001", False, True), ("5", False, False), ("4.999", False, False)])
def test_five_percent_floor_keeps_additional_recovery_reserve(remaining, submit, work):
    value = pilot_admission(week=remaining, floor_percent=D(5))
    assert value.required_submission_percent == D(10)
    assert (not value.validate_for_submission(NOW)) is submit
    assert (not value.validate_for_work(NOW)) is work


@pytest.mark.parametrize("reference", ["", " ", None, 42])
def test_lower_pilot_floor_requires_nonempty_authorization_reference(reference):
    with pytest.raises(UsageEvidenceError, match="authorization"):
        pilot_admission(authorization_reference=reference)


@pytest.mark.parametrize("changes", [
    {"floor_percent": D("4.999")}, {"recovery_reserve_percent": D("4.999")},
    {"floor_percent": D("99")}, {"floor_percent": 10.0},
])
def test_authorized_pilot_cannot_lower_minimum_or_spend_recovery_reserve(changes):
    with pytest.raises(UsageEvidenceError):
        pilot_admission(**changes)


def test_pilot_authorization_is_durable_data_and_defaults_remain_strict():
    pilot = pilot_admission()
    assert asdict(pilot)["authorization_reference"] == pilot.authorization_reference
    assert replace(pilot).authorization_reference == pilot.authorization_reference
    ordinary = admission()
    assert ordinary.floor_percent == D("20")
    assert ordinary.required_submission_percent == D("25")
    strict_default_pilot = PilotUsageAdmission(NOW, ordinary.windows,
        ordinary.available_window_ids, ordinary.source_reference, ordinary.source_sha256)
    assert strict_default_pilot.floor_percent == D("20")
    assert strict_default_pilot.required_submission_percent == D("25")
    with pytest.raises(UsageEvidenceError):
        admission(floor_percent=D("10"))


@pytest.mark.parametrize("age", [-1, 61])
def test_pilot_authorization_does_not_override_stale_or_future_evidence(age):
    value = pilot_admission()
    now = NOW + timedelta(seconds=age)
    assert "usage_snapshot_stale_or_future" in value.validate_for_submission(now)
    assert "usage_snapshot_stale_or_future" in value.validate_for_work(now)


@pytest.mark.parametrize("hour,week", [(None, "50"), ("50", None)])
def test_pilot_authorization_does_not_invent_unknown_allowance(hour, week):
    value = pilot_admission(hour=hour, week=week)
    assert value.minimum_remaining_percent is None
    assert "usage_window_allowance_unknown" in value.validate_for_submission(NOW)
    assert "usage_window_allowance_unknown" in value.validate_for_work(NOW)


def test_pilot_still_requires_every_available_window_and_primary_identity():
    original = pilot_admission()
    unknown_extra = replace(original, windows=(*original.windows, UsageWindow("extra", None)),
                            available_window_ids=(*original.available_window_ids, "extra"))
    assert "usage_window_allowance_unknown" in unknown_extra.validate_for_submission(NOW)
    omitted = replace(original, windows=(UsageWindow("weekly", D("50")),))
    assert "primary_usage_window_missing" in omitted.validate_for_submission(NOW)
    assert "available_usage_window_coverage_mismatch" in omitted.validate_for_submission(NOW)


@pytest.mark.parametrize("floor", [D("5"), D("10"), D("20")])
def test_strict_connector_rejects_scoped_pilot_type_before_any_send(tmp_path, monkeypatch, floor):
    from tests import test_etoro_connector_operator as strict
    from risk.submission_gate import SubmissionBlockedError

    items = strict.workflow(tmp_path)
    strict.approve(items)
    def scoped_admission(*args, **kwargs):
        return PilotUsageAdmission(*args, **kwargs, floor_percent=floor,
            authorization_reference="Synthetic explicit Demo-pilot floor authorization")
    monkeypatch.setattr(strict, "UsageAdmission", scoped_admission)
    with pytest.raises(SubmissionBlockedError, match="usage_floor"):
        strict.submit(items, usage_percent=D("50"))
    assert items[5]["place"] == 0


@pytest.mark.parametrize("remaining,can_send", [(D("15"), True), (D("14.999"), False)])
def test_pilot_connector_uses_authorized_floor_at_send_boundary(tmp_path, monkeypatch, remaining, can_send):
    from tests import test_demo_pilot_operator as pilot
    from execution.domain import CommandState
    from risk.submission_gate import SubmissionBlockedError

    fixture = pilot.workflow(tmp_path)
    pilot.approve(fixture)
    def scoped_admission(*args, **kwargs):
        return PilotUsageAdmission(*args, **kwargs, floor_percent=D("10"),
            authorization_reference="Synthetic explicit Demo-pilot floor authorization")
    monkeypatch.setattr(pilot, "UsageAdmission", scoped_admission)
    if can_send:
        assert pilot.submit(fixture, remaining=remaining).state == CommandState.ACKNOWLEDGED
        assert fixture.host.calls["place"] == 1
        persisted = fixture.op.evidence.read_observations(fixture.command.command_id)
        usage = next(row["usage"] for row in persisted if "usage" in row)
        assert usage["floor_percent"] == "10"
        assert usage["authorization_reference"] == "Synthetic explicit Demo-pilot floor authorization"
    else:
        with pytest.raises(SubmissionBlockedError, match="usage_floor"):
            pilot.submit(fixture, remaining=remaining)
        assert fixture.host.calls["place"] == 0
        assert not fixture.op.ledger.snapshot()["unresolved"]
