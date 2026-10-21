"""Synthetic broker contracts; no order or real-account transport."""
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from brokers.base import BrokerDataUnavailable
from brokers.etoro.client import (EtoroCredentials, EtoroConfigurationError,
    EtoroDemoReadOnlyConfig, EtoroReadOnlyClient, HttpResponse)
from brokers.etoro.eligibility import EtoroEligibilityResponse
from brokers.etoro.endpoints import POST_DEMO_ELIGIBILITY, RateBucket
from brokers.etoro.market_evidence import capture_pilot_market_evidence
from brokers.etoro.schemas import EtoroSchemaError, EtoroSecurity
from execution.domain import Environment
from execution.instruments import Instrument
from tools import demo_market_preflight


NOW = datetime(2026, 10, 21, 14, tzinfo=timezone.utc)
INSTRUMENT = Instrument(UUID("11111111-1111-4111-8111-111111111111"), "AAPL", "NASDAQ", "equity", "USD")


def payload():
    return {"currency": "usd", "notFoundInstrumentIds": [], "notFoundSymbols": [],
        "eligibilities": [{"instrumentId": 1001, "symbol": "AAPL", "allowOpenPosition": True,
            "allowedOrderQuantityType": "all", "tradeUnitType": "units",
            "minPositionExposure": 10, "maxUnitsPerOrder": 10000,
            "leverageConfigs": [{"settlementType": "real", "direction": "long",
                "leverageValues": [1], "isPotential": False, "minPositionAmount": 10}]}]}


class Transport:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return HttpResponse(200, {}, json.dumps(self.bodies.pop(0)).encode())


def client(bodies):
    transport = Transport(bodies)
    config = EtoroDemoReadOnlyConfig(True, "https://synthetic.invalid", "ETORO_DEMO_API_KEY",
        "ETORO_DEMO_USER_KEY", 48, 96, 10, 1, 0)
    return EtoroReadOnlyClient(credentials=EtoroCredentials("synthetic-key", "synthetic-user"),
        config=config, transport=transport), transport


def test_eligibility_transport_has_fixed_body_exact_read_route_and_dedicated_budget():
    reader, transport = client([payload()])
    calls = []
    reader._limiters[RateBucket.DEMO_ELIGIBILITY] = SimpleNamespace(acquire=lambda: calls.append(1))
    response = reader.get_demo_eligibility((1001,))
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == "https://synthetic.invalid/api/v2/trading/info/demo/eligibility"
    assert json.loads(request.body) == {"instrumentIds": [1001], "currency": "USD"}
    assert request.headers["content-type"] == "application/json"
    assert response.items[0].real_long_one_available is True
    assert calls == [1]


@pytest.mark.parametrize("ids", [(), (True,), (0,), (1.1,), ("1001",), (1001, 1001), tuple(range(1, 102))])
def test_invalid_ids_never_reach_transport(ids):
    reader, transport = client([])
    with pytest.raises(ValueError):
        reader.get_demo_eligibility(ids)
    assert transport.requests == []


@pytest.mark.parametrize("endpoint,params", [
    (replace(POST_DEMO_ELIGIBILITY, path="/api/v2/trading/info/eligibility"), {"instrumentIds": [1001]}),
    (replace(POST_DEMO_ELIGIBILITY, rate_bucket=RateBucket.WRITE), {"instrumentIds": [1001]}),
    (POST_DEMO_ELIGIBILITY, {"symbols": ["AAPL"]}),
    (POST_DEMO_ELIGIBILITY, {"instrumentIds": [1001], "currency": "GBP"}),
])
def test_unapproved_eligibility_requests_do_not_reach_transport(endpoint, params):
    reader, transport = client([])
    with pytest.raises(EtoroConfigurationError):
        reader._read(endpoint, params)
    assert transport.requests == []


@pytest.mark.parametrize("mutation", [
    lambda p: p.pop("notFoundInstrumentIds"),
    lambda p: p.update(currency="GBP"),
    lambda p: p.update(notFoundInstrumentIds=[1001]),
    lambda p: p.update(eligibilities=[]),
    lambda p: p["eligibilities"].append(deepcopy(p["eligibilities"][0])),
    lambda p: p["eligibilities"][0].update(instrumentId=1002),
    lambda p: p["eligibilities"][0].update(allowOpenPosition="true"),
    lambda p: p["eligibilities"][0].update(minPositionExposure="NaN"),
    lambda p: p["eligibilities"][0]["leverageConfigs"][0].update(isPotential=None),
    lambda p: p["eligibilities"][0]["leverageConfigs"][0].update(leverageValues=[True]),
    lambda p: p["eligibilities"][0]["leverageConfigs"].append(deepcopy(p["eligibilities"][0]["leverageConfigs"][0])),
])
def test_malformed_partial_or_ambiguous_broker_evidence_fails(mutation):
    body = payload()
    mutation(body)
    with pytest.raises(EtoroSchemaError):
        EtoroEligibilityResponse.parse(body, (1001,))


def full_bodies(body=None, exchange="Nasdaq", symbol="AAPL"):
    return [{"results": [{"instrumentId": 1001, "symbol": symbol, "type": "Stocks", "exchangeId": 4}],
             "pagination": {"hasNext": False, "nextPageToken": None}},
            {"exchangeInfo": [{"exchangeID": 4, "exchangeDescription": exchange}]},
            body or payload()]


def test_capture_binds_identity_calendar_and_config_but_cannot_authorize():
    reader, transport = client(full_bodies())
    report = demo_market_preflight.preflight_report(client=reader, instrument=INSTRUMENT,
                  broker_instrument_id=1001, now=NOW)
    assert report["market_evidence_captured"] is True
    evidence = report["evidence"]
    assert evidence["session"]["internal_instrument_id"] == str(INSTRUMENT.internal_instrument_id)
    assert evidence["session"]["scheduled_regular_session_open"] is True
    assert evidence["supported_pilot_configuration"] is True
    assert report["submission_available"] is evidence["submission_available"] is False
    assert report["qualification_credit"] is False
    assert report["session_opens_at_uk"] == "2026-10-21 14:30 BST"
    assert len(transport.requests) == 3
    assert "quote_and_halt_evidence_missing" in report["blockers"]


@pytest.mark.parametrize("exchange,symbol", [("NYSE", "AAPL"), ("Nasdaq", "MSFT")])
def test_security_or_exchange_mismatch_blocks_before_eligibility(exchange, symbol):
    reader, transport = client(full_bodies(exchange=exchange, symbol=symbol))
    with pytest.raises(BrokerDataUnavailable, match="identity_mismatch"):
        capture_pilot_market_evidence(reader, INSTRUMENT, broker_instrument_id=1001, now=NOW)
    assert all("eligibility" not in request.url for request in transport.requests)


@pytest.mark.parametrize("changes", [
    {"settlementType": "cfd"}, {"direction": "short"}, {"isPotential": True}, {"leverageValues": [2]},
])
def test_missing_actual_real_long_one_configuration_is_blocked(changes):
    body = payload()
    body["eligibilities"][0]["leverageConfigs"][0].update(changes)
    reader, _ = client(full_bodies(body))
    report = demo_market_preflight.preflight_report(client=reader, instrument=INSTRUMENT,
                 broker_instrument_id=1001, now=NOW)
    assert "pilot_broker_configuration_unavailable" in report["blockers"]
    assert report["submission_available"] is False


def test_capture_crossing_close_assesses_completed_time(monkeypatch):
    from brokers.etoro import market_evidence
    start = datetime(2026, 11, 27, 17, 59, 59, tzinfo=timezone.utc)
    times = iter((start, start + timedelta(seconds=2)))
    class Clock:
        @staticmethod
        def now(zone):
            return next(times)
    monkeypatch.setattr(market_evidence, "datetime", Clock)
    reader, _ = client(full_bodies())
    evidence = capture_pilot_market_evidence(reader, INSTRUMENT, broker_instrument_id=1001)
    assert evidence.session.scheduled_regular_session_open is False
    assert evidence.session.assessed_at == start + timedelta(seconds=2)


def test_disabled_cli_never_loads_credentials_or_initializes_client(monkeypatch, capsys):
    monkeypatch.setattr(demo_market_preflight.EtoroDemoReadOnlyConfig, "load", lambda _: SimpleNamespace(enabled=False))
    monkeypatch.setattr(demo_market_preflight.EtoroReadOnlyClient, "from_config", lambda _: pytest.fail("credentials must not load"))
    assert demo_market_preflight.main([]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["submission_available"] is False
    assert report["market_evidence_captured"] is False


def test_capture_errors_are_redacted(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private token and broker body")
    monkeypatch.setattr(demo_market_preflight, "capture_pilot_market_evidence", fail)
    report = demo_market_preflight.preflight_report(client=object(), instrument=INSTRUMENT, broker_instrument_id=1001)
    assert "private token" not in json.dumps(report)
    assert report["capture_error_type"] == "RuntimeError"
