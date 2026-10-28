"""Bounded direct Demo execution under an independently checked standing mandate.

Only /me, the official Demo v2 create route, and its lookup are reachable. The
caller injects dedicated credentials; this module never discovers credentials.
Every response is privately persisted before decoding. A claimed request is
never submitted again, including after an exception or a process restart.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlencode
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import requests

from brokers.base import BrokerExecution, BrokerReconciliationResult, BrokerSubmissionResult
from brokers.etoro.account_evidence import _exact_sum
from brokers.etoro.capture_transport import RawCaptureResponse, _NoRedirectSession, private_json, private_write
from brokers.etoro.client import EtoroCredentials, HttpRequest
from brokers.etoro.connector import economic_decimal, instant, number, positive_id, reference_id, _sanitized
from brokers.etoro.schemas import EtoroSchemaError, _pnl_timestamp, exact_datetime_epoch_seconds
from execution.domain import (BrokerOperation, CommandState, Environment, OrderSide, OrderType,
                              SizingMethod, _aware_utc, canonical_json, payload_hash)

BASE_URL = "https://public-api.etoro.com"
PROFILE_PATH = "/api/v1/me"
SUBMIT_PATH = "/api/v2/trading/execution/demo/orders"
LOOKUP_PATH = "/api/v2/trading/info/demo/orders:lookup"
_LONDON = ZoneInfo("Europe/London")
_MAX_BODY = 8 * 1024 * 1024


class DirectDemoError(ValueError):
    """Only fixed reason codes are surfaced; never headers or remote bodies."""


def _private_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise DirectDemoError("private_owned_directory_required")
    path.chmod(0o700)


def _json_body(value) -> bytes:
    """Emit monetary Decimal values as JSON numbers without a float round trip."""
    def encode(item):
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise DirectDemoError("finite_json_numbers_required")
            return format(item, "f")
        if isinstance(item, dict):
            return "{" + ",".join(json.dumps(k) + ":" + encode(v) for k, v in sorted(item.items())) + "}"
        if isinstance(item, (list, tuple)):
            return "[" + ",".join(encode(v) for v in item) + "]"
        if isinstance(item, float):
            raise DirectDemoError("exact_json_numbers_required")
        return json.dumps(item, allow_nan=False, separators=(",", ":"))
    return encode(value).encode("utf-8")


def build_order_payload(instrument_id, amount):
    """The only supported direct order; monetary numbers remain exact."""
    instrument_id = int(positive_id(instrument_id, "instrument_id"))
    if isinstance(amount, float):
        raise DirectDemoError("exact_order_amount_required")
    amount = number(amount, "amount")
    if amount <= 0:
        raise DirectDemoError("positive_order_amount_required")
    return {"action": "open", "transaction": "buy", "instrumentId": instrument_id,
            "settlementType": "real", "orderType": "mkt", "leverage": 1,
            "amount": amount, "orderCurrency": "usd"}


def verify_profile(profile, expected_account_id):
    """Validate an already recorded raw /me response without retaining PII."""
    try:
        expected = positive_id(expected_account_id, "expected_account_id")
        scopes = profile.get("scopes")
        if (positive_id(profile.get("demoCid"), "demo_cid") != expected
                or not isinstance(scopes, list) or not all(isinstance(x, str) for x in scopes)
                or not set(scopes).intersection({"etoro-public:demo:read", "etoro-public:trade.demo:read"})
                or not set(scopes).intersection({"etoro-public:demo:write", "etoro-public:trade.demo:write"})):
            raise ValueError
    except Exception:
        raise DirectDemoError("direct_demo_identity_read_write_scope_unverified") from None
    return {"demo_cid": expected}


class DirectDemoHttpTransport:
    """One requests send, with no retry, redirect, proxy, cookie or netrc use."""

    def send(self, request):
        allowed = ((request.method == "GET" and request.url == BASE_URL + PROFILE_PATH)
                   or (request.method == "POST" and request.url == BASE_URL + SUBMIT_PATH)
                   or (request.method == "GET" and request.url.startswith(BASE_URL + LOOKUP_PATH + "?")))
        if not allowed or not 0 < request.timeout_seconds <= 10:
            raise DirectDemoError("direct_demo_transport_boundary")
        with _NoRedirectSession() as session:
            session.trust_env = False
            session.auth = None
            session.proxies.clear()
            session.cookies.clear()
            session.headers["Accept-Encoding"] = "identity"
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            outbound = session.prepare_request(requests.Request(
                request.method, request.url, headers=dict(request.headers), data=request.body))
            started = time.monotonic()
            with session.send(outbound, timeout=request.timeout_seconds, stream=True,
                              allow_redirects=False, verify=True, proxies={}) as response:
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in {"identity", "gzip", "deflate"}:
                    encoding = "unsupported"
                chunks, length, outcome = [], 0, "complete"
                try:
                    while chunk := response.raw.read1(65536, decode_content=False):
                        chunks.append(chunk)
                        length += len(chunk)
                        if length > _MAX_BODY or time.monotonic() - started > request.timeout_seconds:
                            outcome = "bounded_read_exceeded"
                            break
                except Exception:
                    outcome = "partial" if chunks else "failed"
                return RawCaptureResponse(int(response.status_code), b"".join(chunks), encoding, outcome)


class DirectDemoClient:
    """Allowlisted client with a durable, exclusive claim for every POST UUID."""

    def __init__(self, *, credentials: EtoroCredentials, receipt_dir: Path | str,
                 timeout_seconds: float = 10, transport=None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not isinstance(credentials, EtoroCredentials):
            raise DirectDemoError("dedicated_injected_credentials_required")
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 10:
            raise DirectDemoError("bounded_timeout_required")
        self._credentials = credentials
        self.receipt_dir = Path(receipt_dir)
        _private_directory(self.receipt_dir)
        self.timeout_seconds = timeout_seconds
        self.transport = transport if transport is not None else DirectDemoHttpTransport()
        self.clock = clock

    def get_profile(self):
        return self._send("GET", PROFILE_PATH, reference=uuid4())

    def submit_order(self, payload, *, reference, pre_transport_guard):
        # No optional fields, close orders, CFD settlement or Real endpoint can
        # enter through this API, even if the caller omits adapter validation.
        wanted = build_order_payload(payload.get("instrumentId"), payload.get("amount"))
        if payload != wanted:
            raise DirectDemoError("unsupported_direct_demo_order")
        if not callable(pre_transport_guard):
            raise DirectDemoError("pre_transport_guard_required")
        return self._send("POST", SUBMIT_PATH, reference=reference, body=_json_body(wanted),
                          pre_transport_guard=pre_transport_guard)

    def lookup_order(self, *, reference_id=None, order_id=None):
        if (reference_id is None) == (order_id is None):
            raise DirectDemoError("exactly_one_lookup_handle_required")
        query = ({"referenceId": str(UUID(str(reference_id)))} if reference_id is not None
                 else {"orderId": positive_id(order_id, "order_id")})
        return self._send("GET", LOOKUP_PATH, reference=uuid4(), query=query)

    def _send(self, method, path, *, reference, body=None, query=None, pre_transport_guard=None):
        if (method, path) not in {("GET", PROFILE_PATH), ("GET", LOOKUP_PATH), ("POST", SUBMIT_PATH)}:
            raise DirectDemoError("direct_demo_route_not_allowed")
        reference = str(UUID(str(reference)))
        basename = ("submit-" if method == "POST" else "read-") + reference
        started = _aware_utc(self.clock(), "now")
        receipt = {"method": method, "path": path, "query": query or {},
                   "request_id": reference, "request_started_at": started.isoformat(),
                   "body_sha256": hashlib.sha256(body).hexdigest() if body else None,
                   "body": body.decode() if body else None}
        try:
            private_json(self.receipt_dir / f"{basename}.request.json", receipt)
        except FileExistsError:
            raise DirectDemoError("request_reference_already_claimed_lookup_only") from None
        headers = {"x-api-key": self._credentials.api_key, "x-user-key": self._credentials.user_key,
                   "x-request-id": reference, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = BASE_URL + path + (("?" + urlencode(query)) if query else "")
        request = HttpRequest(method, url, headers, self.timeout_seconds, body)
        if method == "POST":
            try:
                # The request receipt has already been fsynced. Its latency
                # must never extend authorization to reach the network.
                if not callable(pre_transport_guard) or pre_transport_guard() is False:
                    raise ValueError
            except Exception:
                private_json(self.receipt_dir / f"{basename}.failure.json", {
                    "outcome": "pre_transport_guard_blocked", "request_id": reference,
                    "checked_at": _aware_utc(self.clock(), "now").isoformat()})
                raise DirectDemoError("pre_transport_guard_blocked_lookup_only") from None
        try:
            response = self.transport.send(request)
        except Exception:
            private_json(self.receipt_dir / f"{basename}.failure.json", {"outcome": "transport_unknown",
                         "response_received_at": _aware_utc(self.clock(), "now").isoformat()})
            raise DirectDemoError("transport_outcome_unknown_lookup_only") from None
        # Persist exact response bytes first, including errors, redirects and
        # partial streams; nothing parsed can escape ahead of this evidence.
        private_write(self.receipt_dir / f"{basename}.response.bin", response.body)
        completed = _aware_utc(self.clock(), "now")
        private_json(self.receipt_dir / f"{basename}.response.json", {
            "status_code": response.status_code, "content_encoding": response.content_encoding,
            "read_outcome": response.read_outcome, "response_received_at": completed.isoformat(),
            "body_sha256": hashlib.sha256(response.body).hexdigest(), "body_bytes": len(response.body)})
        if not started <= completed or (completed - started).total_seconds() > self.timeout_seconds:
            raise DirectDemoError("response_time_bound_exceeded_lookup_only")
        if response.status_code != 200 or response.read_outcome != "complete":
            raise DirectDemoError("response_not_complete_success_lookup_only")
        # We explicitly request identity encoding. Never allocate an unbounded
        # expansion merely to check its size afterwards; preserve and hold it.
        if response.content_encoding != "identity":
            raise DirectDemoError("response_encoding_unsupported_lookup_only")
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result
            def invalid_constant(_):
                raise ValueError
            decoded = response.body
            if len(decoded) > _MAX_BODY:
                raise ValueError
            result = json.loads(decoded, parse_float=Decimal, parse_constant=invalid_constant, object_pairs_hook=unique)
            if not isinstance(result, dict):
                raise ValueError
        except Exception:
            raise DirectDemoError("response_json_invalid_lookup_only") from None
        return result


class DirectBindingStore:
    """Immutable economic binding, authorization and reference send claim."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        _private_directory(self.path.parent)
        if self.path.is_symlink():
            raise DirectDemoError("private_binding_database_required")
        # Set mode before sqlite can write even the first page.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bindings (
                  command_id TEXT PRIMARY KEY, reference_id TEXT UNIQUE NOT NULL,
                  payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS authorizations (
                  command_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dispatches (
                  command_id TEXT PRIMARY KEY, reference_id TEXT UNIQUE NOT NULL,
                  claimed_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outcomes (
                  sequence INTEGER PRIMARY KEY, command_id TEXT NOT NULL,
                  payload TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(command_id,digest));
            """)

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _decode(row, reason):
        if not row:
            raise DirectDemoError(reason + "_missing")
        value = json.loads(row[0])
        if payload_hash(value) != row[1]:
            raise DirectDemoError(reason + "_corrupt")
        return value

    def bind(self, command_id, value):
        encoded, digest = canonical_json(value), payload_hash(value)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload,digest FROM bindings WHERE command_id=?", (str(command_id),)).fetchone()
            if old and old != (encoded, digest):
                raise DirectDemoError("direct_binding_immutable")
            try:
                db.execute("INSERT OR IGNORE INTO bindings VALUES (?,?,?,?)",
                           (str(command_id), value["reference_id"], encoded, digest))
            except sqlite3.IntegrityError:
                raise DirectDemoError("request_reference_already_bound") from None
            actual = db.execute("SELECT command_id FROM bindings WHERE reference_id=?", (value["reference_id"],)).fetchone()
            if not actual or actual[0] != str(command_id):
                raise DirectDemoError("request_reference_already_bound")

    def read(self, command_id):
        with closing(self._connect()) as db:
            return self._decode(db.execute("SELECT payload,digest FROM bindings WHERE command_id=?",
                                          (str(command_id),)).fetchone(), "direct_binding")

    def authorize(self, command_id, value):
        bound = self.read(command_id)
        start, end = instant(value.get("authorized_at"), "authorized_at"), instant(value.get("valid_until"), "valid_until")
        if not start < end <= instant(bound["expires_at"], "expires_at"):
            raise DirectDemoError("authorization_outside_bound_validity")
        if value.get("authorization_hash", bound["authorization_hash"]) != bound["authorization_hash"]:
            raise DirectDemoError("authorization_identity_mismatch")
        encoded, digest = canonical_json(value), payload_hash(value)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload,digest FROM authorizations WHERE command_id=?", (str(command_id),)).fetchone()
            if old and old != (encoded, digest):
                raise DirectDemoError("direct_authorization_immutable")
            db.execute("INSERT OR IGNORE INTO authorizations VALUES (?,?,?)", (str(command_id), encoded, digest))

    def claim(self, command_id, now):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            bound = self._decode(db.execute("SELECT payload,digest FROM bindings WHERE command_id=?",
                                           (str(command_id),)).fetchone(), "direct_binding")
            authorization = self._decode(db.execute("SELECT payload,digest FROM authorizations WHERE command_id=?",
                                                   (str(command_id),)).fetchone(), "direct_authorization")
            if not instant(authorization["authorized_at"], "authorized_at") <= _aware_utc(now, "now") < instant(authorization["valid_until"], "valid_until"):
                raise DirectDemoError("direct_authorization_expired")
            try:
                db.execute("INSERT INTO dispatches VALUES (?,?,?)", (str(command_id), bound["reference_id"], _aware_utc(now, "now").isoformat()))
            except sqlite3.IntegrityError:
                raise DirectDemoError("direct_already_dispatched_lookup_only") from None

    def read_authorization(self, command_id):
        """Read the immutable dispatch deadline without claiming another send."""
        with closing(self._connect()) as db:
            return self._decode(db.execute("SELECT payload,digest FROM authorizations WHERE command_id=?",
                                          (str(command_id),)).fetchone(), "direct_authorization")

    def append(self, command_id, value):
        value = _sanitized(value)
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO outcomes(command_id,payload,digest) VALUES (?,?,?)",
                       (str(command_id), canonical_json(value), payload_hash(value)))

    def outcomes(self, command_id):
        with closing(self._connect()) as db:
            rows = db.execute("SELECT payload,digest FROM outcomes WHERE command_id=? ORDER BY sequence", (str(command_id),)).fetchall()
        return [self._decode(row, "direct_outcome") for row in rows]


class DirectDemoAdapter:
    broker_name = "etoro"
    environment = Environment.DEMO
    read_only = False
    # Unlike MCP placement, the local logical UUID really is our X-Request-Id.
    external_reference_identity = False

    def __init__(self, *, expected_account_id, client, bindings, clock, pre_send_guard=None):
        self.expected_account_id = positive_id(expected_account_id, "expected_account_id")
        self.client, self.bindings, self.clock = client, bindings, clock
        self.pre_send_guard = pre_send_guard

    def verify_demo_identity(self):
        try:
            verify_profile(self.client.get_profile(), self.expected_account_id)
        except Exception:
            raise DirectDemoError("direct_demo_identity_read_write_scope_unverified") from None
        return {"demo_cid": self.expected_account_id, "verified_at": _aware_utc(self.clock(), "now")}

    @staticmethod
    def _scope(intent):
        if (intent.environment != Environment.DEMO or intent.side != OrderSide.BUY
                or intent.order_type != OrderType.MARKET or intent.sizing_method != SizingMethod.FIXED_NOTIONAL
                or intent.currency != "USD" or intent.target_leverage != 1):
            raise DirectDemoError("direct_requires_demo_usd_real_long_one_market_order")

    def bind(self, command, *, intent, instrument_id, authorization_hash, amount=None, expires_at=None, evidence=None):
        self._scope(intent)
        now = _aware_utc(self.clock(), "now")
        amount = intent.sizing_value if amount is None else number(amount, "amount")
        expiry = min(intent.expires_at, _aware_utc(expires_at, "expires_at")) if expires_at else intent.expires_at
        if (command.intent_hash != intent.intent_hash or command.broker != "etoro"
                or command.environment != Environment.DEMO or command.operation != BrokerOperation.SUBMIT_ORDER
                or amount != intent.sizing_value or now >= expiry
                or not isinstance(authorization_hash, str) or not authorization_hash.strip()):
            raise DirectDemoError("direct_order_binding_mismatch")
        payload = build_order_payload(instrument_id, amount)
        if command.payload_hash != payload_hash(payload):
            raise DirectDemoError("direct_wire_payload_binding_mismatch")
        value = {"intent_hash": intent.intent_hash, "command_payload_hash": command.payload_hash,
                 "internal_instrument_id": str(intent.internal_instrument_id),
                 "account_id": self.expected_account_id, "instrument_id": payload["instrumentId"],
                 "amount": amount, "reference_id": str(command.logical_request_id),
                 "expires_at": expiry, "prepared_at": now, "authorization_hash": authorization_hash,
                 "payload": payload, "evidence": _sanitized(evidence or {})}
        self.bindings.bind(command.command_id, value)
        return self.bindings.read(command.command_id)

    def validate_binding(self, command, intent, now):
        self._scope(intent)
        bound = self._bound(command)
        return self._validate_binding_values(command, intent, bound, now)

    @staticmethod
    def _validate_binding_values(command, intent, bound, now):
        """Pure final checks: no I/O can age the clock after it is sampled."""
        now = _aware_utc(now, "now")
        if (intent.intent_hash != bound["intent_hash"] or intent.sizing_value != number(bound["amount"], "amount")
                or str(intent.internal_instrument_id) != bound["internal_instrument_id"]
                or payload_hash(build_order_payload(bound["instrument_id"], bound["amount"])) != command.payload_hash):
            raise DirectDemoError("direct_intent_binding_mismatch")
        if (now >= instant(bound["expires_at"], "expires_at") or now >= intent.expires_at
                or now < instant(bound["prepared_at"], "prepared_at")
                or now.astimezone(_LONDON).date() != instant(bound["prepared_at"], "prepared_at").astimezone(_LONDON).date()):
            raise DirectDemoError("direct_binding_expired")
        return bound

    def _shared_guard(self):
        if not callable(self.pre_send_guard):
            raise DirectDemoError("fresh_pre_send_guard_required")
        try:
            if self.pre_send_guard() is False:
                raise ValueError
        except Exception:
            raise DirectDemoError("fresh_pre_send_guard_blocked") from None

    def _wire_guard(self, command, intent):
        self._scope(intent)
        bound = self._bound(command)
        authorization = self.bindings.read_authorization(command.command_id)
        self._shared_guard()
        now = _aware_utc(self.clock(), "now")
        self._validate_binding_values(command, intent, bound, now)
        if (authorization.get("authorization_hash", bound["authorization_hash"]) != bound["authorization_hash"]
                or not instant(authorization["authorized_at"], "authorized_at") <= now
                       < instant(authorization["valid_until"], "valid_until")):
            raise DirectDemoError("direct_authorization_expired")

    def _bound(self, command):
        bound = self.bindings.read(command.command_id)
        if (command.broker != "etoro" or command.environment != Environment.DEMO
                or command.operation != BrokerOperation.SUBMIT_ORDER
                or command.intent_hash != bound["intent_hash"] or command.payload_hash != bound["command_payload_hash"]
                or self.expected_account_id != bound["account_id"]
                or str(command.logical_request_id) != bound["reference_id"]
                or (command.broker_reference_id and command.broker_reference_id != bound["reference_id"])):
            raise DirectDemoError("direct_command_binding_mismatch")
        return bound

    def submit_order(self, intent, command):
        self.validate_binding(command, intent, self.clock())
        if (command.state != CommandState.SUBMISSION_PENDING or len(command.attempt_history) != 1
                or command.attempt_history[0].outcome != "send_started"
                or command.attempt_history[0].request_id != command.logical_request_id):
            raise DirectDemoError("durable_coordinator_send_claim_required")
        identity = self.verify_demo_identity()
        self.bindings.append(command.command_id, {"dispatch_identity": identity})
        self._shared_guard()
        bound = self.validate_binding(command, intent, self.clock())
        self.bindings.claim(command.command_id, self.clock())
        try:
            raw = self.client.submit_order(build_order_payload(bound["instrument_id"], bound["amount"]),
                                           reference=command.logical_request_id,
                                           pre_transport_guard=lambda: self._wire_guard(command, intent))
            order_id = positive_id(raw.get("orderId"), "order_id")
            if reference_id(raw.get("referenceId")) != bound["reference_id"]:
                raise ValueError
            self.bindings.append(command.command_id, {"submission": raw, "orderId": order_id,
                                                     "referenceId": bound["reference_id"]})
        except Exception:
            self.bindings.append(command.command_id, {"outcome": "unknown", "referenceId": bound["reference_id"]})
            return BrokerSubmissionResult(command.logical_request_id, False, None, bound["reference_id"], "unknown", True)
        return BrokerSubmissionResult(command.logical_request_id, True, order_id, bound["reference_id"], "accepted", False)

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
        positions = raw.get("positionExecutions")
        if not isinstance(positions, list):
            raise DirectDemoError("lookup_execution_coverage_unknown")
        if status in {5, 9, 10} or (positions and status != 3):
            self.bindings.append(command.command_id, {"lookup": raw, "orderId": actual_order, "referenceId": reference})
            return BrokerReconciliationResult(command.command_id, False, "unknown", actual_order, None,
                (("reason", "partial_cumulative_execution_requires_reconciliation"),), ())
        states = {1: "acknowledged", 2: "acknowledged", 3: "filled", 4: "rejected",
                  6: "cancel_pending", 7: "cancelled", 8: "rejected", 11: "acknowledged", 12: "acknowledged"}
        if status not in states or (status == 3 and not positions):
            raise DirectDemoError("lookup_status_or_fills_unknown")
        executions = []
        now_seconds = exact_datetime_epoch_seconds(_aware_utc(self.clock(), "now"))
        earliest = exact_datetime_epoch_seconds(instant(bound["prepared_at"], "prepared_at"))
        if command.attempt_history:
            earliest = max(earliest, exact_datetime_epoch_seconds(command.attempt_history[0].attempted_at))
        for position in positions:
            opening = position.get("openingData", {})
            position_id = positive_id(position.get("positionId"), "position_id")
            if (positive_id(opening.get("orderId"), "opening_order_id") != actual_order or position.get("state") != "open"):
                raise DirectDemoError("opening_order_or_position_mismatch")
            units, price = number(opening.get("units"), "units"), number(opening.get("avgPrice"), "price")
            fees, taxes = number(opening.get("fees"), "fees"), number(opening.get("taxes"), "taxes")
            principal = number(position.get("investedAmountCurrency"), "executed_amount")
            if (principal <= 0 or number(position.get("addedFunds"), "added_funds") != 0
                    or number(position.get("remainingUnits"), "remaining_units") != units):
                raise DirectDemoError("unchanged_full_opening_principal_required")
            if min(units, price) <= 0 or min(fees, taxes) < 0 or number(opening.get("avgConversionRate"), "conversion_rate") != 1:
                raise DirectDemoError("unsupported_execution_amount_or_conversion")
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
        if executions and _exact_sum(x.executed_amount for x in executions) != number(bound["amount"], "amount"):
            raise DirectDemoError("full_execution_principal_mismatch")
        current = {x.broker_execution_id: payload_hash(asdict(x)) for x in executions}
        for outcome in prior:
            if outcome.get("execution_hashes") and outcome["execution_hashes"] != current:
                raise DirectDemoError("durable_execution_economics_changed")
        self.bindings.append(command.command_id, {"lookup": raw, "orderId": actual_order,
                                                 "referenceId": reference, "execution_hashes": current})
        return BrokerReconciliationResult(command.command_id, status in {3, 4, 7, 8}, states[status], actual_order,
            executions[0].broker_position_id if len(executions) == 1 else None,
            (("status_id", str(status)), ("connector_broker_reference_id", reference)), tuple(executions))
