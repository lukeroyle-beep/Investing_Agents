"""Credential-free bridge to the supported eToro prepare/place/lookup tools.

Callbacks are supplied by the authenticated connector host. Tokens stay in
memory; only their hashes are durable. A lost send is a lookup obligation,
never permission to call place_trade again, even with the same token.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping
from uuid import UUID

from brokers.base import BrokerExecution, BrokerReconciliationResult, BrokerSubmissionResult
from brokers.etoro.schemas import EtoroSchemaError, _pnl_timestamp
from execution.domain import Environment, OrderSide, OrderType, SizingMethod, _aware_utc, canonical_json, payload_hash


class ConnectorEvidenceError(ValueError):
    pass


def response_object(value):
    """Prefer raw JSON text so broker decimal precision survives decoding."""
    if isinstance(value, str):
        value = json.loads(value, parse_float=Decimal)
    if not isinstance(value, Mapping):
        raise ConnectorEvidenceError("connector_response_object_required")
    def reject_floats(item):
        if isinstance(item, float):
            raise ConnectorEvidenceError("raw_json_or_exact_decimal_response_required")
        if isinstance(item, Mapping):
            for child in item.values():
                reject_floats(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                reject_floats(child)
    if value.get("isError"):
        raise ConnectorEvidenceError("connector_reported_error")
    if "content" in value:
        parts = [x["text"] for x in value["content"] if x.get("type") == "text"]
        if len(parts) != 1:
            raise ConnectorEvidenceError("single_raw_json_connector_response_required")
        return response_object(parts[0])
    if "statusCode" in value and "body" in value:
        if value.get("statusCode") != 200 or value.get("bodyTruncated") is True:
            raise ConnectorEvidenceError("successful_complete_connector_read_required")
        return response_object(value["body"])
    reject_floats(value)
    return dict(value)


def number(value, name):
    if value is None or isinstance(value, bool):
        raise ConnectorEvidenceError(f"{name}_unknown")
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ConnectorEvidenceError(f"{name}_invalid") from exc
    if not result.is_finite():
        raise ConnectorEvidenceError(f"{name}_invalid")
    return result


def economic_decimal(value):
    """Canonical value spelling without Decimal.normalize context rounding."""
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return Decimal(rendered)


def instant(value, name):
    try:
        return _aware_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")), name)
    except (ValueError, TypeError) as exc:
        raise ConnectorEvidenceError(f"{name}_timezone_required") from exc


def positive_id(value, name):
    parsed = number(value, name)
    if parsed <= 0 or parsed != parsed.to_integral_value():
        raise ConnectorEvidenceError(f"{name}_invalid")
    return str(int(parsed))


def reference_id(value):
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        return str(UUID(value))
    except ValueError as exc:
        raise ConnectorEvidenceError("broker_reference_id_invalid") from exc


def _sanitized(value):
    if isinstance(value, Mapping):
        return {k: _sanitized(v) for k, v in value.items()
                if not any(secret in k.lower().replace("_", "").replace("-", "")
                           for secret in ("token", "authorization", "apikey", "userkey", "credential", "secret"))}
    if isinstance(value, (list, tuple)):
        return [_sanitized(v) for v in value]
    return value


class ConnectorBindingStore:
    """Immutable preview binding plus append-only dispatch observations."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS previews (
                    command_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS token_bindings (
                    token_hash TEXT PRIMARY KEY, command_id TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS dispatches (
                    command_id TEXT PRIMARY KEY, claimed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dispatch_authorizations (
                    command_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outcomes (
                    sequence INTEGER PRIMARY KEY, command_id TEXT NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL);
            """)
        self.path.chmod(0o600)

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def bind(self, command_id, value):
        encoded, digest = canonical_json(value), payload_hash(value)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,digest FROM previews WHERE command_id=?", (str(command_id),)).fetchone()
            if row and row != (encoded, digest):
                raise ConnectorEvidenceError("preview_binding_immutable")
            prior = db.execute("SELECT command_id FROM token_bindings WHERE token_hash=?", (value["token_hash"],)).fetchone()
            if prior and prior[0] != str(command_id):
                raise ConnectorEvidenceError("preview_token_already_bound_to_another_command")
            db.execute("INSERT OR IGNORE INTO token_bindings VALUES (?,?)", (value["token_hash"], str(command_id)))
            db.execute("INSERT OR IGNORE INTO previews VALUES (?,?,?)", (str(command_id), encoded, digest))

    def read(self, command_id):
        with closing(self._connect()) as db:
            row = db.execute("SELECT payload,digest FROM previews WHERE command_id=?", (str(command_id),)).fetchone()
        if not row:
            raise ConnectorEvidenceError("preview_binding_missing")
        value = json.loads(row[0])
        if payload_hash(value) != row[1]:
            raise ConnectorEvidenceError("preview_binding_corrupt")
        return value

    def claim(self, command_id, now):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload,digest FROM dispatch_authorizations WHERE command_id=?", (str(command_id),)).fetchone()
            if not row or payload_hash(json.loads(row[0])) != row[1]:
                raise ConnectorEvidenceError("fresh_dispatch_authorization_missing_or_corrupt")
            authorization = json.loads(row[0])
            if not instant(authorization["authorized_at"], "authorized_at") <= _aware_utc(now, "now") < instant(authorization["valid_until"], "valid_until"):
                raise ConnectorEvidenceError("fresh_dispatch_authorization_expired")
            try:
                db.execute("INSERT INTO dispatches VALUES (?,?)", (str(command_id), _aware_utc(now, "now").isoformat()))
            except sqlite3.IntegrityError as exc:
                raise ConnectorEvidenceError("connector_already_dispatched_lookup_only") from exc

    def authorize(self, command_id, value):
        encoded, digest = canonical_json(value), payload_hash(value)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload,digest FROM dispatch_authorizations WHERE command_id=?", (str(command_id),)).fetchone()
            if old and old != (encoded, digest):
                raise ConnectorEvidenceError("dispatch_authorization_immutable")
            db.execute("INSERT OR IGNORE INTO dispatch_authorizations VALUES (?,?,?)", (str(command_id), encoded, digest))

    def append(self, command_id, value):
        value = _sanitized(value)
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO outcomes(command_id,payload,digest) VALUES (?,?,?)",
                       (str(command_id), canonical_json(value), payload_hash(value)))

    def outcomes(self, command_id):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload,digest FROM outcomes WHERE command_id=? ORDER BY sequence", (str(command_id),)).fetchall()
        result = []
        for payload, digest in rows:
            value = json.loads(payload)
            if payload_hash(value) != digest:
                raise ConnectorEvidenceError("connector_outcome_corrupt")
            result.append(value)
        return result


class EtoroConnectorAdapter:
    broker_name = "etoro"
    environment = Environment.DEMO
    read_only = False
    external_reference_identity = True

    def __init__(self, *, bindings: ConnectorBindingStore, expected_account_id: str,
                 prepare_trade: Callable, place_trade: Callable, lookup_order: Callable,
                 get_profile: Callable, clock: Callable[[], datetime]):
        self.bindings = bindings
        self.expected_account_id = positive_id(expected_account_id, "expected_account_id")
        self.prepare_trade, self.place_trade, self.lookup_order = prepare_trade, place_trade, lookup_order
        self.get_profile = get_profile
        self.clock = clock
        self._tokens = {}
        self._preview_identity = None

    def verify_demo_identity(self):
        """Verify the connected Demo identity; retain no personal profile fields."""
        try:
            response = response_object(self.get_profile({}))
            profile = response.get("profile", {})
            scopes = profile.get("scopes")
            if (response.get("statusCode") != 200 or response.get("error")
                    or positive_id(profile.get("demoCid"), "profile_demo_cid") != self.expected_account_id
                    or not isinstance(scopes, list)
                    or "etoro-public:trade.demo:write" not in scopes):
                raise ValueError
        except Exception:
            raise ConnectorEvidenceError("connected_demo_identity_or_write_scope_unverified") from None
        return {"demo_cid": self.expected_account_id,
                "scope": "etoro-public:trade.demo:write", "verified_at": _aware_utc(self.clock(), "now")}

    def preview(self, *, intent, instrument_id):
        self._scope(intent)
        self._preview_identity = self.verify_demo_identity()
        amount = float(intent.sizing_value)
        if number(amount, "amount") != intent.sizing_value:
            raise ConnectorEvidenceError("connector_numeric_argument_precision_loss")
        return response_object(self.prepare_trade({
            "account": "demo", "instrumentId": instrument_id, "direction": "buy",
            "amount": amount, "leverage": 1, "orderType": "mkt"}))

    @staticmethod
    def _scope(intent):
        if (intent.environment != Environment.DEMO or intent.side != OrderSide.BUY
                or intent.order_type != OrderType.MARKET or intent.sizing_method != SizingMethod.FIXED_NOTIONAL
                or intent.currency != "USD" or intent.target_leverage != 1):
            raise ConnectorEvidenceError("connector_requires_demo_usd_fixed_notional_long_one_market_order")

    def bind_preview(self, command, *, intent, instrument_id, preview, eligibility, now):
        self._scope(intent)
        identity = self._preview_identity or self.verify_demo_identity()
        if not 0 <= (_aware_utc(now, "now") - identity["verified_at"]).total_seconds() <= 15:
            identity = self.verify_demo_identity()
        raw = response_object(preview)
        confirmation = raw.get("confirmation", {})
        token = raw.get("token") or raw.get("confirmationToken")
        if raw.get("verdict") != "ready" or raw.get("account") != "demo" or not isinstance(token, str) or not token:
            raise ConnectorEvidenceError("ready_demo_preview_with_token_required")
        market = confirmation.get("market", {})
        if (positive_id(market.get("instrumentId"), "instrument_id") != str(instrument_id)
                or confirmation.get("direction") != "buy" or confirmation.get("orderType") != "mkt"
                or number(confirmation.get("amount"), "amount") != intent.sizing_value
                or number(confirmation.get("leverage"), "leverage") != 1
                or confirmation.get("units") is not None or confirmation.get("contracts") is not None):
            raise ConnectorEvidenceError("preview_exact_order_mismatch")
        expires = instant(raw.get("expiresAt"), "preview_expiry")
        if expires <= _aware_utc(now, "now"):
            raise ConnectorEvidenceError("preview_expired")
        quote = confirmation.get("quote", {})
        quote_time = instant(quote.get("asOf"), "preview_quote")
        if not 0 <= (_aware_utc(now, "now") - quote_time).total_seconds() <= 30:
            raise ConnectorEvidenceError("preview_quote_stale")
        if number(quote.get("ask"), "ask") < number(quote.get("bid"), "bid") or number(quote.get("bid"), "bid") <= 0:
            raise ConnectorEvidenceError("preview_quote_invalid")
        eligible = response_object(eligibility)
        entries = eligible.get("eligibilities", [])
        if (str(eligible.get("currency")).upper() != "USD" or len(entries) != 1
                or eligible.get("notFoundInstrumentIds") != [] or eligible.get("notFoundSymbols") != []):
            raise ConnectorEvidenceError("complete_exact_eligibility_required")
        configuration = entries[0]
        matches = [x for x in configuration.get("leverageConfigs", [])
                   if x.get("direction") == "long" and 1 in x.get("leverageValues", [])]
        # prepare_trade has no settlement argument. An unambiguous real-long-1
        # configuration is required, so the bridge cannot select an x1 CFD.
        if (len(matches) != 1 or matches[0].get("settlementType") != "real"
                or matches[0].get("isPotential") is not False
                or configuration.get("allowOpenPosition") is not True
                or positive_id(configuration.get("instrumentId"), "instrument_id") != str(instrument_id)
                or configuration.get("symbol") != market.get("symbol")
                or configuration.get("allowedOrderQuantityType") not in {"all", "amountOnly"}
                or configuration.get("tradeUnitType") != "units"):
            raise ConnectorEvidenceError("unambiguous_real_long_one_eligibility_required")
        minimum = max(number(matches[0].get("minPositionAmount"), "minimum_amount"),
                      number(configuration.get("minPositionExposure"), "minimum_exposure"))
        estimated_units = number(confirmation.get("estimatedUnits"), "estimated_units")
        if (intent.sizing_value < minimum or estimated_units <= 0
                or estimated_units > number(configuration.get("maxUnitsPerOrder"), "maximum_units")
                or configuration.get("unitsQuantityType") not in {"whole", "fractional"}
                or (configuration.get("unitsQuantityType") == "whole" and estimated_units != estimated_units.to_integral_value())):
            raise ConnectorEvidenceError("broker_order_size_out_of_bounds")
        upfront = number(confirmation.get("totalUpfrontCost"), "upfront_cost")
        balance = confirmation.get("balance", {})
        if (upfront < 0 or balance.get("sufficient") is not True
                or number(balance.get("availableCash"), "available_cash") < intent.sizing_value + upfront
                or number(balance.get("required"), "required_cash") < intent.sizing_value + upfront):
            raise ConnectorEvidenceError("preview_balance_or_cost_unknown")
        value = {"schema_version": 1, "intent_hash": intent.intent_hash,
                 "command_payload_hash": command.payload_hash, "account_id": self.expected_account_id,
                 "instrument_id": instrument_id, "amount": str(intent.sizing_value),
                 "token_hash": payload_hash(token), "expires_at": expires,
                 "confirmation": _sanitized(confirmation), "warnings": _sanitized(raw.get("warnings", [])),
                 "confirmation_hash": payload_hash({"confirmation": _sanitized(confirmation), "warnings": _sanitized(raw.get("warnings", []))}),
                 "eligibility": eligible, "eligibility_hash": payload_hash(eligible),
                 "preview_identity": identity,
                 "prepared_at": _aware_utc(now, "now"), "reference_id": reference_id(raw.get("referenceId"))}
        self.bindings.bind(command.command_id, value)
        self._tokens[str(command.command_id)] = token
        return self.bindings.read(command.command_id)

    def arm_token(self, command_id, token):
        if payload_hash(token) != self.bindings.read(command_id)["token_hash"]:
            raise ConnectorEvidenceError("preview_token_mismatch")
        self._tokens[str(command_id)] = token

    def validate_binding(self, command, intent, now):
        self._scope(intent)
        bound = self.bindings.read(command.command_id)
        if (command.broker != self.broker_name or command.environment != Environment.DEMO
                or bound["intent_hash"] != intent.intent_hash or bound["command_payload_hash"] != command.payload_hash
                or bound["account_id"] != self.expected_account_id):
            raise ConnectorEvidenceError("preview_command_binding_mismatch")
        if _aware_utc(now, "now") >= instant(bound["expires_at"], "preview_expiry"):
            raise ConnectorEvidenceError("preview_expired_new_approval_required")
        token = self._tokens.get(str(command.command_id))
        if token is None or payload_hash(token) != bound["token_hash"]:
            raise ConnectorEvidenceError("matching_in_memory_preview_token_required")
        return bound

    def submit_order(self, intent, command):
        now = self.clock()
        self.validate_binding(command, intent, now)
        if not any(x.outcome == "send_started" for x in command.attempt_history):
            raise ConnectorEvidenceError("durable_coordinator_send_claim_required")
        identity = self.verify_demo_identity()
        self.bindings.append(command.command_id, {"dispatch_identity": identity})
        # The identity read may have consumed the remaining source/approval TTL.
        now = self.clock()
        self.validate_binding(command, intent, now)
        self.bindings.claim(command.command_id, now)
        token = self._tokens.pop(str(command.command_id))
        try:
            response = response_object(self.place_trade({"token": token}))
        except Exception:
            self.bindings.append(command.command_id, {"outcome": "unknown", "reason": "placement_response_unavailable"})
            raise ConnectorEvidenceError("placement_response_unavailable_lookup_only") from None
        self.bindings.append(command.command_id, {"placement_response": response})
        order_id, reference = self._placement_handles(response)
        outcome = response.get("outcome")
        ambiguous = (outcome not in {"executed", "partiallyFilled", "pending", "rejected", "cancelled", "expired"}
                     or (outcome in {"executed", "partiallyFilled", "pending"} and not order_id and not reference))
        return BrokerSubmissionResult(command.logical_request_id,
            outcome in {"executed", "partiallyFilled", "pending"}, order_id,
            reference, str(outcome or "unknown"), ambiguous)

    @staticmethod
    def _placement_handles(response):
        if response.get("account", "demo") != "demo":
            raise ConnectorEvidenceError("placement_environment_mismatch")
        order_id = positive_id(response["orderId"], "order_id") if response.get("orderId") is not None else None
        return order_id, reference_id(response.get("referenceId"))

    def reconcile_command(self, command):
        bound = self.bindings.read(command.command_id)
        order_id = positive_id(command.broker_order_id, "order_id") if command.broker_order_id else None
        reference = reference_id(command.broker_reference_id or bound.get("reference_id"))
        for outcome in self.bindings.outcomes(command.command_id):
            if "placement_response" not in outcome:
                continue
            found_order, found_reference = self._placement_handles(outcome["placement_response"])
            if ((order_id and found_order and order_id != found_order)
                    or (reference and found_reference and reference != found_reference)):
                raise ConnectorEvidenceError("conflicting_durable_broker_recovery_identity")
            order_id, reference = found_order or order_id, found_reference or reference
        if not order_id and not reference:
            # The connector generates its request ID internally. The local
            # logical UUID is NOT its broker reference and cannot substitute.
            raise ConnectorEvidenceError("recovery_handle_unavailable_lookup_only_no_resend")
        query = {"orderId": str(order_id)} if order_id else {"referenceId": str(reference)}
        raw = response_object(self.lookup_order(query))
        actual_order = positive_id(raw.get("orderId"), "order_id")
        asset = raw.get("asset", {})
        if (positive_id(raw.get("accountId"), "account_id") != self.expected_account_id
                or (order_id and actual_order != str(order_id))
                or raw.get("action") != "open" or raw.get("transaction") != "buy" or raw.get("type") != "mkt"
                or positive_id(asset.get("instrumentId"), "instrument_id") != str(bound["instrument_id"])
                or asset.get("side") != "long" or asset.get("settlementType") != "real"
                or number(asset.get("leverage"), "leverage") != 1 or str(asset.get("currency")).upper() != "USD"
                or str(raw.get("orderCurrency")).upper() != "USD"
                or number(raw.get("requestedAmount"), "requested_amount") != number(bound["amount"], "amount")
                or raw.get("requestedUnits") is not None or raw.get("requestedContracts") is not None):
            raise ConnectorEvidenceError("lookup_approved_order_mismatch")
        status = int(positive_id(raw.get("status", {}).get("id"), "status_id"))
        positions = raw.get("positionExecutions")
        if not isinstance(positions, list):
            raise ConnectorEvidenceError("lookup_execution_coverage_unknown")
        # The broker describes openingData as aggregate average-price data,
        # not individually identified fill events. Stage only a final full fill.
        if status in {5, 9, 10} or (positions and status != 3):
            return BrokerReconciliationResult(command.command_id, False, "unknown", actual_order, None,
                (("reason", "partial_cumulative_execution_requires_reconciliation"),), ())
        states = {1: "acknowledged", 2: "acknowledged", 3: "filled", 4: "rejected",
                  6: "cancel_pending", 7: "cancelled", 8: "rejected", 11: "acknowledged", 12: "acknowledged"}
        if status not in states or (status == 3 and not positions):
            raise ConnectorEvidenceError("lookup_status_or_fills_unknown")
        if positions and not reference:
            raise ConnectorEvidenceError("broker_reference_identity_unknown")
        if command.broker_reference_id and reference and command.broker_reference_id != str(reference):
            raise ConnectorEvidenceError("broker_reference_identity_mismatch")
        if raw.get("referenceId") is not None and reference_id(raw["referenceId"]) != reference:
            raise ConnectorEvidenceError("lookup_reference_identity_mismatch")
        executions = []
        for position in positions:
            opening = position.get("openingData", {})
            position_id = positive_id(position.get("positionId"), "position_id")
            if (positive_id(opening.get("orderId"), "opening_order_id") != actual_order
                    or position.get("state") != "open"):
                raise ConnectorEvidenceError("opening_order_or_position_mismatch")
            units, price = number(opening.get("units"), "units"), number(opening.get("avgPrice"), "price")
            fees, taxes = number(opening.get("fees"), "fees"), number(opening.get("taxes"), "taxes")
            principal = number(position.get("investedAmountCurrency"), "executed_amount")
            if (principal <= 0 or number(position.get("addedFunds"), "added_funds") != 0
                    or number(position.get("remainingUnits"), "remaining_units") != units):
                raise ConnectorEvidenceError("unchanged_full_opening_principal_required")
            if min(units, price) <= 0 or min(fees, taxes) < 0 or number(opening.get("avgConversionRate"), "conversion_rate") != 1:
                raise ConnectorEvidenceError("unsupported_execution_amount_or_conversion")
            try:
                execution_source, _ = _pnl_timestamp(opening.get("executionTime"))
            except EtoroSchemaError:
                raise ConnectorEvidenceError("execution_time_exact_offset_precision_required") from None
            executions.append(BrokerExecution(
                broker_execution_id=f"order:{actual_order}:position:{position_id}:open",
                broker_order_id=actual_order, broker_instrument_id=int(bound["instrument_id"]),
                broker_position_id=position_id,
                broker_reference_id=str(reference),
                environment=Environment.DEMO, side="long", action="buy", units=economic_decimal(units), price=economic_decimal(price),
                executed_at=instant(execution_source, "execution_time"),
                price_rate_id=positive_id(opening.get("priceId"), "price_id"),
                fees=economic_decimal(fees), taxes=economic_decimal(taxes), currency="USD",
                executed_amount=economic_decimal(principal), executed_at_source=execution_source))
        if len({x.broker_execution_id for x in executions}) != len(executions):
            raise ConnectorEvidenceError("duplicate_position_execution")
        self.bindings.append(command.command_id, {"lookup": _sanitized(raw), "orderId": actual_order,
                                                "referenceId": reference})
        return BrokerReconciliationResult(command.command_id, status in {3, 4, 7, 8}, states[status],
            actual_order, executions[0].broker_position_id if len(executions) == 1 else None,
            (("status_id", str(status)), ("connector_broker_reference_id", str(reference or ""))), tuple(executions))
