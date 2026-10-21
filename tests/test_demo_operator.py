from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from agents.fill_agent import fill_agent
from brokers.base import BrokerExecution, BrokerReconciliationResult, BrokerSubmissionResult
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.operator import DemoOperator, OperatorEvidenceError, OperatorEvidenceStore
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import AccountEvidence, QuoteEvidence, RiskLimits
from risk.submission_gate import GovernanceWritePolicy
from scheduler.lock import ScheduleRunLock
from shared.paths import RISK_CONFIG_PATH


NOW = datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc)


class FakeDemoAdapter:
    broker_name = "fake"
    environment = Environment.DEMO
    read_only = False

    def __init__(self):
        self.sends = 0
        self.lookups = 0
        self.error = None
        self.result_transform = lambda result: result

    def submit_order(self, intent, command):
        self.sends += 1
        if self.error:
            raise self.error
        return BrokerSubmissionResult(command.logical_request_id, True, "order-1",
                                      str(command.logical_request_id), "queued")

    def reconcile_command(self, command):
        self.lookups += 1
        execution = BrokerExecution(
            broker_execution_id="execution-1", broker_order_id="order-1",
            broker_instrument_id=1001, broker_position_id="position-1",
            broker_reference_id=str(command.logical_request_id), environment=Environment.DEMO,
            side="long", action="buy", units=Decimal("1"), price=Decimal("100"),
            executed_at=NOW + timedelta(seconds=1), price_rate_id="rate-1",
            fees=Decimal("0.10"), taxes=Decimal("0"), currency="USD")
        return self.result_transform(BrokerReconciliationResult(
            command.command_id, True, "filled", "order-1", "position-1", (), (execution,)))


def workflow(tmp_path, *, writes=True, account_changes=None):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = Instrument.create(canonical_symbol="SYNTH", exchange="NASDAQ",
                                   asset_type="equity", currency="USD", sector="technology")
    registry.register(instrument)
    registry.record_mapping(BrokerInstrumentMapping.from_resolution(
        instrument=instrument, broker="fake", environment="demo", broker_instrument_id=1001,
        exact_match_symbol="SYNTH", metadata={"source": "fixture"}, resolved_at=NOW))
    config = ExecutionConfig("1.0", True, writes, "fake", Environment.DEMO,
                             True, ("equity", "etf"), ("buy",), Decimal("1"))
    coordinator = ExecutionCoordinator(store=store, registry=registry, config=config)
    operator = DemoOperator(coordinator=coordinator, limits=RiskLimits.load(RISK_CONFIG_PATH),
                            evidence_store=OperatorEvidenceStore(tmp_path / "operator.sqlite3"),
                            staged_fills_path=tmp_path / "broker_fills.csv")
    intent = OrderIntent.create(strategy_id="fixture", run_id="fixture-run",
                                internal_instrument_id=instrument.internal_instrument_id,
                                environment="demo", side="buy", order_type="market",
                                sizing_method="fixed_notional", sizing_value="100", currency="USD",
                                expires_at=NOW + timedelta(minutes=5))
    values = dict(snapshot_id="fixture-account", environment="demo", observed_at=NOW,
                  currency="USD", equity=Decimal("10000"), cash=Decimal("10000"),
                  daily_pnl=Decimal("0"), peak_equity=Decimal("10000"), positions=(),
                  pending_orders=(), pending_orders_complete=True)
    values.update(account_changes or {})
    account = AccountEvidence(**values)
    quote = QuoteEvidence("fixture-quote", instrument.internal_instrument_id, NOW,
                          Decimal("99.9"), Decimal("100"), True)
    return operator, intent, account, quote


def prepare(operator, intent, account, quote):
    return operator.prepare(intent=intent, account=account, quote=quote,
                            source_kind="fixture", now=NOW)


def submit(operator, command, adapter, *, now=NOW):
    return operator.submit(command.command_id, adapter=adapter,
                           kill_switch=KillSwitchStatus(False, True, Environment.DEMO, 1, "fixture", NOW),
                           regular_session_open=True,
                           governance=GovernanceWritePolicy("demo_manual", True, True, True), now=now)


def approved(tmp_path, **kwargs):
    operator, intent, account, quote = workflow(tmp_path, **kwargs)
    command = prepare(operator, intent, account, quote)
    operator.approve(command.command_id, approver="fixture-operator", human_confirmed=True, now=NOW)
    return operator, command, FakeDemoAdapter()


def test_complete_lifecycle_preserves_evidence_but_never_claims_economic_or_live_success(tmp_path):
    operator, command, adapter = approved(tmp_path)
    sent = submit(operator, command, adapter)
    assert sent.state == CommandState.ACKNOWLEDGED
    receipt = operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert receipt.broker_terminal and receipt.staged_execution_ids == ("execution-1",)
    assert not receipt.economic_reconciliation_verified
    assert not receipt.qualification_credit and not receipt.live_demo_qualified
    assert receipt.source_kind == "fixture"
    proof = operator.evidence.read_preparation(command.command_id)
    assert proof["account"]["snapshot_id"] == "fixture-account"
    assert proof["quote"]["quote_id"] == "fixture-quote"
    assert proof["decision"]["intent_hash"] == proof["intent"]["intent_hash"]
    assert len(pd.read_csv(operator.staged_fills_path)) == 1
    operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=3))
    assert len(pd.read_csv(operator.staged_fills_path)) == 1
    assert adapter.sends == 1


def test_full_flow_uses_actual_fill_agent_exactly_once(isolated_workspace, monkeypatch):
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_OFFLINE_OPERATOR_FIXTURE")
    operator, command, adapter = approved(isolated_workspace / "control")
    operator.staged_fills_path = isolated_workspace / "data" / "broker_fills.csv"
    submit(operator, command, adapter)
    operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    fill_agent.run_fill_agent()
    state_path = isolated_workspace / "data" / "portfolio_state.csv"
    cash_path = isolated_workspace / "data" / "cash_state.csv"
    state_before, cash_before = state_path.read_bytes(), cash_path.read_bytes()
    fill_agent.run_fill_agent()
    assert state_path.read_bytes() == state_before
    assert cash_path.read_bytes() == cash_before
    state = pd.read_csv(state_path)
    assert len(state) == 1
    assert state.iloc[0]["quantity"] == 1
    assert state.iloc[0]["ticker"] == "SYNTH"
    fills = pd.read_csv(isolated_workspace / "data" / "trade_fills.csv")
    assert len(fills) == 1 and fills.iloc[0]["fees"] == pytest.approx(.1)


def test_no_implicit_human_approval_or_disabled_write_bypass(tmp_path):
    operator, intent, account, quote = workflow(tmp_path)
    command = prepare(operator, intent, account, quote)
    with pytest.raises(Exception, match="human"):
        operator.approve(command.command_id, approver="operator", human_confirmed=False, now=NOW)
    adapter = FakeDemoAdapter()
    with pytest.raises(OperatorEvidenceError, match="approval"):
        submit(operator, command, adapter)
    assert adapter.sends == 0
    disabled, command2, adapter2 = approved(tmp_path / "disabled", writes=False)
    with pytest.raises(Exception, match="disabled"):
        submit(disabled, command2, adapter2)
    assert adapter2.sends == 0


def test_original_account_age_is_not_extended_by_decision_time(tmp_path):
    operator, command, adapter = approved(tmp_path, account_changes={"observed_at": NOW - timedelta(seconds=14)})
    with pytest.raises(OperatorEvidenceError, match="account.*stale"):
        submit(operator, command, adapter, now=NOW + timedelta(seconds=2))
    assert adapter.sends == 0


def test_incomplete_account_is_risk_rejected_and_cannot_be_approved(tmp_path):
    operator, intent, account, quote = workflow(tmp_path, account_changes={"pending_orders_complete": False})
    command = prepare(operator, intent, account, quote)
    assert command.state == CommandState.RISK_REJECTED
    with pytest.raises(Exception, match="risk-rejected"):
        operator.approve(command.command_id, approver="operator", human_confirmed=True, now=NOW)


def test_source_label_cannot_manufacture_broker_provenance(tmp_path):
    operator, intent, account, quote = workflow(tmp_path)
    with pytest.raises(OperatorEvidenceError, match="source label is not proof"):
        operator.prepare(intent=intent, account=account, quote=quote,
                         source_kind="broker_demo", now=NOW)
    assert not operator.coordinator.store.list_commands()


def test_fixture_cannot_submit_or_lookup_through_etoro_adapter(tmp_path):
    operator, command, adapter = approved(tmp_path)
    adapter.broker_name = "etoro"
    with pytest.raises(OperatorEvidenceError, match="fake Demo adapter"):
        submit(operator, command, adapter)
    with pytest.raises(OperatorEvidenceError, match="fake Demo adapter"):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW)
    assert adapter.sends == adapter.lookups == 0


@pytest.mark.parametrize("changes", [
    {"broker_order_id": "another-order"}, {"broker_reference_id": "another-reference"},
    {"broker_instrument_id": 9999}, {"currency": "GBP"}, {"environment": Environment.REAL},
    {"side": "short"}, {"action": "sell"}, {"units": Decimal("-1")},
    {"price": Decimal("101")}, {"executed_at": NOW - timedelta(days=1)},
])
def test_mismatched_executions_never_stage(tmp_path, changes):
    operator, command, adapter = approved(tmp_path)
    submit(operator, command, adapter)
    adapter.result_transform = lambda r: replace(r, executions=(replace(r.executions[0], **changes),))
    with pytest.raises(OperatorEvidenceError):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert not operator.staged_fills_path.exists()
    assert operator.coordinator.store.get_command(command.command_id).state == CommandState.ACKNOWLEDGED


def test_lost_submission_response_recovers_by_lookup_not_resend(tmp_path):
    operator, command, adapter = approved(tmp_path)
    adapter.error = TimeoutError("synthetic lost acknowledgement")
    with pytest.raises(TimeoutError):
        submit(operator, command, adapter)
    assert operator.coordinator.store.get_command(command.command_id).state == CommandState.UNKNOWN
    operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert adapter.sends == 1
    with pytest.raises(Exception):
        submit(operator, command, adapter)
    assert adapter.sends == 1


def test_crash_after_terminal_lookup_before_staging_is_recoverable(tmp_path, monkeypatch):
    operator, command, adapter = approved(tmp_path)
    submit(operator, command, adapter)
    import execution.operator as module
    original = module.stage_broker_executions
    monkeypatch.setattr(module, "stage_broker_executions", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert operator.coordinator.store.get_command(command.command_id).state == CommandState.RECONCILED
    assert not operator.staged_fills_path.exists()
    monkeypatch.setattr(module, "stage_broker_executions", original)
    restarted = DemoOperator(coordinator=operator.coordinator, limits=operator.limits,
                             evidence_store=OperatorEvidenceStore(operator.evidence.path),
                             staged_fills_path=operator.staged_fills_path)
    restarted.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=3))
    assert len(pd.read_csv(operator.staged_fills_path)) == 1
    assert adapter.sends == 1


def test_changed_or_dropped_execution_evidence_is_not_silently_deduplicated(tmp_path):
    operator, command, adapter = approved(tmp_path)
    submit(operator, command, adapter)
    operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    adapter.result_transform = lambda r: replace(r, executions=(replace(r.executions[0], fees=Decimal("2")),))
    with pytest.raises(OperatorEvidenceError, match="contradictory evidence"):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=3))
    adapter.result_transform = lambda r: replace(r, executions=())
    with pytest.raises(OperatorEvidenceError, match="dropped"):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=3))


def test_execution_cannot_predate_the_durable_submission_attempt(tmp_path):
    operator, command, adapter = approved(tmp_path)
    submit(operator, command, adapter, now=NOW + timedelta(seconds=10))
    # Adapter's execution is at prepare+1s, before the actual send at +10s.
    with pytest.raises(OperatorEvidenceError, match="timestamp.*command lifetime"):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=11))
    assert not operator.staged_fills_path.exists()
    assert operator.coordinator.store.get_command(command.command_id).state == CommandState.ACKNOWLEDGED


def test_lookup_fill_requires_a_persisted_send_attempt(tmp_path):
    operator, command, adapter = approved(tmp_path)
    # Emulate the crash boundary after durable authorization but before send.
    store = operator.coordinator.store
    store.transition_command(command.command_id, CommandState.SUBMISSION_PENDING)
    with pytest.raises(OperatorEvidenceError, match="no persisted send attempt"):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert not operator.staged_fills_path.exists()


def test_different_operator_ledgers_share_one_staging_lock(tmp_path):
    first, first_command, first_adapter = approved(tmp_path / "first")
    second, second_command, second_adapter = approved(tmp_path / "second")
    shared_staging = tmp_path / "shared" / "broker_fills.csv"
    first.staged_fills_path = second.staged_fills_path = shared_staging
    submit(first, first_command, first_adapter)
    submit(second, second_command, second_adapter)
    lock = ScheduleRunLock(shared_staging.with_name("broker_fills.csv.operator.lock"))
    lease = lock.acquire(run_id=str(first_command.command_id), now=NOW)
    try:
        with pytest.raises(OperatorEvidenceError, match="staging lock"):
            second.reconcile(second_command.command_id, adapter=second_adapter, now=NOW + timedelta(seconds=2))
        assert second_adapter.lookups == 0
        assert not shared_staging.exists()
    finally:
        lock.release(lease)
    second.reconcile(second_command.command_id, adapter=second_adapter, now=NOW + timedelta(seconds=2))
    assert len(pd.read_csv(shared_staging)) == 1
    assert not lock.path.exists()


def test_stale_or_corrupt_lock_is_never_automatically_removed(tmp_path):
    operator, command, adapter = approved(tmp_path)
    submit(operator, command, adapter)
    lock_path = tmp_path / "broker_fills.csv.operator.lock"
    lock_path.write_text("unreadable stale lock", encoding="utf-8")
    with pytest.raises(OperatorEvidenceError, match="inspect and recover.*manually"):
        operator.reconcile(command.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert lock_path.read_text(encoding="utf-8") == "unreadable stale lock"
    assert adapter.lookups == 0


def test_approval_recovers_between_ledgers_without_minting_another_approval(tmp_path, monkeypatch):
    operator, intent, account, quote = workflow(tmp_path)
    command = prepare(operator, intent, account, quote)
    original = operator.evidence.save_approval
    monkeypatch.setattr(operator.evidence, "save_approval",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic evidence write failure")))
    with pytest.raises(OSError):
        operator.approve(command.command_id, approver="original-operator", human_confirmed=True, now=NOW)
    store = operator.coordinator.store
    assert store.get_command(command.command_id).state == CommandState.APPROVED
    original_approval = store.get_command_approval(command.command_id)
    monkeypatch.setattr(operator.evidence, "save_approval", original)
    with pytest.raises(Exception, match="original approver"):
        operator.approve(command.command_id, approver="someone-else", human_confirmed=True, now=NOW)
    with pytest.raises(Exception, match="human"):
        operator.approve(command.command_id, approver="original-operator", human_confirmed=False, now=NOW)
    recovered = operator.approve(command.command_id, approver="original-operator",
                                 human_confirmed=True, now=NOW + timedelta(seconds=1))
    assert recovered == original_approval
    assert operator.evidence.read_approval(command.command_id)["approval_id"] == str(original_approval.approval_id)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    submit(operator, command, FakeDemoAdapter(), now=NOW + timedelta(seconds=1))


def test_approval_recovery_does_not_extend_expiry(tmp_path, monkeypatch):
    operator, intent, account, quote = workflow(tmp_path)
    command = prepare(operator, intent, account, quote)
    original = operator.evidence.save_approval
    monkeypatch.setattr(operator.evidence, "save_approval",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError):
        operator.approve(command.command_id, approver="operator", human_confirmed=True, now=NOW)
    monkeypatch.setattr(operator.evidence, "save_approval", original)
    with pytest.raises(Exception, match="stale"):
        operator.approve(command.command_id, approver="operator", human_confirmed=True,
                         now=NOW + timedelta(minutes=6))


def test_prepare_retry_reuses_command_and_cannot_change_its_evidence(tmp_path):
    operator, intent, account, quote = workflow(tmp_path)
    first = prepare(operator, intent, account, quote)
    repeated = prepare(operator, intent, account, quote)
    assert repeated == first
    assert len(operator.coordinator.store.list_commands()) == 1
    with pytest.raises(OperatorEvidenceError, match="cannot replace"):
        prepare(operator, intent, replace(account, cash=Decimal("9999")), quote)


def test_missing_preparation_proof_never_creates_a_replacement_command(tmp_path, monkeypatch):
    operator, intent, account, quote = workflow(tmp_path)
    original = operator.evidence.save_preparation
    monkeypatch.setattr(operator.evidence, "save_preparation",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError):
        prepare(operator, intent, account, quote)
    initial = operator.coordinator.store.list_commands()
    assert len(initial) == 1
    monkeypatch.setattr(operator.evidence, "save_preparation", original)
    with pytest.raises(OperatorEvidenceError, match="evidence is missing"):
        prepare(operator, intent, account, quote)
    assert operator.coordinator.store.list_commands() == initial


def test_preparation_is_immutable_and_tampering_is_detected(tmp_path):
    operator, command, _ = approved(tmp_path)
    proof = operator.evidence.read_preparation(command.command_id)
    altered = {**proof, "source_kind": "broker_demo"}
    with pytest.raises(OperatorEvidenceError, match="immutable"):
        operator.evidence.save_preparation(command.command_id, altered)
    with sqlite3.connect(operator.evidence.path) as connection:
        connection.execute("UPDATE preparations SET payload=? WHERE command_id=?",
                           (json.dumps(altered), str(command.command_id)))
    with pytest.raises(OperatorEvidenceError, match="checksum"):
        operator.evidence.read_preparation(command.command_id)
