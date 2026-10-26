"""Synthetic currency-only metadata contracts; no network or valuation source."""
from copy import deepcopy
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit, urlencode

import pytest

from brokers.etoro.capture_transport import _check_route, CaptureBoundaryError
from brokers.etoro.client import EtoroConfigurationError, HttpRequest
from brokers.etoro.endpoints import GET_DEMO_POSITION_CURRENCIES
from brokers.etoro.pnl_currency_metadata import (
    PNL_CURRENCY_METADATA_QUERY, parse_demo_position_currencies,
)
from brokers.etoro.schemas import EtoroDetailedPortfolio, EtoroSchemaError
from tests.contracts.etoro.test_etoro_account_evidence import _client, _detail


def _currency_metadata():
    return {
        "accountCurrency": "USD", "timestamp": "ambiguous-account-time", "mirrors": [],
        "instruments": [{"instrumentId": 1001, "orders": [], "positions": [{
            "positionId": 7001, "instrumentId": 1001, "assetCurrency": "USD",
            "direction": "long", "leverage": 1, "settlementType": "real", "units": 10,
            "rateTimestamp": "stale-or-ambiguous", "currentRate": None, "acctCcyReturn": None,
        }]}],
    }


def _positions():
    return EtoroDetailedPortfolio.parse(_detail()).positions


def _parse(body):
    return parse_demo_position_currencies(body, "9001", _positions())


def test_metadata_matches_exact_holdings_and_ignores_all_valuation_values():
    body = _currency_metadata()
    original = deepcopy(body)
    value, = _parse(body)
    assert (value.position_id, value.instrument_id, value.asset_currency) == ("7001", 1001, "USD")
    assert not hasattr(value, "timestamp")
    assert not hasattr(value, "observed_at")
    assert not hasattr(value, "pnl")
    assert body == original
    body.pop("timestamp")
    body["instruments"][0]["positions"][0].pop("rateTimestamp")
    assert _parse(body) == (value,)


def test_gbx_is_accepted_from_explicit_code_without_guessing_a_numeric_currency_id():
    body = _currency_metadata()
    body["instruments"][0]["positions"][0]["assetCurrency"] = "GBX"
    assert _parse(body)[0].asset_currency == "GBX"


@pytest.mark.parametrize("field,value", [
    ("positionId", 7002), ("positionId", True), ("instrumentId", 1002),
    ("units", 11), ("units", "10.000000000000001"), ("leverage", 2),
    ("direction", "short"), ("settlementType", "cfd"),
    ("assetCurrency", "GBP"), ("assetCurrency", "EUR"), ("assetCurrency", None),
    ("assetCurrency", "usd"), ("assetCurrency", "USD "), ("assetCurrency", []),
])
def test_inconsistent_position_currency_metadata_is_rejected(field, value):
    body = _currency_metadata()
    body["instruments"][0]["positions"][0][field] = value
    with pytest.raises(EtoroSchemaError):
        _parse(body)


@pytest.mark.parametrize("field", ["positionId", "instrumentId", "units", "leverage", "direction", "settlementType", "assetCurrency"])
def test_missing_metadata_identity_or_structural_fields_are_not_inferred(field):
    body = _currency_metadata()
    del body["instruments"][0]["positions"][0][field]
    with pytest.raises(EtoroSchemaError):
        _parse(body)


@pytest.mark.parametrize("mutation", [
    "missing_instruments", "missing_group", "extra_group", "duplicate_group", "duplicate_position",
    "missing_position", "orders_missing", "orders_nonempty", "orders_null",
    "mirrors_missing", "mirror", "currency", "unknown_orders", "unknown_root_orders",
])
def test_complete_currency_metadata_scope_is_required(mutation):
    body = _currency_metadata()
    group = body["instruments"][0]
    if mutation == "missing_instruments": del body["instruments"]
    elif mutation == "missing_group": body["instruments"] = []
    elif mutation == "extra_group": body["instruments"].append({"instrumentId": 1002, "positions": [], "orders": []})
    elif mutation == "duplicate_group": body["instruments"].append(deepcopy(group))
    elif mutation == "duplicate_position": group["positions"].append(deepcopy(group["positions"][0]))
    elif mutation == "missing_position": group["positions"] = []
    elif mutation == "orders_missing": del group["orders"]
    elif mutation == "orders_nonempty": group["orders"] = [{"orderId": 1}]
    elif mutation == "orders_null": group["orders"] = None
    elif mutation == "mirrors_missing": del body["mirrors"]
    elif mutation == "mirror": body["mirrors"] = [{"mirrorId": 1}]
    elif mutation == "currency": body["accountCurrency"] = "EUR"
    elif mutation == "unknown_orders": group["pendingOrders"] = []
    elif mutation == "unknown_root_orders": body["orders"] = []
    with pytest.raises(EtoroSchemaError):
        _parse(body)


def test_client_routes_one_unfiltered_read_with_cid_in_header_and_no_body():
    client, transport = _client([_currency_metadata()])
    result = client.get_demo_position_currencies("9001", _positions())
    assert result == _parse(_currency_metadata())
    request, = transport.requests
    assert request.method == "GET" and request.body is None
    assert urlsplit(request.url).path == GET_DEMO_POSITION_CURRENCIES.path
    assert parse_qs(urlsplit(request.url).query) == {key: [value] for key, value in PNL_CURRENCY_METADATA_QUERY}
    assert request.headers["CID"] == "9001"
    assert "9001" not in request.url


@pytest.mark.parametrize("customer_id", [None, True, 9001, "", "0", "-1", "9001.0", "09001", "９００１", "2147483648", "9001\r\nInjected: value"])
def test_invalid_customer_id_fails_before_network(customer_id):
    client, transport = _client([_currency_metadata()])
    with pytest.raises(EtoroSchemaError):
        client.get_demo_position_currencies(customer_id, _positions())
    assert transport.requests == []


def test_inconsistent_expected_customer_identity_fails_before_network():
    client, transport = _client([_currency_metadata()])
    with pytest.raises(EtoroSchemaError):
        client.get_demo_position_currencies("9002", _positions())
    assert transport.requests == []


@pytest.mark.parametrize("mutation", ["filter", "hide_positions", "hide_orders", "hide_mirrors", "realtime", "missing_cid"])
def test_client_rejects_filtered_or_changed_detail_requests(mutation):
    client, transport = _client([_currency_metadata()])
    params = {**dict(PNL_CURRENCY_METADATA_QUERY), "CID": "9001"}
    if mutation == "filter": params["instrumentIds"] = [1001]
    elif mutation == "hide_positions": params["positionLevel"] = "None"
    elif mutation == "hide_orders": params["orderLevel"] = "None"
    elif mutation == "hide_mirrors": params["mirrorLevel"] = "None"
    elif mutation == "realtime": params["conversionMode"] = "Realtime"
    elif mutation == "missing_cid": del params["CID"]
    with pytest.raises(EtoroConfigurationError):
        client._read(GET_DEMO_POSITION_CURRENCIES, params)
    assert transport.requests == []


def _request():
    url = "https://public-api.etoro.com" + GET_DEMO_POSITION_CURRENCIES.path + "?" + urlencode(PNL_CURRENCY_METADATA_QUERY)
    return HttpRequest("GET", url, {"CID": "9001"}, 1)


def test_recorder_admits_only_the_fixed_currency_metadata_request():
    request = _request()
    assert _check_route(request).path == GET_DEMO_POSITION_CURRENCIES.path


@pytest.mark.parametrize("mutation", ["body", "post", "filter", "missing_query", "duplicate", "hide", "missing_cid", "invalid_cid", "real"])
def test_recorder_rejects_currency_metadata_request_drift(mutation):
    request = _request()
    if mutation == "body": request = replace(request, body=b"{}")
    elif mutation == "post": request = replace(request, method="POST")
    elif mutation == "filter": request = replace(request, url=request.url + "&instrumentIds=1001")
    elif mutation == "missing_query": request = replace(request, url=request.url.split("?")[0])
    elif mutation == "duplicate": request = replace(request, url=request.url + "&mirrorLevel=Details")
    elif mutation == "hide": request = replace(request, url=request.url.replace("mirrorLevel=Details", "mirrorLevel=None"))
    elif mutation == "missing_cid": request = replace(request, headers={})
    elif mutation == "invalid_cid": request = replace(request, headers={"CID": "9001\nInjected: 1"})
    elif mutation == "real": request = replace(request, url=request.url.replace("/demo/", "/real/"))
    with pytest.raises(CaptureBoundaryError):
        _check_route(request)
