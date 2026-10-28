"""Immutable finite Demo authority, separate from the original one-shot pilot.

This module never creates an account, ledger, approval token or broker session.
An authority names the existing ledger and a finite, reviewed execution scope.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from execution.domain import Environment, canonical_json, payload_hash
from execution.pilot_mandate import PilotMandateError, _identity, _timestamp, _unique_pairs
from risk.kill_switch import KillSwitch, KillSwitchStatus


POLICY = "finite-demo-v2"
MANDATE_SCHEMA = "finite-demo-mandate-v2"
_SCOPE_KEYS = {"instruments", "max_submissions", "max_per_day", "per_order_cap",
               "total_commitment", "max_positions", "min_cash_fraction", "loss_stop", "order_types"}
_INSTRUMENT_KEYS = {"instrument_id", "symbol", "exchange", "asset_type", "economic_class",
                    "currency", "issuer", "sector", "class_source_reference"}


class FiniteDemoMandateError(PilotMandateError):
    pass


def _decimal(value, name):
    if isinstance(value, (float, bool)) or value is None:
        raise FiniteDemoMandateError(f"finite_{name}_exact_decimal_required")
    try:
        result = Decimal(value)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise FiniteDemoMandateError(f"finite_{name}_exact_decimal_required") from exc
    if not result.is_finite():
        raise FiniteDemoMandateError(f"finite_{name}_invalid")
    return result


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise FiniteDemoMandateError(f"finite_{name}_positive_integer_required")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise FiniteDemoMandateError(f"finite_{name}_required")
    return value


def _commit(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{40}", value) is None:
        raise FiniteDemoMandateError("finite_reviewed_commit_required")
    return value


def validate_scope(scope):
    """Normalize exact requested bounds; code ceilings do not create authority."""
    if not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
        raise FiniteDemoMandateError("finite_scope_shape_invalid")
    scope = dict(scope)
    for key in ("max_submissions", "max_per_day", "max_positions"):
        _positive_int(scope[key], key)
    if scope["max_positions"] > 5:
        raise FiniteDemoMandateError("finite_count_limit_invalid")
    for key in ("per_order_cap", "total_commitment", "min_cash_fraction", "loss_stop"):
        scope[key] = str(_decimal(scope[key], key))
    if not (0 < Decimal(scope["per_order_cap"]) <= 100
            and 0 < Decimal(scope["total_commitment"]) <= 500
            and Decimal(scope["per_order_cap"]) <= Decimal(scope["total_commitment"])
            and Decimal("0.20") <= Decimal(scope["min_cash_fraction"]) <= 1
            and 0 < Decimal(scope["loss_stop"]) <= 25):
        raise FiniteDemoMandateError("finite_risk_limit_invalid")
    # Other order types need their own reviewed payload and terminal evidence.
    if scope["order_types"] != ["market"]:
        raise FiniteDemoMandateError("finite_supported_explicit_order_types_required")
    if not isinstance(scope["instruments"], list) or not scope["instruments"]:
        raise FiniteDemoMandateError("finite_instrument_allowlist_required")
    ids, symbols, normalized = set(), set(), []
    for instrument in scope["instruments"]:
        if not isinstance(instrument, dict) or set(instrument) != _INSTRUMENT_KEYS:
            raise FiniteDemoMandateError("finite_instrument_shape_invalid")
        item = dict(instrument)
        _positive_int(item["instrument_id"], "instrument_id")
        for key in _INSTRUMENT_KEYS - {"instrument_id"}:
            _text(item[key], key)
        if (item["asset_type"] not in {"equity", "etf"} or item["currency"] != "USD"
                or item["economic_class"] not in {"equity", "fixed_income", "commodity", "crypto"}
                or item["asset_type"] == "equity" and item["economic_class"] != "equity"):
            raise FiniteDemoMandateError("finite_supported_usd_real_asset_scope_required")
        if item["instrument_id"] in ids or (item["symbol"], item["exchange"]) in symbols:
            raise FiniteDemoMandateError("finite_duplicate_instrument")
        ids.add(item["instrument_id"])
        symbols.add((item["symbol"], item["exchange"]))
        normalized.append(item)
    scope["instruments"] = normalized
    return scope


@dataclass(frozen=True, slots=True)
class FiniteOrderCandidate:
    command_id: str
    instrument_id: int
    amount: Decimal
    expected_costs: Decimal
    intent_hash: str
    order_type: str = "market"
    currency: str = "USD"
    side: str = "buy"
    leverage: Decimal = Decimal("1")
    settlement_type: str = "real"


@dataclass(frozen=True, slots=True)
class FiniteDemoMandate:
    path: Path
    account_id: str
    mandate_hash: str
    mandate_id: str
    source_reference: str
    issued_at: datetime
    expires_at: datetime
    ledger_path: Path
    ledger_inception_hash: str
    reviewed_commit: str
    baseline_claims: tuple[tuple[str, str], ...]
    _scope_json: str

    @property
    def scope(self):
        # A returned dict cannot mutate this immutable authority in memory.
        return json.loads(self._scope_json)

    @classmethod
    def create(cls, path, *, account_id, source_reference, issued_at, expires_at,
               ledger, reviewed_commit, scope):
        from agents.fill_agent.pilot_ledger import PilotLedger
        if not isinstance(ledger, PilotLedger):
            raise FiniteDemoMandateError("finite_original_pilot_ledger_required")
        account_id = _identity(account_id)
        state = ledger.snapshot()
        moment, expires = _timestamp(issued_at), _timestamp(expires_at)
        if (state["account_id"] != account_id or state["inception"]["policy"] != "demo-pilot-v1"
                or moment < _timestamp(state["inception"]["inception"])):
            raise FiniteDemoMandateError("finite_original_inception_mismatch")
        if expires <= moment:
            raise FiniteDemoMandateError("finite_expiry_must_follow_issue")
        payload = {"schema": MANDATE_SCHEMA, "policy": POLICY, "environment": "demo",
            "broker": "etoro", "transport": "official_demo_api", "mandate_id": str(uuid4()),
            "account_id": account_id, "source_reference": _text(source_reference, "source_reference"),
            "issued_at": moment, "expires_at": expires,
            "ledger_path": str(ledger.path.resolve()), "ledger_inception_hash": payload_hash(state["inception"]),
            "baseline_claims": [{"command_id": s["command_id"], "sha256": payload_hash({k: v for k, v in s.items()
                if k not in {"reconciled", "journal_complete"}})} for s in state["submissions"]],
            "reviewed_commit": _commit(reviewed_commit), "scope": validate_scope(scope)}
        path = Path(path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise FiniteDemoMandateError("finite_mandate_exists_never_overwrite") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json({"mandate": payload, "sha256": payload_hash(payload)}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls.load(path, expected_account_id=account_id)

    @classmethod
    def load(cls, path, *, expected_account_id):
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 65536:
                raise FiniteDemoMandateError("finite_mandate_size_invalid")
            record = json.loads(raw, object_pairs_hook=_unique_pairs)
            if not isinstance(record, dict) or set(record) != {"mandate", "sha256"}:
                raise FiniteDemoMandateError("finite_mandate_record_invalid")
            payload = record["mandate"]
            keys = {"schema", "policy", "environment", "broker", "transport", "mandate_id",
                    "account_id", "source_reference", "issued_at", "expires_at", "ledger_path",
                    "ledger_inception_hash", "reviewed_commit", "baseline_claims", "scope"}
            if not isinstance(payload, dict) or set(payload) != keys or payload_hash(payload) != record["sha256"]:
                raise FiniteDemoMandateError("finite_mandate_integrity_failure")
            if (payload["schema"] != MANDATE_SCHEMA or payload["policy"] != POLICY
                    or payload["environment"] != "demo" or payload["broker"] != "etoro"
                    or payload["transport"] != "official_demo_api"):
                raise FiniteDemoMandateError("finite_mandate_scope_invalid")
            if _identity(payload["account_id"]) != _identity(expected_account_id):
                raise FiniteDemoMandateError("finite_wrong_demo_account")
            UUID(payload["mandate_id"])
            _text(payload["source_reference"], "source_reference")
            _commit(payload["reviewed_commit"])
            issued, expires = _timestamp(payload["issued_at"]), _timestamp(payload["expires_at"])
            if expires <= issued:
                raise FiniteDemoMandateError("finite_expiry_must_follow_issue")
            ledger_path = Path(payload["ledger_path"])
            if (not ledger_path.is_absolute() or str(ledger_path.resolve()) != str(ledger_path)
                    or re.fullmatch(r"[a-f0-9]{64}", payload["ledger_inception_hash"]) is None):
                raise FiniteDemoMandateError("finite_original_ledger_binding_invalid")
            normalized = validate_scope(payload["scope"])
            if normalized != payload["scope"]:
                raise FiniteDemoMandateError("finite_noncanonical_scope")
            baseline = payload["baseline_claims"]
            if not isinstance(baseline, list):
                raise FiniteDemoMandateError("finite_baseline_claims_invalid")
            seen = set()
            for claim in baseline:
                if (not isinstance(claim, dict) or set(claim) != {"command_id", "sha256"}
                        or not isinstance(claim["command_id"], str) or not claim["command_id"]
                        or re.fullmatch(r"[a-f0-9]{64}", claim["sha256"]) is None
                        or claim["command_id"] in seen):
                    raise FiniteDemoMandateError("finite_baseline_claims_invalid")
                seen.add(claim["command_id"])
            return cls(Path(path).absolute(), payload["account_id"], record["sha256"], payload["mandate_id"],
                payload["source_reference"], issued, expires, ledger_path, payload["ledger_inception_hash"],
                payload["reviewed_commit"], tuple((s["command_id"], s["sha256"]) for s in baseline),
                canonical_json(normalized))
        except PilotMandateError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise FiniteDemoMandateError("finite_mandate_missing_or_invalid") from exc

    def validate_binding(self, *, ledger, now, state=None, reviewed_commit=None):
        from agents.fill_agent.pilot_ledger import PilotLedger
        if type(self) is not FiniteDemoMandate or not isinstance(ledger, PilotLedger):
            raise FiniteDemoMandateError("finite_typed_mandate_and_original_ledger_required")
        if self.load(self.path, expected_account_id=self.account_id) != self:
            raise FiniteDemoMandateError("finite_mandate_changed_after_load")
        moment = _timestamp(now)
        if moment < self.issued_at or moment >= self.expires_at:
            raise FiniteDemoMandateError("finite_mandate_outside_authorized_time")
        state = state if state is not None else ledger.snapshot()
        if (ledger.expected_account_id != self.account_id or state["account_id"] != self.account_id
                or ledger.path.resolve() != self.ledger_path
                or payload_hash(state["inception"]) != self.ledger_inception_hash):
            raise FiniteDemoMandateError("finite_original_ledger_inception_required")
        if reviewed_commit is not None and _commit(reviewed_commit) != self.reviewed_commit:
            raise FiniteDemoMandateError("finite_reviewed_commit_mismatch")
        present = {s["command_id"]: payload_hash({k: v for k, v in s.items()
                   if k not in {"reconciled", "journal_complete"}}) for s in state["submissions"]}
        if any(present.get(command_id) != digest for command_id, digest in self.baseline_claims):
            raise FiniteDemoMandateError("finite_original_claim_history_required")
        return state

    def validate(self, *, ledger, kill_switch, now, reviewed_commit=None):
        state = self.validate_binding(ledger=ledger, now=now, reviewed_commit=reviewed_commit)
        if state["unresolved"] or state["loss_stopped"]:
            raise FiniteDemoMandateError("finite_ledger_blocked_lookup_only")
        status = kill_switch.status() if isinstance(kill_switch, KillSwitch) else kill_switch
        if (not isinstance(status, KillSwitchStatus) or status.environment != Environment.DEMO
                or status.engaged is not False or status.integrity_valid is not True or not status.writes_allowed):
            raise FiniteDemoMandateError("finite_revoked_or_kill_switch_unverified")
        return status

    def validate_candidate(self, candidate):
        if type(candidate) is not FiniteOrderCandidate:
            raise FiniteDemoMandateError("finite_typed_order_candidate_required")
        _text(candidate.command_id, "command_id")
        _positive_int(candidate.instrument_id, "instrument_id")
        if (not isinstance(candidate.intent_hash, str)
                or re.fullmatch(r"[a-f0-9]{64}", candidate.intent_hash) is None):
            raise FiniteDemoMandateError("finite_exact_intent_hash_required")
        if (candidate.order_type not in self.scope["order_types"] or candidate.currency != "USD"
                or candidate.side != "buy" or _decimal(candidate.leverage, "leverage") != 1
                or candidate.settlement_type != "real"):
            raise FiniteDemoMandateError("finite_candidate_execution_scope_invalid")
        amount, costs = _decimal(candidate.amount, "amount"), _decimal(candidate.expected_costs, "expected_costs")
        from brokers.etoro.account_evidence import _exact_sum
        if amount <= 0 or costs < 0 or _exact_sum((amount, costs)) > Decimal(self.scope["per_order_cap"]):
            raise FiniteDemoMandateError("finite_candidate_order_cap_exceeded")
        matches = [i for i in self.scope["instruments"] if i["instrument_id"] == candidate.instrument_id]
        if len(matches) != 1:
            raise FiniteDemoMandateError("finite_candidate_instrument_not_authorized")
        return matches[0]
