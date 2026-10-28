"""Generic finite-mandate market reads, retaining each original request clock.

Only the already documented getRates v2 route is used. Its ``date`` field is
documented UTC; that normalization is local to this route, and source time is
never substituted with receipt time. A contrary exchange-open flag blocks.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

from brokers.etoro.pilot_reads import DirectPilotReads, read_receipt_envelope
from brokers.etoro.schemas import _pnl_timestamp, exact_datetime_epoch_seconds
from execution.batch_assessment import validated_instrument_spec
from execution.domain import _aware_utc, payload_hash
from execution.pilot_assessment import _datetime, _number, _seconds
from risk.pretrade import QuoteEvidence

SEARCH_PATH = "/api/v1/market-data/search"
RATE_PATH = "/api/v2/market-data/rates"
STATE_MAX_AGE_SECONDS = 15
QUOTE_MAX_AGE_SECONDS = 30
QUOTE_HANDOFF_MAX_SECONDS = 5
# The provider already emits instrumentId. Selecting it again produced duplicate
# JSON keys in the observed pilot (PR16, 55d8a40); retain that proven workaround.
# The returned identity remains mandatory and is checked against the finite scope.
SEARCH_FIELDS = "internalSymbolFull,isCurrentlyTradable,isExchangeOpen,isActiveInPlatform,isBuyEnabled,isDelisted"
_STATE_FIELDS = frozenset(SEARCH_FIELDS.split(",")) | {"instrumentId"}
_MARKET_FIELDS = _STATE_FIELDS | {"instrument_id", "quote_type", "quote_id", "received_at",
    "clock_basis", "rate_time_basis", "state_request_started_at", "state_received_at",
    "quote_request_started_at", "quote_received_at", "quote_source_at", "quote_source_normalized_utc",
    "quote_source_epoch_seconds", "quote_currency", "quote_currency_basis", "source_age_limit_seconds",
    "quote_handoff_limit_seconds", "instrument_spec", "state_receipt", "rate_receipt", "evidence_hash"}


class BatchMarketError(ValueError):
    """Market evidence is incomplete, stale or disagrees with the finite scope."""


def _search_arguments(instrument_id):
    return {"path": SEARCH_PATH, "query": {"instrumentId": str(instrument_id),
        "fields": SEARCH_FIELDS, "pageNumber": "1", "pageSize": "100"}}


def _rate_arguments(instrument_id):
    return {"path": RATE_PATH, "query": {"instrumentIds": str(instrument_id)}}


def _checked_receipt(receipt, expected_arguments, current, max_age):
    _, payload = read_receipt_envelope(receipt)
    if receipt["arguments"] != expected_arguments:
        raise BatchMarketError("market_exact_read_arguments_required")
    start, end = _seconds(receipt["request_started_at"]), _seconds(receipt["response_received_at"])
    if not 0 <= end - start <= 10 or end > current or not 0 <= current - start <= max_age:
        raise BatchMarketError("market_receipt_stale_future_or_unbounded")
    return payload, start, end


def _rate_source_time(value):
    if not isinstance(value, str):
        raise BatchMarketError("market_original_rate_timestamp_required")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    normalized = value + "Z" if parsed.tzinfo is None else value
    return _pnl_timestamp(normalized)


def _projection(*, state_receipt, rate_receipt, instrument, instrument_spec, now):
    spec = validated_instrument_spec(instrument_spec, instrument)
    instrument_id, symbol = spec["instrument_id"], spec["symbol"]
    current = exact_datetime_epoch_seconds(_aware_utc(now, "market_now"))
    state_raw, state_start, state_end = _checked_receipt(
        state_receipt, _search_arguments(instrument_id), current, STATE_MAX_AGE_SECONDS)
    rows = state_raw.get("items")
    if (not isinstance(rows, list) or len(rows) != 1 or type(state_raw.get("totalItems")) is not int
            or state_raw["totalItems"] != len(rows) or not isinstance(rows[0], dict)):
        raise BatchMarketError("market_complete_exact_search_required")
    state = rows[0]
    if (type(state.get("instrumentId")) is not int or state["instrumentId"] != instrument_id
            or state.get("internalSymbolFull") != symbol):
        raise BatchMarketError("market_exact_instrument_state_required")
    if (not all(state.get(key) is True for key in
                ("isCurrentlyTradable", "isExchangeOpen", "isActiveInPlatform", "isBuyEnabled"))
            or state.get("isDelisted") is not False):
        raise BatchMarketError("market_not_open_and_tradable")
    rates, rate_start, rate_end = _checked_receipt(
        rate_receipt, _rate_arguments(instrument_id), current, QUOTE_MAX_AGE_SECONDS)
    rows = rates.get("results")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise BatchMarketError("market_exact_rate_required")
    rate = rows[0]
    if (type(rate.get("instrumentId")) is not int or rate["instrumentId"] != instrument_id
            or rate.get("quoteType") != "realtime"):
        raise BatchMarketError("market_exact_realtime_rate_required")
    # A currency stated by a response cannot contradict the reviewed mapping.
    # Neither optional field is manufactured or treated as a required API field.
    if any(rate[field] != "USD" for field in ("currency", "quoteCurrency") if field in rate):
        raise BatchMarketError("market_quote_currency_mismatch")
    spelling, source_time = _rate_source_time(rate.get("date"))
    if source_time > rate_end or not 0 <= current - source_time <= QUOTE_MAX_AGE_SECONDS:
        raise BatchMarketError("rate_source_time_stale_or_future")
    bid, ask = _number(rate.get("bid"), "bid"), _number(rate.get("ask"), "ask")
    quote = QuoteEvidence(payload_hash(rates), instrument.internal_instrument_id,
                          _datetime(source_time), bid, ask, True)
    market_state = {**{field: deepcopy(state[field]) for field in _STATE_FIELDS},
        "instrument_id": instrument_id, "quote_type": "realtime",
        "quote_id": quote.quote_id, "received_at": state_receipt["request_started_at"],
        "clock_basis": "bounded_market_request_start", "rate_time_basis": "getRates.date_documented_UTC",
        "state_request_started_at": state_receipt["request_started_at"],
        "state_received_at": state_receipt["response_received_at"],
        "quote_request_started_at": rate_receipt["request_started_at"],
        "quote_received_at": rate_receipt["response_received_at"], "quote_source_at": rate["date"],
        "quote_source_normalized_utc": spelling, "quote_source_epoch_seconds": source_time,
        "quote_currency": "USD", "quote_currency_basis": "reviewed_instrument_spec_and_registry",
        "source_age_limit_seconds": QUOTE_MAX_AGE_SECONDS,
        "quote_handoff_limit_seconds": QUOTE_HANDOFF_MAX_SECONDS,
        "instrument_spec": spec, "state_receipt": deepcopy(state_receipt),
        "rate_receipt": deepcopy(rate_receipt)}
    market_state["evidence_hash"] = payload_hash(market_state)
    return quote, market_state


def decode_market_state(value):
    """Restore the sole Decimal projection after canonical-JSON persistence.

    Original receipt JSON strings remain byte-for-byte unchanged. Full identity,
    hash and source validation is still performed by validate_market_evidence.
    """
    try:
        if not isinstance(value, dict) or set(value) != _MARKET_FIELDS:
            raise BatchMarketError("market_exact_persisted_shape_required")
        restored = deepcopy(value)
        epoch = restored["quote_source_epoch_seconds"]
        if not isinstance(epoch, (str, Decimal)):
            raise BatchMarketError("market_exact_persisted_source_epoch_required")
        epoch = Decimal(epoch)
        if not epoch.is_finite():
            raise BatchMarketError("market_exact_persisted_source_epoch_required")
        restored["quote_source_epoch_seconds"] = epoch
        # Canonical JSON sorts object keys. Restore the original request query
        # order from its retained URL so the existing byte-provenance validator
        # can reproduce that exact URL; never change a query's names or values.
        for name in ("state_receipt", "rate_receipt"):
            receipt = restored[name]
            pairs = parse_qsl(urlsplit(receipt["raw_request"]["url"]).query, keep_blank_values=True)
            if len(dict(pairs)) != len(pairs) or dict(pairs) != receipt["arguments"]["query"]:
                raise BatchMarketError("market_persisted_request_query_mismatch")
            receipt["arguments"]["query"] = dict(pairs)
        return restored
    except BatchMarketError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise BatchMarketError("market_malformed_persisted_evidence") from exc


def validate_market_evidence(*, quote, market_state, instrument, instrument_spec, now,
                             require_handoff=False):
    """Reparse originals, then optionally enforce the five-second send handoff."""
    try:
        if type(require_handoff) is not bool or not isinstance(market_state, dict):
            raise BatchMarketError("market_exact_evidence_required")
        rebuilt_quote, rebuilt_state = _projection(state_receipt=market_state["state_receipt"],
            rate_receipt=market_state["rate_receipt"], instrument=instrument,
            instrument_spec=instrument_spec, now=now)
        if quote != rebuilt_quote or market_state != rebuilt_state:
            raise BatchMarketError("market_evidence_changed")
        if require_handoff:
            current = exact_datetime_epoch_seconds(_aware_utc(now, "market_handoff_now"))
            if not 0 <= current - _seconds(market_state["quote_received_at"]) <= QUOTE_HANDOFF_MAX_SECONDS:
                raise BatchMarketError("market_quote_handoff_exceeded")
    except BatchMarketError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        raise BatchMarketError("market_malformed_or_unqualified_evidence") from exc


class DirectBatchReads:
    """Read-only direct host; no credentials lookup, retries or execution route."""

    def __init__(self, *, credentials, receipt_dir, clock=lambda: datetime.now(timezone.utc),
                 timeout_seconds=10, transport=None):
        self.clock = clock
        self.reader = DirectPilotReads(credentials=credentials, receipt_dir=receipt_dir,
            clock=clock, timeout_seconds=timeout_seconds, transport=transport)

    def read(self, path, **kwargs):
        return self.reader.read(path, **kwargs)

    def market(self, instrument, instrument_spec):
        try:
            spec = validated_instrument_spec(instrument_spec, instrument)
            state_args = _search_arguments(spec["instrument_id"])
            rate_args = _rate_arguments(spec["instrument_id"])
            state_receipt = self.read(**state_args)
            rate_receipt = self.read(**rate_args)
            return _projection(state_receipt=state_receipt, rate_receipt=rate_receipt,
                instrument=instrument, instrument_spec=spec, now=self.clock())
        except BatchMarketError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            raise BatchMarketError("market_malformed_or_unqualified_evidence") from exc
