"""Versioned component clocks: source ages stay separate from collection."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from brokers.base import BrokerDataUnavailable
from brokers.etoro.account_evidence import (
    AccountComponentTimeEvidence, ComponentCashObservation,
    assess_pnl_component_time_evidence, qualify_pnl_component_time_evidence,
)
from execution.domain import payload_hash
from tests.contracts.etoro.test_etoro_account_evidence import NOW
from tests.contracts.etoro.test_etoro_account_evidence import (
    _aggregate, _detail, _capture as _capture_aggregate,
)
from tests.contracts.etoro.test_etoro_pnl_capture import _capture
from tests.contracts.etoro.test_etoro_pnl_schema import _pnl
from tests.contracts.etoro.test_etoro_saved_pnl_evidence import _saved_case
from brokers.etoro.saved_pnl_evidence import assess_saved_pnl_capture


def _cash(**changes):
    values = dict(
        demo_customer_id="9001", available_cash=Decimal("9000"), observed_at=NOW,
        source_endpoint="/api/v1/trading/info/demo/aggregate",
        source_timestamp_field="timestamp", source_sha256="a" * 64,
    )
    values["source_timestamp"] = changes.get("observed_at", NOW).isoformat()
    return ComponentCashObservation(**(values | changes))


def _assessment(*, evidence=None, cash=None, assessed_at=NOW, **changes):
    if evidence is None:
        evidence, _ = _capture()
    return assess_pnl_component_time_evidence(
        evidence, assessed_at=assessed_at,
        cash_observation=_cash() if cash is None else cash, **changes,
    )


def test_fresh_components_have_no_atomic_timestamp_and_keep_legacy_digest():
    evidence, _ = _capture()
    original_hash = payload_hash(evidence)
    proof = qualify_pnl_component_time_evidence(
        evidence, assessed_at=NOW, cash_observation=_cash())
    assert proof.model_version == "etoro-component-time-v1"
    assert proof.observed_at is None and proof.evidence.observed_at is None
    assert proof.earliest_component_at == proof.latest_component_at == NOW
    assert proof.validate_at(NOW) == () and proof.qualified
    assert proof.source_evidence_sha256 == original_hash == payload_hash(evidence)
    assert proof.demo_customer_id == "9001" and proof.currency == "USD"
    assert proof.evidence_hash == payload_hash(proof)


def test_cash_is_not_proven_by_matching_bracket_receipts():
    evidence, _ = _capture()
    assessment = assess_pnl_component_time_evidence(evidence, assessed_at=NOW)
    assert assessment.validate_at(NOW) == ("cash_component_observation_unproven",)
    assert not assessment.qualified
    with pytest.raises(BrokerDataUnavailable, match="cash_component_observation_unproven"):
        qualify_pnl_component_time_evidence(evidence, assessed_at=NOW)


def test_reassessment_cannot_renew_any_old_clock():
    proof = _assessment()
    blockers = proof.validate_at(NOW + timedelta(seconds=16))
    assert set(blockers) == {
        "component_structure_stale_or_future", "component_current_valuation_stale_or_future",
        "cash_component_stale_or_future",
    }
    replay = replace(proof, assessed_at=NOW + timedelta(days=1))
    assert not replay.qualified
    assert replay.earliest_component_at == NOW and replay.cash_observation.observed_at == NOW


def test_future_mark_at_capture_cannot_be_rehabilitated_later():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = "2026-10-21T14:30:00.0000001Z"
    evidence, _ = _capture(pnl=pnl)
    proof = _assessment(evidence=evidence, assessed_at=NOW + timedelta(seconds=1))
    assert "component_original_valuation_stale_or_future" in proof.validate_at(proof.assessed_at)
    assert "component_current_valuation_stale_or_future" not in proof.validate_at(proof.assessed_at)
    assert not proof.qualified


@pytest.mark.parametrize("timestamp,expected", [
    ("2026-10-21T14:29:45.0000000Z", ()),
    ("2026-10-21T14:29:44.9999999Z", (
        "component_original_valuation_stale_or_future", "component_current_valuation_stale_or_future",
        "component_time_skew_exceeded")),
])
def test_component_skew_and_freshness_preserve_seventh_fractional_digit(timestamp, expected):
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = timestamp
    evidence, _ = _capture(pnl=pnl)
    assert _assessment(evidence=evidence).validate_at(NOW) == expected


def test_stricter_runtime_age_and_model_skew_are_independent():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = "2026-10-21T14:29:51Z"
    evidence, _ = _capture(pnl=pnl)
    proof = _assessment(evidence=evidence, max_component_skew_seconds=Decimal("8"))
    assert proof.validate_at(NOW) == ("component_time_skew_exceeded",)
    assert "component_current_valuation_stale_or_future" in proof.validate_at(NOW, age_limit=8)


def test_collection_interval_is_checked_at_its_earliest_bound():
    evidence, _ = _capture()
    evidence = replace(evidence, capture_started_at=NOW - timedelta(seconds=12))
    proof = _assessment(evidence=evidence)
    assert proof.validate_at(NOW) == ()
    assert "component_structure_stale_or_future" in proof.validate_at(NOW + timedelta(seconds=4))


@pytest.mark.parametrize("changes,reason", [
    ({"demo_customer_id": "9002"}, "cash_component_identity_or_value_mismatch"),
    ({"available_cash": Decimal("9000.0000001")}, "cash_component_identity_or_value_mismatch"),
    ({"observed_at": NOW + timedelta(microseconds=1)}, "cash_component_stale_or_future"),
    ({"observed_at": NOW - timedelta(seconds=16)}, "cash_component_stale_or_future"),
])
def test_cash_requires_same_account_exact_amount_and_original_current_time(changes, reason):
    assert reason in _assessment(cash=_cash(**changes)).validate_at(NOW)


@pytest.mark.parametrize("changes", [
    {"source_endpoint": "/api/v1/trading/info/real/portfolio"},
    {"source_timestamp_field": ""}, {"source_sha256": "a" * 63},
    {"available_cash": 9000.0}, {"available_cash": Decimal("NaN")},
    {"observed_at": NOW.replace(tzinfo=None)}, {"currency": "GBP"},
])
def test_cash_contract_rejects_missing_or_invalid_provenance_and_precision(changes):
    with pytest.raises(BrokerDataUnavailable):
        _cash(**changes)


def test_pending_and_position_coverage_cannot_be_silently_narrowed():
    evidence, _ = _capture()
    partial_pending = replace(evidence, checked_pending_collections=evidence.checked_pending_collections[:-1])
    assert "component_pending_coverage_incomplete" in _assessment(evidence=partial_pending).validate_at(NOW)
    partial_positions = replace(evidence, positions=())
    assert "component_position_coverage_incomplete" in _assessment(evidence=partial_positions).validate_at(NOW)
    partial_currency = replace(evidence, currency_metadata=())
    assert "component_position_coverage_incomplete" in _assessment(evidence=partial_currency).validate_at(NOW)


def test_position_values_and_metadata_must_still_reconcile():
    evidence, _ = _capture()
    changed = replace(evidence.positions[0], current_exposure=Decimal("999"))
    proof = _assessment(evidence=replace(evidence, positions=(changed,)))
    assert "component_position_evidence_inconsistent" in proof.validate_at(NOW)
    proof = _assessment(evidence=replace(evidence, security_metadata_checksum="f" * 64))
    assert "component_metadata_hash_mismatch" in proof.validate_at(NOW)


def test_invested_margin_and_exposure_are_distinct_without_double_counting_fees():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(
        marginInAccountCurrency=777, exposureInAccountCurrency=840)
    evidence, _ = _capture(pnl=pnl)
    proof = _assessment(evidence=evidence)
    assert proof.validate_at(NOW) == ()
    assert proof.invested_amount == Decimal("1000")
    assert proof.broker_margin == Decimal("777")
    assert proof.positions[0].current_exposure == Decimal("840")
    assert proof.equity == proof.available_cash + proof.invested_amount + proof.current_pnl
    changed = replace(evidence, equity=evidence.equity - Decimal("0.0000001"))
    assert "component_equity_identity_mismatch" in _assessment(evidence=changed).validate_at(NOW)


def test_source_hash_and_version_are_bound_without_changing_saved_shape():
    proof = _assessment()
    with pytest.raises(BrokerDataUnavailable, match="source_evidence_hash_mismatch"):
        replace(proof, source_evidence_sha256="0" * 64)
    with pytest.raises(BrokerDataUnavailable, match="model_not_supported"):
        replace(proof, model_version="etoro-component-time-v2")
    with pytest.raises(BrokerDataUnavailable, match="model_not_supported"):
        replace(proof, observed_at=NOW)
    with pytest.raises(BrokerDataUnavailable, match="model_not_supported"):
        replace(proof, max_component_skew_seconds=Decimal("16"))
    assert isinstance(proof, AccountComponentTimeEvidence)


def test_saved_assessment_adds_explicit_model_without_rewriting_old_acceptance():
    replay = assess_saved_pnl_capture(**_saved_case())
    assert replay.assess_component_time().validate_at(NOW) == ("cash_component_observation_unproven",)
    proof = replay.assess_component_time(cash_observation=_cash())
    assert proof.qualified and replay.risk_input_compatible is False
    assert replay.baseline_input_compatible is False and replay.qualification_credit is False
    stale = assess_saved_pnl_capture(**_saved_case(assessed_at=NOW + timedelta(days=1)))
    assert not stale.assess_component_time(cash_observation=_cash()).qualified


def test_aggregate_snapshot_preserves_cost_capital_separately_from_margin():
    aggregate = _aggregate()
    aggregate["instrumentAggregates"][0].update(
        totalMarginAccountCurrency=777, liquidationValueAccountCurrency=797,
        netCurrentExposureAccountCurrency=840)
    aggregate["accountTotals"]["accountTotalUsedMargin"] = 777
    evidence, _ = _capture_aggregate(aggregate=aggregate)
    assert evidence.snapshot.invested_amount == Decimal("1000")
    assert evidence.snapshot.used_margin == Decimal("777")
    assert evidence.snapshot.positions[0].current_exposure == Decimal("840")
    assert evidence.snapshot.equity == Decimal("10020")


def test_large_balances_preserve_small_invested_and_pnl_coefficients():
    detail, pnl = _detail(), _pnl()
    for body in (detail, pnl):
        body["clientPortfolio"]["credit"] = "1000000000000000000000000000000"
        body["clientPortfolio"]["positions"][0]["amount"] = "0.000000000001"
    pnl["clientPortfolio"]["unrealizedPnL"] = "0.000000000002"
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["pnL"] = "0.000000000002"
    evidence, _ = _capture(detail=detail, pnl=pnl)
    assert evidence.equity == Decimal("1000000000000000000000000000000.000000000003")
    cash = _cash(available_cash=Decimal("1000000000000000000000000000000"))
    assert _assessment(evidence=evidence, cash=cash).qualified


def test_dispatch_deadline_uses_oldest_cash_or_position_component_not_receipt():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = "2026-10-21T14:29:50.0000001Z"
    evidence, _ = _capture(pnl=pnl)
    proof = _assessment(evidence=evidence)
    from brokers.etoro.schemas import exact_datetime_epoch_seconds
    assert proof.freshness_deadline_epoch_seconds() == exact_datetime_epoch_seconds(NOW) + Decimal("5.0000001")
    assert proof.freshness_deadline() == NOW + timedelta(seconds=5)
    cash_earlier = _assessment(evidence=evidence, cash=_cash(observed_at=NOW - timedelta(seconds=12)))
    assert cash_earlier.freshness_deadline() == NOW + timedelta(seconds=3)
    no_cash = assess_pnl_component_time_evidence(evidence, assessed_at=NOW)
    with pytest.raises(BrokerDataUnavailable, match="cash_component_observation_unproven"):
        no_cash.freshness_deadline()


def test_cash_time_retains_and_checks_seventh_fractional_digit():
    cash = _cash(source_timestamp="2026-10-21T14:30:00.0000001Z")
    proof = _assessment(cash=cash)
    assert cash.observed_at == NOW
    assert "cash_component_stale_or_future" in proof.validate_at(NOW)
    with pytest.raises(BrokerDataUnavailable, match="source_time_mismatch"):
        _cash(source_timestamp="2026-10-21T14:30:01Z")
