"""Synthetic standing-mandate authorization; no network or credential access."""
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger
from execution.domain import (Approval, Environment, OrderIntent, RiskCheck, RiskDecision,
    RiskOutcome, canonical_json, payload_hash)
from execution.instruments import BrokerInstrumentMapping, Instrument
from execution.pilot_mandate import PilotMandateError, StandingPilotMandate
from risk.approval import ApprovalAuthority, ApprovalError
from risk.kill_switch import KillSwitch, KillSwitchStatus, RESET_ACKNOWLEDGEMENT
from risk.pretrade import RiskLimits
from shared.paths import RISK_CONFIG_PATH
from tests.test_pilot_ledger import Account, NOW

D = Decimal
CHECKS = ("order_cap", "capital_cap", "position_cap", "cash_buffer", "issuer_cap",
          "loss_stop", "reconciled", "daily_submission")


def order(instrument, **overrides):
    values = dict(strategy_id="synthetic-standing-pilot", run_id="synthetic-run",
        internal_instrument_id=instrument.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=NOW + timedelta(minutes=5), target_leverage="1")
    values.update(overrides)
    return OrderIntent.create(**values)


@pytest.fixture
def f(tmp_path):
    account = Account()
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", account, [{"kind": "synthetic-legacy"}], NOW)
    instrument = Instrument.create(canonical_symbol="SBUX", exchange="NASDAQ", asset_type="equity",
        currency="USD", sector="consumer")
    mapping = BrokerInstrumentMapping.from_resolution(instrument=instrument, broker="etoro", environment="demo",
        broker_instrument_id=1142, exact_match_symbol="SBUX", metadata={"source": "synthetic"}, resolved_at=NOW)
    intent = order(instrument)
    decision = RiskDecision(uuid4(), intent.intent_hash, account.source_hash, "synthetic-quote", NOW,
        (("policy", "demo-pilot-v1"),), tuple(RiskCheck(name, True, "true", "true") for name in CHECKS),
        RiskOutcome.ACCEPTED, (), NOW)
    switch = KillSwitch(state_path=tmp_path / "switch.json", audit_path=tmp_path / "switch-audit.jsonl")
    switch.initialize_engaged(operator_id="synthetic-human", reason="fixture setup", now=NOW)
    switch.reset(operator_id="synthetic-human", reason="explicit fixture authorization",
                 acknowledgement=RESET_ACKNOWLEDGEMENT, now=NOW)
    mandate = StandingPilotMandate.create(tmp_path / "standing-mandate.json", account_id=account.account_id,
        source_reference="Synthetic current-chat human standing authorization", issued_at=NOW, ledger=ledger)
    return SimpleNamespace(account=account, ledger=ledger, instrument=instrument, mapping=mapping,
        intent=intent, decision=decision, switch=switch, mandate=mandate,
        limits=RiskLimits.load(RISK_CONFIG_PATH), path=tmp_path)


def approve(f, **overrides):
    values = dict(intent=f.intent, decision=f.decision, account=f.account, instrument=f.instrument,
        mapping=f.mapping, ledger=f.ledger, kill_switch=f.switch, limits=f.limits, now=NOW,
        order_payload={"action":"open", "transaction":"buy", "instrumentId":1142,
            "settlementType":"real", "orderType":"mkt", "leverage":1, "amount":D(10), "orderCurrency":"usd"})
    values.update(overrides)
    return f.mandate.issue_approval(**values)


def test_create_is_exclusive_and_preserves_existing_inception(f):
    original = f.mandate.path.read_bytes()
    inception = f.ledger.snapshot()["inception"]
    with pytest.raises(PilotMandateError, match="never_overwrite"):
        StandingPilotMandate.create(f.mandate.path, account_id=f.account.account_id,
            source_reference="Replacement must fail", issued_at=NOW, ledger=f.ledger)
    assert f.mandate.path.read_bytes() == original
    assert f.ledger.snapshot()["inception"] == inception
    assert f.mandate.path.stat().st_mode & 0o777 == 0o600
    assert StandingPilotMandate.load(f.mandate.path, expected_account_id=f.account.account_id) == f.mandate
    payload = json.loads(original)["mandate"]
    assert payload["scope"]["expiry_at"] is None
    assert payload["scope"]["validity"] == "until_first_submission_or_revocation"
    assert payload["scope"]["instrument_id"] == 1142
    assert payload["scope"]["amount"] == "10"
    assert payload["scope"]["guardrails"]["usage_work_floor_percent"] == "10"
    assert payload["scope"]["guardrails"]["usage_recovery_reserve_percent"] == "5"


def test_dynamic_approval_identifies_standing_not_fake_per_order_human(f, monkeypatch):
    def forbidden_issue(*args, **kwargs):
        pytest.fail("Standing mandate must not fabricate human_confirmed=True")
    monkeypatch.setattr(ApprovalAuthority, "issue", forbidden_issue)
    approval = approve(f, approval_limits={"api_payload_hash": "b" * 64})
    assert isinstance(approval, Approval)
    assert approval.approver == "standing-mandate:" + f.mandate.mandate_id
    fields = dict(approval.limits)
    assert fields["authorization_kind"] == "standing_mandate"
    assert fields["mandate_hash"] == f.mandate.mandate_hash
    assert fields["authorization_source_reference"] == f.mandate.source_reference
    assert fields["risk_decision_id"] == str(f.decision.decision_id)
    assert fields["account_snapshot_id"] == f.account.source_hash
    assert fields["quote_id"] == f.decision.quote_id
    assert fields["api_payload_hash"] == "b" * 64
    assert fields["maximum_submissions"] == fields["maximum_confirmed_pilot_fills"] == "1"
    ApprovalAuthority.verify(approval=approval, intent=f.intent, now=NOW)
    assert approval.expires_at == f.account.deadline
    with pytest.raises(ApprovalError, match="stale"):
        ApprovalAuthority.verify(approval=approval, intent=f.intent, now=approval.expires_at)


@pytest.mark.parametrize("field", ["authorization_kind", "mandate_hash", "risk_decision_id", "account_snapshot_id", "settlement_type"])
def test_callers_cannot_override_protected_standing_authorization(f, field):
    with pytest.raises(PilotMandateError, match="cannot_be_overridden"):
        approve(f, approval_limits={field: "forged"})


def test_integrity_is_rechecked_on_every_use(f):
    raw = json.loads(f.mandate.path.read_text())
    raw["mandate"]["source_reference"] = "Changed after load"
    f.mandate.path.write_text(json.dumps(raw))
    with pytest.raises(PilotMandateError, match="integrity"):
        approve(f)
    raw["sha256"] = payload_hash(raw["mandate"])
    f.mandate.path.write_text(json.dumps(raw))
    with pytest.raises(PilotMandateError, match="changed_after_load"):
        approve(f)


@pytest.mark.parametrize("field,value", [("amount", "11"), ("instrument_id", 1001),
    ("environment", "real"), ("maximum_submissions", 2), ("settlement_type", "cfd")])
def test_even_rehashed_artifact_cannot_expand_fixed_scope(f, field, value):
    raw = json.loads(f.mandate.path.read_text())
    raw["mandate"]["scope"][field] = value
    raw["sha256"] = payload_hash(raw["mandate"])
    f.mandate.path.write_text(json.dumps(raw))
    with pytest.raises(PilotMandateError, match="scope_or_schema"):
        StandingPilotMandate.load(f.mandate.path, expected_account_id=f.account.account_id)


def test_missing_duplicate_or_wrong_account_mandate_fails_closed(f):
    with pytest.raises(PilotMandateError, match="wrong_demo_account"):
        StandingPilotMandate.load(f.mandate.path, expected_account_id="999")
    with pytest.raises(PilotMandateError, match="missing_or_invalid"):
        StandingPilotMandate.load(f.path / "missing.json", expected_account_id=f.account.account_id)
    f.mandate.path.write_text('{"mandate":{},"mandate":{},"sha256":"x"}')
    with pytest.raises(PilotMandateError, match="duplicate_json_key"):
        StandingPilotMandate.load(f.mandate.path, expected_account_id=f.account.account_id)


def test_existing_ledger_identity_cannot_be_replaced_to_regain_authorization(f):
    second = PilotLedger.create(f.path / "replacement.sqlite3", f.account, [], NOW)
    with pytest.raises(PilotMandateError, match="original_ledger_inception"):
        approve(f, ledger=second)
    with pytest.raises(PilotMandateError, match="existing_pilot_ledger_required"):
        approve(f, ledger=SimpleNamespace(snapshot=lambda: f.ledger.snapshot()))


def test_first_claim_exhausts_mandate_across_restart_even_after_rejection(f):
    f.ledger.reserve_and_claim("one-off-attempt", D("10"), D("0.01"), f.account, NOW, instrument_id=1142)
    f.ledger.record_outcome("one-off-attempt", {"status": "rejected"})
    resumed = StandingPilotMandate.load(f.mandate.path, expected_account_id=f.account.account_id)
    with pytest.raises(PilotMandateError, match="one_attempt_exhausted_lookup_only"):
        resumed.validate(ledger=PilotLedger(f.ledger.path, f.account.account_id), kill_switch=f.switch,
                         now=NOW + timedelta(days=1))
    with pytest.raises(PilotMandateError, match="one_attempt_exhausted_lookup_only"):
        StandingPilotMandate.create(f.path / "another-mandate.json", account_id=f.account.account_id,
            source_reference="same authorization is not permission to retry", issued_at=NOW, ledger=f.ledger)
    assert len(f.ledger.snapshot()["submissions"]) == 1


def test_shared_kill_switch_revocation_is_read_fresh_and_never_reset(f):
    assert f.mandate.validate(ledger=f.ledger, kill_switch=f.switch, now=NOW).writes_allowed
    f.switch.engage(operator_id="synthetic-human", reason="revoke standing authorization", now=NOW)
    state_before = f.switch.state_path.read_bytes()
    with pytest.raises(PilotMandateError, match="revoked_or_kill_switch"):
        approve(f)
    assert f.switch.state_path.read_bytes() == state_before
    assert f.switch.status().engaged


@pytest.mark.parametrize("status", [None,
    KillSwitchStatus(False, False, Environment.DEMO, 1, "invalid", NOW),
    KillSwitchStatus(False, True, Environment.REAL, 1, "wrong account type", NOW)])
def test_missing_invalid_or_real_kill_switch_cannot_authorize(f, status):
    with pytest.raises(PilotMandateError, match="kill_switch"):
        approve(f, kill_switch=status)


@pytest.mark.parametrize("overrides", [
    {"environment": "real"}, {"currency": "GBP"}, {"side": "sell"},
    {"target_leverage": "2"}, {"sizing_value": "10.01"}, {"sizing_method": "units"},
    {"order_type": "limit", "limit_price": "100"}])
def test_order_scope_cannot_drift_under_standing_authorization(f, overrides):
    with pytest.raises(PilotMandateError, match="order_outside_scope"):
        approve(f, intent=order(f.instrument, **overrides))


@pytest.mark.parametrize("target,overrides", [
    ("instrument", {"canonical_symbol": "AAPL"}), ("instrument", {"asset_type": "etf"}),
    ("instrument", {"currency": "GBP"}), ("mapping", {"broker_instrument_id": 1001}),
    ("mapping", {"environment": Environment.REAL}), ("mapping", {"exact_match_symbol": "AAPL"}),
    ("mapping", {"internal_instrument_id": uuid4()})])
def test_instrument_scope_binds_internal_and_broker_identity(f, target, overrides):
    with pytest.raises(PilotMandateError, match="exact_instrument_mapping"):
        approve(f, **{target: replace(getattr(f, target), **overrides)})


def test_fresh_same_account_and_pilot_accepted_checks_are_required(f):
    with pytest.raises(PilotMandateError, match="same_demo_account"):
        approve(f, account=Account(account_id="999"))
    with pytest.raises(PilotMandateError, match="fresh_accepted_pilot_risk"):
        approve(f, decision=replace(f.decision, checks=()))
    with pytest.raises(PilotMandateError, match="fresh_accepted_pilot_risk"):
        approve(f, decision=replace(f.decision, computed_exposures=(("policy", "strict"),)))
    with pytest.raises(PilotMandateError, match="fresh_accepted_pilot_risk"):
        approve(f, decision=replace(f.decision, account_snapshot_id="unrelated"))
    with pytest.raises(PilotMandateError, match="fresh_accepted_pilot_risk"):
        approve(f, decision=replace(f.decision, quote_observed_at=NOW-timedelta(seconds=31)))
    failed = (RiskCheck("order_cap", False, "false", "true"), *f.decision.checks[1:])
    with pytest.raises(PilotMandateError, match="fresh_accepted_pilot_risk"):
        approve(f, decision=replace(f.decision, checks=failed, outcome=RiskOutcome.REJECTED))
    with pytest.raises(ValueError, match="stale"):
        approve(f, now=NOW + timedelta(seconds=61))


def test_future_mandate_and_missing_human_reference_are_not_authority(f):
    with pytest.raises(PilotMandateError, match="not_yet_issued"):
        approve(f, now=NOW-timedelta(seconds=1))
    with pytest.raises(PilotMandateError, match="human_source_reference"):
        StandingPilotMandate.create(f.path / "blank.json", account_id=f.account.account_id,
            source_reference=" ", issued_at=NOW, ledger=f.ledger)
    assert not (f.path / "blank.json").exists()


@pytest.mark.parametrize("change", [{"settlementType":"cfd"}, {"amount":10.0},
    {"instrumentId":1142.0}, {"leverage":True}, {"extra":"unsupported"}, {"orderCurrency":"USD"}])
def test_standing_approval_requires_exact_official_demo_payload(f, change):
    payload = {"action":"open", "transaction":"buy", "instrumentId":1142,
        "settlementType":"real", "orderType":"mkt", "leverage":1, "amount":D(10), "orderCurrency":"usd"}
    payload.update(change)
    with pytest.raises(PilotMandateError, match="exact_official_demo_payload"):
        approve(f, order_payload=payload)
