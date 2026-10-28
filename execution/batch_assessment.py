"""Source-bound assessments for finite multi-instrument Demo mandates.

Original pilot receipt parsers are reused without changing their constants or
scope. Quote currency is independently supplied by the mapped instrument;
amount/ask arithmetic is supported only when both are USD.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, localcontext
from fractions import Fraction

from brokers.etoro.account_evidence import _exact_sum
from brokers.etoro.autonomous_api import build_order_payload
from brokers.etoro.eligibility import EtoroEligibilityResponse
from brokers.etoro.schemas import _pnl_timestamp, exact_datetime_epoch_seconds
from execution.domain import _aware_utc, payload_hash
from execution.instruments import Instrument
from execution.pilot_assessment import (
    COST_PATH, ELIGIBILITY_PATH, MAX_AGE_SECONDS, PilotAssessmentError,
    _CONTRACT, _COST_TYPES, _ZERO_ONLY_COSTS, _datetime, _number, _receipt,
)
from execution.pilot_policy import preview_costs
from risk.pretrade import QuoteEvidence

D = Decimal
_SPEC_FIELDS = frozenset({"instrument_id", "symbol", "exchange", "asset_type", "economic_class",
                          "currency", "issuer", "sector", "class_source_reference"})


class BatchAssessmentError(PilotAssessmentError):
    """Incomplete, stale or unsupported finite-scope evidence."""


def validated_instrument_spec(value, instrument):
    """Bind the reviewed finite scope to the already resolved registry object."""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping) or set(value) != _SPEC_FIELDS or not isinstance(instrument, Instrument):
        raise BatchAssessmentError("assessment_exact_instrument_spec_required")
    spec = deepcopy(dict(value))
    if (type(spec["instrument_id"]) is not int or spec["instrument_id"] <= 0
            or any(not isinstance(spec[name], str) or not spec[name].strip()
                   or spec[name] != spec[name].strip() for name in _SPEC_FIELDS - {"instrument_id"})
            or spec["asset_type"] not in {"equity", "etf"}
            or spec["economic_class"] not in {"equity", "fixed_income", "commodity", "crypto"}
            or (spec["asset_type"] == "equity" and spec["economic_class"] != "equity")
            or spec["currency"] != "USD"):
        raise BatchAssessmentError("assessment_supported_usd_real_asset_spec_required")
    for field, attribute in (("symbol", "canonical_symbol"), ("exchange", "exchange"),
                             ("asset_type", "asset_type"), ("currency", "currency"), ("sector", "sector")):
        if spec[field] != getattr(instrument, attribute):
            raise BatchAssessmentError("assessment_instrument_spec_mapping_mismatch")
    return spec


def _payload(value, instrument_id):
    if (not isinstance(value, dict) or type(value.get("instrumentId")) is not int
            or type(value.get("leverage")) is not int):
        raise BatchAssessmentError("assessment_exact_scoped_order_required")
    amount = _number(value.get("amount"), "amount")
    expected = build_order_payload(instrument_id, amount)
    if value != expected or set(value) != set(expected):
        raise BatchAssessmentError("assessment_exact_scoped_order_required")
    return expected


@dataclass(frozen=True, slots=True)
class BatchOrderAssessment:
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
            raise BatchAssessmentError("assessment_projection_hash_mismatch")

    def validate(self, now):
        """Rebuild from retained originals; frozen fields alone do not freeze dicts."""
        if payload_hash(self.evidence) != self.evidence_hash:
            raise BatchAssessmentError("assessment_evidence_changed")
        rebuilt = assess_batch_order(cost_receipt=self.evidence["cost_receipt"],
            eligibility_receipt=self.evidence["eligibility_receipt"], payload=self.evidence["payload"],
            quote=self.evidence["quote"], now=now, instrument=self.evidence["instrument"],
            instrument_spec=self.evidence["instrument_spec"], quote_currency=self.evidence["quote_currency"],
            account_currency=self.evidence["account_currency"],
            allow_unreported_buffer=self.evidence["allow_unreported_buffer"])
        if self != rebuilt:
            raise BatchAssessmentError("assessment_projection_changed")


def assess_batch_order(*, instrument_spec, instrument, cost_receipt, eligibility_receipt, payload, quote,
                       quote_currency, account_currency, now, allow_unreported_buffer=False):
    """Assess one finite-mandate USD equity/ETF market buy without I/O.

    The caller verifies account identity, current finite mandate, whole-account
    limits and market receipts before dispatch. This is a bound assessment,
    not a connector preparation token or approval.
    """
    try:
        return _assess_batch_order(instrument_spec, instrument, cost_receipt, eligibility_receipt,
            payload, quote, quote_currency, account_currency, now, allow_unreported_buffer)
    except BatchAssessmentError:
        raise
    except PilotAssessmentError as exc:
        raise BatchAssessmentError(str(exc)) from exc
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise BatchAssessmentError("assessment_malformed_or_unqualified_evidence") from exc


def _assess_batch_order(instrument_spec, instrument, cost_receipt, eligibility_receipt, payload,
                        quote, quote_currency, account_currency, now, allow_unreported_buffer):
    if type(allow_unreported_buffer) is not bool:
        raise BatchAssessmentError("assessment_explicit_buffer_mode_required")
    spec = validated_instrument_spec(instrument_spec, instrument)
    instrument_id, symbol = spec["instrument_id"], spec["symbol"]
    if quote_currency != "USD" or account_currency != "USD":
        raise BatchAssessmentError("assessment_usd_quote_and_account_required")
    scoped = _payload(payload, instrument_id)
    moment = _aware_utc(now, "assessment_now")
    current = exact_datetime_epoch_seconds(moment)
    if (not isinstance(quote, QuoteEvidence) or quote.internal_instrument_id != instrument.internal_instrument_id
            or quote.regular_session_open is not True or not quote.bid.is_finite() or not quote.ask.is_finite()
            or quote.bid <= 0 or quote.ask < quote.bid):
        raise BatchAssessmentError("assessment_exact_valid_quote_required")
    quote_time = exact_datetime_epoch_seconds(quote.observed_at)
    if not 0 <= current - quote_time <= MAX_AGE_SECONDS:
        raise BatchAssessmentError("assessment_quote_stale_or_future")
    cost_request, cost, cost_start, cost_end = _receipt(cost_receipt, COST_PATH, current)
    if _payload(cost_request, instrument_id) != scoped:
        raise BatchAssessmentError("assessment_cost_request_mismatch")
    eligibility_request, eligibility, eligibility_start, _ = _receipt(eligibility_receipt, ELIGIBILITY_PATH, current)
    if (eligibility_request != {"instrumentIds": [instrument_id], "currency": "USD"}
            or type(eligibility_request["instrumentIds"][0]) is not int):
        raise BatchAssessmentError("assessment_exact_eligibility_request_required")
    parsed = EtoroEligibilityResponse.parse(eligibility, (instrument_id,))
    item = parsed.items[0]
    raw_item = eligibility["eligibilities"][0]
    if (item.symbol != symbol or not item.allow_open_position or not item.real_long_one_available
            or item.allowed_quantity_type not in {"all", "amountOnly"} or item.trade_unit_type != "units"
            or raw_item.get("unitsQuantityType") not in {"whole", "fractional"}
            or raw_item.get("orderFillBehaviorType") not in {"bestEffort", "fillOrKill"}):
        raise BatchAssessmentError("assessment_real_long_one_eligibility_required")
    # The optional broker field describes funds temporarily reserved until
    # execution/cancellation. Its omission is not evidence of a zero buffer.
    # Keep that uncertainty separate from principal and opening-cost reserves;
    # no new entry is permitted while the claimed order remains unreconciled.
    buffer_present = "additionalBufferPercent" in raw_item
    buffer_value = raw_item.get("additionalBufferPercent")
    if not buffer_present and not allow_unreported_buffer:
        raise BatchAssessmentError("assessment_additional_cash_buffer_unknown")
    if buffer_present and buffer_value is not None:
        buffer_value = _number(buffer_value, "additional_buffer")
        if buffer_value != 0:
            raise BatchAssessmentError("assessment_additional_cash_buffer_unsupported")
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
        raise BatchAssessmentError("assessment_order_size_out_of_bounds")
    if (type(cost.get("instrumentId")) is not int or cost["instrumentId"] != instrument_id
            or cost.get("symbol") != symbol):
        raise BatchAssessmentError("assessment_cost_instrument_mismatch")
    source_time = _pnl_timestamp(cost.get("lastUpdated"))[1]
    if source_time > cost_end or not 0 <= current - source_time <= MAX_AGE_SECONDS:
        raise BatchAssessmentError("assessment_cost_source_stale_or_future")
    rows = cost.get("costs")
    if not isinstance(rows, list) or not rows:
        raise BatchAssessmentError("assessment_applicable_cost_breakdown_required")
    normalized, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"costType", "value", "currency"}:
            raise BatchAssessmentError("assessment_cost_component_shape_invalid")
        kind = row["costType"]
        if kind not in _COST_TYPES or kind in seen:
            raise BatchAssessmentError("assessment_cost_component_unknown_or_duplicate")
        seen.add(kind)
        value = _number(row["value"], "cost")
        if row["currency"] != "USD" or value < 0:
            raise BatchAssessmentError("assessment_usd_nonnegative_cost_required")
        if kind in _ZERO_ONLY_COSTS and value != 0:
            raise BatchAssessmentError("assessment_markup_or_recurring_cost_unsupported")
        normalized.append({"type": kind, "value": value, "currency": "USD"})
    upfront = _exact_sum(row["value"] for row in normalized if row["type"] in {"transactionFee", "sdrt"})
    # Non-terminating amount/ask divisions round upward for the spread reserve.
    # Eligibility above compares exact rational units, without context rounding.
    with localcontext() as context:
        context.prec = max(80, len(quote.ask.as_tuple().digits) + len(scoped["amount"].as_tuple().digits) + 30)
        context.rounding = ROUND_CEILING
        estimated_units = scoped["amount"] / quote.ask
    preview = {"confirmation": {"market": {"instrumentId": instrument_id, "symbol": symbol},
        "direction": "buy", "orderType": "mkt", "amount": scoped["amount"], "leverage": 1,
        "settlementType": "real", "orderCurrency": "USD", "costs": normalized,
        "totalUpfrontCost": upfront, "estimatedUnits": estimated_units,
        "quote": {"ask": quote.ask, "bid": quote.bid, "asOf": quote.observed_at}}}
    preview_costs(preview)
    deadline = _datetime(min(cost_start, eligibility_start, source_time, quote_time) + MAX_AGE_SECONDS)
    evidence = {"schema": "finite-demo-order-assessment-v1",
        "instrument_spec": spec, "instrument": instrument, "quote_currency": quote_currency,
        "account_currency": account_currency, "additional_buffer": buffer_evidence,
        "allow_unreported_buffer": allow_unreported_buffer, "payload": deepcopy(payload),
        "payload_hash": payload_hash(payload), "cost_receipt": deepcopy(cost_receipt),
        "eligibility_receipt": deepcopy(eligibility_receipt), "quote": quote,
        "cost_coverage_contract": deepcopy(_CONTRACT), "cost_source_timestamp": cost["lastUpdated"],
        "estimated_units_basis": "amount_divided_by_ask_rounded_up_for_conservative_spread_reserve",
        "preview_hash": payload_hash(preview), "deadline": deadline}
    return BatchOrderAssessment(preview, deadline, evidence, payload_hash(evidence))
