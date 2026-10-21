"""Synthetic offline contracts, not acceptance evidence from a live account."""
from __future__ import annotations

import json
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import pytest

from brokers.base import BrokerDataUnavailable, BrokerWriteDisabled
from brokers.etoro.account_evidence import capture_pretrade_account_evidence
from brokers.etoro.adapter import EtoroDemoExecutionAdapter
from brokers.etoro.client import (
    EtoroClientError, EtoroCredentials, EtoroDemoReadOnlyConfig,
    EtoroReadOnlyClient, HttpResponse,
)
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS, EtoroSchemaError
from execution.domain import BrokerCommand, Environment, OrderIntent


NOW = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)


class Transport:
    def __init__(self, bodies):
        self.responses = deque(
            body if isinstance(body, HttpResponse) else
            HttpResponse(status_code=200, headers={}, body=json.dumps(body).encode())
            for body in bodies
        )
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        assert request.method == "GET" and request.body is None
        assert "/real/" not in request.url
        return self.responses.popleft()


def _client(bodies):
    transport = Transport(bodies)
    client = EtoroReadOnlyClient(
        credentials=EtoroCredentials(api_key="synthetic-key", user_key="synthetic-user"),
        config=EtoroDemoReadOnlyConfig(
            enabled=True, base_url="https://synthetic.invalid",
            api_key_environment_variable="ETORO_DEMO_API_KEY",
            user_key_environment_variable="ETORO_DEMO_USER_KEY",
            account_reads_per_minute=48, market_reads_per_minute=96,
            writes_per_minute=10, timeout_seconds=1, max_read_retries=0,
        ),
        transport=transport, sleeper=lambda _: None,
    )
    return client, transport


def _detail():
    return {"clientPortfolio": {
        "credit": 9000, "bonusCredit": 0, "mirrors": [],
        **{name: [] for name in DEMO_PENDING_COLLECTIONS},
        "positions": [{
            "positionID": 7001, "CID": 9001, "instrumentID": 1001,
            "mirrorID": 0, "units": 10, "amount": 1000, "isBuy": True,
            "leverage": 1, "settlementTypeID": 1,
        }],
    }}


def _aggregate():
    return {
        "cid": 9001, "timestamp": NOW.isoformat(), "accountCurrency": "USD",
        "accountTotals": {
            "accountAvailableCash": 9000, "accountFrozenCash": 0,
            "accountCurrentPnl": 20, "accountTotalValue": 10020,
            "accountTotalUsedMargin": 1000, "accountBalance": 9000,
        },
        "instrumentAggregates": [{
            "instrumentId": 1001, "assetCurrency": "USD", "netUnits": 10,
            "netCurrentExposureAccountCurrency": 1020, "avgLeverage": 1,
            "accountCurrencyReturn": 20,
        }], "mirrors": [],
    }


def _security(identifier=1001, kind="Stocks"):
    return {"instrumentId": identifier, "symbol": "SYNTH", "type": kind, "exchangeId": 1}


def _page(*securities, has_next=False, token=None):
    return {"results": list(securities), "pagination": {"hasNext": has_next, "nextPageToken": token}}


def _capture(detail=None, aggregate=None, metadata=None, last=None):
    detail = _detail() if detail is None else detail
    aggregate = _aggregate() if aggregate is None else aggregate
    metadata = _page(_security()) if metadata is None else metadata
    last = deepcopy(detail) if last is None else last
    bodies = [detail, aggregate]
    if detail["clientPortfolio"].get("positions"):
        bodies.append(metadata)
    bodies.append(last)
    client, transport = _client(bodies)
    evidence = capture_pretrade_account_evidence(client, expected_demo_customer_id="9001", now=NOW)
    return evidence, transport


def test_capture_qualifies_only_explicit_empty_pending_and_preserves_limitations(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    evidence, transport = _capture()
    assert not list(tmp_path.iterdir())  # No ExecutionStore/registry/files initialized.
    snapshot = evidence.snapshot
    assert snapshot.pending_orders_complete is True
    assert snapshot.pending_orders == ()
    assert snapshot.executions_complete is False
    assert snapshot.positions[0].current_exposure == Decimal("1020")
    assert snapshot.current_pnl == Decimal("20")
    assert evidence.daily_pnl is None and evidence.peak_equity is None
    assert evidence.checked_pending_collections == DEMO_PENDING_COLLECTIONS
    assert "execution_history_not_complete" in evidence.limitations
    assert "bracketed_reads_are_not_an_atomic_broker_snapshot" in evidence.limitations
    assert evidence.contract_version == "v1.398.0"
    assert len(evidence.security_metadata_checksum) == 64
    assert len(transport.requests) == 4
    assert all(not urlparse(item.url).query for item in transport.requests if "/demo/" in item.url)
    assert parse_qs(urlparse(transport.requests[2].url).query) == {"instrumentsIds": ["1001"], "pageSize": ["100"]}


def test_empty_account_still_requires_detailed_and_aggregate_identity():
    detail, aggregate = _detail(), _aggregate()
    detail["clientPortfolio"]["positions"] = []
    aggregate["instrumentAggregates"] = []
    aggregate["accountTotals"].update(accountCurrentPnl=0, accountTotalValue=9000, accountTotalUsedMargin=0)
    evidence, transport = _capture(detail=detail, aggregate=aggregate)
    assert evidence.snapshot.positions == ()
    assert len(transport.requests) == 3  # No invented metadata response required.
    aggregate["cid"] = 9002
    with pytest.raises(BrokerDataUnavailable, match="identity_mismatch"):
        _capture(detail=detail, aggregate=aggregate)


@pytest.mark.parametrize("name", (*DEMO_PENDING_COLLECTIONS, "mirrors", "positions", "credit", "bonusCredit"))
def test_omitted_account_fields_never_become_empty(name):
    detail = _detail()
    del detail["clientPortfolio"][name]
    with pytest.raises(EtoroSchemaError):
        _capture(detail=detail)


@pytest.mark.parametrize("name", (*DEMO_PENDING_COLLECTIONS, "mirrors"))
def test_any_nonempty_order_or_mirror_collection_blocks_whole_account(name):
    detail = _detail()
    detail["clientPortfolio"][name] = [{"synthetic": "unsupported"}]
    with pytest.raises(BrokerDataUnavailable, match="account_scope_not_supported"):
        _capture(detail=detail)


def test_new_order_family_is_not_silently_ignored():
    detail = _detail()
    detail["clientPortfolio"]["futureOrders"] = []
    with pytest.raises(EtoroSchemaError, match="unhandled order"):
        _capture(detail=detail)


@pytest.mark.parametrize("field,value", [
    ("isBuy", False), ("leverage", 2), ("settlementTypeID", 0),
    ("settlementTypeID", 3), ("settlementTypeID", 99), ("mirrorID", 1),
])
def test_unsupported_holdings_are_not_discarded(field, value):
    detail = _detail()
    detail["clientPortfolio"]["positions"][0][field] = value
    with pytest.raises(BrokerDataUnavailable, match="account_scope_not_supported"):
        _capture(detail=detail)


@pytest.mark.parametrize("field", ["CID", "instrumentID", "mirrorID", "isBuy", "leverage", "settlementTypeID", "units", "amount"])
def test_omitted_position_properties_never_get_safe_defaults(field):
    detail = _detail()
    del detail["clientPortfolio"]["positions"][0][field]
    with pytest.raises(EtoroSchemaError):
        _capture(detail=detail)


def test_duplicate_position_fails():
    detail = _detail()
    detail["clientPortfolio"]["positions"] *= 2
    with pytest.raises(EtoroSchemaError, match="duplicate position"):
        _capture(detail=detail)


@pytest.mark.parametrize("bad_id", [1001.9, True, False, 0, -1])
def test_fractional_boolean_or_nonpositive_aggregate_id_fails(bad_id):
    aggregate = _aggregate()
    aggregate["instrumentAggregates"][0]["instrumentId"] = bad_id
    with pytest.raises(EtoroSchemaError, match="integer"):
        _capture(aggregate=aggregate)


@pytest.mark.parametrize("location", ["detailed", "aggregate"])
def test_wrong_demo_identity_rejected(location):
    detail, aggregate = _detail(), _aggregate()
    if location == "detailed":
        detail["clientPortfolio"]["positions"][0]["CID"] = 9002
    else:
        aggregate["cid"] = 9002
    with pytest.raises(BrokerDataUnavailable, match="identity_mismatch"):
        _capture(detail=detail, aggregate=aggregate)


@pytest.mark.parametrize("identity", [None, "", "wrong", "-1", "0", True, "9001.5"])
def test_expected_identity_required_before_network(identity):
    client, transport = _client([])
    with pytest.raises(BrokerDataUnavailable, match="expected_demo_account_identity_required"):
        capture_pretrade_account_evidence(client, expected_demo_customer_id=identity, now=NOW)
    assert transport.requests == []


@pytest.mark.parametrize("offset", [-16, 1])
def test_aggregate_timestamp_must_be_fresh_and_not_future(offset):
    aggregate = _aggregate()
    aggregate["timestamp"] = (NOW + timedelta(seconds=offset)).isoformat()
    with pytest.raises(BrokerDataUnavailable, match="stale_or_future"):
        _capture(aggregate=aggregate)


def test_detailed_capture_must_be_unchanged_at_both_ends():
    last = _detail()
    last["clientPortfolio"]["credit"] = 8999
    with pytest.raises(BrokerDataUnavailable, match="changed_during_capture"):
        _capture(last=last)


@pytest.mark.parametrize("field,value", [
    ("accountFrozenCash", 1), ("accountAvailableCash", 8999),
    ("accountBalance", 9001), ("accountTotalValue", 10021),
    ("accountTotalUsedMargin", 1001), ("accountCurrentPnl", 21),
])
def test_contradictory_totals_do_not_qualify(field, value):
    aggregate = _aggregate()
    aggregate["accountTotals"][field] = value
    with pytest.raises(BrokerDataUnavailable):
        _capture(aggregate=aggregate)


@pytest.mark.parametrize("field,value", [
    ("assetCurrency", "EUR"), ("avgLeverage", 2), ("netUnits", -10),
    ("netUnits", 9), ("netCurrentExposureAccountCurrency", 0),
    ("netCurrentExposureAccountCurrency", 20),
])
def test_contradictory_or_unsupported_aggregates_do_not_qualify(field, value):
    aggregate = _aggregate()
    aggregate["instrumentAggregates"][0][field] = value
    with pytest.raises(BrokerDataUnavailable):
        _capture(aggregate=aggregate)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong", "mirrors_missing", "mirror", "currency"])
def test_aggregate_coverage_and_scope_must_match_all_positions(mutation):
    aggregate = _aggregate()
    if mutation == "missing":
        aggregate["instrumentAggregates"] = []
    elif mutation == "duplicate":
        aggregate["instrumentAggregates"] *= 2
    elif mutation == "wrong":
        aggregate["instrumentAggregates"][0]["instrumentId"] = 1002
    elif mutation == "mirrors_missing":
        del aggregate["mirrors"]
    elif mutation == "mirror":
        aggregate["mirrors"] = [{}]
    else:
        aggregate["accountCurrency"] = "EUR"
    with pytest.raises((BrokerDataUnavailable, EtoroSchemaError)):
        _capture(aggregate=aggregate)


@pytest.mark.parametrize("metadata", [
    _page(), _page(_security(), _security()), _page(_security(1002)),
    _page(_security(1001.5)), _page(_security(), _security(1002)),
])
def test_metadata_exact_id_coverage_rejects_missing_duplicate_wrong_or_fractional(metadata):
    with pytest.raises(EtoroSchemaError):
        _capture(metadata=metadata)


@pytest.mark.parametrize("kind", ["Crypto", "CFD", "Unknown", "stocks"])
def test_real_settlement_does_not_imply_stock_or_etf(kind):
    with pytest.raises(BrokerDataUnavailable, match="instrument type"):
        _capture(metadata=_page(_security(kind=kind)))


def test_metadata_pagination_requires_exact_complete_terminal_set():
    client, transport = _client([
        _page(_security(), has_next=True, token="next"),
        _page(_security(1002), token="allowed-terminal-token"),
    ])
    assert [item.instrument_id for item in client.get_security_metadata((1002, 1001))] == [1001, 1002]
    assert parse_qs(urlparse(transport.requests[1].url).query)["pageToken"] == ["next"]


@pytest.mark.parametrize("pages", [
    [_page(_security(), has_next=True)],
    [_page(_security(), has_next=True, token=123)],
    [_page(has_next=True, token="empty")],
    [_page(_security(), has_next=True, token="repeat"), _page(_security(1002), has_next=True, token="repeat")],
    [_page(_security(), has_next=True, token="next"), _page(_security())],
])
def test_invalid_or_cyclic_metadata_pagination_fails_closed(pages):
    client, _ = _client(pages)
    with pytest.raises(EtoroSchemaError):
        client.get_security_metadata((1001, 1002))


def test_aggregate_415_is_a_blocker_not_a_route_or_method_fallback():
    client, transport = _client([
        _detail(), HttpResponse(status_code=415, headers={}, body=b"synthetic error"),
    ])
    with pytest.raises(EtoroClientError, match="415"):
        capture_pretrade_account_evidence(client, expected_demo_customer_id="9001", now=NOW)
    assert len(transport.requests) == 2
    assert all(item.method == "GET" for item in transport.requests)


def _submission_fixture():
    intent = OrderIntent.create(
        strategy_id="synthetic", run_id="synthetic",
        internal_instrument_id=UUID("00000000-0000-4000-8000-000000000001"),
        environment="demo", side="buy", order_type="market",
        sizing_method="fixed_notional", sizing_value=Decimal("100"),
        currency="USD", expires_at=NOW + timedelta(minutes=5),
    )
    payload = {"action": "open", "transaction": "buy", "instrumentId": 1001,
               "settlementType": "real", "orderType": "mkt", "leverage": 1,
               "amount": "100", "orderCurrency": "usd"}
    command = BrokerCommand.create(
        intent_hash=intent.intent_hash, broker="etoro", environment=Environment.DEMO,
        operation="submit_order", broker_payload=payload,
    )
    class ExecutionClient:
        environment = Environment.DEMO
        read_only = False
        calls = []
        def submit_open_order(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return SimpleNamespace(order_id="synthetic-order", reference_id="synthetic-reference")
    execution_client = ExecutionClient()
    read_client, _ = _client([])
    registry = SimpleNamespace(broker_mapping=lambda *args, **kwargs: SimpleNamespace(broker_instrument_id=1001))
    adapter = EtoroDemoExecutionAdapter(client=read_client, execution_client=execution_client, registry=registry)
    return adapter, intent, command, execution_client


@pytest.mark.parametrize("tamper", ["intent", "payload", "mapping"])
def test_submission_adapter_rejects_unbound_intent_payload_or_instrument(tamper):
    adapter, intent, command, execution_client = _submission_fixture()
    if tamper == "intent":
        command = replace(command, intent_hash="0" * 64)
    elif tamper == "payload":
        command = replace(command, payload_hash="0" * 64)
    else:
        adapter.registry = SimpleNamespace(broker_mapping=lambda *args, **kwargs: SimpleNamespace(broker_instrument_id=1002))
    with pytest.raises(BrokerWriteDisabled, match="hash"):
        adapter.submit_order(intent, command)
    assert execution_client.calls == []
