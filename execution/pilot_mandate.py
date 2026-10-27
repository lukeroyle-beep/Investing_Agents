"""Durable, explicitly scoped standing authorization for one Demo API attempt.

The mandate is human authorization of a bounded workflow, never a claim that a
human approved a newly generated order preview. Dynamic approvals retain that
distinction and still expire with their fresh risk/account/quote evidence.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.pilot_account import PilotAccount
from execution.domain import (Approval, Environment, OrderIntent, OrderSide, OrderType,
    RiskDecision, RiskOutcome, SizingMethod, _aware_utc, canonical_json, immutable_pairs, payload_hash)
from execution.instruments import BrokerInstrumentMapping, Instrument
from execution.usage_reserve import PilotUsageAdmission, MAX_USAGE_AGE_SECONDS
from risk.approval import ApprovalAuthority, ApprovalError
from risk.kill_switch import KillSwitch, KillSwitchStatus
from risk.pretrade import RiskLimits


POLICY = "demo-pilot-v1"
MANDATE_SCHEMA = "standing-demo-pilot-mandate-v1"
AUTHORIZATION_KIND = "standing_mandate"
PILOT_USAGE_AMENDMENT_FILENAME = "pilot-usage-amendment.json"
_SCOPE = {
    "policy": POLICY, "environment": "demo", "broker": "etoro", "transport": "official_demo_api",
    "instrument_id": 1142, "symbol": "SBUX", "asset_type": "equity", "currency": "USD",
    "amount": "10", "side": "buy", "order_type": "market", "leverage": "1", "settlement_type": "real",
    "maximum_submissions": 1, "maximum_confirmed_pilot_fills": 1,
    "validity": "until_first_submission_or_revocation", "expiry_at": None,
    "guardrails": {"order_including_costs_usd": "100", "pilot_capital_and_commitments_usd": "500",
        "maximum_pilot_positions": 5, "minimum_available_cash_equity_fraction": "0.20",
        "maximum_opening_submissions_per_london_day": 1, "cumulative_pilot_loss_stop_usd": "25",
        "usage_work_floor_percent": "10", "usage_recovery_reserve_percent": "5",
        "preserve_existing_inception": True, "preserve_legacy_holdings": True,
        "no_addition_to_over_limit_issuer": True, "recovery": "lookup_only_no_resubmission"},
}
_REQUIRED_CHECKS = frozenset({"order_cap", "capital_cap", "position_cap", "cash_buffer",
                             "issuer_cap", "loss_stop", "reconciled", "daily_submission"})


class PilotMandateError(ApprovalError):
    """Standing authorization is absent, altered, exhausted or out of scope."""


def _identity(value):
    if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise PilotMandateError("mandate_exact_demo_account_identity_required")
    return value


def _timestamp(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _aware_utc(value, "mandate_time")
    except (ValueError, TypeError, AttributeError) as exc:
        raise PilotMandateError("mandate_timezone_aware_time_required") from exc


def _unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise PilotMandateError("mandate_duplicate_json_key")
        result[key] = value
    return result


def _read(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 65536:
            raise PilotMandateError("mandate_file_size_invalid")
        record = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(record, dict) or set(record) != {"mandate", "sha256"}:
            raise PilotMandateError("mandate_record_shape_invalid")
        payload = record["mandate"]
        if not isinstance(payload, dict) or payload_hash(payload) != record["sha256"]:
            raise PilotMandateError("mandate_integrity_failure")
        expected_keys = {"schema", "mandate_id", "account_id", "source_reference", "issued_at",
                         "ledger_path", "ledger_inception_hash", "scope"}
        if set(payload) != expected_keys or payload["schema"] != MANDATE_SCHEMA or payload["scope"] != _SCOPE:
            raise PilotMandateError("mandate_scope_or_schema_invalid")
        if (not isinstance(payload["source_reference"], str) or not payload["source_reference"].strip()
                or not isinstance(payload["ledger_path"], str) or not Path(payload["ledger_path"]).is_absolute()
                or re.fullmatch(r"[a-f0-9]{64}", payload["ledger_inception_hash"]) is None):
            raise PilotMandateError("mandate_authority_or_inception_binding_invalid")
        _identity(payload["account_id"])
        UUID(payload["mandate_id"])
        _timestamp(payload["issued_at"])
        return payload, record["sha256"]
    except PilotMandateError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PilotMandateError("mandate_missing_or_invalid") from exc


def _existing_ledger(ledger, account_id):
    if not isinstance(ledger, PilotLedger):
        raise PilotMandateError("mandate_existing_pilot_ledger_required")
    state = ledger.snapshot()
    inception = state["inception"]
    if (state["account_id"] != account_id or inception.get("account_id") != account_id
            or inception.get("policy") != POLICY):
        raise PilotMandateError("mandate_existing_demo_inception_mismatch")
    if (state["submissions"] or state["positions"] or state["position_count"] != 0
            or state["committed_capital"] != 0):
        raise PilotMandateError("mandate_one_attempt_exhausted_lookup_only")
    if state["unresolved"] or state["loss_stopped"] or state["pilot_pnl"] <= Decimal("-25"):
        raise PilotMandateError("mandate_pilot_ledger_blocked")
    return state


@dataclass(frozen=True, slots=True)
class StandingPilotMandate:
    path: Path
    account_id: str
    mandate_hash: str
    mandate_id: str
    source_reference: str
    issued_at: datetime
    ledger_path: Path
    ledger_inception_hash: str

    @classmethod
    def create(cls, path, *, account_id, source_reference, issued_at, ledger):
        """Create once from the human's standing authorization; never reset state."""
        account_id = _identity(account_id)
        moment = _timestamp(issued_at)
        if not isinstance(source_reference, str) or not source_reference.strip():
            raise PilotMandateError("mandate_human_source_reference_required")
        state = _existing_ledger(ledger, account_id)
        if moment < _timestamp(state["inception"]["inception"]):
            raise PilotMandateError("mandate_issue_precedes_existing_inception")
        payload = {"schema": MANDATE_SCHEMA, "mandate_id": str(uuid4()), "account_id": account_id,
            "source_reference": source_reference, "issued_at": moment,
            "ledger_path": str(ledger.path.resolve()), "ledger_inception_hash": payload_hash(state["inception"]),
            "scope": _SCOPE}
        record = {"mandate": payload, "sha256": payload_hash(payload)}
        path = Path(path).absolute()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise PilotMandateError("mandate_already_exists_never_overwrite") from exc
        # A failed write remains a fail-closed artifact; no automatic deletion
        # or recreation is allowed to manufacture renewed authorization.
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
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
        payload, digest = _read(path)
        if payload["account_id"] != _identity(expected_account_id):
            raise PilotMandateError("mandate_wrong_demo_account")
        return cls(Path(path).absolute(), payload["account_id"], digest, payload["mandate_id"],
                   payload["source_reference"], _timestamp(payload["issued_at"]),
                   Path(payload["ledger_path"]), payload["ledger_inception_hash"])

    def validate(self, *, ledger, kill_switch, now):
        """Re-read the immutable mandate and shared switch immediately before use."""
        loaded = self.load(self.path, expected_account_id=self.account_id)
        if self != loaded:
            raise PilotMandateError("mandate_changed_after_load")
        moment = _timestamp(now)
        if moment < self.issued_at:
            raise PilotMandateError("mandate_not_yet_issued")
        state = _existing_ledger(ledger, self.account_id)
        if ledger.path.resolve() != self.ledger_path or payload_hash(state["inception"]) != self.ledger_inception_hash:
            raise PilotMandateError("mandate_original_ledger_inception_required")
        status = kill_switch.status() if isinstance(kill_switch, KillSwitch) else kill_switch
        if (not isinstance(status, KillSwitchStatus) or status.environment != Environment.DEMO
                or status.engaged is not False or status.integrity_valid is not True
                or not status.writes_allowed):
            raise PilotMandateError("mandate_revoked_or_kill_switch_unverified")
        return status

    def issue_approval(self, *, intent, decision, account, instrument, mapping,
                       order_payload, ledger, kill_switch, limits, now, usage,
                       usage_amendment=None, approval_limits=None):
        """Issue a fresh dynamic approval under standing, not per-order, consent."""
        moment = _timestamp(now)
        status = self.validate(ledger=ledger, kill_switch=kill_switch, now=moment)
        floor = Decimal("10")
        if usage_amendment is not None:
            if type(usage_amendment) is not PilotUsageAmendment:
                raise PilotMandateError("typed_pilot_usage_amendment_required")
            usage_amendment.validate(mandate=self, ledger=ledger, now=moment)
            floor = usage_amendment.floor_percent
        if (type(usage) is not PilotUsageAdmission or usage.floor_percent < floor
                or usage.recovery_reserve_percent < Decimal("5")
                or usage.validate_for_submission(moment)):
            raise PilotMandateError("mandate_fresh_authorized_usage_floor_and_reserve_required")
        if (usage.floor_percent < Decimal("10")
                and usage.authorization_reference != usage_amendment.source_reference):
            raise PilotMandateError("mandate_usage_amendment_authorization_reference_mismatch")
        if not isinstance(account, PilotAccount) or account.account_id != self.account_id:
            raise PilotMandateError("mandate_fresh_same_demo_account_required")
        account.validate_fresh(moment)
        if (not isinstance(intent, OrderIntent) or intent.compute_hash() != intent.intent_hash
                or intent.environment != Environment.DEMO or intent.currency != "USD"
                or intent.side != OrderSide.BUY or intent.order_type != OrderType.MARKET
                or intent.sizing_method != SizingMethod.FIXED_NOTIONAL or intent.sizing_value != Decimal("10")
                or intent.target_leverage != 1 or intent.limit_price is not None):
            raise PilotMandateError("mandate_order_outside_scope")
        if (not isinstance(instrument, Instrument) or not isinstance(mapping, BrokerInstrumentMapping)
                or instrument.internal_instrument_id != intent.internal_instrument_id
                or mapping.internal_instrument_id != intent.internal_instrument_id
                or instrument.canonical_symbol != "SBUX" or instrument.asset_type != "equity"
                or instrument.currency != "USD" or mapping.broker != "etoro"
                or mapping.environment != Environment.DEMO or mapping.broker_instrument_id != 1142
                or mapping.exact_match_symbol != "SBUX"):
            raise PilotMandateError("mandate_exact_instrument_mapping_required")
        expected_payload = {"action": "open", "transaction": "buy", "instrumentId": 1142,
            "settlementType": "real", "orderType": "mkt", "leverage": 1,
            "amount": Decimal("10"), "orderCurrency": "usd"}
        if (not isinstance(order_payload, Mapping) or dict(order_payload) != expected_payload
                or type(order_payload.get("instrumentId")) is not int
                or type(order_payload.get("leverage")) is not int
                or type(order_payload.get("amount")) not in {Decimal, int}):
            raise PilotMandateError("mandate_exact_official_demo_payload_required")
        if not isinstance(limits, RiskLimits) or limits.environment != Environment.DEMO:
            raise PilotMandateError("mandate_demo_dynamic_limits_required")
        if (not isinstance(decision, RiskDecision) or decision.outcome != RiskOutcome.ACCEPTED
                or decision.intent_hash != intent.intent_hash or decision.account_snapshot_id != account.source_hash
                or dict(decision.computed_exposures).get("policy") != POLICY
                or len({check.name for check in decision.checks}) != len(decision.checks)
                or not _REQUIRED_CHECKS <= {check.name for check in decision.checks}
                or not all(check.passed is True for check in decision.checks)
                or not 0 <= (moment - decision.decided_at).total_seconds() <= min(30, limits.approval_max_age_seconds)
                or not 0 <= (moment - decision.quote_observed_at).total_seconds() <= min(30, limits.quote_max_age_seconds)):
            raise PilotMandateError("mandate_fresh_accepted_pilot_risk_required")
        expires_at = min(intent.expires_at, account.deadline,
            decision.decided_at + timedelta(seconds=limits.approval_max_age_seconds),
            decision.quote_observed_at + timedelta(seconds=min(30, limits.quote_max_age_seconds)),
            usage.observed_at + timedelta(seconds=MAX_USAGE_AGE_SECONDS))
        if expires_at <= moment:
            raise PilotMandateError("mandate_dynamic_evidence_expired")
        bound = {"risk_decision_id": str(decision.decision_id), "account_snapshot_id": decision.account_snapshot_id,
            "quote_id": decision.quote_id, "authorization_kind": AUTHORIZATION_KIND,
            "mandate_hash": self.mandate_hash, "mandate_id": self.mandate_id,
            "authorization_source_reference": self.source_reference, "account_id": self.account_id,
            "order_payload_hash": payload_hash(order_payload),
            "policy": POLICY, "broker_instrument_id": "1142", "symbol": "SBUX", "amount_usd": "10",
            "settlement_type": "real", "maximum_submissions": "1", "maximum_confirmed_pilot_fills": "1",
            "kill_switch_revision": str(status.revision), "usage_work_floor_percent": str(usage.floor_percent),
            "usage_recovery_reserve_percent": str(usage.recovery_reserve_percent),
            "usage_floor_authorization_reference": usage.authorization_reference,
            "usage_source_reference": usage.source_reference, "usage_source_sha256": usage.source_sha256,
            "usage_observed_at": usage.observed_at.isoformat(),
            "usage_amendment_hash": usage_amendment.amendment_hash if usage_amendment else "",
            "usage_amendment_source_reference": usage_amendment.source_reference if usage_amendment else ""}
        if approval_limits:
            if set(approval_limits) & set(bound):
                raise PilotMandateError("mandate_protected_approval_limits_cannot_be_overridden")
            bound.update({str(k): str(v) for k, v in approval_limits.items()})
        approval = Approval(approval_id=uuid4(), intent_hash=intent.intent_hash,
            approver="standing-mandate:" + self.mandate_id, environment=Environment.DEMO,
            limits=immutable_pairs(bound), issued_at=moment, expires_at=expires_at)
        ApprovalAuthority.verify(approval=approval, intent=intent, now=moment)
        return approval


_USAGE_AMENDMENT_SCHEMA = "standing-demo-pilot-usage-amendment-v1"
_USAGE_AMENDMENT_SCOPE = {
    "policy": POLICY, "change": "usage_work_floor_only",
    "original_work_floor_percent": "10", "work_floor_percent": "5",
    "recovery_reserve_percent": "5", "preserve_original_mandate": True,
    "preserve_existing_inception": True,
}


def _amendment_binding(mandate, ledger=None, *, require_unclaimed=False):
    if type(mandate) is not StandingPilotMandate:
        raise PilotMandateError("usage_amendment_original_standing_mandate_required")
    if StandingPilotMandate.load(mandate.path, expected_account_id=mandate.account_id) != mandate:
        raise PilotMandateError("usage_amendment_original_mandate_changed")
    if ledger is not None:
        if not isinstance(ledger, PilotLedger):
            raise PilotMandateError("usage_amendment_existing_pilot_ledger_required")
        state = (_existing_ledger(ledger, mandate.account_id) if require_unclaimed else ledger.snapshot())
        if (state["account_id"] != mandate.account_id
                or state["inception"].get("account_id") != mandate.account_id
                or state["inception"].get("policy") != POLICY
                or ledger.path.resolve() != mandate.ledger_path
                or payload_hash(state["inception"]) != mandate.ledger_inception_hash):
            raise PilotMandateError("usage_amendment_original_ledger_inception_required")


@dataclass(frozen=True, slots=True)
class PilotUsageAmendment:
    """Additive human authorization of a five-point floor; never a reset."""

    path: Path
    amendment_hash: str
    amendment_id: str
    mandate_hash: str
    account_id: str
    ledger_path: Path
    ledger_inception_hash: str
    source_reference: str
    issued_at: datetime

    @property
    def floor_percent(self):
        return Decimal("5")

    @property
    def recovery_reserve_percent(self):
        return Decimal("5")

    @classmethod
    def create(cls, path, *, mandate, ledger, source_reference, issued_at):
        _amendment_binding(mandate, ledger, require_unclaimed=True)
        moment = _timestamp(issued_at)
        if not isinstance(source_reference, str) or not source_reference.strip():
            raise PilotMandateError("usage_amendment_current_human_source_reference_required")
        if moment < mandate.issued_at:
            raise PilotMandateError("usage_amendment_cannot_precede_original_mandate")
        payload = {"schema": _USAGE_AMENDMENT_SCHEMA, "amendment_id": str(uuid4()),
            "mandate_hash": mandate.mandate_hash, "account_id": mandate.account_id,
            "ledger_path": str(mandate.ledger_path), "ledger_inception_hash": mandate.ledger_inception_hash,
            "source_reference": source_reference, "issued_at": moment, "scope": _USAGE_AMENDMENT_SCOPE}
        record = {"amendment": payload, "sha256": payload_hash(payload)}
        path = Path(path).absolute()
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError as exc:
            raise PilotMandateError("usage_amendment_already_exists_never_overwrite") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls.load(path, mandate=mandate)

    @classmethod
    def load(cls, path, *, mandate):
        _amendment_binding(mandate)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                raw = handle.read(65537)
            if len(raw) > 65536:
                raise PilotMandateError("usage_amendment_file_size_invalid")
            record = json.loads(raw, object_pairs_hook=_unique_pairs)
            if not isinstance(record, dict) or set(record) != {"amendment", "sha256"}:
                raise PilotMandateError("usage_amendment_record_shape_invalid")
            payload = record["amendment"]
            if not isinstance(payload, dict) or payload_hash(payload) != record["sha256"]:
                raise PilotMandateError("usage_amendment_integrity_failure")
            keys = {"schema", "amendment_id", "mandate_hash", "account_id", "ledger_path",
                    "ledger_inception_hash", "source_reference", "issued_at", "scope"}
            if (set(payload) != keys or payload["schema"] != _USAGE_AMENDMENT_SCHEMA
                    or payload["scope"] != _USAGE_AMENDMENT_SCOPE):
                raise PilotMandateError("usage_amendment_scope_or_schema_invalid")
            if (payload["mandate_hash"] != mandate.mandate_hash or payload["account_id"] != mandate.account_id
                    or payload["ledger_path"] != str(mandate.ledger_path)
                    or payload["ledger_inception_hash"] != mandate.ledger_inception_hash):
                raise PilotMandateError("usage_amendment_original_mandate_binding_mismatch")
            if not isinstance(payload["source_reference"], str) or not payload["source_reference"].strip():
                raise PilotMandateError("usage_amendment_current_human_source_reference_required")
            UUID(payload["amendment_id"])
            moment = _timestamp(payload["issued_at"])
            if moment < mandate.issued_at:
                raise PilotMandateError("usage_amendment_cannot_precede_original_mandate")
            return cls(Path(path).absolute(), record["sha256"], payload["amendment_id"],
                payload["mandate_hash"], payload["account_id"], Path(payload["ledger_path"]),
                payload["ledger_inception_hash"], payload["source_reference"], moment)
        except PilotMandateError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise PilotMandateError("usage_amendment_missing_or_invalid") from exc

    def validate(self, *, mandate, ledger, now):
        # Quota authorization continues through lookup recovery. Opening a new
        # order separately requires StandingPilotMandate.validate's empty ledger.
        loaded = self.load(self.path, mandate=mandate)
        if loaded != self:
            raise PilotMandateError("usage_amendment_changed_after_load")
        if _timestamp(now) < self.issued_at:
            raise PilotMandateError("usage_amendment_not_yet_issued")
        _amendment_binding(mandate, ledger)
