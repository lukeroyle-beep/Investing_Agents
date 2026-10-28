"""Finite-mandate Demo adapter with retained quote clocks and terminal economics.

The inherited transport still permits only USD amount market buys at leverage
one with real settlement. Partial status 5 is lookup-only. Official order lookup
statuses 9/10 end the remainder; independent whole-account reconciliation is
still required before another order can be admitted.

Status contract checked 2026-10-28 against the official OpenAPI page:
https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details
"""
from __future__ import annotations

from dataclasses import asdict
import json
from decimal import Decimal, localcontext

from brokers.base import BrokerExecution, BrokerReconciliationResult
from brokers.etoro.account_evidence import _exact_sum
from brokers.etoro.autonomous_api import DirectDemoAdapter, DirectDemoError
from brokers.etoro.batch_reads import decode_market_state, validate_market_evidence
from brokers.etoro.connector import economic_decimal, instant, number, positive_id, reference_id
from brokers.etoro.schemas import EtoroSchemaError, _pnl_timestamp, exact_datetime_epoch_seconds
from execution.domain import Environment, _aware_utc, canonical_json, payload_hash
from execution.instruments import Instrument
from risk.pretrade import QuoteEvidence

_STATUS_NAMES = {1: "Received", 2: "Placed", 3: "Filled", 4: "Rejected",
    5: "PartiallyFilled", 6: "PendingCancel", 7: "Canceled", 8: "Expired",
    9: "CanceledPartiallyFilled", 10: "RejectedPartiallyFilled",
    11: "WaitingForMarket", 12: "PendingTriggeredRate"}
_TERMINAL = frozenset({3, 4, 7, 8, 9, 10})


def _market(bound):
    """Decode canonical storage without inventing an omitted clock or identity."""
    try:
        evidence = json.loads(canonical_json(bound["evidence"]))
        instrument = Instrument(**evidence["instrument"])
        quote_values = dict(evidence["quote"])
        quote_values["observed_at"] = instant(quote_values["observed_at"], "quote_source")
        quote = QuoteEvidence(**quote_values)
        state = decode_market_state(evidence["market_state"])
        spec = evidence["instrument_spec"]
        if (str(spec["instrument_id"]) != str(bound["instrument_id"])
                or str(instrument.internal_instrument_id) != bound["internal_instrument_id"]):
            raise ValueError
        return quote, state, instrument, spec
    except (KeyError, ValueError, TypeError, ArithmeticError):
        raise DirectDemoError("batch_bound_market_evidence_required") from None


class BatchDemoAdapter(DirectDemoAdapter):
    """A new finite path; the original single-order adapter is unchanged."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._request_hooks = {}

    def submit_order(self, intent, command):
        # Recording after transport avoids an fsync between the final clock
        # check and the send. The timestamp itself is sampled in _wire_guard,
        # never reconstructed from response time. A crash may leave it absent.
        key = str(command.command_id)
        self._request_hooks.pop(key, None)
        try:
            return super().submit_order(intent, command)
        finally:
            hook = self._request_hooks.pop(key, None)
            if hook is not None:
                self.bindings.append(command.command_id, {"request_hook": hook})

    def bind(self, command, **kwargs):
        # Validate before preserving the immutable binding. A malformed preview
        # must not consume an instrument/reference binding.
        intent = kwargs["intent"]
        proposed = {"evidence": kwargs.get("evidence"),
            "instrument_id": kwargs["instrument_id"],
            "internal_instrument_id": str(intent.internal_instrument_id)}
        quote, state, instrument, spec = _market(proposed)
        validate_market_evidence(quote=quote, market_state=state, instrument=instrument,
            instrument_spec=spec, now=self.clock())
        return super().bind(command, **kwargs)

    def _wire_guard(self, command, intent):
        self._scope(intent)
        bound = self._bound(command)
        authorization = self.bindings.read_authorization(command.command_id)
        self._shared_guard()
        quote, state, instrument, spec = _market(bound)
        validate_market_evidence(quote=quote, market_state=state, instrument=instrument,
            instrument_spec=spec, now=self.clock(), require_handoff=True)
        # Parsing is complete. These final checks do no I/O and resample the
        # clock after all retained evidence work, at the HTTP request hook.
        now = _aware_utc(self.clock(), "now")
        self._validate_binding_values(command, intent, bound, now)
        if (authorization.get("authorization_hash", bound["authorization_hash"]) != bound["authorization_hash"]
                or not instant(authorization["authorized_at"], "authorized_at") <= now
                       < instant(authorization["valid_until"], "valid_until")):
            raise DirectDemoError("direct_authorization_expired")
        current = exact_datetime_epoch_seconds(now)
        if not Decimal(0) <= current - state["quote_source_epoch_seconds"] <= Decimal(30):
            raise DirectDemoError("batch_quote_source_stale")
        received = exact_datetime_epoch_seconds(instant(state["quote_received_at"], "quote_received_at"))
        if not Decimal(0) <= current - received <= Decimal(5):
            raise DirectDemoError("batch_quote_handoff_exceeded")
        state_start = exact_datetime_epoch_seconds(instant(state["state_request_started_at"], "state_request_started_at"))
        if not Decimal(0) <= current - state_start <= Decimal(15):
            raise DirectDemoError("batch_market_state_stale")
        self._request_hooks[str(command.command_id)] = {
            "request_hook_at": now.isoformat(),
            "quote_source_at": state["quote_source_at"],
            "quote_source_normalized_utc": state["quote_source_normalized_utc"],
            "quote_source_epoch_seconds": state["quote_source_epoch_seconds"],
            "quote_received_at": state["quote_received_at"],
            "source_age_seconds": current - state["quote_source_epoch_seconds"],
            "receipt_to_request_hook_seconds": current - received,
            "source_age_limit_seconds": 30, "handoff_limit_seconds": 5,
            "quote_id": quote.quote_id, "market_evidence_hash": state["evidence_hash"],
            "clock_observation": "local_pre_transport_hook",
            "broker_receipt_observed": False}

    def reconcile_command(self, command):
        bound = self._bound(command)
        reference = bound["reference_id"]
        order_id = positive_id(command.broker_order_id, "order_id") if command.broker_order_id else None
        prior = self.bindings.outcomes(command.command_id)
        for outcome in prior:
            if outcome.get("referenceId") and outcome["referenceId"] != reference:
                raise DirectDemoError("conflicting_durable_recovery_reference")
            found = outcome.get("orderId")
            if found:
                found = positive_id(found, "order_id")
                if order_id and found != order_id:
                    raise DirectDemoError("conflicting_durable_recovery_order")
                order_id = found
        raw = self.client.lookup_order(**({"order_id": order_id} if order_id else {"reference_id": reference}))
        actual_order = positive_id(raw.get("orderId"), "order_id")
        asset = raw.get("asset", {})
        if (positive_id(raw.get("accountId"), "account_id") != self.expected_account_id
                or (order_id and actual_order != order_id)
                or raw.get("action") != "open" or raw.get("transaction") != "buy" or raw.get("type") != "mkt"
                or positive_id(asset.get("instrumentId"), "instrument_id") != str(bound["instrument_id"])
                or asset.get("side") != "long" or asset.get("settlementType") != "real"
                or number(asset.get("leverage"), "leverage") != 1 or str(asset.get("currency")).upper() != "USD"
                or str(raw.get("orderCurrency")).upper() != "USD"
                or number(raw.get("requestedAmount"), "requested_amount") != number(bound["amount"], "amount")
                or raw.get("requestedUnits") is not None or raw.get("requestedContracts") is not None
                or (raw.get("referenceId") is not None and reference_id(raw["referenceId"]) != reference)):
            raise DirectDemoError("lookup_bound_order_mismatch")
        status = int(positive_id(raw.get("status", {}).get("id"), "status_id"))
        if status not in _STATUS_NAMES or raw.get("status", {}).get("name", _STATUS_NAMES[status]) != _STATUS_NAMES[status]:
            raise DirectDemoError("lookup_status_unknown_or_conflicting")
        positions = raw.get("positionExecutions")
        if not isinstance(positions, list):
            raise DirectDemoError("lookup_execution_coverage_unknown")
        raw_hash = payload_hash(raw)
        if status not in _TERMINAL:
            # Even a partial position is not ready for economic finalization:
            # its cumulative units may still grow. Retain it and look up again.
            if any("terminal_fingerprint" in outcome for outcome in prior):
                raise DirectDemoError("durable_terminal_outcome_changed")
            self.bindings.append(command.command_id, {"lookup": raw, "lookup_sha256": raw_hash,
                "orderId": actual_order, "referenceId": reference})
            return BrokerReconciliationResult(command.command_id, False, "unknown", actual_order, None,
                (("status_id", str(status)), ("reason", "nonterminal_lookup_only")), ())
        if (status in {3, 9, 10} and not positions) or (status in {4, 7, 8} and positions):
            raise DirectDemoError("lookup_terminal_execution_coverage_mismatch")
        executions = []
        now_seconds = exact_datetime_epoch_seconds(_aware_utc(self.clock(), "now"))
        earliest = exact_datetime_epoch_seconds(instant(bound["prepared_at"], "prepared_at"))
        if command.attempt_history:
            earliest = max(earliest, exact_datetime_epoch_seconds(command.attempt_history[0].attempted_at))
        for position in positions:
            opening = position.get("openingData", {})
            position_id = positive_id(position.get("positionId"), "position_id")
            if positive_id(opening.get("orderId"), "opening_order_id") != actual_order or position.get("state") != "open":
                raise DirectDemoError("opening_order_or_position_mismatch")
            units, price = number(opening.get("units"), "units"), number(opening.get("avgPrice"), "price")
            fees, taxes = number(opening.get("fees"), "fees"), number(opening.get("taxes"), "taxes")
            principal = number(position.get("investedAmountCurrency"), "executed_amount")
            if (principal <= 0 or number(position.get("addedFunds"), "added_funds") != 0
                    or number(position.get("remainingUnits"), "remaining_units") != units
                    or opening.get("contracts") is not None):
                raise DirectDemoError("unchanged_opening_principal_units_required")
            if min(units, price) <= 0 or min(fees, taxes) < 0 or number(opening.get("avgConversionRate"), "conversion_rate") != 1:
                raise DirectDemoError("unsupported_execution_amount_or_conversion")
            # USD quote and account with conversion one must reconcile exactly.
            # The API does not publish a rounding tolerance. Until that contract
            # is qualified, even a genuine rounded difference stays lookup-only
            # for review instead of silently accepting a denomination mismatch.
            try:
                with localcontext() as context:
                    context.prec = max(50, len(units.as_tuple().digits) + len(price.as_tuple().digits) + 1)
                    product = units * price
                if product != principal:
                    raise ArithmeticError
            except ArithmeticError:
                raise DirectDemoError("arithmetic_rounding_or_denomination_requires_review") from None
            try:
                execution_source, source_seconds = _pnl_timestamp(opening.get("executionTime"))
            except EtoroSchemaError:
                raise DirectDemoError("execution_time_exact_offset_precision_required") from None
            if not earliest <= source_seconds <= now_seconds:
                raise DirectDemoError("execution_time_outside_command_window")
            executions.append(BrokerExecution(
                broker_execution_id=f"order:{actual_order}:position:{position_id}:open",
                broker_order_id=actual_order, broker_instrument_id=int(bound["instrument_id"]), broker_position_id=position_id,
                broker_reference_id=reference, environment=Environment.DEMO, side="long", action="buy",
                units=economic_decimal(units), price=economic_decimal(price), executed_at=instant(execution_source, "execution_time"),
                price_rate_id=positive_id(opening.get("priceId"), "price_id"), fees=economic_decimal(fees),
                taxes=economic_decimal(taxes), currency="USD", executed_amount=economic_decimal(principal),
                executed_at_source=execution_source))
        if len({x.broker_execution_id for x in executions}) != len(executions):
            raise DirectDemoError("duplicate_position_execution")
        principal = _exact_sum(x.executed_amount for x in executions)
        requested = number(bound["amount"], "amount")
        if (status == 3 and principal != requested) or (status in {9, 10} and not 0 < principal < requested):
            raise DirectDemoError("terminal_execution_principal_mismatch")
        if status in {4, 7, 8}:
            if number(raw.get("totalCosts"), "terminal_no_fill_costs") != 0:
                raise DirectDemoError("terminal_no_fill_costs_unreconciled")
            # A dated terminal rejection/cancellation plus an unchanged fresh
            # account is required downstream. A missing field is not zero.
            try:
                _, updated = _pnl_timestamp(raw.get("lastUpdate"))
            except EtoroSchemaError:
                raise DirectDemoError("terminal_no_fill_source_time_required") from None
            if not earliest <= updated <= now_seconds:
                raise DirectDemoError("terminal_no_fill_time_outside_command_window")
        current = {x.broker_execution_id: payload_hash(asdict(x)) for x in executions}
        fingerprint = payload_hash({"status_id": status, "executions": current,
            "order_id": actual_order, "reference": reference, "requested_amount": requested})
        for outcome in prior:
            if "terminal_fingerprint" in outcome and outcome["terminal_fingerprint"] != fingerprint:
                raise DirectDemoError("durable_terminal_outcome_changed")
        self.bindings.append(command.command_id, {"lookup": raw, "lookup_sha256": raw_hash, "orderId": actual_order,
            "referenceId": reference, "execution_hashes": current, "terminal_fingerprint": fingerprint})
        state = "partially_filled_terminal" if status in {9, 10} else {3: "filled", 4: "rejected", 7: "cancelled", 8: "rejected"}[status]
        return BrokerReconciliationResult(command.command_id, True, state, actual_order,
            executions[0].broker_position_id if len(executions) == 1 else None,
            (("status_id", str(status)), ("status_name", _STATUS_NAMES[status]),
             ("remainder_terminal", "true"), ("lookup_sha256", raw_hash),
             ("connector_broker_reference_id", reference)), tuple(executions))
