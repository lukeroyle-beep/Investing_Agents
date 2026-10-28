"""Synthetic multi-instrument assessments; no broker writes or live credentials."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from decimal import Decimal as D
from fractions import Fraction
import json
from uuid import NAMESPACE_URL, uuid5

import pytest

from execution.batch_assessment import BatchAssessmentError, assess_batch_order
from execution.domain import payload_hash
from execution.instruments import Instrument
from execution.pilot_assessment import INSTRUMENT_ID, SYMBOL, assess_order
from execution.pilot_policy import preview_costs
from tests.test_pilot_assessment import NOW, change_response, encode, inputs as pilot_inputs
from tests.test_pilot_reads import direct_receipt


def instrument_scope(instrument_id=1004, symbol="MSFT", *, asset_type="equity", economic_class="equity"):
    spec = {"instrument_id": instrument_id, "symbol": symbol, "exchange": "NASDAQ",
        "asset_type": asset_type, "economic_class": economic_class, "currency": "USD",
        "issuer": "synthetic-issuer-" + symbol, "sector": "technology",
        "class_source_reference": "synthetic-reviewed-classification-v1"}
    instrument = Instrument.create(internal_instrument_id=uuid5(NAMESPACE_URL, "synthetic-batch/" + str(instrument_id)),
        canonical_symbol=symbol, exchange=spec["exchange"], asset_type=asset_type,
        currency=spec["currency"], sector=spec["sector"])
    return instrument, spec


def batch_inputs(instrument_id=1004, symbol="MSFT", *, amount=D(10), asset_type="equity", economic_class="equity"):
    args = pilot_inputs()
    instrument, spec = instrument_scope(instrument_id, symbol, asset_type=asset_type, economic_class=economic_class)
    args.update(instrument=instrument, instrument_spec=spec, account_currency="USD", quote_currency="USD")
    args["payload"].update(instrumentId=instrument_id, amount=amount)
    args["cost_receipt"]["arguments"]["body"] = encode(args["payload"])
    args["eligibility_receipt"]["arguments"]["body"] = encode({"instrumentIds": [instrument_id], "currency": "USD"})
    change_response(args, "cost", lambda c: c.update(instrumentId=instrument_id, symbol=symbol))
    change_response(args, "eligibility", lambda e: e["eligibilities"][0].update(instrumentId=instrument_id, symbol=symbol))
    args["quote"] = replace(args["quote"], internal_instrument_id=instrument.internal_instrument_id)
    return args


@pytest.mark.parametrize("instrument_id,symbol,asset_type,economic_class", [
    (1004, "MSFT", "equity", "equity"), (1142, "SBUX", "equity", "equity"),
    (8801, "SYNBOND", "etf", "fixed_income"), (8802, "SYNGOLD", "etf", "commodity"),
    (8803, "SYNCOIN", "etf", "crypto"),
])
def test_independently_bound_instruments_and_economic_exposures(instrument_id, symbol, asset_type, economic_class):
    args = batch_inputs(instrument_id, symbol, asset_type=asset_type, economic_class=economic_class)
    result = assess_batch_order(**args)
    assert result.preview["confirmation"]["market"] == {"instrumentId": instrument_id, "symbol": symbol}
    assert result.evidence["instrument_spec"] == args["instrument_spec"]
    assert result.evidence["instrument"].internal_instrument_id == args["quote"].internal_instrument_id
    assert preview_costs(result.preview) == (D(".01"), D(".011"))
    result.validate(NOW + timedelta(seconds=30))
    assert INSTRUMENT_ID == 1142 and SYMBOL == "SBUX"
    assert assess_order(**pilot_inputs()).preview["confirmation"]["market"]["instrumentId"] == 1142


def test_exact_decimal_amount_and_units_do_not_use_dollar10_or_quote_currency_conversion():
    amount = D("12.123456789012345678901234567890123456789")
    args = batch_inputs(amount=amount)
    args["quote"] = replace(args["quote"], ask=D(3), bid=D("2.99"))
    result = assess_batch_order(**args)
    assert result.preview["confirmation"]["amount"] == amount
    units = result.preview["confirmation"]["estimatedUnits"]
    assert Fraction(units) >= Fraction(amount) / 3
    assert Fraction(preview_costs(result.preview)[1]) >= Fraction(D(".01")) + Fraction(amount) / 300
    assert result.evidence["payload_hash"] == payload_hash(args["payload"])


@pytest.mark.parametrize("field,value", [("quote_currency", "GBX"), ("account_currency", "GBP"),
    ("quote_currency", None), ("account_currency", "usd")])
def test_currency_mismatch_or_absent_currency_blocks(field, value):
    args = batch_inputs()
    args[field] = value
    with pytest.raises(BatchAssessmentError, match="usd_quote_and_account"):
        assess_batch_order(**args)


@pytest.mark.parametrize("field,value", [("currency", "GBX"), ("asset_type", "crypto"),
    ("asset_type", "commodity"), ("asset_type", "forex"), ("economic_class", "fixed_income"),
    ("class_source_reference", ""), ("symbol", "SBUX"), ("instrument_id", True),
    ("instrument_id", 1142), ("exchange", "LSE"), ("sector", "other")])
def test_reviewed_spec_cannot_expand_or_change_the_mapped_instrument(field, value):
    args = batch_inputs()
    args["instrument_spec"][field] = value
    with pytest.raises(BatchAssessmentError):
        assess_batch_order(**args)


@pytest.mark.parametrize("field,value", [("amount", 10.0), ("amount", "10"), ("amount", True),
    ("amount", D(0)), ("leverage", 2), ("settlementType", "cfd"), ("transaction", "sellShort"),
    ("orderType", "limitIOC"), ("units", D(1)), ("instrumentId", 1142)])
def test_only_exact_amount_sized_real_long_x1_market_orders_are_supported(field, value):
    args = batch_inputs()
    args["payload"][field] = value
    with pytest.raises(BatchAssessmentError):
        assess_batch_order(**args)


@pytest.mark.parametrize("edit", [
    lambda e: e["eligibilities"][0].update(minPositionExposure=D("10.00000000000000000000000001")),
    lambda e: e["eligibilities"][0].update(maxUnitsPerOrder=D(".09999999999999999999999999")),
    lambda e: e["eligibilities"][0].update(tradeUnitType="contracts"),
    lambda e: e["eligibilities"][0].update(allowedOrderQuantityType="unitsOnly"),
    lambda e: e["eligibilities"][0]["leverageConfigs"][0].update(settlementType="cfd"),
    lambda e: e.update(currency="GBX"),
])
def test_exact_minimum_maximum_and_real_asset_eligibility_are_required(edit):
    args = batch_inputs()
    change_response(args, "eligibility", edit)
    with pytest.raises(BatchAssessmentError):
        assess_batch_order(**args)


@pytest.mark.parametrize("kind", ["cost", "eligibility"])
def test_receipts_from_another_scoped_instrument_cannot_be_reused(kind):
    args = batch_inputs()
    args[kind + "_receipt"] = batch_inputs(8801, "SYNBOND", asset_type="etf", economic_class="fixed_income")[kind + "_receipt"]
    with pytest.raises(BatchAssessmentError):
        assess_batch_order(**args)


@pytest.mark.parametrize("failure", ["quote", "cost_source", "cost_request", "eligibility_request", "future_cost"])
def test_source_and_receipt_clocks_remain_independent(failure):
    args = batch_inputs()
    old = NOW - timedelta(seconds=30, microseconds=1)
    if failure == "quote":
        args["quote"] = replace(args["quote"], observed_at=old)
    elif failure in {"cost_request", "eligibility_request"}:
        args[failure.replace("_request", "_receipt")]["request_started_at"] = old.isoformat()
    else:
        stamp = "2026-10-27T14:30:00.0000001Z" if failure == "future_cost" else old.isoformat()
        change_response(args, "cost", lambda c: c.update(lastUpdated=stamp))
    with pytest.raises(BatchAssessmentError):
        assess_batch_order(**args)


@pytest.mark.parametrize("target", ["preview", "evidence", "rehash_projection"])
def test_original_evidence_is_copied_and_projection_is_reparsed(target):
    args = batch_inputs()
    result = assess_batch_order(**args)
    args["instrument_spec"]["symbol"] = "OTHER"
    args["cost_receipt"]["arguments"]["body"] = "{}"
    result.validate(NOW)
    with pytest.raises(FrozenInstanceError):
        result.deadline = NOW
    if target == "preview":
        result.preview["confirmation"]["amount"] = D(11)
    elif target == "evidence":
        result.evidence["instrument_spec"]["instrument_id"] = 1142
    else:
        result.evidence["additional_buffer"]["status"] = "explicit_zero"
        result = replace(result, evidence_hash=payload_hash(result.evidence))
    with pytest.raises(BatchAssessmentError, match="changed"):
        result.validate(NOW)


def test_original_direct_provenance_is_reused_and_tampering_blocks(tmp_path):
    args = batch_inputs()
    for kind in ("cost", "eligibility"):
        item = args[kind + "_receipt"]
        envelope = json.loads(item["result"]["content"][0]["text"])
        args[kind + "_receipt"] = direct_receipt(tmp_path / kind, item["arguments"]["path"],
            json.loads(envelope["body"], parse_float=D), body=item["arguments"]["body"], start=NOW, end=NOW)
    result = assess_batch_order(**args)
    assert result.evidence["cost_receipt"] == args["cost_receipt"]
    result.validate(NOW)
    args["cost_receipt"]["raw_response"]["body_sha256"] = "0" * 64
    with pytest.raises(BatchAssessmentError):
        assess_batch_order(**args)


def test_missing_buffer_remains_unknown_and_requires_explicit_mode():
    args = batch_inputs()
    change_response(args, "eligibility", lambda e: e["eligibilities"][0].pop("additionalBufferPercent"))
    with pytest.raises(BatchAssessmentError, match="buffer_unknown"):
        assess_batch_order(**args)
    result = assess_batch_order(**args, allow_unreported_buffer=True)
    assert result.evidence["additional_buffer"]["status"] == "not_reported"
    result.validate(NOW)
