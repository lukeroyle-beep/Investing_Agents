from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.domain import OrderIntent, RiskOutcome
from execution.instruments import Instrument
from execution.store import ApprovalReplayError, ExecutionStore
from risk.approval import ApprovalAuthority, ApprovalError
from risk.kill_switch import KILL_SWITCH_SCHEMA_VERSION, RESET_ACKNOWLEDGEMENT, KillSwitch, KillSwitchError
from risk.pretrade import (
    AccountEvidence,
    PendingOrderEvidence,
    PositionEvidence,
    PreTradeRiskEvaluator,
    QuoteEvidence,
    RiskLimits,
)
from risk.submission_gate import (
    GovernanceWritePolicy,
    SubmissionBlockedError,
    SubmissionGate,
)
from shared.paths import RISK_CONFIG_PATH, config_path


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)


def _limits() -> RiskLimits:
    return RiskLimits.load(RISK_CONFIG_PATH)


def _instrument() -> Instrument:
    return Instrument.create(
        canonical_symbol="AAPL",
        exchange="NASDAQ",
        asset_type="equity",
        currency="USD",
        sector="technology",
    )


def _intent(instrument: Instrument, **overrides) -> OrderIntent:
    values = {
        "strategy_id": "quality_v1",
        "run_id": "RUN_WP9",
        "internal_instrument_id": instrument.internal_instrument_id,
        "environment": "demo",
        "side": "buy",
        "order_type": "market",
        "sizing_method": "fixed_notional",
        "sizing_value": "100",
        "currency": "USD",
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return OrderIntent.create(**values)


def _account(instrument: Instrument, **overrides) -> AccountEvidence:
    values = {
        "snapshot_id": "acct-20260828-143000",
        "environment": "demo",
        "observed_at": NOW - timedelta(seconds=2),
        "currency": "USD",
        "equity": Decimal("10000"),
        "cash": Decimal("9000"),
        "daily_pnl": Decimal("0"),
        "peak_equity": Decimal("10000"),
        "positions": (),
        "pending_orders": (),
        "pending_orders_complete": True,
    }
    values.update(overrides)
    return AccountEvidence(**values)


def _quote(instrument: Instrument, **overrides) -> QuoteEvidence:
    values = {
        "quote_id": "rate-778899",
        "internal_instrument_id": instrument.internal_instrument_id,
        "observed_at": NOW - timedelta(seconds=1),
        "bid": Decimal("99.9"),
        "ask": Decimal("100"),
        "regular_session_open": True,
    }
    values.update(overrides)
    return QuoteEvidence(**values)


def test_healthy_demo_order_passes_all_independent_checks():
    instrument = _instrument()
    decision = PreTradeRiskEvaluator(_limits()).evaluate(
        intent=_intent(instrument),
        instrument=instrument,
        account=_account(instrument),
        quote=_quote(instrument),
        now=NOW,
    )
    assert decision.outcome == RiskOutcome.ACCEPTED
    assert decision.reasons == ()
    assert len(decision.checks) >= 19


def test_pending_orders_are_deduplicated_once_but_contradictions_fail_closed():
    instrument = _instrument()
    pending = PendingOrderEvidence(
        broker_order_id="pending-1",
        internal_instrument_id=instrument.internal_instrument_id,
        sector="technology",
        side="buy",
        remaining_notional=Decimal("400"),
    )
    evaluator = PreTradeRiskEvaluator(_limits())
    accepted = evaluator.evaluate(
        intent=_intent(instrument),
        instrument=instrument,
        account=_account(instrument, pending_orders=(pending, pending)),
        quote=_quote(instrument),
        now=NOW,
    )
    assert accepted.outcome == RiskOutcome.ACCEPTED
    assert dict(accepted.computed_exposures)["projected_issuer"] == "500"

    contradiction = replace(pending, remaining_notional=Decimal("300"))
    rejected = evaluator.evaluate(
        intent=_intent(instrument),
        instrument=instrument,
        account=_account(instrument, pending_orders=(pending, contradiction)),
        quote=_quote(instrument),
        now=NOW,
    )
    assert rejected.outcome == RiskOutcome.REJECTED
    assert "pending_orders_consistent" in rejected.reasons


@pytest.mark.parametrize(
    ("account_changes", "quote_changes", "reason"),
    [
        ({"pending_orders_complete": False}, {}, "pending_orders_complete"),
        ({"daily_pnl": Decimal("-100")}, {}, "daily_loss_stop"),
        ({"equity": Decimal("9400")}, {}, "drawdown_stop"),
        ({"observed_at": NOW - timedelta(seconds=16)}, {}, "account_freshness"),
        ({}, {"observed_at": NOW - timedelta(seconds=31)}, "quote_freshness"),
        ({}, {"regular_session_open": False}, "regular_hours"),
    ],
)
def test_critical_risk_ambiguity_rejects(account_changes, quote_changes, reason):
    instrument = _instrument()
    decision = PreTradeRiskEvaluator(_limits()).evaluate(
        intent=_intent(instrument),
        instrument=instrument,
        account=_account(instrument, **account_changes),
        quote=_quote(instrument, **quote_changes),
        now=NOW,
    )
    assert decision.outcome == RiskOutcome.REJECTED
    assert reason in decision.reasons


def test_approval_is_per_intent_expiring_and_explicitly_human():
    instrument = _instrument()
    intent = _intent(instrument)
    decision = PreTradeRiskEvaluator(_limits()).evaluate(
        intent=intent,
        instrument=instrument,
        account=_account(instrument),
        quote=_quote(instrument),
        now=NOW,
    )
    authority = ApprovalAuthority(_limits())
    with pytest.raises(ApprovalError, match="human"):
        authority.issue(
            intent=intent,
            decision=decision,
            approver="operator@example.test",
            now=NOW,
            human_confirmed=False,
        )
    approval = authority.issue(
        intent=intent,
        decision=decision,
        approver="operator@example.test",
        now=NOW,
        human_confirmed=True,
    )
    authority.verify(approval=approval, intent=intent, now=NOW + timedelta(seconds=30))

    changed = _intent(instrument, sizing_value="99")
    with pytest.raises(ApprovalError, match="invalidated"):
        authority.verify(approval=approval, intent=changed, now=NOW)
    with pytest.raises(ApprovalError, match="stale"):
        authority.verify(approval=approval, intent=intent, now=approval.expires_at)


def test_persisted_approval_is_single_use(tmp_path):
    instrument = _instrument()
    intent = _intent(instrument)
    decision = PreTradeRiskEvaluator(_limits()).evaluate(
        intent=intent,
        instrument=instrument,
        account=_account(instrument),
        quote=_quote(instrument),
        now=NOW,
    )
    approval = ApprovalAuthority(_limits()).issue(
        intent=intent,
        decision=decision,
        approver="operator@example.test",
        now=NOW,
        human_confirmed=True,
    )
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    store.save_intent(intent)
    store.save_approval(approval)
    store.consume_approval(approval.approval_id, consumed_at=NOW)
    with pytest.raises(ApprovalReplayError):
        store.consume_approval(approval.approval_id, consumed_at=NOW)


def test_submission_gate_rechecks_quote_session_kill_switch_and_config(tmp_path):
    instrument = _instrument()
    intent = _intent(instrument)
    evaluator = PreTradeRiskEvaluator(_limits())
    decision = evaluator.evaluate(
        intent=intent,
        instrument=instrument,
        account=_account(instrument),
        quote=_quote(instrument),
        now=NOW,
    )
    approval = ApprovalAuthority(_limits()).issue(
        intent=intent,
        decision=decision,
        approver="operator@example.test",
        now=NOW,
        human_confirmed=True,
    )
    switch = KillSwitch(
        state_path=tmp_path / "kill_switch.json",
        audit_path=tmp_path / "kill_switch_audit.jsonl",
    )
    switch.initialize_engaged(
        operator_id="operator@example.test", reason="safe start", now=NOW
    )
    gate = SubmissionGate(_limits())
    governance = GovernanceWritePolicy(
        execution_mode="demo_manual",
        allow_broker_api=True,
        allow_order_submission=True,
        manual_signoff_required=True,
    )
    with pytest.raises(SubmissionBlockedError, match="kill switch"):
        gate.authorize(
            intent=intent,
            decision=decision,
            approval=approval,
            kill_switch=switch.status(),
            regular_session_open=True,
            broker_writes_enabled=True,
            governance=governance,
            now=NOW,
        )
    switch.reset(
        operator_id="operator@example.test",
        reason="manual Demo validation",
        acknowledgement=RESET_ACKNOWLEDGEMENT,
        now=NOW + timedelta(seconds=1),
    )
    with pytest.raises(SubmissionBlockedError, match="session"):
        gate.authorize(
            intent=intent,
            decision=decision,
            approval=approval,
            kill_switch=switch.status(),
            regular_session_open=False,
            broker_writes_enabled=True,
            governance=governance,
            now=NOW + timedelta(seconds=1),
        )
    with pytest.raises(SubmissionBlockedError, match="stale"):
        gate.authorize(
            intent=intent,
            decision=decision,
            approval=approval,
            kill_switch=switch.status(),
            regular_session_open=True,
            broker_writes_enabled=True,
            governance=governance,
            now=NOW + timedelta(seconds=31),
        )
    authorization = gate.authorize(
        intent=intent,
        decision=decision,
        approval=approval,
        kill_switch=switch.status(),
        regular_session_open=True,
        broker_writes_enabled=True,
        governance=governance,
        now=NOW + timedelta(seconds=2),
    )
    assert authorization.intent_hash == intent.intent_hash


def test_checked_in_governance_keeps_demo_writes_disabled():
    governance = GovernanceWritePolicy.load(config_path("governance.yaml"))
    assert not governance.demo_writes_allowed


def test_kill_switch_is_persistent_audited_and_fail_closed_on_corruption(tmp_path):
    switch = KillSwitch(
        state_path=tmp_path / "control" / "kill_switch.json",
        audit_path=tmp_path / "control" / "kill_switch_audit.jsonl",
    )
    assert not switch.status().writes_allowed
    initialized = switch.initialize_engaged(
        operator_id="operator@example.test", reason="initial safe state", now=NOW
    )
    assert initialized.integrity_valid and initialized.engaged
    with pytest.raises(KillSwitchError, match="acknowledgement"):
        switch.reset(
            operator_id="operator@example.test",
            reason="Demo validation",
            acknowledgement="yes",
            now=NOW + timedelta(seconds=1),
        )
    reset = switch.reset(
        operator_id="operator@example.test",
        reason="Demo validation",
        acknowledgement=RESET_ACKNOWLEDGEMENT,
        now=NOW + timedelta(seconds=1),
    )
    assert reset.writes_allowed

    switch.state_path.write_text("{}", encoding="utf-8")
    corrupted = switch.status()
    assert corrupted.engaged and not corrupted.integrity_valid
    assert not corrupted.writes_allowed
