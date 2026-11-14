"""Demo execution submits only after a consumed approval and a durable send claim.

Synthetic credentials and an in-memory transport. No broker host is contacted.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from brokers.base import BrokerWriteDisabled
from brokers.etoro.adapter import EtoroDemoExecutionAdapter
from execution.domain import (
    BrokerCommand, CommandAttempt, CommandState, OrderIntent, RiskCheck, RiskDecision,
)
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.store import ExecutionStore
from risk.approval import ApprovalAuthority
from risk.pretrade import RiskLimits
from shared.paths import RISK_CONFIG_PATH
from tests.contracts.etoro.test_etoro_gate_b_contracts import (
    NOW,
    REQUEST_ID,
    FakeTransport,
    _accepted,
    _execution_client,
    _open_payload,
    _read_client,
    _response,
)


def _ready(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(Instrument.create(
        canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity", currency="USD",
    ))
    registry.record_mapping(BrokerInstrumentMapping.from_resolution(
        instrument=instrument, broker="etoro", environment="demo",
        broker_instrument_id=1001, exact_match_symbol="AAPL",
        metadata={"instrumentId": 1001}, resolved_at=NOW,
    ))
    intent = OrderIntent.create(
        strategy_id="offline_authority", run_id="RUN_AUTHORITY",
        internal_instrument_id=instrument.internal_instrument_id, environment="demo",
        side="buy", order_type="market", sizing_method="fixed_notional",
        sizing_value="100", currency="USD", expires_at=NOW + timedelta(minutes=5),
    )
    store.save_intent(intent)
    command = BrokerCommand.create(
        intent_hash=intent.intent_hash, operation="submit_order", broker="etoro",
        environment="demo", broker_payload=_open_payload(), logical_request_id=REQUEST_ID,
    )
    decision = RiskDecision(
        uuid4(), intent.intent_hash, "offline-account", "offline-quote", NOW, (),
        (RiskCheck("offline_fixture", True, "100", "1000"),), "accepted", (), NOW,
    )
    command = store.save_risk_bound_command(command, decision)
    approval = ApprovalAuthority(RiskLimits.load(RISK_CONFIG_PATH)).issue(
        intent=intent, decision=decision, approver="offline-human@example.test",
        now=NOW, human_confirmed=True,
    )
    command = store.approve_command(command, approval, now=NOW)
    transport = FakeTransport([_response(_accepted(), 202)])
    adapter = EtoroDemoExecutionAdapter(
        client=_read_client(), execution_client=_execution_client(transport),
        registry=registry, store=store,
    )
    return store, intent, decision, approval, command, adapter, transport


def test_proposed_command_without_approval_or_claim_does_not_post(tmp_path):
    store, intent, _, _, command, adapter, transport = _ready(tmp_path)
    proposed = store.get_command(command.command_id)
    # Rewind the in-memory view to the probed shape: proposed, no approval use, no attempts.
    probed = replace(proposed, state=CommandState.PROPOSED, attempt_history=())
    with pytest.raises(BrokerWriteDisabled, match="persisted_consumed_approval_and_send_claim_required"):
        adapter.submit_order(intent, probed)
    assert transport.requests == []


def test_forged_in_memory_send_claim_without_store_claim_does_not_post(tmp_path):
    store, intent, _, _, command, adapter, transport = _ready(tmp_path)
    approved = store.get_command(command.command_id)
    forged = replace(approved, state=CommandState.SUBMISSION_PENDING, attempt_history=(
        CommandAttempt(1, approved.logical_request_id, NOW, "send_started"),
    ))
    with pytest.raises(BrokerWriteDisabled, match="persisted_consumed_approval_and_send_claim_required"):
        adapter.submit_order(intent, forged)
    assert transport.requests == []


def test_approved_and_claimed_command_still_posts_once(tmp_path):
    store, intent, decision, approval, command, adapter, transport = _ready(tmp_path)
    pending = store.authorize_submission(
        command, intent, decision, approval,
        authorized_at=NOW, valid_until=NOW + timedelta(seconds=60),
    )
    claimed = store.claim_submission(pending, intent, CommandAttempt(
        1, pending.logical_request_id, NOW, "send_started",
    ))
    result = adapter.submit_order(intent, claimed)
    assert result.accepted_for_processing is True
    assert result.request_id == claimed.logical_request_id
    assert len(transport.requests) == 1
    assert transport.requests[0].method == "POST"
    assert transport.requests[0].headers["x-request-id"] == str(claimed.logical_request_id)
