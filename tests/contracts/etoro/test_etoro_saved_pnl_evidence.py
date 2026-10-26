"""Pure replay contracts from synthetic payloads, never retained private data."""
from __future__ import annotations

import json
import socket
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import timedelta
from decimal import Decimal

import pytest

from brokers.base import BrokerDataUnavailable
from brokers.etoro.account_evidence import capture_pnl_account_evidence
from brokers.etoro.client import HttpResponse
from brokers.etoro.saved_pnl_evidence import assess_saved_pnl_capture
from brokers.etoro.schemas import EtoroSchemaError
from execution.domain import canonical_json, payload_hash
from tests.contracts.etoro.test_etoro_account_evidence import NOW, _client
from tests.contracts.etoro.test_etoro_pnl_capture import _bracket, _capture
from tests.contracts.etoro.test_etoro_pnl_schema import _pnl


def _saved_case(*, assessed_at=None, **variants):
    """Create original evidence through an existing synthetic-only transport."""
    evidence, _ = _capture(**variants)
    opening, pnl, securities, currency, closing = _bracket(**variants)
    return {
        "opening_payload": opening,
        "pnl_payload": pnl,
        "securities_payload": securities,
        "currency_payload": currency,
        "closing_payload": closing,
        "expected_demo_customer_id": "9001",
        "capture_started_at": evidence.capture_started_at,
        "capture_completed_at": evidence.capture_completed_at,
        "saved_account_evidence": json.loads(canonical_json(evidence)),
        "expected_evidence_sha256": payload_hash(evidence),
        "assessed_at": NOW if assessed_at is None else assessed_at,
    }


def _assert_no_risk_conversion(assessment):
    assert assessment.risk_input_compatible is False
    assert assessment.baseline_input_compatible is False
    assert assessment.trading_readiness == "BLOCKED"
    assert assessment.qualification_credit is False
    assert isinstance(assessment.compatibility_blockers, tuple)
    assert "whole_account_observation_time_unavailable" in assessment.compatibility_blockers
    assert assessment.evidence.observed_at is None
    assert assessment.evidence.daily_pnl is None
    assert assessment.evidence.peak_equity is None
    assert not hasattr(assessment.evidence, "snapshot")


def test_replay_preserves_historical_acceptance_without_refreshing_old_marks():
    case = _saved_case(assessed_at=NOW + timedelta(days=1))
    before = deepcopy(case)
    assessment = assess_saved_pnl_capture(**case)
    assert assessment.source_consistency_verified is True
    assert assessment.historical_restricted_capture_accepted is True
    assert assessment.evidence.valuation_ages_seconds == (Decimal("0"),)
    assert assessment.evidence.valuation_freshness == (True,)
    assert assessment.present_valuation_ages_seconds == (Decimal("86400"),)
    assert assessment.present_valuation_freshness == (False,)
    assert assessment.present_valuations_fresh is False
    assert assessment.assessed_at == NOW + timedelta(days=1)
    assert payload_hash(assessment.evidence) == case["expected_evidence_sha256"]
    assert case == before
    _assert_no_risk_conversion(assessment)


def test_fresh_replay_is_still_frozen_and_incompatible_with_risk_and_baselines():
    assessment = assess_saved_pnl_capture(**_saved_case())
    assert assessment.historical_restricted_capture_accepted is True
    assert assessment.present_valuations_fresh is True
    assert assessment.present_valuation_ages_seconds == (Decimal("0"),)
    _assert_no_risk_conversion(assessment)
    with pytest.raises(FrozenInstanceError):
        assessment.assessed_at = NOW + timedelta(seconds=1)


@pytest.mark.parametrize("identity", [" 9001 ", "09001"])
def test_replay_normalizes_the_same_expected_identity_as_online_capture(identity):
    case = _saved_case()
    case["expected_demo_customer_id"] = identity
    assessment = assess_saved_pnl_capture(**case)
    assert assessment.evidence.demo_customer_id == "9001"
    assert assessment.historical_restricted_capture_accepted is True


def test_saved_numeric_decoding_must_match_online_decimal_precision_and_scale():
    # Include meaningful precision and irrelevant raw-source scale: both are
    # retained by the online client and bound into its primary source hashes.
    encoded = [json.dumps(body).replace('"credit": 9000', '"credit": 9000.00')
               .replace('"units": 10', '"units": 10.1234567890123456789').encode()
               for body in _bracket()]
    client, _ = _client([HttpResponse(status_code=200, headers={}, body=b) for b in encoded])
    evidence = capture_pnl_account_evidence(client, expected_demo_customer_id="9001", now=NOW)
    case = _saved_case()
    keys = ("opening_payload", "pnl_payload", "securities_payload", "currency_payload", "closing_payload")
    for key, body in zip(keys, encoded):
        case[key] = json.loads(body, parse_float=Decimal) if key != "securities_payload" else json.loads(body)
    case["saved_account_evidence"] = json.loads(canonical_json(evidence))
    case["expected_evidence_sha256"] = payload_hash(evidence)
    assessment = assess_saved_pnl_capture(**case)
    assert assessment.evidence.positions[0].units == Decimal("10.1234567890123456789")
    assert assessment.evidence.available_cash == Decimal("9000.00")
    for key, body in zip(keys, encoded):
        case[key] = json.loads(body)
    with pytest.raises(BrokerDataUnavailable, match="saved_account_evidence_hash_mismatch"):
        assess_saved_pnl_capture(**case)


@pytest.mark.parametrize("timestamp,age_now,fresh_now", [
    ("2026-10-21T14:29:44.9999999Z", "16.0000001", False),
    ("2026-10-21T14:30:00.0000001Z", "0.9999999", True),
])
def test_later_replay_cannot_rehabilitate_stale_or_future_historical_capture(
        timestamp, age_now, fresh_now):
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = timestamp
    assessment = assess_saved_pnl_capture(**_saved_case(
        pnl=pnl, assessed_at=NOW + timedelta(seconds=1)))
    assert assessment.source_consistency_verified is True
    assert assessment.historical_restricted_capture_accepted is False
    assert assessment.evidence.valuation_freshness == (False,)
    assert assessment.present_valuation_ages_seconds == (Decimal(age_now),)
    assert assessment.present_valuation_freshness == (fresh_now,)
    assert assessment.present_valuations_fresh is fresh_now
    _assert_no_risk_conversion(assessment)


@pytest.mark.parametrize("timestamp,age,fresh", [
    ("2026-10-21T14:30:00.0000000Z", "15", True),
    ("2026-10-21T14:29:59.9999999Z", "15.0000001", False),
])
def test_present_age_keeps_seventh_fractional_digit_at_freshness_boundary(timestamp, age, fresh):
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = timestamp
    assessment = assess_saved_pnl_capture(**_saved_case(
        pnl=pnl, assessed_at=NOW + timedelta(seconds=15)))
    assert assessment.evidence.position_valuations[0].timestamp == timestamp
    assert assessment.historical_restricted_capture_accepted is True
    assert assessment.present_valuation_ages_seconds == (Decimal(age),)
    assert assessment.present_valuations_fresh is fresh


def test_replay_reconciles_invested_amount_without_substituting_broker_margin_or_fees():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(
        marginInAccountCurrency=777, marginInAssetCurrency=777)
    assessment = assess_saved_pnl_capture(**_saved_case(pnl=pnl))
    evidence = assessment.evidence
    assert evidence.available_cash == Decimal("9000")
    assert evidence.invested_amount == Decimal("1000")
    assert evidence.broker_margin == Decimal("777")
    assert evidence.current_pnl == Decimal("20")
    assert evidence.equity == Decimal("10020")
    assert evidence.position_valuations[0].total_fees == Decimal("2")
    assert evidence.position_valuations[0].total_external_fees == Decimal("3")
    assert evidence.position_valuations[0].total_external_taxes == Decimal("1")
    assert assessment.historical_restricted_capture_accepted is True
    _assert_no_risk_conversion(assessment)


@pytest.mark.parametrize("tamper", ["saved_total", "expected_digest", "consistent_raw_cash"])
def test_replay_requires_raw_recomputation_saved_evidence_and_expected_digest_to_agree(tamper):
    case = _saved_case()
    if tamper == "saved_total":
        case["saved_account_evidence"]["equity"] = "10021"
    elif tamper == "expected_digest":
        case["expected_evidence_sha256"] = "0" * 64
    else:
        for key in ("opening_payload", "pnl_payload", "closing_payload"):
            case[key]["clientPortfolio"]["credit"] += 1
    with pytest.raises(BrokerDataUnavailable, match="saved_account_evidence_hash_mismatch"):
        assess_saved_pnl_capture(**case)


def test_saved_freshness_boolean_and_rehashed_saved_claim_are_not_replay_authority():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = (
        "2026-10-21T14:29:44.9999999Z")
    case = _saved_case(pnl=pnl)
    case["saved_account_evidence"]["valuation_freshness"] = [True]
    case["expected_evidence_sha256"] = payload_hash(case["saved_account_evidence"])
    with pytest.raises(BrokerDataUnavailable, match="saved_account_evidence_hash_mismatch"):
        assess_saved_pnl_capture(**case)


@pytest.mark.parametrize("digest", [None, "a" * 63, "A" * 64])
def test_expected_digest_must_be_explicit_lowercase_sha256(digest):
    case = _saved_case()
    case["expected_evidence_sha256"] = digest
    with pytest.raises(BrokerDataUnavailable, match="saved_account_evidence_hash_required"):
        assess_saved_pnl_capture(**case)


@pytest.mark.parametrize("stage", ["opening_payload", "pnl_payload", "closing_payload"])
def test_each_primary_payload_must_match_independent_demo_identity(stage):
    case = _saved_case()
    case[stage]["clientPortfolio"]["positions"][0]["CID"] = 9002
    # Opening identity is also checked while parsing currency coverage;
    # either strict parser or shared validator must reject the mismatch.
    with pytest.raises((BrokerDataUnavailable, EtoroSchemaError), match="identity"):
        assess_saved_pnl_capture(**case)


@pytest.mark.parametrize("metadata", ["securities", "currency"])
def test_replay_rechecks_exact_metadata_coverage(metadata):
    case = _saved_case()
    if metadata == "securities":
        case["securities_payload"]["results"] = []
    else:
        case["currency_payload"]["instruments"][0]["positions"] = []
    with pytest.raises((BrokerDataUnavailable, EtoroSchemaError), match="coverage|cover"):
        assess_saved_pnl_capture(**case)


def test_omitted_pending_collection_is_not_interpreted_as_empty_during_replay():
    case = _saved_case()
    del case["pnl_payload"]["clientPortfolio"]["orders"]
    with pytest.raises(EtoroSchemaError, match="orders"):
        assess_saved_pnl_capture(**case)


@pytest.mark.parametrize("clock", ["capture_started_at", "capture_completed_at"])
def test_recorded_capture_clocks_are_bound_to_saved_evidence(clock):
    case = _saved_case(assessed_at=NOW + timedelta(seconds=2))
    case[clock] += timedelta(seconds=-1 if clock == "capture_started_at" else 1)
    with pytest.raises(BrokerDataUnavailable, match="saved_account_evidence_hash_mismatch"):
        assess_saved_pnl_capture(**case)


@pytest.mark.parametrize("assessed_at", [NOW.replace(tzinfo=None), NOW - timedelta(microseconds=1)])
def test_assessment_clock_requires_aware_time_at_or_after_capture_completion(assessed_at):
    case = _saved_case(assessed_at=assessed_at)
    with pytest.raises(BrokerDataUnavailable, match="saved_pnl_assessment_time_not_valid"):
        assess_saved_pnl_capture(**case)


def test_single_page_replay_refuses_incomplete_security_pagination():
    case = _saved_case()
    case["securities_payload"]["pagination"].update(hasNext=True, nextPageToken="synthetic-next-page")
    with pytest.raises(BrokerDataUnavailable, match="saved_pnl_security_pagination_not_supported"):
        assess_saved_pnl_capture(**case)


def test_replay_uses_materialized_payloads_without_clients_network_or_stores(tmp_path, monkeypatch):
    import requests

    from brokers.etoro import account_evidence, client
    from execution import risk_baseline, store

    case = _saved_case(assessed_at=NOW + timedelta(days=1))

    def prohibited(*args, **kwargs):
        pytest.fail("offline saved-evidence replay must not use client, network, capture, or store capabilities")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(client.EtoroReadOnlyClient, "__init__", prohibited)
    monkeypatch.setattr(client.EtoroReadOnlyClient, "get_demo_detailed_portfolio", prohibited)
    monkeypatch.setattr(client.EtoroReadOnlyClient, "get_demo_pnl_portfolio", prohibited)
    monkeypatch.setattr(account_evidence, "capture_pnl_account_evidence", prohibited)
    monkeypatch.setattr(requests.Session, "send", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "socket", prohibited)
    monkeypatch.setattr(risk_baseline.RiskBaselineStore, "__init__", prohibited)
    monkeypatch.setattr(store.ExecutionStore, "__init__", prohibited)
    assessment = assess_saved_pnl_capture(**case)
    assert assessment.source_consistency_verified is True
    assert assessment.historical_restricted_capture_accepted is True
    assert assessment.present_valuations_fresh is False
    _assert_no_risk_conversion(assessment)
    assert list(tmp_path.iterdir()) == []
