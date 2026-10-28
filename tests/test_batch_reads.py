"""Synthetic direct market receipts preserve source and handoff clocks."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from brokers.etoro.autonomous_api import _json_body
from brokers.etoro.batch_reads import (
    BatchMarketError, DirectBatchReads, RATE_PATH, SEARCH_PATH,
    decode_market_state, validate_market_evidence,
)
from brokers.etoro.capture_transport import RawCaptureResponse
from execution.domain import canonical_json
from tests.test_batch_assessment import NOW, instrument_scope
from tests.test_pilot_reads import KEYS


class MarketTransport:
    def __init__(self, clock, responses):
        self.clock, self.responses, self.calls = clock, list(responses), []

    def send(self, request):
        self.calls.append(request)
        self.clock[0] += timedelta(milliseconds=100)
        response = self.responses.pop(0)
        body = _json_body(response)
        if urlsplit(request.url).path == SEARCH_PATH:
            fields = parse_qs(urlsplit(request.url).query)["fields"][0].split(",")
            # Reproduce the observed provider projection defect from PR16: the
            # ID is unconditional and selecting it repeats the JSON key.
            if "instrumentId" in fields and response.get("items"):
                marker = b'"instrumentId":' + str(response["items"][0]["instrumentId"]).encode()
                body = body.replace(marker, marker + b"," + marker)
        return RawCaptureResponse(200, body)


def setup_market(tmp_path, *, instrument_id=1004, symbol="MSFT", edit_state=None, edit_rate=None):
    instrument, spec = instrument_scope(instrument_id, symbol)
    state = {"items": [{"instrumentId": instrument_id, "internalSymbolFull": symbol,
        "isCurrentlyTradable": True, "isExchangeOpen": True, "isActiveInPlatform": True,
        "isBuyEnabled": True, "isDelisted": False}], "totalItems": 1}
    rates = {"results": [{"instrumentId": instrument_id, "quoteType": "realtime",
        "date": NOW.isoformat(), "ask": D("100.0000000000000000000001"), "bid": D("99.99")}]}
    if edit_state:
        edit_state(state)
    if edit_rate:
        edit_rate(rates)
    clock = [NOW]
    transport = MarketTransport(clock, [state, rates])
    host = DirectBatchReads(credentials=KEYS, receipt_dir=tmp_path / "reads", clock=lambda: clock[0], transport=transport)
    return host, instrument, spec, clock, transport


@pytest.mark.parametrize("instrument_id,symbol", [(1004, "MSFT"), (1142, "SBUX"), (8802, "SYNTHETIC")])
def test_generic_exact_id_symbol_mapping_and_explicit_independent_clocks(tmp_path, instrument_id, symbol):
    host, instrument, spec, clock, transport = setup_market(tmp_path, instrument_id=instrument_id, symbol=symbol)
    quote, state = host.market(instrument, spec)
    assert quote.internal_instrument_id == instrument.internal_instrument_id
    assert quote.observed_at == NOW and quote.ask == D("100.0000000000000000000001")
    assert state["quote_source_at"] == NOW.isoformat()
    assert state["state_request_started_at"] == NOW.isoformat()
    assert state["state_received_at"] == (NOW + timedelta(milliseconds=100)).isoformat()
    assert state["quote_request_started_at"] == state["state_received_at"]
    assert state["quote_received_at"] == (NOW + timedelta(milliseconds=200)).isoformat()
    assert state["quote_currency_basis"] == "reviewed_instrument_spec_and_registry"
    assert all(call.method == "GET" and call.body is None for call in transport.calls)
    assert SEARCH_PATH + "?instrumentId=" + str(instrument_id) in transport.calls[0].url
    assert "instrumentId" not in parse_qs(urlsplit(transport.calls[0].url).query)["fields"][0].split(",")
    assert RATE_PATH + "?instrumentIds=" + str(instrument_id) in transport.calls[1].url
    validate_market_evidence(quote=quote, market_state=state, instrument=instrument,
        instrument_spec=spec, now=clock[0], require_handoff=True)


def test_persisted_market_state_reparses_original_json_without_decimal_loss(tmp_path):
    host, instrument, spec, clock, _ = setup_market(tmp_path)
    quote, state = host.market(instrument, spec)
    encoded = canonical_json(state)
    decoded = decode_market_state(json.loads(encoded))
    assert decoded == state
    assert decoded["rate_receipt"]["result"]["body"] == state["rate_receipt"]["result"]["body"]
    validate_market_evidence(quote=quote, market_state=decoded, instrument=instrument,
        instrument_spec=spec, now=clock[0], require_handoff=True)
    with pytest.raises(BatchMarketError, match="persisted_shape"):
        decode_market_state({**json.loads(encoded), "unexpected": 1})


@pytest.mark.parametrize("field,value", [("isExchangeOpen", False), ("isExchangeOpen", None),
    ("isCurrentlyTradable", False), ("isActiveInPlatform", 1), ("isBuyEnabled", False),
    ("isDelisted", True)])
def test_quote_movements_do_not_override_exchange_or_tradability_conflicts(tmp_path, field, value):
    host, instrument, spec, _, _ = setup_market(tmp_path, edit_state=lambda s: s["items"][0].update({field: value}))
    with pytest.raises(BatchMarketError, match="not_open_and_tradable"):
        host.market(instrument, spec)


@pytest.mark.parametrize("edit", [
    lambda s: s.update(totalItems=2),
    lambda s: s.update(totalItems=True),
    lambda s: s["items"].append(deepcopy(s["items"][0])),
    lambda s: s["items"][0].update(instrumentId=1142),
    lambda s: s["items"][0].pop("instrumentId"),
    lambda s: s["items"][0].update(internalSymbolFull="SBUX"),
])
def test_complete_exact_market_identity_is_required(tmp_path, edit):
    host, instrument, spec, _, _ = setup_market(tmp_path, edit_state=edit)
    with pytest.raises(BatchMarketError):
        host.market(instrument, spec)


@pytest.mark.parametrize("edit", [
    lambda r: r["results"].append(deepcopy(r["results"][0])),
    lambda r: r["results"][0].update(instrumentId=1142),
    lambda r: r["results"][0].update(quoteType="delayed"),
    lambda r: r["results"][0].update(currency="GBX"),
    lambda r: r["results"][0].update(quoteCurrency="GBP"),
    lambda r: r["results"][0].update(ask="100"),
    lambda r: r["results"][0].update(ask=D("99")),
    lambda r: r["results"][0].update(date=(NOW - timedelta(seconds=30)).isoformat()),
    lambda r: r["results"][0].update(date=(NOW + timedelta(seconds=1)).isoformat()),
    lambda r: r["results"][0].update(date="2026-10-27T14:30:00-00:00"),
])
def test_exact_fresh_usd_realtime_quote_is_required(tmp_path, edit):
    host, instrument, spec, _, _ = setup_market(tmp_path, edit_rate=edit)
    with pytest.raises(BatchMarketError):
        host.market(instrument, spec)


def test_documented_v2_utc_normalization_retains_original_spelling(tmp_path):
    host, instrument, spec, _, _ = setup_market(tmp_path,
        edit_rate=lambda r: r["results"][0].update(date="2026-10-27T14:30:00.0000001"))
    quote, state = host.market(instrument, spec)
    assert quote.observed_at == NOW  # Conservative floor for Python's microsecond clock.
    assert state["quote_source_at"] == "2026-10-27T14:30:00.0000001"
    assert state["quote_source_normalized_utc"] == "2026-10-27T14:30:00.0000001Z"
    assert state["quote_source_epoch_seconds"] % 1 == D(".0000001")
    assert state["rate_time_basis"] == "getRates.date_documented_UTC"


def test_five_second_quote_receipt_handoff_is_separate_from_source_age(tmp_path):
    host, instrument, spec, clock, _ = setup_market(tmp_path)
    quote, state = host.market(instrument, spec)
    kwargs = dict(quote=quote, market_state=state, instrument=instrument, instrument_spec=spec)
    validate_market_evidence(**kwargs, now=clock[0] + timedelta(seconds=5), require_handoff=True)
    with pytest.raises(BatchMarketError, match="handoff"):
        validate_market_evidence(**kwargs, now=clock[0] + timedelta(seconds=5, microseconds=1), require_handoff=True)
    validate_market_evidence(**kwargs, now=clock[0] + timedelta(seconds=6))
    with pytest.raises(BatchMarketError, match="stale"):
        validate_market_evidence(**kwargs, now=NOW + timedelta(seconds=15, microseconds=1))


@pytest.mark.parametrize("target", ["projection", "quote", "receipt", "source_clock", "scope"])
def test_retained_evidence_is_reparsed_before_dispatch(tmp_path, target):
    host, instrument, spec, clock, _ = setup_market(tmp_path)
    quote, state = host.market(instrument, spec)
    if target == "projection":
        state["quote_currency"] = "GBP"
    elif target == "quote":
        quote = replace(quote, ask=D(101))
    elif target == "receipt":
        state["rate_receipt"]["raw_response"]["body_sha256"] = "0" * 64
    elif target == "source_clock":
        state["quote_source_epoch_seconds"] += 1
    else:
        spec["instrument_id"] = 1142
    with pytest.raises(BatchMarketError):
        validate_market_evidence(quote=quote, market_state=state, instrument=instrument,
            instrument_spec=spec, now=clock[0], require_handoff=True)


def test_non_usd_mapping_is_rejected_before_any_read(tmp_path):
    host, instrument, spec, _, transport = setup_market(tmp_path)
    spec["currency"] = "GBX"
    with pytest.raises(BatchMarketError):
        host.market(instrument, spec)
    assert transport.calls == []
