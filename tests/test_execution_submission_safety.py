"""Offline adversarial checks for the durable human-approved send boundary."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from brokers.base import BrokerReconciliationResult, BrokerSubmissionResult
from execution.coordinator import ExecutionConfig, ExecutionCoordinator, ExecutionDisabledError
from execution.domain import (
    BrokerCommand, CommandAttempt, CommandState, DomainValidationError, Environment,
    OrderIntent, RiskCheck, RiskDecision,
)
from execution.instruments import Instrument, InstrumentRegistry
from execution.store import ExecutionStore, ExecutionStoreError, SubmissionReplayError
from risk.approval import ApprovalAuthority
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import RiskLimits
from risk.submission_gate import GovernanceWritePolicy, SubmissionGate
from shared.paths import RISK_CONFIG_PATH


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)


class OfflineAdapter:
    broker_name = "fake"
    environment = Environment.DEMO
    read_only = False

    def __init__(self, *, failure=None, ambiguous=False, wrong_request=False):
        self.failure, self.ambiguous, self.wrong_request = failure, ambiguous, wrong_request
        self.sends = 0
        self.lookups = 0
        self.lock = Lock()

    def submit_order(self, intent, command):
        with self.lock:
            self.sends += 1
        if self.failure:
            raise self.failure
        return BrokerSubmissionResult(
            request_id=uuid4() if self.wrong_request else command.logical_request_id,
            accepted_for_processing=True, broker_order_id="offline-1",
            broker_reference_id=str(command.logical_request_id), raw_status="queued",
            ambiguous=self.ambiguous,
        )

    def reconcile_command(self, command):
        self.lookups += 1
        return BrokerReconciliationResult(
            command_id=command.command_id, reconciled=True,
            lifecycle_state="rejected", broker_order_id="offline-1",
            broker_position_id=None, details=(("source", "offline_fixture"),),
        )


def _workflow(tmp_path, *, state="submission_pending"):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(Instrument.create(
        canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity", currency="USD"
    ))
    config = ExecutionConfig(
        schema_version="1.0", intent_import_enabled=True, broker_writes_enabled=True,
        adapter="fake", environment=Environment.DEMO, require_internal_instrument_id=True,
        allowed_asset_types=("equity", "etf"), allowed_sides=("buy",),
        allowed_leverage=Decimal("1"),
    )
    coordinator = ExecutionCoordinator(store=store, registry=registry, config=config)
    intent = coordinator.prepare_intent(OrderIntent.create(
        strategy_id="offline_safety_test", run_id="RUN_SAFETY",
        internal_instrument_id=instrument.internal_instrument_id, environment="demo",
        side="buy", order_type="market", sizing_method="fixed_notional",
        sizing_value="100", currency="USD", expires_at=NOW + timedelta(minutes=5),
    ))
    # These are domain/atomicity tests, not a substitution for the independently
    # tested pretrade evaluator. No external account, quote or transport is used.
    decision = RiskDecision(
        decision_id=uuid4(), intent_hash=intent.intent_hash,
        account_snapshot_id="offline-account", quote_id="offline-quote",
        quote_observed_at=NOW, computed_exposures=(),
        checks=(RiskCheck("offline_fixture", True, "100", "1000"),),
        outcome="accepted", reasons=(), decided_at=NOW,
    )
    command = coordinator.record_risk_decision(
        intent=intent, decision=decision, broker_payload={"amount": "100"}, broker="fake"
    )
    limits = RiskLimits.load(RISK_CONFIG_PATH)
    approval = ApprovalAuthority(limits).issue(
        intent=intent, decision=decision, approver="offline-human@example.test",
        now=NOW, human_confirmed=True,
    )
    flow = SimpleNamespace(
        store=store, registry=registry, config=config, coordinator=coordinator,
        intent=intent, decision=decision, command=command, approval=approval,
        gate=SubmissionGate(limits),
        kill=KillSwitchStatus(False, True, Environment.DEMO, 1, "offline fixture", NOW),
        governance=GovernanceWritePolicy("demo_manual", True, True, True),
    )
    if state in {"approved", "submission_pending"}:
        flow.command = coordinator.record_approval(command=command, approval=approval, now=NOW)
    if state == "submission_pending":
        flow.command = _authorize(flow)
    return flow


def _authorize(flow, **overrides):
    kwargs = dict(
        command=flow.command, intent=flow.intent, decision=flow.decision,
        approval=flow.approval, kill_switch=flow.kill, regular_session_open=True,
        submission_gate=flow.gate, governance=flow.governance, now=NOW,
    )
    kwargs.update(overrides)
    return flow.coordinator.mark_submission_pending(**kwargs)


def _send(flow, adapter, **overrides):
    kwargs = dict(command=flow.command, intent=flow.intent, adapter=adapter, now=NOW)
    kwargs.update(overrides)
    return flow.coordinator.submit_pending_command(**kwargs)


def test_typed_bindings_survive_restart_and_stale_snapshot_cannot_resend(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    acknowledged = _send(flow, adapter)
    assert acknowledged.state == CommandState.ACKNOWLEDGED
    restarted = ExecutionStore(flow.store.path)
    assert restarted.get_risk_decision(flow.decision.decision_id) == flow.decision
    assert restarted.get_approval(flow.approval.approval_id) == flow.approval
    assert restarted.get_command_risk_decision(flow.command.command_id) == flow.decision
    assert restarted.get_command_approval(flow.command.command_id) == flow.approval
    flow.coordinator = ExecutionCoordinator(store=restarted, registry=flow.registry, config=flow.config)
    with pytest.raises(DomainValidationError, match="stale"):
        _send(flow, adapter)
    assert adapter.sends == 1
    assert len(restarted.get_command(flow.command.command_id).attempt_history) == 1


def test_concurrent_callers_can_claim_only_one_send(tmp_path):
    flow, adapter, barrier = _workflow(tmp_path), OfflineAdapter(), Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        try:
            return _send(flow, adapter)
        except (DomainValidationError, ExecutionStoreError) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))
    assert sum(isinstance(result, BrokerCommand) for result in results) == 1
    assert adapter.sends == 1
    assert len(flow.store.get_command(flow.command.command_id).attempt_history) == 1


def test_crash_after_durable_claim_requires_lookup_not_resend(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter(failure=SystemExit("simulated crash"))
    with pytest.raises(SystemExit):
        _send(flow, adapter)
    store = ExecutionStore(flow.store.path)
    flow.coordinator = ExecutionCoordinator(store=store, registry=flow.registry, config=flow.config)
    flow.command = store.get_command(flow.command.command_id)
    assert flow.command.state == CommandState.SUBMISSION_PENDING
    with pytest.raises(SubmissionReplayError, match="lookup"):
        _send(flow, adapter)
    recovery = OfflineAdapter()
    reconciled, _ = flow.coordinator.reconcile_command(command=flow.command, adapter=recovery)
    assert reconciled.state == CommandState.RECONCILED
    assert adapter.sends == 1
    assert recovery.sends == 0 and recovery.lookups == 1


def test_claim_committed_before_transport_even_if_process_never_sends(tmp_path):
    flow = _workflow(tmp_path)
    claimed = flow.store.claim_submission(flow.command, flow.intent, CommandAttempt(
        1, flow.command.logical_request_id, NOW, "send_started"
    ))
    flow.command = ExecutionStore(flow.store.path).get_command(claimed.command_id)
    adapter = OfflineAdapter()
    with pytest.raises(SubmissionReplayError):
        _send(flow, adapter)
    assert adapter.sends == 0


@pytest.mark.parametrize("change", [
    {"logical_request_id": uuid4()}, {"payload_hash": "f" * 64},
    {"intent_hash": "e" * 64},
])
def test_modified_command_snapshot_never_reaches_transport(tmp_path, change):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    with pytest.raises(DomainValidationError, match="stale|persisted"):
        _send(flow, adapter, command=replace(flow.command, **change))
    assert adapter.sends == 0
    assert not flow.store.get_command(flow.command.command_id).attempt_history


def test_different_intent_id_with_same_hash_is_not_persisted_authorization(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    other = replace(flow.intent, intent_id=uuid4())
    assert other.intent_hash == flow.intent.intent_hash
    with pytest.raises(DomainValidationError, match="persisted intent"):
        _send(flow, adapter, intent=other)
    assert adapter.sends == 0


def test_manually_transitioned_legacy_command_cannot_bypass_authorization(tmp_path):
    flow, adapter = _workflow(tmp_path, state="awaiting_approval"), OfflineAdapter()
    manual = BrokerCommand.create(
        intent_hash=flow.intent.intent_hash, operation="submit_order", broker="fake",
        environment="demo", broker_payload={"amount": "100"},
    )
    flow.store.save_command(manual)
    for state in ("awaiting_approval", "approved", "submission_pending"):
        manual = flow.store.transition_command(manual.command_id, state)
    with pytest.raises(DomainValidationError, match="persisted risk"):
        _send(flow, adapter, command=manual)
    assert adapter.sends == 0


@pytest.mark.parametrize("key", ["risk_decision_id", "account_snapshot_id", "quote_id"])
def test_approval_must_bind_all_risk_evidence(tmp_path, key):
    flow = _workflow(tmp_path, state="awaiting_approval")
    limits = dict(flow.approval.limits)
    limits[key] = str(uuid4())
    forged = replace(flow.approval, limits=tuple(limits.items()))
    with pytest.raises(DomainValidationError, match="bind"):
        flow.coordinator.record_approval(command=flow.command, approval=forged, now=NOW)
    assert flow.store.get_approval(forged.approval_id) is None
    assert flow.store.get_command(flow.command.command_id).state == CommandState.AWAITING_APPROVAL


@pytest.mark.parametrize("kind", ["decision", "approval"])
def test_same_record_id_cannot_substitute_changed_evidence(tmp_path, kind):
    flow = _workflow(tmp_path, state="approved")
    replacement = (replace(flow.decision, computed_exposures=(("forged", "1"),))
                   if kind == "decision" else replace(flow.approval, approver="another-human"))
    with pytest.raises(DomainValidationError, match="evidence|authorization"):
        _authorize(flow, **{kind: replacement})
    assert flow.store.get_command(flow.command.command_id).state == CommandState.APPROVED
    # Failed authorization has not consumed the original approval.
    assert _authorize(flow).state == CommandState.SUBMISSION_PENDING


def test_approval_consumption_and_pending_transition_roll_back_together(tmp_path, monkeypatch):
    flow = _workflow(tmp_path, state="approved")
    original = flow.store._update_command

    def fail_transition(*args, **kwargs):
        raise RuntimeError("simulated state write failure")

    monkeypatch.setattr(flow.store, "_update_command", fail_transition)
    with pytest.raises(RuntimeError, match="simulated"):
        _authorize(flow)
    assert flow.store.get_command(flow.command.command_id).state == CommandState.APPROVED
    with sqlite3.connect(flow.store.path) as connection:
        assert connection.execute("SELECT consumed_at FROM approvals").fetchone()[0] is None
        assert connection.execute("SELECT authorized_at FROM command_authorizations").fetchone()[0] is None
    monkeypatch.setattr(flow.store, "_update_command", original)
    assert _authorize(flow).state == CommandState.SUBMISSION_PENDING


def test_approval_persistence_and_approved_transition_roll_back_together(tmp_path, monkeypatch):
    flow = _workflow(tmp_path, state="awaiting_approval")
    original = flow.store._update_command
    monkeypatch.setattr(flow.store, "_update_command", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failure")))
    with pytest.raises(RuntimeError):
        flow.coordinator.record_approval(command=flow.command, approval=flow.approval, now=NOW)
    assert flow.store.get_approval(flow.approval.approval_id) is None
    assert flow.store.get_command_approval(flow.command.command_id) is None
    monkeypatch.setattr(flow.store, "_update_command", original)
    assert flow.coordinator.record_approval(command=flow.command, approval=flow.approval, now=NOW).state == CommandState.APPROVED


def test_failed_claim_transaction_neither_sends_nor_leaves_partial_claim(tmp_path, monkeypatch):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    original = flow.store._update_command

    def fail_attempt_update(*args, **kwargs):
        raise RuntimeError("simulated attempt write failure")

    monkeypatch.setattr(flow.store, "_update_command", fail_attempt_update)
    with pytest.raises(RuntimeError, match="attempt write"):
        _send(flow, adapter)
    assert adapter.sends == 0
    assert not flow.store.get_command(flow.command.command_id).attempt_history
    with sqlite3.connect(flow.store.path) as connection:
        assert connection.execute("SELECT submission_claimed_at FROM command_authorizations").fetchone()[0] is None
    monkeypatch.setattr(flow.store, "_update_command", original)
    assert _send(flow, adapter).state == CommandState.ACKNOWLEDGED
    assert adapter.sends == 1


def test_stale_approval_and_pending_transitions_do_not_persist_extra_evidence(tmp_path):
    flow = _workflow(tmp_path, state="awaiting_approval")
    awaiting = flow.command
    flow.command = flow.coordinator.record_approval(command=awaiting, approval=flow.approval, now=NOW)
    another = replace(flow.approval, approval_id=uuid4())
    with pytest.raises(DomainValidationError, match="stale"):
        flow.coordinator.record_approval(command=awaiting, approval=another, now=NOW)
    assert flow.store.get_approval(another.approval_id) is None
    assert _authorize(flow).state == CommandState.SUBMISSION_PENDING
    with pytest.raises(DomainValidationError, match="stale"):
        _authorize(flow)


def test_new_request_id_cannot_work_around_same_immutable_intent(tmp_path):
    flow = _workflow(tmp_path)
    new_decision = replace(flow.decision, decision_id=uuid4())
    with pytest.raises(SubmissionReplayError, match="intent already"):
        flow.coordinator.record_risk_decision(
            intent=flow.intent, decision=new_decision, broker_payload={"amount": "100"}, broker="fake"
        )
    assert flow.store.get_risk_decision(new_decision.decision_id) is None
    assert len(flow.store.list_commands()) == 1


def test_superseded_risk_blocks_already_authorized_send(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    revoked = replace(flow.decision, decision_id=uuid4(), outcome="rejected",
                      checks=(RiskCheck("new_check", False, "bad", "good"),), reasons=("new_check",))
    flow.store.save_risk_decision(revoked)
    with pytest.raises(DomainValidationError, match="superseded"):
        _send(flow, adapter)
    assert adapter.sends == 0


def test_risk_freshness_is_rechecked_at_actual_send_not_only_pending(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    with pytest.raises(DomainValidationError, match="stale"):
        _send(flow, adapter, now=NOW + timedelta(seconds=16))
    assert adapter.sends == 0
    assert not flow.store.get_command(flow.command.command_id).attempt_history


@pytest.mark.parametrize("attempt_change", [
    {"request_id": uuid4()}, {"attempt_number": 2}, {"outcome": "retry"},
])
def test_attempt_identity_and_number_cannot_be_rewritten(tmp_path, attempt_change):
    flow = _workflow(tmp_path)
    attempt = replace(CommandAttempt(1, flow.command.logical_request_id, NOW, "send_started"), **attempt_change)
    with pytest.raises(DomainValidationError, match="original request"):
        flow.store.record_attempt(flow.command.command_id, attempt)
    assert not flow.store.get_command(flow.command.command_id).attempt_history


@pytest.mark.parametrize("kwargs", [{"ambiguous": True}, {"wrong_request": True}])
def test_ambiguous_or_mismatched_response_requires_lookup(tmp_path, kwargs):
    flow, adapter = _workflow(tmp_path), OfflineAdapter(**kwargs)
    unknown = _send(flow, adapter)
    assert unknown.state == CommandState.UNKNOWN
    with pytest.raises(DomainValidationError):
        _send(flow, adapter)
    assert adapter.sends == 1


def test_transport_exception_is_ambiguous_and_cannot_be_retried(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter(failure=TimeoutError("unknown"))
    with pytest.raises(TimeoutError):
        _send(flow, adapter)
    assert flow.store.get_command(flow.command.command_id).state == CommandState.UNKNOWN
    with pytest.raises(DomainValidationError):
        _send(flow, adapter)
    assert adapter.sends == 1


@pytest.mark.parametrize("adapter_change", [
    {"broker_name": "other"}, {"environment": Environment.REAL}, {"read_only": True},
])
def test_wrong_adapter_cannot_claim_or_send(tmp_path, adapter_change):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    for key, value in adapter_change.items():
        setattr(adapter, key, value)
    with pytest.raises(ExecutionDisabledError):
        _send(flow, adapter)
    assert adapter.sends == 0
    assert not flow.store.get_command(flow.command.command_id).attempt_history


def test_write_disable_after_pending_is_still_enforced(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    flow.coordinator.config = replace(flow.config, broker_writes_enabled=False)
    with pytest.raises(ExecutionDisabledError, match="disabled"):
        _send(flow, adapter)
    assert adapter.sends == 0


def test_naive_approval_time_rejected_without_persistence(tmp_path):
    flow = _workflow(tmp_path, state="awaiting_approval")
    with pytest.raises(DomainValidationError):
        flow.coordinator.record_approval(command=flow.command, approval=flow.approval, now=NOW.replace(tzinfo=None))
    assert flow.store.get_approval(flow.approval.approval_id) is None


def test_lookup_response_must_match_command_identity(tmp_path):
    flow, adapter = _workflow(tmp_path), OfflineAdapter()
    flow.command = _send(flow, adapter)
    original_lookup = adapter.reconcile_command
    adapter.reconcile_command = lambda command: replace(original_lookup(command), command_id=uuid4())
    with pytest.raises(DomainValidationError, match="another command"):
        flow.coordinator.reconcile_command(command=flow.command, adapter=adapter)
    assert flow.store.get_command(flow.command.command_id).state == CommandState.RECONCILING
    assert adapter.sends == 1
