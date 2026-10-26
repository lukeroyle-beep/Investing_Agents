"""Synthetic bracket contracts; these never establish live Demo acceptance."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from brokers.base import BrokerDataUnavailable
from brokers.etoro import account_evidence
from brokers.etoro.account_evidence import capture_pnl_account_evidence
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS, EtoroSchemaError
from execution.domain import DomainValidationError, Environment
from risk.pretrade import AccountEvidence, RiskEvidenceError
from tests.contracts.etoro.test_etoro_account_evidence import (
    NOW, _client, _detail, _page, _security,
)
from tests.contracts.etoro.test_etoro_pnl_schema import _pnl
from tests.contracts.etoro.test_etoro_pnl_currency_metadata import _currency_metadata


def _currency_for(detail):
    body = _currency_metadata()
    template = deepcopy(body["instruments"][0]["positions"][0])
    instruments = {}
    for position in detail["clientPortfolio"]["positions"]:
        item = deepcopy(template)
        item.update(positionId=position["positionID"], instrumentId=position["instrumentID"],
                    units=position["units"], leverage=position["leverage"],
                    settlementType="real" if position["settlementTypeID"] == 1 else "cfd")
        instrument = instruments.setdefault(position["instrumentID"], {
            "instrumentId": position["instrumentID"], "positions": [], "orders": []})
        instrument["positions"].append(item)
    body["instruments"] = list(instruments.values())
    return body


def _bracket(detail=None, pnl=None, metadata=None, currencies=None, last=None):
    first = _detail() if detail is None else detail
    return [first, _pnl() if pnl is None else pnl,
            _page(_security()) if metadata is None else metadata,
            _currency_for(first) if currencies is None else currencies,
            deepcopy(first) if last is None else last]


def _capture(*, now=NOW, limit=15, **values):
    client, transport = _client(_bracket(**values))
    evidence = capture_pnl_account_evidence(
        client, expected_demo_customer_id="9001", now=now,
        account_max_age_seconds=limit,
    )
    return evidence, transport


def _at(stage, first, pnl, last):
    return {"first": first, "pnl": pnl, "last": last}[stage]["clientPortfolio"]


def test_bracket_uses_unfiltered_pnl_route_and_preserves_separate_evidence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    evidence, transport = _capture()
    paths = [urlparse(request.url).path for request in transport.requests]
    assert paths == ["/api/v1/trading/info/demo/portfolio",
                     "/api/v1/trading/info/demo/pnl",
                     "/api/v2/market-data/instruments",
                     "/api/v2/trading/info/demo/instrument-breakdown",
                     "/api/v1/trading/info/demo/portfolio"]
    assert [request.method for request in transport.requests] == ["GET"] * 5
    assert all(request.body is None for request in transport.requests)
    assert all(not urlparse(transport.requests[index].url).query for index in (0, 1, 4))
    assert parse_qs(urlparse(transport.requests[3].url).query) == {
        "conversionMode": ["eToroApp"], "positionLevel": ["Normal"],
        "orderLevel": ["Normal"], "mirrorLevel": ["Details"]}
    assert parse_qs(urlparse(transport.requests[2].url).query) == {
        "instrumentsIds": ["1001"], "pageSize": ["100"]}
    assert list(tmp_path.iterdir()) == []
    assert evidence.capture_started_at == evidence.capture_completed_at == NOW
    assert evidence.observed_at is None and not hasattr(evidence, "snapshot")
    assert evidence.checked_pending_collections == DEMO_PENDING_COLLECTIONS
    assert evidence.daily_pnl is None and evidence.peak_equity is None
    assert evidence.valuation_ages_seconds == (Decimal("0"),)
    assert evidence.valuation_freshness == (True,) and evidence.valuations_fresh
    assert "whole_account_observation_time_unavailable" in evidence.limitations
    assert "bracketed_reads_are_not_an_atomic_broker_snapshot" in evidence.limitations
    assert "execution_history_not_complete" in evidence.limitations


@pytest.mark.parametrize("stage", ["first", "pnl", "last"])
def test_every_source_checks_independently_selected_identity(stage):
    first, pnl, last = _detail(), _pnl(), _detail()
    _at(stage, first, pnl, last)["positions"][0]["CID"] = 9002
    with pytest.raises(BrokerDataUnavailable, match="demo_account_identity_mismatch"):
        _capture(detail=first, pnl=pnl, last=last)


@pytest.mark.parametrize("identity", [None, "", "x", "-1", "0", True, "9001.5"])
def test_expected_identity_rejected_before_first_request(identity):
    client, transport = _client([])
    with pytest.raises(BrokerDataUnavailable, match="expected_demo_account_identity_required"):
        capture_pnl_account_evidence(client, expected_demo_customer_id=identity, now=NOW)
    assert transport.requests == []


@pytest.mark.parametrize("stage", ["first", "pnl", "last"])
@pytest.mark.parametrize("collection", DEMO_PENDING_COLLECTIONS)
def test_every_pending_collection_is_required_at_every_observation(stage, collection):
    first, pnl, last = _detail(), _pnl(), _detail()
    del _at(stage, first, pnl, last)[collection]
    with pytest.raises(EtoroSchemaError):
        _capture(detail=first, pnl=pnl, last=last)


@pytest.mark.parametrize("stage", ["first", "pnl", "last"])
@pytest.mark.parametrize("collection", (*DEMO_PENDING_COLLECTIONS, "mirrors"))
def test_any_pending_order_or_mirror_blocks_the_entire_bracket(stage, collection):
    first, pnl, last = _detail(), _pnl(), _detail()
    _at(stage, first, pnl, last)[collection] = [{"synthetic": "active"}]
    with pytest.raises(BrokerDataUnavailable, match="account_scope_not_supported"):
        _capture(detail=first, pnl=pnl, last=last)


@pytest.mark.parametrize("stage", ["pnl", "last"])
@pytest.mark.parametrize("field,value", [
    ("units", "10.000000001"), ("amount", "1000.000000001"),
    ("settlementTypeID", 0), ("instrumentID", 1002), ("positionID", 7002),
])
def test_structural_equality_is_exact_and_has_no_unit_or_money_tolerance(stage, field, value):
    first, pnl, last = _detail(), _pnl(), _detail()
    _at(stage, first, pnl, last)["positions"][0][field] = value
    with pytest.raises(BrokerDataUnavailable, match="structure_or_cash_mismatch|changed_during_capture"):
        _capture(detail=first, pnl=pnl, last=last)


@pytest.mark.parametrize("stage", ["pnl", "last"])
def test_cash_must_remain_exact_across_all_reads(stage):
    first, pnl, last = _detail(), _pnl(), _detail()
    _at(stage, first, pnl, last)["credit"] = "9000.000000001"
    with pytest.raises(BrokerDataUnavailable, match="structure_or_cash_mismatch|changed_during_capture"):
        _capture(detail=first, pnl=pnl, last=last)


@pytest.mark.parametrize("stage", ["first", "pnl", "last"])
@pytest.mark.parametrize("field,value", [
    ("isBuy", False), ("leverage", 2), ("settlementTypeID", 2), ("mirrorID", 1),
])
def test_unsupported_holdings_cannot_enter_through_any_source(stage, field, value):
    first, pnl, last = _detail(), _pnl(), _detail()
    _at(stage, first, pnl, last)["positions"][0][field] = value
    with pytest.raises(BrokerDataUnavailable, match="account_scope_not_supported"):
        _capture(detail=first, pnl=pnl, last=last)


@pytest.mark.parametrize("mutation", ["missing", "additional", "duplicate"])
def test_exact_position_coverage_rejects_dropped_added_or_duplicate_positions(mutation):
    pnl = _pnl()
    positions = pnl["clientPortfolio"]["positions"]
    if mutation == "missing":
        positions.clear()
    else:
        extra = deepcopy(positions[0])
        if mutation == "additional":
            extra["positionID"] = 7002
        positions.append(extra)
    with pytest.raises((BrokerDataUnavailable, EtoroSchemaError)):
        _capture(pnl=pnl)


@pytest.mark.parametrize("metadata", [
    _page(), _page(_security(), _security()), _page(_security(1002)),
    _page(_security(), _security(1002)),
])
def test_metadata_exact_coverage_rejects_missing_duplicate_wrong_and_extra(metadata):
    with pytest.raises((BrokerDataUnavailable, EtoroSchemaError)):
        _capture(metadata=metadata)


@pytest.mark.parametrize("kind", ["Stocks", "ETF", "Crypto"])
@pytest.mark.parametrize("settlement", [0, 1])
def test_supported_holdings_and_settlements_remain_available(kind, settlement):
    detail, pnl = _detail(), _pnl()
    detail["clientPortfolio"]["positions"][0]["settlementTypeID"] = settlement
    pnl["clientPortfolio"]["positions"][0]["settlementTypeID"] = settlement
    evidence, _ = _capture(detail=detail, pnl=pnl, metadata=_page(_security(kind=kind)))
    assert evidence.valuations_fresh and len(evidence.positions) == 1


@pytest.mark.parametrize("kind", ["CFD", "Currencies", "Futures", "stocks", "Unknown"])
def test_metadata_instrument_scope_is_preserved(kind):
    with pytest.raises(BrokerDataUnavailable, match="instrument type"):
        _capture(metadata=_page(_security(kind=kind)))


def test_equity_uses_invested_amount_and_broker_pnl_without_repeated_fee_deductions():
    pnl = _pnl()
    position = pnl["clientPortfolio"]["positions"][0]
    # Deliberately distinct quantities rule out accidental substitutions.
    position["unrealizedPnL"].update(marginInAccountCurrency=777,
                                     exposureInAccountCurrency=840)
    evidence, _ = _capture(pnl=pnl)
    assert evidence.available_cash == Decimal("9000")
    assert evidence.invested_amount == Decimal("1000")
    assert evidence.broker_margin == Decimal("777")
    assert evidence.positions[0].current_exposure == Decimal("840")
    assert evidence.current_pnl == Decimal("20")
    assert evidence.equity == Decimal("10020")
    valuation = evidence.position_valuations[0]
    assert (valuation.total_fees, valuation.total_external_fees,
            valuation.total_external_taxes) == (Decimal("2"), Decimal("3"), Decimal("1"))


def test_portfolio_pnl_must_reconcile_with_every_position():
    pnl = _pnl()
    pnl["clientPortfolio"]["unrealizedPnL"] = "20.021"
    with pytest.raises(BrokerDataUnavailable, match="account_unrealized_pnl_evidence_mismatch"):
        _capture(pnl=pnl)


def test_pnl_without_positions_cannot_vacuously_prove_account_identity():
    detail = _detail()
    detail["clientPortfolio"]["positions"] = []
    with pytest.raises(BrokerDataUnavailable, match="identity_unproven_without_positions"):
        _capture(detail=detail)


@pytest.mark.parametrize("timestamp,age,fresh", [
    ("2026-10-21T14:30:00.0000000Z", "0", True),
    ("2026-10-21T14:30:00.0000001Z", "-0.0000001", False),
    ("2026-10-21T14:29:45.0000000Z", "15", True),
    ("2026-10-21T14:29:44.9999999Z", "15.0000001", False),
    ("2026-10-21T14:29:45.0000001Z", "14.9999999", True),
    ("2026-10-21T14:29:59.1234567Z", "0.8765433", True),
])
def test_seventh_fractional_digit_decides_freshness_without_rounding(timestamp, age, fresh):
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = timestamp
    evidence, _ = _capture(pnl=pnl)
    assert evidence.position_valuations[0].timestamp == timestamp
    assert evidence.valuation_ages_seconds == (Decimal(age),)
    assert evidence.valuation_freshness == (fresh,)
    assert evidence.valuations_fresh is fresh
    assert evidence.observed_at is None


def test_all_valuation_times_are_checked_individually_at_capture_completion(monkeypatch):
    detail, pnl = _detail(), _pnl()
    second_detail = deepcopy(detail["clientPortfolio"]["positions"][0])
    second_detail.update(positionID=7002, instrumentID=1002, amount=500, units=5)
    detail["clientPortfolio"]["positions"].append(second_detail)
    second_pnl = deepcopy(pnl["clientPortfolio"]["positions"][0])
    second_pnl.update(second_detail)
    second_pnl["unrealizedPnL"].update(pnL=-10, timestamp="2026-10-21T14:29:45.0000000Z")
    pnl["clientPortfolio"]["positions"].append(second_pnl)
    pnl["clientPortfolio"]["unrealizedPnL"] = 10
    moments = iter((NOW, NOW + timedelta(seconds=1)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(moments)

    monkeypatch.setattr(account_evidence, "datetime", Clock)
    evidence, _ = _capture(now=None, detail=detail, pnl=pnl,
                           metadata=_page(_security(), _security(1002)))
    assert evidence.capture_started_at == NOW
    assert evidence.capture_completed_at == NOW + timedelta(seconds=1)
    assert evidence.valuation_ages_seconds == (Decimal("1"), Decimal("16"))
    assert evidence.valuation_freshness == (True, False)
    assert not evidence.valuations_fresh
    assert evidence.current_pnl == Decimal("10") and evidence.equity == Decimal("10510")


def test_stricter_existing_account_age_limit_is_respected():
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = "2026-10-21T14:29:54.9999999Z"
    evidence, _ = _capture(pnl=pnl, limit=5)
    assert evidence.valuation_max_age_seconds == Decimal("5")
    assert not evidence.valuations_fresh


@pytest.mark.parametrize("limit", [0, -1, 16, True, 15.0])
def test_valuation_limit_cannot_relax_existing_capture_ceiling(limit):
    client, transport = _client([])
    with pytest.raises(BrokerDataUnavailable, match="account_valuation_limit_not_valid"):
        capture_pnl_account_evidence(client, expected_demo_customer_id="9001", now=NOW,
                                     account_max_age_seconds=limit)
    assert transport.requests == []


@pytest.mark.parametrize("elapsed", [-1, 16])
def test_capture_interval_has_independent_validity_gate(monkeypatch, elapsed):
    moments = iter((NOW, NOW + timedelta(seconds=elapsed)))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(moments)

    monkeypatch.setattr(account_evidence, "datetime", Clock)
    with pytest.raises(BrokerDataUnavailable, match="account_capture_timing_not_valid"):
        _capture(now=None)


def test_detail_valuation_changes_do_not_masquerade_as_structural_changes():
    last = _detail()
    last["clientPortfolio"]["positions"][0]["unrealizedPnL"] = {"pnL": 21}
    last["clientPortfolio"]["timestamp"] = "ambiguous-account-time"
    evidence, _ = _capture(last=last)
    assert evidence.detailed_snapshot_id != evidence.closing_detailed_snapshot_id
    assert evidence.observed_at is None and evidence.current_pnl == Decimal("20")


def test_pretrade_risk_does_not_accept_missing_whole_account_observation_time():
    evidence, _ = _capture()
    with pytest.raises((DomainValidationError, RiskEvidenceError), match="observed_at|observation"):
        AccountEvidence(snapshot_id=evidence.pnl_snapshot_id, environment=Environment.DEMO,
                        observed_at=evidence.observed_at, currency="USD", equity=evidence.equity,
                        cash=evidence.available_cash, daily_pnl=Decimal("0"),
                        peak_equity=evidence.equity, positions=(), pending_orders=(),
                        pending_orders_complete=True)


@pytest.mark.parametrize("numeric_id,code", [(999, "USD"), (1, "GBX")])
def test_known_usd_currency_id_cannot_contradict_explicit_asset_currency(numeric_id, code):
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["assetCurrencyId"] = numeric_id
    currencies = _currency_for(_detail())
    currencies["instruments"][0]["positions"][0]["assetCurrency"] = code
    with pytest.raises(BrokerDataUnavailable, match="account_currency_metadata_inconsistent"):
        _capture(pnl=pnl, currencies=currencies)


@pytest.mark.parametrize("numeric_id", [998, 999])
def test_gbx_code_comes_from_metadata_without_an_invented_global_numeric_id(numeric_id):
    # These IDs are synthetic examples, not assertions about eToro's namespace.
    # Only the documented USD=1 relation is known; explicit GBX metadata is the
    # source for this bracket and unrelated captures do not create an ID map.
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["assetCurrencyId"] = numeric_id
    currencies = _currency_for(_detail())
    currencies["instruments"][0]["positions"][0]["assetCurrency"] = "GBX"
    evidence, _ = _capture(pnl=pnl, currencies=currencies)
    assert evidence.currency_metadata[0].asset_currency == "GBX"
    assert evidence.position_valuations[0].asset_currency_id == numeric_id


@pytest.mark.parametrize("same_instrument,numeric_ids,codes", [
    (True, (1, 999), ("USD", "GBX")),
    (False, (999, 999), ("GBX", "USD")),
    (False, (998, 999), ("GBX", "GBX")),
])
def test_currency_pairing_contradictions_across_positions_reject_entire_capture(
        same_instrument, numeric_ids, codes):
    detail, pnl = _detail(), _pnl()
    second_detail = deepcopy(detail["clientPortfolio"]["positions"][0])
    second_detail.update(positionID=7002, instrumentID=1001 if same_instrument else 1002)
    detail["clientPortfolio"]["positions"].append(second_detail)
    second_pnl = deepcopy(pnl["clientPortfolio"]["positions"][0])
    second_pnl.update(second_detail)
    pnl["clientPortfolio"]["positions"].append(second_pnl)
    pnl["clientPortfolio"]["unrealizedPnL"] = 40
    for position, numeric_id in zip(pnl["clientPortfolio"]["positions"], numeric_ids):
        position["unrealizedPnL"]["assetCurrencyId"] = numeric_id
    currencies = _currency_for(detail)
    by_id = {str(position["positionId"]): position
             for instrument in currencies["instruments"] for position in instrument["positions"]}
    by_id["7001"]["assetCurrency"], by_id["7002"]["assetCurrency"] = codes
    securities = _page(_security()) if same_instrument else _page(_security(), _security(1002))
    with pytest.raises(BrokerDataUnavailable, match="account_currency_metadata_inconsistent"):
        _capture(detail=detail, pnl=pnl, currencies=currencies, metadata=securities)


@pytest.mark.parametrize("field", ["exposureInAccountCurrency", "exposureInAssetCurrency"])
@pytest.mark.parametrize("value", [Decimal("0"), Decimal("-1"), Decimal("1")])
def test_each_exposure_measure_must_be_strictly_positive(field, value):
    pnl = _pnl()
    pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"][field] = str(value)
    if value > 0:
        evidence, _ = _capture(pnl=pnl)
        assert evidence.valuations_fresh
    else:
        with pytest.raises(BrokerDataUnavailable, match="account_scope_not_supported: exposure"):
            _capture(pnl=pnl)
