"""Real source-parser fixtures for marks following external import reconciliation."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger, PilotLedgerError
from brokers.etoro.pilot_account import build_pilot_account
from execution.domain import BrokerCommand, CommandState, OrderIntent, payload_hash
from tests.test_external_lifecycle import (
    D, NOW, apply, database_rows, fixture, recover,
)
from tests.test_finite_demo_mandate import candidate, enable, make, terminal
from tests.test_pilot_ledger import execution, post_capture, recapture


COMMAND = "11111111-1111-4111-8111-111111111111"
REFS = {"preparation": payload_hash({"fixture": "synthetic checkpoint recovery"})}


def capture_for(account, milliseconds):
    """Extend the last actual receipt and rebuild; never forge projection clocks."""
    sources = deepcopy(account.raw_sources)
    completed = account.capture_completed_at + timedelta(milliseconds=milliseconds)
    sources["last"]["response_received_at"] = completed.isoformat()
    return build_pilot_account(sources["profile"], sources["first"], sources["pnl"],
                               sources["last"], account.account_id, completed)


def prepare(fixture, kind, milliseconds=100):
    ledger, store, _, journal, proof = fixture
    record = apply(fixture)
    recover(fixture, record)
    at = NOW + timedelta(seconds=6)
    enable(ledger, at)
    _, mandate = make(ledger.path.parent, ledger=ledger, at=at)
    pre = recapture(proof.account, at)
    intent = OrderIntent.create(strategy_id="test", run_id="synthetic", internal_instrument_id=UUID(COMMAND),
        environment="demo", side="buy", order_type="market", sizing_method="fixed_notional",
        sizing_value="10", currency="USD", expires_at=at + timedelta(minutes=5))
    command = BrokerCommand.create(command_id=COMMAND, intent_hash=intent.intent_hash, operation="submit_order",
        broker="etoro", environment="demo", broker_payload={"instrument_id": 1, "amount": "10"})
    store.save_intent(intent)
    store.save_command(command)
    journal.record_intent(COMMAND, mandate_hash=mandate.mandate_hash, evidence_refs=REFS)
    ledger.reserve_under_mandate(mandate, replace(candidate(COMMAND), intent_hash=intent.intent_hash), pre, at)
    journal.record_dispatch(COMMAND)
    fill = replace(execution("200", "10", "0", at + timedelta(seconds=1)), broker_order_id="900")
    post = (post_capture(pre, fill, at + timedelta(seconds=2)) if kind == "open"
            else recapture(pre, at + timedelta(seconds=2)))
    post = capture_for(post, milliseconds)
    lookup = terminal(COMMAND, fill=fill, status="3") if kind == "open" else terminal(COMMAND, status="7")

    def reconcile(account=post, now=None):
        now = now or account.capture_completed_at
        if kind == "open":
            return ledger.reconcile_open(COMMAND, (fill,), pre, account, now, terminal_evidence=lookup)
        return ledger.reconcile_no_fill(COMMAND, pre, account, now, terminal_evidence=lookup)

    return ledger, post, reconcile


@pytest.mark.parametrize("kind", ["open", "no_fill"])
@pytest.mark.parametrize("milliseconds", [0, 100])
def test_reconciliation_marks_accepted_capture_and_recovers_idempotently(fixture, kind, milliseconds):
    ledger, post, reconcile = prepare(fixture, kind, milliseconds)
    before = database_rows(ledger)
    state = reconcile()
    assert post.capture_completed_at - post.capture_started_at == timedelta(milliseconds=milliseconds)
    assert state["committed_capital"] == D("22.37" if kind == "open" else "12.37")
    assert state["position_count"] == (2 if kind == "open" else 1)
    assert state["pilot_pnl"] == D("0.24")
    assert state["submissions"][0]["reconciled"] and state["unresolved"]
    assert not state["submissions"][0]["journal_complete"]
    after = database_rows(ledger)
    assert after["inception"] == before["inception"]
    assert after["submissions"] == before["submissions"]
    assert after["events"][:len(before["events"])] == before["events"]
    appended = after["events"][len(before["events"]):]
    assert [row[1] for row in appended] == ["reconciled_" + kind, "mark"]
    receipt = ledger.reconciliation_receipt(COMMAND)["reconciliation"]
    assert receipt["terminal_evidence"]["lifecycle_state"] == ("filled" if kind == "open" else "cancelled")
    assert receipt["post_account"]["source_hash"] == post.source_hash
    reopened = PilotLedger(ledger.path, "123")
    assert reopened.snapshot() == state
    # Recovery repeats the retained original evidence, creating no new economic rows.
    assert reconcile() == state
    assert database_rows(ledger) == after
    _, store, _, journal, _ = fixture
    command = store.get_command(COMMAND)
    lookup = receipt["terminal_evidence"]
    # Represent the synthetic operational commit, then recover the actual
    # execution journal from retained ledger and operational-store evidence.
    with store._transaction() as db:
        row = db.execute("SELECT * FROM broker_commands WHERE command_id=?", (COMMAND,)).fetchone()
        store._update_command(db, row, command, replace(command, state=CommandState.RECONCILED,
            broker_order_id=lookup["broker_order_id"],
            broker_reference_id=dict(lookup["details"])["connector_broker_reference_id"]),
            details={"fixture": "synthetic reconciled commit"})
    mandate_hash = state["submissions"][0]["mandate_hash"]
    terminal_record = journal.reconstruct(COMMAND, mandate_hash=mandate_hash, evidence_refs=REFS)
    assert terminal_record["payload"]["actual_principal"] == ("10" if kind == "open" else "0")
    assert terminal_record["payload"]["actual_costs"] == "0"
    assert terminal_record["payload"]["outcome"] == ("filled" if kind == "open" else "cancelled")
    assert terminal_record["payload"]["post_source_hash"] == post.source_hash
    assert journal.is_complete(COMMAND) and database_rows(ledger) == after
    events = journal.events()
    assert journal.reconstruct(COMMAND, mandate_hash=mandate_hash, evidence_refs=REFS) == terminal_record
    assert journal.events() == events
    reopened.mark_journal_complete(COMMAND, journal_reference=terminal_record["chain_hash"], now=post.capture_completed_at)
    assert not reopened.snapshot()["unresolved"]
    completed = database_rows(ledger)
    reopened.mark_journal_complete(COMMAND, journal_reference=terminal_record["chain_hash"], now=post.capture_completed_at)
    assert database_rows(ledger) == completed


@pytest.mark.parametrize("kind", ["open", "no_fill"])
@pytest.mark.parametrize("invalid", ["altered", "stale", "overlapping"])
def test_reconciliation_rejects_other_capture_evidence_without_mutation(fixture, kind, invalid):
    ledger, post, reconcile = prepare(fixture, kind)
    before = database_rows(ledger)
    if invalid == "altered":
        account = replace(post, cash=post.cash + 1, equity=post.equity + 1)
        now, reason = post.capture_completed_at, "projection_mismatch"
    elif invalid == "stale":
        account = post
        now, reason = post.capture_completed_at + timedelta(seconds=31), "stale_or_future"
    else:
        # Starts before the claim and finishes after it; matching economics are insufficient.
        account = capture_for(recapture(post, NOW + timedelta(seconds=5, milliseconds=950)), 100)
        now, reason = post.capture_completed_at, "post_capture_precedes_submission"
    with pytest.raises(ValueError, match=reason):
        reconcile(account, now)
    assert database_rows(ledger) == before
    assert not ledger.snapshot()["submissions"][0]["reconciled"]


@pytest.mark.parametrize("kind", ["open", "no_fill"])
def test_mark_failure_rolls_back_reconciliation_and_retry_replays_once(fixture, kind, monkeypatch):
    ledger, post, reconcile = prepare(fixture, kind)
    before = database_rows(ledger)
    original_append = ledger._append

    def fail_after_mark(db, event_kind, command, value):
        original_append(db, event_kind, command, value)
        if event_kind == "mark":
            raise OSError("synthetic mark persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "_append", fail_after_mark)
        with pytest.raises(OSError, match="mark persistence"):
            reconcile()
    assert database_rows(ledger) == before
    assert not ledger.snapshot()["submissions"][0]["reconciled"]
    state = reconcile()
    assert state["submissions"][0]["reconciled"]
    after = database_rows(ledger)
    assert reconcile() == state and database_rows(ledger) == after


@pytest.mark.parametrize("kind", ["open", "no_fill"])
@pytest.mark.parametrize("change", [
    {"cash": "1"}, {"qualification_credit": True}, {"source_hash": "f" * 64},
    {"capture_started_at": "2026-10-23T12:00:05.950000Z"},
])
def test_reconciliation_mark_exception_requires_the_exact_accepted_capture(fixture, kind, change, monkeypatch):
    ledger, _, reconcile = prepare(fixture, kind)
    before = database_rows(ledger)
    original_mark = ledger._mark

    def substitute(db, state, data, now, **kwargs):
        return original_mark(db, state, {**data, **change}, now, **kwargs)

    monkeypatch.setattr(ledger, "_mark", substitute)
    with pytest.raises(PilotLedgerError, match="reconciled_mark_capture_mismatch"):
        reconcile()
    assert database_rows(ledger) == before


@pytest.mark.parametrize("kind", ["open", "no_fill"])
@pytest.mark.parametrize("capture", ["exact", "overlapping", "older"])
def test_ordinary_marks_keep_checkpoint_overlap_rejection(fixture, kind, capture):
    ledger, post, reconcile = prepare(fixture, kind)
    reconcile()
    before = database_rows(ledger)
    candidate = post if capture == "exact" else capture_for(recapture(post,
        post.capture_completed_at - timedelta(milliseconds=50 if capture == "overlapping" else 200)), 100)
    with pytest.raises(PilotLedgerError, match="source_precedes_external_checkpoint"):
        ledger.refresh_marks(candidate, max(candidate.capture_completed_at, post.capture_completed_at))
    after = database_rows(ledger)
    assert after["inception"] == before["inception"] and after["submissions"] == before["submissions"]
    assert after["events"][:len(before["events"])] == before["events"]
    assert [row[1] for row in after["events"][len(before["events"]):]] == ["hold"]
    with closing(ledger._connect()) as db:
        assert ledger._state(db)["_account_checkpoint"]["source_hash"] == post.source_hash


@pytest.mark.parametrize("kind", ["open", "no_fill"])
def test_accepted_capture_cannot_replace_a_later_checkpoint(fixture, kind):
    ledger, post, reconcile = prepare(fixture, kind)
    reconcile()
    accepted = ledger.reconciliation_receipt(COMMAND)["reconciliation"]["post_account"]
    newer = recapture(post, post.capture_completed_at + timedelta(seconds=1))
    ledger.refresh_marks(newer, newer.capture_completed_at)
    before = database_rows(ledger)
    with closing(ledger._connect()) as db, db:
        with pytest.raises(PilotLedgerError, match="reconciled_mark_capture_mismatch"):
            ledger._mark(db, ledger._state(db), accepted, newer.capture_completed_at, reconciled_command_id=COMMAND)
    assert database_rows(ledger) == before
