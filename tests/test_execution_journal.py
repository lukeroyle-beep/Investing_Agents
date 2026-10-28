"""Synthetic receipt tests; no broker connection or economic writes by journal."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
from uuid import uuid4

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger, PilotLedgerError
from agents.journal_agent.execution_journal import ExecutionJournal, ExecutionJournalError, _clean
from brokers.base import BrokerReconciliationResult
from execution.domain import BrokerCommand, CommandState, OrderIntent, payload_hash
from execution.finite_demo_mandate import FiniteDemoMandate, FiniteOrderCandidate
from execution.store import ExecutionStore
from tests.test_pilot_ledger import Account, D, NOW, execution, post_capture, recapture


MANDATE = payload_hash({"synthetic_mandate": "finite-scope"})
REFS = {"preparation": payload_hash({"synthetic_evidence": "prepared"})}


@pytest.fixture
def source(tmp_path):
    ledger = PilotLedger.create(tmp_path / "economic.sqlite3", Account(), [], NOW)
    store = ExecutionStore(tmp_path / "operational.sqlite3")
    intent = OrderIntent.create(strategy_id="test", run_id="synthetic", internal_instrument_id=uuid4(),
        environment="demo", side="buy", order_type="market", sizing_method="fixed_notional",
        sizing_value="99", currency="USD", expires_at=NOW + timedelta(minutes=5))
    command = BrokerCommand.create(intent_hash=intent.intent_hash, operation="submit_order",
        broker="etoro", environment="demo", broker_payload={"instrument_id": 1, "amount": "99"})
    store.save_intent(intent)
    store.save_command(command)
    journal = ExecutionJournal.create(tmp_path / "journal.sqlite3", ledger=ledger, store=store, runtime_root=tmp_path)
    return ledger, store, journal, intent, command


def intent_event(source):
    _, _, journal, _, command = source
    return journal.record_intent(command.command_id, mandate_hash=MANDATE, evidence_refs=REFS)


def claimed(source):
    ledger, _, journal, intent, command = source
    intent_event(source)
    ledger.reserve_and_claim(str(command.command_id), D("99"), D("1"), Account(), NOW,
        intent_hash=intent.intent_hash, instrument_id=1)
    return journal.record_dispatch(command.command_id)


def committed(source, *, outcome=True):
    ledger, store, journal, _, command = source
    claimed(source)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = post_capture(Account(), fill, NOW + timedelta(seconds=2))
    if outcome:
        ledger.record_outcome(str(command.command_id), {"state": "acknowledged", "order_id": fill.broker_order_id})
    ledger.reconcile_open(str(command.command_id), (fill,), Account(), post, NOW + timedelta(seconds=2))
    # Synthetic fixture represents the operational commit; production journal
    # only reads it and can never make this transition itself.
    with store._transaction() as db:
        row = db.execute("SELECT * FROM broker_commands WHERE command_id=?", (str(command.command_id),)).fetchone()
        store._update_command(db, row, command, replace(command, state=CommandState.RECONCILED,
            broker_order_id=fill.broker_order_id, broker_reference_id=fill.broker_reference_id), details={"fixture": "reconciled"})
    return fill, post


def test_create_explicit_bound_and_never_reset(source, tmp_path):
    ledger, store, journal, _, _ = source
    assert journal.ensure_ready() is True
    assert journal.events() == ()
    with pytest.raises(ExecutionJournalError, match="never_reset"):
        ExecutionJournal.create(journal.path, ledger=ledger, store=store, runtime_root=tmp_path)
    with pytest.raises(ExecutionJournalError, match="missing"):
        ExecutionJournal(tmp_path / "absent.sqlite3", ledger=ledger, store=store, runtime_root=tmp_path)
    other = PilotLedger.create(tmp_path / "other.sqlite3", Account(), [], NOW)
    with pytest.raises(ExecutionJournalError, match="binding_mismatch"):
        ExecutionJournal(journal.path, ledger=other, store=store, runtime_root=tmp_path)


def test_exact_duplicates_are_noops_and_conflicting_identity_fails(source):
    first = intent_event(source)
    assert intent_event(source) == first
    assert len(source[2].events()) == 1
    with pytest.raises(ExecutionJournalError, match="conflicting_duplicate"):
        source[2].record_intent(source[4].command_id, mandate_hash=payload_hash({"changed": True}), evidence_refs=REFS)
    assert source[2].events() == (first,)


def test_restart_reconstructs_full_audit_only_from_retained_sources(source, tmp_path):
    ledger, store, journal, _, command = source
    fill, _ = committed(source)
    before = ledger.path.read_bytes()
    recovered = ExecutionJournal(journal.path, ledger=ledger, store=store, runtime_root=tmp_path)
    final = recovered.reconstruct(command.command_id, mandate_hash=MANDATE, evidence_refs=REFS)
    assert final["payload"]["broker_order_id"] == fill.broker_order_id
    assert final["payload"]["actual_principal"] == "99"
    assert final["payload"]["actual_costs"] == "1"
    assert final["payload"]["outcome"] == "filled"
    assert final["payload"]["pre_source_hash"] != final["payload"]["post_source_hash"]
    assert recovered.is_complete(command.command_id)
    assert recovered.reconstruct(command.command_id, mandate_hash=MANDATE, evidence_refs=REFS) == final
    assert ledger.path.read_bytes() == before
    ledger.mark_journal_complete(str(command.command_id), journal_reference=final["chain_hash"], now=NOW + timedelta(seconds=3))
    assert recovered.terminal(command.command_id) == final


def test_crash_after_economic_commit_without_outcome_uses_operational_receipt(source):
    _, _, journal, _, command = source
    committed(source, outcome=False)
    final = journal.reconstruct(command.command_id, mandate_hash=MANDATE, evidence_refs=REFS)
    outcomes = [r for r in journal.events() if r["phase"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["payload"]["source_kind"] == "persisted_operational_command"
    assert final["phase"] == "reconciliation"


def test_pending_and_partial_observations_never_create_terminal_verification(source):
    ledger, _, journal, _, command = source
    claimed(source)
    ledger.record_outcome(str(command.command_id), {"state": "partially_filled", "order_id": "synthetic-order", "terminal": False})
    assert journal.reconstruct(command.command_id, mandate_hash=MANDATE, evidence_refs=REFS) is None
    assert not journal.is_complete(command.command_id)
    with pytest.raises(ExecutionJournalError, match="operational_reconciliation_required"):
        journal.record_reconciliation(command.command_id)
    assert [r["phase"] for r in journal.events()] == ["intent", "dispatch", "outcome"]


def test_claim_and_source_intent_must_match(source):
    ledger, _, journal, _, command = source
    intent_event(source)
    ledger.reserve_and_claim(str(command.command_id), D("99"), D("1"), Account(), NOW,
        intent_hash=payload_hash({"different": "intent"}), instrument_id=1)
    with pytest.raises(ExecutionJournalError, match="dispatch_intent_mismatch"):
        journal.record_dispatch(command.command_id)


def test_no_dispatch_or_terminal_can_be_created_from_user_summary(source):
    _, _, journal, _, command = source
    intent_event(source)
    with pytest.raises(ExecutionJournalError, match="dispatch_claim_required"):
        journal.record_dispatch(command.command_id)
    with pytest.raises(TypeError):
        journal.record_reconciliation(command.command_id, verified=True, pre_account={"cash": "100"})
    assert len(journal.events()) == 1


@pytest.mark.parametrize("payload", [
    {"token": "private"}, {"nested": [{"api-key": "private"}]},
    {"nested": {"User_Key": "private"}}, {"result": '{"credential":"private"}'},
    {"result": '{"body":"{\\"accessToken\\":\\"private\\"}"}'},
    {"password": "private"}, {"Authorization": "private"},
])
def test_sensitive_fields_rejected_recursively(payload):
    with pytest.raises(ExecutionJournalError, match="sensitive"):
        _clean(payload)


def test_secret_bearing_source_fails_before_storage(source):
    ledger, _, journal, _, command = source
    claimed(source)
    # The older economic cleaner preserves unknown field names; the journal
    # rejects instead of copying secret-bearing data into another repository.
    ledger.record_outcome(str(command.command_id), {"password": "private"})
    with pytest.raises(ExecutionJournalError, match="sensitive"):
        journal.record_outcome(command.command_id)
    assert b"private" not in journal.path.read_bytes()


def test_concurrent_identical_intents_make_one_event(source):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: intent_event(source), range(8)))
    assert all(r == results[0] for r in results)
    assert len(source[2].events()) == 1


@pytest.mark.parametrize("change", ["payload", "suffix", "gap", "head"])
def test_chain_detects_modified_payload_or_deleted_suffix(source, change):
    _, _, journal, _, _ = source
    claimed(source)
    with sqlite3.connect(journal.path) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute("DROP TRIGGER events_no_delete")
        if change == "payload":
            db.execute("UPDATE events SET payload='{}' WHERE sequence=1")
        elif change == "suffix":
            db.execute("DELETE FROM events WHERE sequence=2")
        elif change == "gap":
            db.execute("DELETE FROM events WHERE sequence=1")
        else:
            db.execute("UPDATE head SET digest='corrupt'")
    with pytest.raises(ExecutionJournalError, match="integrity|truncation"):
        journal.ensure_ready()


def test_immutable_database_triggers(source):
    intent_event(source)
    with sqlite3.connect(source[2].path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            db.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE binding SET digest='changed'")


def test_corrupt_storage_and_removed_storage_fail_closed(source):
    _, _, journal, _, _ = source
    journal.path.write_bytes(b"not a sqlite database")
    with pytest.raises(ExecutionJournalError, match="storage_corrupt|storage_unavailable"):
        journal.ensure_ready()
    journal.path.unlink()
    with pytest.raises(ExecutionJournalError, match="missing"):
        journal.ensure_ready()
    assert not journal.path.exists()


def test_changed_committed_evidence_invalidates_terminal_readback(source):
    ledger, _, journal, _, command = source
    committed(source)
    journal.reconstruct(command.command_id, mandate_hash=MANDATE, evidence_refs=REFS)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TRIGGER events_no_update")
        row = db.execute("SELECT sequence,payload FROM events WHERE kind='reconciled_open'").fetchone()
        payload = json.loads(row[1])
        payload["post_account"]["cash"] = "999999"
        db.execute("UPDATE events SET payload=?,digest=? WHERE sequence=?",
            (json.dumps(payload), payload_hash(payload), row[0]))
    with pytest.raises(ExecutionJournalError, match="account_projection_mismatch"):
        journal.is_complete(command.command_id)


def test_partial_economic_outcome_without_verified_terminal_commit_is_incomplete(source):
    ledger, _, journal, _, command = source
    claimed(source)
    fill = execution(amount="40", fees="0", now=NOW + timedelta(seconds=1))
    post = post_capture(Account(), fill, NOW + timedelta(seconds=2))
    with pytest.raises(PilotLedgerError, match="full_requested_principal"):
        ledger.reconcile_open(str(command.command_id), (fill,), Account(), post, NOW + timedelta(seconds=2))
    ledger.record_outcome(str(command.command_id), {"state": "partially_filled", "order_id": fill.broker_order_id})
    journal.record_outcome(command.command_id)
    assert journal.terminal(command.command_id) is None


def finite_claim(source):
    ledger, _, journal, intent, command = source
    ledger.enable_finite_mandates(source_reference="synthetic-review", reviewed_commit="a" * 40, now=NOW)
    scope = {"instruments": [{"instrument_id": 1, "symbol": "TEST", "exchange": "TEST",
        "asset_type": "equity", "economic_class": "equity", "currency": "USD", "issuer": "TEST",
        "sector": "TEST", "class_source_reference": "synthetic"}], "max_submissions": 2,
        "max_per_day": 2, "per_order_cap": "100", "total_commitment": "200", "max_positions": 5,
        "min_cash_fraction": "0.20", "loss_stop": "25", "order_types": ["market"]}
    mandate = FiniteDemoMandate.create(journal.path.parent / "mandate.json", account_id="123",
        source_reference="synthetic-review", issued_at=NOW, expires_at=NOW + timedelta(days=1),
        ledger=ledger, reviewed_commit="a" * 40, scope=scope)
    journal.record_intent(command.command_id, mandate_hash=mandate.mandate_hash, evidence_refs=REFS)
    candidate = FiniteOrderCandidate(str(command.command_id), 1, D("99"), D("1"), intent.intent_hash)
    ledger.reserve_under_mandate(mandate, candidate, Account(), NOW)
    journal.record_dispatch(command.command_id)
    return mandate


@pytest.mark.parametrize("kind", ["partial", "cancelled", "rejected"])
def test_source_verified_partial_and_no_fill_terminal_reconstruction(source, kind):
    ledger, store, journal, _, command = source
    mandate = finite_claim(source)
    partial = kind == "partial"
    fill = execution(amount="40", fees="0", now=NOW + timedelta(seconds=1))
    fills = (fill,) if partial else ()
    post = post_capture(Account(), fill, NOW + timedelta(seconds=2)) if partial else recapture(Account(), NOW + timedelta(seconds=2))
    result = BrokerReconciliationResult(command.command_id, True,
        "partially_filled_terminal" if partial else kind, fill.broker_order_id,
        fill.broker_position_id if partial else None,
        (("connector_broker_reference_id", fill.broker_reference_id), ("remainder_terminal", "true"),
         ("lookup_sha256", payload_hash({"synthetic_lookup": kind})),
         ("status_id", "9" if partial else "4" if kind == "cancelled" else "8")), fills)
    if partial:
        ledger.reconcile_open(str(command.command_id), fills, Account(), post, NOW + timedelta(seconds=2),
            terminal_partial=True, terminal_evidence=result)
    else:
        ledger.reconcile_no_fill(str(command.command_id), Account(), post, NOW + timedelta(seconds=2), terminal_evidence=result)
    with store._transaction() as db:
        row = db.execute("SELECT * FROM broker_commands WHERE command_id=?", (str(command.command_id),)).fetchone()
        store._update_command(db, row, command, replace(command, state=CommandState.RECONCILED,
            broker_order_id=fill.broker_order_id, broker_reference_id=fill.broker_reference_id), details={"fixture": "reconciled"})
    final = journal.reconstruct(command.command_id, mandate_hash=mandate.mandate_hash, evidence_refs=REFS)
    assert final["payload"]["outcome"] == ("partially_filled" if partial else kind)
    assert final["payload"]["actual_principal"] == ("40" if partial else "0")
    assert journal.is_complete(command.command_id)
