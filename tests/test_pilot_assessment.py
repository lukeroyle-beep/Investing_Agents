"""Pure standing-pilot assessment using original synthetic MCP receipts."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from fractions import Fraction
import json
from uuid import uuid4

import pytest

from execution.domain import payload_hash
from execution.pilot_assessment import (COST_PATH, ELIGIBILITY_PATH, INTERNAL_INSTRUMENT_ID,
    PilotAssessmentError, assess_order)
from execution.pilot_policy import preview_costs
from risk.pretrade import QuoteEvidence


NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)


def encode(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + ":" + encode(v) for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(encode(v) for v in value) + "]"
    return json.dumps(value)


def payload():
    return {"action": "open", "transaction": "buy", "instrumentId": 1142,
        "settlementType": "real", "orderType": "mkt", "leverage": 1,
        "amount": D(10), "orderCurrency": "usd"}


def cost():
    return {"instrumentId": 1142, "symbol": "SBUX", "lastUpdated": NOW.isoformat(),
        "costs": [{"costType": "transactionFee", "value": D("0.01"), "currency": "USD"},
                  {"costType": "marketSpread", "value": D("0.001"), "currency": "USD"}]}


def eligibility():
    return {"currency": "USD", "notFoundInstrumentIds": [], "notFoundSymbols": [],
        "eligibilities": [{"instrumentId": 1142, "symbol": "SBUX", "allowOpenPosition": True,
            "allowedOrderQuantityType": "all", "tradeUnitType": "units", "unitsQuantityType": "fractional",
            "orderFillBehaviorType": "bestEffort", "additionalBufferPercent": None,
            "minPositionExposure": 10, "maxUnitsPerOrder": 10000,
            "leverageConfigs": [{"settlementType": "real", "direction": "long", "leverageValues": [1],
                "isPotential": False, "minPositionAmount": 10}]}]}


def receipt(path, body, response, *, start=NOW, end=NOW):
    envelope = {"statusCode": 200, "isSuccess": True, "bodyTruncated": False, "body": encode(response)}
    return {"tool": "etoro_execute_read", "arguments": {"path": path, "body": encode(body)},
        "request_started_at": start.isoformat(), "response_received_at": end.isoformat(),
        "result": {"isError": False, "content": [{"type": "text", "text": encode(envelope)}]}}


def inputs():
    return {"cost_receipt": receipt(COST_PATH, payload(), cost()),
        "eligibility_receipt": receipt(ELIGIBILITY_PATH, {"instrumentIds": [1142], "currency": "USD"}, eligibility()),
        "payload": payload(), "quote": QuoteEvidence("synthetic-quote", INTERNAL_INSTRUMENT_ID,
            NOW, D("99.99"), D(100), True), "now": NOW}


def change_response(args, kind, edit):
    item = args[kind + "_receipt"]
    envelope = json.loads(item["result"]["content"][0]["text"])
    response = json.loads(envelope["body"], parse_float=D)
    edit(response)
    envelope["body"] = encode(response)
    item["result"]["content"][0]["text"] = encode(envelope)


def test_exact_assessment_binds_originals_and_is_compatible_with_pilot_costs():
    args = inputs()
    result = assess_order(**args)
    assert result.deadline == NOW + timedelta(seconds=30)
    assert preview_costs(result.preview) == (D(".01"), D(".011"))
    assert result.evidence["payload_hash"] == payload_hash(args["payload"])
    assert result.evidence["cost_receipt"] == args["cost_receipt"]
    assert result.evidence_hash == payload_hash(result.evidence)
    assert "token" not in result.preview and "verdict" not in result.preview
    result.validate(NOW + timedelta(seconds=30))
    with pytest.raises(PilotAssessmentError, match="stale"):
        result.validate(NOW + timedelta(seconds=30, microseconds=1))
    with pytest.raises(FrozenInstanceError):
        result.deadline = NOW
    args["payload"]["amount"] = D(11)
    args["cost_receipt"]["arguments"]["path"] = "/changed"
    result.validate(NOW)


@pytest.mark.parametrize("target", ["preview", "evidence"])
def test_mutating_nested_assessment_cannot_change_admitted_order(target):
    result = assess_order(**inputs())
    if target == "preview":
        result.preview["confirmation"]["totalUpfrontCost"] = D(0)
    else:
        result.evidence["payload"]["amount"] = D(11)
    with pytest.raises(PilotAssessmentError, match="changed"):
        result.validate(NOW)


def test_applicable_cost_list_does_not_invent_missing_zero_rows():
    args = inputs()
    change_response(args, "cost", lambda c: c.update(costs=[{"costType": "transactionFee", "value": 0, "currency": "USD"}]))
    result = assess_order(**args)
    assert result.preview["confirmation"]["costs"] == [{"type": "transactionFee", "value": D(0), "currency": "USD"}]
    assert result.evidence["cost_coverage_contract"]["zero_rows_required_by_schema"] is False
    assert preview_costs(result.preview) == (D(0), D(".001"))


@pytest.mark.parametrize("kind", ["markup", "overnightFee", "overWeekendFee"])
def test_unsupported_charge_must_be_zero_without_tiny_value_rounding(kind):
    args = inputs()
    change_response(args, "cost", lambda c: c["costs"].append({"costType": kind, "value": D("1e-60"), "currency": "USD"}))
    with pytest.raises(PilotAssessmentError, match="unsupported"):
        assess_order(**args)


@pytest.mark.parametrize("edit", [
    lambda c: c.update(costs=[]),
    lambda c: c.pop("costs"),
    lambda c: c["costs"].append(deepcopy(c["costs"][0])),
    lambda c: c["costs"][0].update(costType="unrecognizedFee"),
    lambda c: c["costs"][0].update(currency="GBP"),
    lambda c: c["costs"][0].update(value=-1),
    lambda c: c["costs"][0].update(value="0"),
    lambda c: c.update(instrumentId=1004),
    lambda c: c.update(symbol="MSFT"),
])
def test_incomplete_or_mismatched_cost_evidence_blocks(edit):
    args = inputs()
    change_response(args, "cost", edit)
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


@pytest.mark.parametrize("field,value", [("amount", "10"), ("amount", 10.0), ("amount", True),
    ("instrumentId", 1004), ("instrumentId", D(1142)), ("leverage", True), ("leverage", 2),
    ("settlementType", "cfd"), ("orderCurrency", "GBP"), ("orderType", "mit"),
    ("transaction", "sellShort"), ("positionIds", [123])])
def test_payload_cannot_expand_standing_scope(field, value):
    args = inputs()
    args["payload"][field] = value
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


def test_cost_request_must_match_exact_numeric_submission():
    args = inputs()
    request = payload()
    request["amount"] = D(11)
    args["cost_receipt"]["arguments"]["body"] = encode(request)
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)
    request["amount"] = "10"
    args["cost_receipt"]["arguments"]["body"] = encode(request)
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


@pytest.mark.parametrize("kind", ["cost", "eligibility"])
@pytest.mark.parametrize("mode", ["real_path", "extra_query", "unknown_success", "truncated", "failed", "duplicate_key", "nonfinite"])
def test_raw_receipt_and_request_contract_fail_closed(kind, mode):
    args = inputs()
    item = args[kind + "_receipt"]
    if mode == "real_path":
        item["arguments"]["path"] = item["arguments"]["path"].replace("/demo/", "/real/")
    elif mode == "extra_query":
        item["arguments"]["query"] = {"account": "other"}
    elif mode == "duplicate_key":
        item["arguments"]["body"] = item["arguments"]["body"].replace("{", '{"currency":"GBP",', 1) if kind == "eligibility" else item["arguments"]["body"].replace("{", '{"amount":11,', 1)
    elif mode == "nonfinite":
        item["arguments"]["body"] = item["arguments"]["body"].replace("{", '{"unexpected":NaN,', 1)
    else:
        envelope = json.loads(item["result"]["content"][0]["text"])
        if mode == "unknown_success":
            envelope.pop("isSuccess")
        elif mode == "truncated":
            envelope["bodyTruncated"] = True
        else:
            envelope["statusCode"] = 500
        item["result"]["content"][0]["text"] = encode(envelope)
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


@pytest.mark.parametrize("kind", ["cost", "eligibility"])
@pytest.mark.parametrize("start,end", [(-11, 0), (1, 0), (-31, -25), (0, 1)])
def test_request_duration_and_original_source_clocks_are_bounded(kind, start, end):
    args = inputs()
    item = args[kind + "_receipt"]
    item["request_started_at"] = (NOW + timedelta(seconds=start)).isoformat()
    item["response_received_at"] = (NOW + timedelta(seconds=end)).isoformat()
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


@pytest.mark.parametrize("timestamp", ["2026-10-27T14:29:29.9999999Z", "2026-10-27T14:30:00.0000001Z",
    "2026-10-27T14:30:00", "2026-10-27T14:30:00-00:00"])
def test_cost_broker_timestamp_is_neither_redated_nor_truncated(timestamp):
    args = inputs()
    change_response(args, "cost", lambda c: c.update(lastUpdated=timestamp))
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


def test_deadline_uses_oldest_request_or_source_clock_with_conservative_rounding():
    args = inputs()
    args["eligibility_receipt"]["request_started_at"] = (NOW - timedelta(seconds=10)).isoformat()
    change_response(args, "cost", lambda c: c.update(lastUpdated="2026-10-27T14:29:45.1234567Z"))
    result = assess_order(**args)
    assert result.deadline == datetime(2026, 10, 27, 14, 30, 15, 123456, tzinfo=timezone.utc)


@pytest.mark.parametrize("edit", [
    lambda e: e.update(currency="GBP"),
    lambda e: e.update(notFoundInstrumentIds=[1142]),
    lambda e: e["eligibilities"][0].update(symbol="MSFT"),
    lambda e: e["eligibilities"][0].update(allowOpenPosition=False),
    lambda e: e["eligibilities"][0].update(allowedOrderQuantityType="unitsOnly"),
    lambda e: e["eligibilities"][0].update(tradeUnitType="contracts"),
    lambda e: e["eligibilities"][0].update(additionalBufferPercent=D("1e-60")),
    lambda e: e["eligibilities"][0].pop("additionalBufferPercent"),
    lambda e: e["eligibilities"][0].update(minPositionExposure=D("10.0000000000000000000000000000001")),
    lambda e: e["eligibilities"][0]["leverageConfigs"][0].update(isPotential=True),
    lambda e: e["eligibilities"][0]["leverageConfigs"][0].update(settlementType="cfd"),
])
def test_real_asset_eligibility_and_exact_size_constraints(edit):
    args = inputs()
    change_response(args, "eligibility", edit)
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


@pytest.mark.parametrize("present,value,reason", [
    (False, None, "assessment_additional_cash_buffer_unknown"),
    (True, None, None),
    (True, D(0), None),
    (True, D("1e-60"), "assessment_additional_cash_buffer_unsupported"),
])
def test_additional_buffer_diagnostic_preserves_presence_and_admission(present, value, reason):
    args = inputs()
    def edit(response):
        item = response["eligibilities"][0]
        if present:
            item["additionalBufferPercent"] = value
        else:
            item.pop("additionalBufferPercent")
    change_response(args, "eligibility", edit)
    if reason:
        with pytest.raises(PilotAssessmentError, match="^" + reason + "$"):
            assess_order(**args)
    else:
        result = assess_order(**args)
        assert result.evidence["eligibility_receipt"] == args["eligibility_receipt"]
        result.validate(NOW)


def test_explicit_real_selection_can_coexist_with_an_eligible_cfd_configuration():
    args = inputs()
    change_response(args, "eligibility", lambda e: e["eligibilities"][0]["leverageConfigs"].append({
        "settlementType": "cfd", "direction": "long", "leverageValues": [1], "isPotential": False, "minPositionAmount": 10}))
    assert assess_order(**args).preview["confirmation"]["settlementType"] == "real"


def test_units_boundary_is_exact_and_spread_rounds_conservatively():
    args = inputs()
    args["quote"] = replace(args["quote"], ask=D(3), bid=D("2.99"))
    result = assess_order(**args)
    units = result.preview["confirmation"]["estimatedUnits"]
    assert Fraction(units) >= Fraction(10, 3)
    assert Fraction(preview_costs(result.preview)[1]) >= Fraction(D(".01")) + Fraction(1, 30)
    change_response(args, "eligibility", lambda e: e["eligibilities"][0].update(maxUnitsPerOrder=D("3.333333333333333333333333333333333333333333")))
    with pytest.raises(PilotAssessmentError, match="size"):
        assess_order(**args)


def test_whole_units_are_checked_without_rounding():
    args = inputs()
    change_response(args, "eligibility", lambda e: e["eligibilities"][0].update(unitsQuantityType="whole"))
    with pytest.raises(PilotAssessmentError, match="size"):
        assess_order(**args)
    args["quote"] = replace(args["quote"], ask=D(10), bid=D(10))
    assert assess_order(**args).preview["confirmation"]["estimatedUnits"] == 1


@pytest.mark.parametrize("changes", [{"internal_instrument_id": uuid4()}, {"regular_session_open": False},
    {"observed_at": NOW - timedelta(seconds=31)}, {"observed_at": NOW + timedelta(microseconds=1)}])
def test_quote_must_identify_the_same_fresh_open_instrument(changes):
    args = inputs()
    args["quote"] = replace(args["quote"], **changes)
    with pytest.raises(PilotAssessmentError):
        assess_order(**args)


def test_tiny_fees_survive_projection_and_cost_reserve():
    args = inputs()
    change_response(args, "cost", lambda c: c["costs"].append({"costType": "sdrt", "value": D("1e-60"), "currency": "USD"}))
    upfront, reserve = preview_costs(assess_order(**args).preview)
    assert upfront > D(".01") and reserve > D(".011")
