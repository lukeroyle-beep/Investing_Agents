from copy import deepcopy
import json

import pytest

from scripts.prepare_etoro_history import normalize_capture


def response(body):
    return {"statusCode": 200, "isSuccess": True, "bodyTruncated": False,
            "body": json.dumps(body)}


@pytest.fixture
def capture():
    securities = [{"symbol": "AAPL", "instrumentId": 1001, "type": "Stocks", "exchangeId": 4},
                  {"symbol": "SPY", "instrumentId": 3000, "type": "ETF", "exchangeId": 5}]
    return {"schema_version": 1, "source": "etoro_public_api",
            "captured_at_utc": "2024-01-04T15:00:00Z",
            "request": {"direction": "asc", "interval": "OneDay", "candles_count": 3},
            "securities": response({"results": securities, "pagination": {"hasNext": False}}),
            "histories": {item["symbol"]: response({"interval": "OneDay", "candles": [
                {"instrumentId": item["instrumentId"], "candles": [
                    {"instrumentID": item["instrumentId"], "fromDate": f"2024-01-0{day}T00:00:00Z",
                     "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}
                    for day in (2, 3, 4)]}]}) for item in securities}}


CONFIG = {"tickers": ["AAPL"], "benchmark_tickers": ["SPY"], "start_date": "2024-01-01", "end_date": ""}


def mutate_body(capture, mutate):
    envelope = capture["histories"]["AAPL"]
    body = json.loads(envelope["body"])
    mutate(body)
    envelope["body"] = json.dumps(body)


def test_literal_provider_labels_and_conservative_cutoff_are_auditable(capture):
    before = deepcopy(capture)
    frame, manifest = normalize_capture(capture, CONFIG)
    assert capture == before
    assert set(frame["Date"].dt.strftime("%Y-%m-%d")) == {"2024-01-02", "2024-01-03"}
    assert len(frame) == 4
    assert all(item["excluded_labels"] == ["2024-01-04"] for item in manifest["instruments"])
    assert all(len(item["raw_body_sha256"]) == 64 for item in manifest["instruments"])
    assert all(value is False for value in manifest["qualification"].values())
    assert manifest["return_target_pct"] is None
    assert manifest["broker_orders_submitted"] == 0


def test_cutoff_uses_new_york_date_even_when_utc_has_advanced(capture):
    capture["captured_at_utc"] = "2024-01-05T01:00:00Z"
    frame, manifest = normalize_capture(capture, CONFIG)
    assert manifest["completed_before"] == "2024-01-04"
    assert frame["Date"].max().day == 3
    with pytest.raises(ValueError, match="current New York"):
        normalize_capture(capture, CONFIG, completed_before="2024-01-05")


@pytest.mark.parametrize("field,value", [("statusCode", 500), ("isSuccess", False),
                                       ("bodyTruncated", True), ("bodyTruncated", None)])
def test_failed_or_unproven_complete_capture_is_rejected(capture, field, value):
    capture["histories"]["AAPL"][field] = value
    with pytest.raises(ValueError, match="untruncated"):
        normalize_capture(capture, CONFIG)


@pytest.mark.parametrize("mutation", [
    lambda body: body.update(interval="OneHour"),
    lambda body: body["candles"][0].update(instrumentId=999),
    lambda body: body["candles"][0]["candles"][0].update(instrumentID=999),
    lambda body: body["candles"][0]["candles"].pop(),
    lambda body: body["candles"][0]["candles"].reverse(),
    lambda body: body["candles"][0]["candles"][0].update(fromDate="2024-01-02T00:00:00"),
    lambda body: body["candles"][0]["candles"][0].update(fromDate="2024-01-02T14:30:00Z"),
    lambda body: body["candles"][0]["candles"][1].update(fromDate="2024-01-02T00:00:00Z"),
    lambda body: body["candles"][0]["candles"][-1].update(fromDate="2024-01-05T00:00:00Z"),
    lambda body: body["candles"][0]["candles"][0].update(high=99),
    lambda body: body["candles"][0]["candles"][0].update(close=float("nan")),
    lambda body: body["candles"][0]["candles"][0].update(open=True),
    lambda body: body["candles"][0]["candles"][0].update(volume=-1),
])
def test_malformed_history_cannot_be_repaired_silently(capture, mutation):
    mutate_body(capture, mutation)
    with pytest.raises(ValueError):
        normalize_capture(capture, CONFIG)


def test_metadata_pagination_and_exact_membership_are_required(capture):
    body = json.loads(capture["securities"]["body"])
    body["pagination"]["hasNext"] = True
    capture["securities"] = response(body)
    with pytest.raises(ValueError, match="pagination"):
        normalize_capture(capture, CONFIG)
    body["pagination"]["hasNext"] = False
    body["results"].pop()
    capture["securities"] = response(body)
    with pytest.raises(ValueError, match="symbols exactly"):
        normalize_capture(capture, CONFIG)


def test_missing_benchmark_history_rejects_capture(capture):
    del capture["histories"]["SPY"]
    with pytest.raises(ValueError, match="histories.*exactly"):
        normalize_capture(capture, CONFIG)
