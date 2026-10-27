"""Pure, source-bound assessment for the first standing-mandate Demo order.

The official getCostDemo contract describes ``costs`` as the components that
would apply to the proposed order; its enum is not a required six-row list.
Retain the applicable rows without manufacturing omitted zero-valued charges.
An explicitly selected unreported-buffer mode retains the missing value as
unknown. The default still requires the buffer field. The alternate mode treats
it as an unknown temporary broker reservation, never an opening charge or zero;
the caller must separately verify the human's bound buffer-rule amendment.
This assessment is neither human approval nor a connector preparation token.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from fractions import Fraction
import json
from uuid import NAMESPACE_URL, uuid5

from brokers.etoro.account_evidence import _exact_sum
from brokers.etoro.eligibility import EtoroEligibilityResponse
from brokers.etoro.schemas import _pnl_timestamp, exact_datetime_epoch_seconds
from execution.domain import _aware_utc, payload_hash
from execution.pilot_policy import preview_costs
from risk.pretrade import QuoteEvidence


D = Decimal
COST_PATH = "/api/v2/trading/info/demo/costs"
ELIGIBILITY_PATH = "/api/v2/trading/info/demo/eligibility"
INSTRUMENT_ID = 1142
SYMBOL = "SBUX"
INTERNAL_INSTRUMENT_ID = uuid5(NAMESPACE_URL, "etoro/demo-pilot-v1/instrument/1142")
MAX_AGE_SECONDS = 30
MAX_REQUEST_SECONDS = 10
_COST_TYPES = frozenset({"markup", "marketSpread", "transactionFee", "overnightFee", "overWeekendFee", "sdrt"})
_ZERO_ONLY_COSTS = frozenset({"markup", "overnightFee", "overWeekendFee"})
_CONTRACT = {
    "route_id": "getCostDemo", "api_version": "v1.400.0", "checked_on": "2026-10-27",
    "reference": "https://api-portal.etoro.com/api-reference",
    "coverage": "Cost components that would apply to the proposed order.",
    "zero_rows_required_by_schema": False,
}


class PilotAssessmentError(ValueError):
    """Incomplete, stale or unsupported evidence cannot authorize an order."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise PilotAssessmentError("assessment_duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise PilotAssessmentError("assessment_nonfinite_json_number")


def _json(value):
    if not isinstance(value, str):
        raise PilotAssessmentError("assessment_original_json_text_required")
    return json.loads(value, parse_float=D, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _number(value, field):
    if type(value) is not int and not isinstance(value, D):
        raise PilotAssessmentError("assessment_exact_numeric_" + field + "_required")
    value = D(value)
    if not value.is_finite():
        raise PilotAssessmentError("assessment_finite_" + field + "_required")
    return value


def _payload(value):
    fixed = {"action": "open", "transaction": "buy", "instrumentId": INSTRUMENT_ID,
        "settlementType": "real", "orderType": "mkt", "leverage": 1,
        "amount": D(10), "orderCurrency": "usd"}
    if (not isinstance(value, dict) or set(value) != set(fixed)
            or type(value.get("instrumentId")) is not int or type(value.get("leverage")) is not int
            or _number(value.get("amount"), "amount") != 10 or value != fixed):
        raise PilotAssessmentError("assessment_exact_scoped_order_required")
    return fixed


def _seconds(value):
    if isinstance(value, datetime):
        return exact_datetime_epoch_seconds(_aware_utc(value, "assessment_time"))
    return _pnl_timestamp(value)[1]


def _datetime(seconds):
    # Python clocks have microsecond precision: round an expiry down, never up.
    micros = int((seconds * 1000000).to_integral_value(rounding=ROUND_FLOOR))
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)


def _receipt(receipt, path, now):
    if (not isinstance(receipt, dict)
            or receipt.get("tool", "etoro_execute_read") not in {"etoro_execute_read", "etoro_direct_read"}):
        raise PilotAssessmentError("assessment_supported_read_receipt_required")
    args = receipt.get("arguments")
    if not isinstance(args, dict) or set(args) != {"path", "body"} or args.get("path") != path:
        raise PilotAssessmentError("assessment_exact_read_arguments_required")
    body = _json(args["body"])
    start, end = _seconds(receipt.get("request_started_at")), _seconds(receipt.get("response_received_at"))
    if not 0 <= end - start <= MAX_REQUEST_SECONDS:
        raise PilotAssessmentError("assessment_request_duration_exceeded")
    if not end <= now or not 0 <= now - start <= MAX_AGE_SECONDS:
        raise PilotAssessmentError("assessment_receipt_stale_or_future")
    if receipt.get("tool") == "etoro_direct_read":
        from brokers.etoro.pilot_reads import read_receipt_envelope
        _, response = read_receipt_envelope(receipt)
        if not isinstance(response, dict):
            raise PilotAssessmentError("assessment_response_object_required")
        return body, response, start, end
    result = receipt.get("result")
    if (not isinstance(result, dict) or result.get("isError")
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1
            or not isinstance(result["content"][0], dict) or result["content"][0].get("type") != "text"):
        raise PilotAssessmentError("assessment_raw_mcp_response_required")
    envelope = _json(result["content"][0].get("text"))
    if (not isinstance(envelope, dict) or type(envelope.get("statusCode")) is not int
            or envelope["statusCode"] != 200 or envelope.get("isSuccess") is not True
            or envelope.get("bodyTruncated") is not False or envelope.get("error")):
        raise PilotAssessmentError("assessment_successful_complete_response_required")
    response = _json(envelope.get("body"))
    if not isinstance(response, dict):
        raise PilotAssessmentError("assessment_response_object_required")
    return body, response, start, end


@dataclass(frozen=True, slots=True)
class PilotOrderAssessment:
    preview: dict
    deadline: datetime
    evidence: dict
    evidence_hash: str

    def __post_init__(self):
        object.__setattr__(self, "preview", deepcopy(self.preview))
        object.__setattr__(self, "evidence", deepcopy(self.evidence))
        if (payload_hash(self.evidence) != self.evidence_hash
                or self.evidence.get("preview_hash") != payload_hash(self.preview)
                or self.evidence.get("deadline") != self.deadline):
            raise PilotAssessmentError("assessment_projection_hash_mismatch")

    def validate(self, now):
        """Rebuild from retained originals; frozen fields alone do not freeze dicts."""
        if payload_hash(self.evidence) != self.evidence_hash:
            raise PilotAssessmentError("assessment_evidence_changed")
        rebuilt = assess_order(cost_receipt=self.evidence["cost_receipt"],
            eligibility_receipt=self.evidence["eligibility_receipt"], payload=self.evidence["payload"],
            quote=self.evidence["quote"], now=now,
            allow_unreported_buffer=self.evidence["allow_unreported_buffer"])
        if self != rebuilt:
            raise PilotAssessmentError("assessment_projection_changed")


def assess_order(*, cost_receipt, eligibility_receipt, payload, quote, now, allow_unreported_buffer=False):
    """Assess only SBUX/1142, USD10, real long x1 market; perform no I/O.

    Receipts retain original MCP or direct JSON text, complete response flags and exact
    request arguments. The caller separately binds the authenticated Demo
    identity, whole-account policy, standing mandate, market-open evidence and
    any human amendment authorizing the unreported-buffer mode.
    """
    try:
        return _assess_order(cost_receipt, eligibility_receipt, payload, quote, now, allow_unreported_buffer)
    except PilotAssessmentError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise PilotAssessmentError("assessment_malformed_or_unqualified_evidence") from exc


def _assess_order(cost_receipt, eligibility_receipt, payload, quote, now, allow_unreported_buffer):
    if type(allow_unreported_buffer) is not bool:
        raise PilotAssessmentError("assessment_explicit_buffer_mode_required")
    scoped = _payload(payload)
    moment = _aware_utc(now, "assessment_now")
    current = exact_datetime_epoch_seconds(moment)
    if (not isinstance(quote, QuoteEvidence) or quote.internal_instrument_id != INTERNAL_INSTRUMENT_ID
            or quote.regular_session_open is not True or not quote.bid.is_finite() or not quote.ask.is_finite()
            or quote.bid <= 0 or quote.ask < quote.bid):
        raise PilotAssessmentError("assessment_exact_valid_quote_required")
    quote_time = exact_datetime_epoch_seconds(quote.observed_at)
    if not 0 <= current - quote_time <= MAX_AGE_SECONDS:
        raise PilotAssessmentError("assessment_quote_stale_or_future")
    cost_request, cost, cost_start, cost_end = _receipt(cost_receipt, COST_PATH, current)
    if _payload(cost_request) != scoped:
        raise PilotAssessmentError("assessment_cost_request_mismatch")
    eligibility_request, eligibility, eligibility_start, _ = _receipt(eligibility_receipt, ELIGIBILITY_PATH, current)
    if (eligibility_request != {"instrumentIds": [INSTRUMENT_ID], "currency": "USD"}
            or type(eligibility_request["instrumentIds"][0]) is not int):
        raise PilotAssessmentError("assessment_exact_eligibility_request_required")
    parsed = EtoroEligibilityResponse.parse(eligibility, (INSTRUMENT_ID,))
    item = parsed.items[0]
    raw_item = eligibility["eligibilities"][0]
    if (item.symbol != SYMBOL or not item.allow_open_position or not item.real_long_one_available
            or item.allowed_quantity_type not in {"all", "amountOnly"} or item.trade_unit_type != "units"
            or raw_item.get("unitsQuantityType") not in {"whole", "fractional"}
            or raw_item.get("orderFillBehaviorType") not in {"bestEffort", "fillOrKill"}):
        raise PilotAssessmentError("assessment_real_long_one_eligibility_required")
    # The optional broker field describes funds temporarily reserved until
    # execution/cancellation. Its omission is not evidence of a zero buffer.
    # Keep that uncertainty separate from principal and opening-cost reserves;
    # no new entry is permitted while the claimed order remains unreconciled.
    buffer_present = "additionalBufferPercent" in raw_item
    buffer_value = raw_item.get("additionalBufferPercent")
    if not buffer_present and not allow_unreported_buffer:
        raise PilotAssessmentError("assessment_additional_cash_buffer_unknown")
    if buffer_present and buffer_value is not None:
        buffer_value = _number(buffer_value, "additional_buffer")
        if buffer_value != 0:
            raise PilotAssessmentError("assessment_additional_cash_buffer_unsupported")
    buffer_evidence = {
        "source_field": "additionalBufferPercent",
        "status": "not_reported" if not buffer_present else "explicit_null" if buffer_value is None else "explicit_zero",
        "reported_percent": buffer_value,
        "economic_commitment_basis": "principal_plus_opening_costs_and_embedded_spread",
        "terminal_cash_reconciliation_required": True,
        "continuous_inflight_available_cash_verified": False,
    }
    _number(raw_item.get("minPositionExposure"), "minimum_exposure")
    _number(raw_item.get("maxUnitsPerOrder"), "maximum_units")
    for config in raw_item["leverageConfigs"]:
        _number(config.get("minPositionAmount"), "minimum_amount")
    units_exact = Fraction(scoped["amount"]) / Fraction(quote.ask)
    if (scoped["amount"] < max(item.minimum_exposure, item.real_long_one_minimum_amount)
            or units_exact > Fraction(item.maximum_units)
            or (raw_item["unitsQuantityType"] == "whole" and units_exact.denominator != 1)):
        raise PilotAssessmentError("assessment_order_size_out_of_bounds")
    if (type(cost.get("instrumentId")) is not int or cost["instrumentId"] != INSTRUMENT_ID
            or cost.get("symbol") != SYMBOL):
        raise PilotAssessmentError("assessment_cost_instrument_mismatch")
    source_time = _pnl_timestamp(cost.get("lastUpdated"))[1]
    if source_time > cost_end or not 0 <= current - source_time <= MAX_AGE_SECONDS:
        raise PilotAssessmentError("assessment_cost_source_stale_or_future")
    rows = cost.get("costs")
    if not isinstance(rows, list) or not rows:
        raise PilotAssessmentError("assessment_applicable_cost_breakdown_required")
    normalized, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"costType", "value", "currency"}:
            raise PilotAssessmentError("assessment_cost_component_shape_invalid")
        kind = row["costType"]
        if kind not in _COST_TYPES or kind in seen:
            raise PilotAssessmentError("assessment_cost_component_unknown_or_duplicate")
        seen.add(kind)
        value = _number(row["value"], "cost")
        if row["currency"] != "USD" or value < 0:
            raise PilotAssessmentError("assessment_usd_nonnegative_cost_required")
        if kind in _ZERO_ONLY_COSTS and value != 0:
            raise PilotAssessmentError("assessment_markup_or_recurring_cost_unsupported")
        normalized.append({"type": kind, "value": value, "currency": "USD"})
    upfront = _exact_sum(row["value"] for row in normalized if row["type"] in {"transactionFee", "sdrt"})
    # Non-terminating amount/ask divisions round upward for the spread reserve.
    # Eligibility above compares exact rational units, without context rounding.
    with localcontext() as context:
        context.prec = max(80, len(quote.ask.as_tuple().digits) + 30)
        context.rounding = ROUND_CEILING
        estimated_units = scoped["amount"] / quote.ask
    preview = {"confirmation": {"market": {"instrumentId": INSTRUMENT_ID, "symbol": SYMBOL},
        "direction": "buy", "orderType": "mkt", "amount": scoped["amount"], "leverage": 1,
        "settlementType": "real", "orderCurrency": "USD", "costs": normalized,
        "totalUpfrontCost": upfront, "estimatedUnits": estimated_units,
        "quote": {"ask": quote.ask, "bid": quote.bid, "asOf": quote.observed_at}}}
    preview_costs(preview)
    deadline = _datetime(min(cost_start, eligibility_start, source_time, quote_time) + MAX_AGE_SECONDS)
    evidence = {"schema": "demo-pilot-order-assessment-v1", "additional_buffer": buffer_evidence,
        "allow_unreported_buffer": allow_unreported_buffer, "payload": deepcopy(payload),
        "payload_hash": payload_hash(payload), "cost_receipt": deepcopy(cost_receipt),
        "eligibility_receipt": deepcopy(eligibility_receipt), "quote": quote,
        "cost_coverage_contract": deepcopy(_CONTRACT), "cost_source_timestamp": cost["lastUpdated"],
        "estimated_units_basis": "amount_divided_by_ask_rounded_up_for_conservative_spread_reserve",
        "preview_hash": payload_hash(preview), "deadline": deadline}
    return PilotOrderAssessment(preview, deadline, evidence, payload_hash(evidence))
