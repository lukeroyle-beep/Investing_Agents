"""Synthetic internal mechanics only; no production source qualifier exists."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from agents.fill_agent import external_lifecycle as external
from agents.fill_agent.pilot_ledger import PilotLedger, PilotLedgerError
from agents.journal_agent.execution_journal import ExecutionJournal, ExecutionJournalError
from execution.domain import canonical_json, payload_hash
from execution.finite_demo_mandate import FiniteDemoMandate, FiniteOrderCandidate
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from tests.test_pilot_ledger import Account, Position, NOW, recapture, execution, post_capture
from tests.test_finite_demo_mandate import make, enable, candidate

D = Decimal
COMMIT = "a" * 40


def source(reference, body):
    raw = canonical_json(body)
    return {"reference": reference, "body": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def database_rows(ledger):
    with closing(ledger._connect()) as db:
        return {name: db.execute("SELECT * FROM " + name).fetchall() for name in ("inception", "submissions", "events")}


@pytest.fixture
def fixture(tmp_path):
    initial = Account(positions=tuple(Position(position_id=str(i), instrument_id=9+i) for i in range(1,9)))
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", initial, [{"legacy": "preserved"}], NOW)
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    switch = KillSwitch(state_path=tmp_path/"kill-switch.json", audit_path=tmp_path/"kill-switch.jsonl")
    switch.initialize_engaged(operator_id="synthetic", reason="test only", now=NOW)
    (tmp_path/"operator.lock").touch(mode=0o600)
    snapshot = ledger.snapshot()
    (tmp_path/"pilot-inception.json").write_text(canonical_json({"policy":"demo-pilot-v1", "account_id":"123",
        "inception_hash":payload_hash(snapshot["inception"])}))
    (tmp_path/"consumed-connector-claim.json").write_text('{"fixture":"consumed-never-reset"}')
    journal = ExecutionJournal.create(tmp_path/"execution-journal.sqlite3", ledger=ledger, store=store, runtime_root=tmp_path)
    position = Position(position_id="101", instrument_id=42, units=D("0.412345"), principal=D("12.34"),
        fees=D("0.02"), taxes=D("0.01"), pnl=D("0.27"), exposure=D("12.61"))
    # This source is entirely synthetic. It is injected at the PRIVATE engine,
    # never treated as a supported public statement or qualification fixture.
    current = Account(cash=D("18014.22"), positions=(position,), now=NOW+timedelta(seconds=3))
    closures = tuple({"position_id":p["position_id"], "original_position":p, "cash_proceeds":"1008",
        "account_currency":"USD", "occurred_at_source":(NOW+timedelta(seconds=1)).isoformat(),
        "source_reference":"synthetic-closures"} for p in snapshot["inception"]["account"]["positions"])
    openings = ({"position_id":"101", "order_id":"500", "account_currency":"USD",
        "occurred_at_source":"2026-10-23T12:00:02.1234567Z", "source_reference":"synthetic-opening",
        "cash_debit":"12.37", "quote_currency":"GBX", "quote_price":"1744", "order_type":"limit", "time_in_force":"IOC"},)
    cash = ({"event_id":"synthetic-withdrawal", "amount":"-37.41", "account_currency":"USD",
        "occurred_at_source":(NOW+timedelta(seconds=2)).isoformat(), "source_reference":"synthetic-cash", "kind":"withdrawal"},)
    proof = external._QualifiedLifecycle(current, payload_hash(snapshot["inception"]["account"]), closures,
        openings, cash, (source("synthetic-closures",closures), source("synthetic-opening",openings),
                       source("synthetic-cash",cash)), "synthetic-cash")
    return ledger, store, switch, journal, proof


def plan(fixture, proof=None):
    ledger,_,_,_,original = fixture
    with closing(ledger._connect()) as db:
        return external._record(ledger,ledger._state(db),proof or original,
            reviewed_commit=COMMIT,review_reference="synthetic reviewed mechanical plan")


def apply(fixture, proof=None, *, expected=None, now=None):
    ledger,_,switch,_,original = fixture
    proof = proof or original
    return external._apply_qualified(ledger,proof,expected_plan_hash=expected or payload_hash(plan(fixture,proof)),
        reviewed_commit=COMMIT,review_reference="synthetic reviewed mechanical plan",kill_switch=switch,
        now=now or NOW+timedelta(seconds=4))


def recover(fixture, record):
    ledger,_,_,journal,_ = fixture
    return external.recover_external_journal(ledger,journal,event_id=record["event_id"],now=NOW+timedelta(seconds=5))


@pytest.mark.parametrize("forged", [{"verified":True}, {"apply_supported":True,"cash_residual":"0"},
    {"cash_events_complete":True,"opening_debit":"12.34"}, {"source_contract":"synthetic-complete-statement"}])
def test_public_api_refuses_all_flags_and_planner_reports_without_opening_any_store(fixture,forged):
    ledger=fixture[0];before=files(ledger.path.parent)
    with pytest.raises(external.ExternalLifecycleError,match="source_contract_unqualified"):
        external.import_external_lifecycle(runtime=ledger.path.parent,sources=forged,verified=True)
    assert files(ledger.path.parent)==before


def test_even_private_type_and_mock_qualifier_cannot_enable_public_apply(fixture,monkeypatch):
    ledger,_,_,_,proof=fixture;before=files(ledger.path.parent)
    with pytest.raises(external.ExternalLifecycleError,match="unqualified"):
        external.import_external_lifecycle(runtime=ledger.path.parent,sources=proof)
    monkeypatch.setattr(external,"_qualify_sources",lambda **kwargs:proof)
    with pytest.raises(external.ExternalLifecycleError,match="public_application_not_enabled"):
        external.import_external_lifecycle(runtime=ledger.path.parent,sources={})
    assert files(ledger.path.parent)==before


def test_atomic_external_event_preserves_original_history_and_counts_capital_pnl(fixture):
    ledger,store,_,journal,_=fixture
    before=database_rows(ledger);claim=(ledger.path.parent/"consumed-connector-claim.json").read_bytes()
    record=apply(fixture);state=ledger.snapshot()
    assert state["inception"]["account"]["positions"]==json.loads(before["inception"][0][1])["account"]["positions"]
    assert database_rows(ledger)["inception"]==before["inception"]
    assert database_rows(ledger)["submissions"]==before["submissions"]==[]
    assert database_rows(ledger)["events"][:len(before["events"])]==before["events"]
    assert (ledger.path.parent/"consumed-connector-claim.json").read_bytes()==claim
    assert state["position_count"]==1 and state["committed_capital"]==D("12.37")
    assert state["pilot_pnl"]==D("0.24")  # no 64 USD legacy closure profit credited
    assert state["unresolved"] and not state["external_imports"][0]["journal_complete"]
    assert len(state["external_openings"])==1 and state["submissions"]==()
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM broker_commands").fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0]==0
    terminal=recover(fixture,record)
    assert not ledger.snapshot()["unresolved"]
    assert [e["phase"] for e in journal.events()]==["external_import"]
    assert terminal["command_id"]=="external:"+record["event_id"]
    assert terminal["payload"]["economic_record"]["openings"][0]["quote_currency"]=="GBX"
    assert terminal["payload"]["economic_record"]["openings"][0]["occurred_at_source"].endswith(".1234567Z")
    assert not any(e["phase"] in {"intent","dispatch"} for e in journal.events())


def test_restart_and_exact_replay_never_duplicate_economics_or_journal(fixture):
    ledger,store,_,_,_=fixture
    record=apply(fixture);before=database_rows(ledger)
    assert apply(fixture,now=NOW+timedelta(days=1))==record
    assert database_rows(ledger)==before
    journal=ExecutionJournal(ledger.path.parent/"execution-journal.sqlite3",ledger=ledger,store=store,runtime_root=ledger.path.parent)
    terminal=external.recover_external_journal(ledger,journal,event_id=record["event_id"],now=NOW+timedelta(days=1))
    after=database_rows(ledger);events=journal.events()
    assert external.recover_external_journal(ledger,journal,event_id=record["event_id"],now=NOW+timedelta(days=2))==terminal
    assert database_rows(ledger)==after and journal.events()==events


def test_old_baseline_is_retained_but_retired_positions_no_longer_required_by_marks(fixture):
    ledger,_,_,_,proof=fixture;record=apply(fixture);recover(fixture,record)
    refreshed=recapture(proof.account,NOW+timedelta(seconds=6))
    assert ledger.refresh_marks(refreshed,NOW+timedelta(seconds=6))["pilot_pnl"]==D("0.24")
    assert len(ledger.snapshot()["inception"]["account"]["positions"])==8


def test_source_hash_plan_mutation_and_duplicate_external_identity_fail(fixture):
    ledger,_,_,_,proof=fixture
    before=database_rows(ledger)
    bad=replace(proof,sources=(dict(proof.sources[0],body="changed"),*proof.sources[1:]))
    with pytest.raises(external.ExternalLifecycleError,match="source_bytes_changed"):
        apply(fixture,bad)
    with pytest.raises(external.ExternalLifecycleError,match="exact_reviewed_plan"):
        apply(fixture,expected="0"*64)
    assert database_rows(ledger)==before
    apply(fixture)
    changed=replace(proof,openings=(dict(proof.openings[0],quote_price="1745"),))
    with pytest.raises(external.ExternalLifecycleError,match="identity_conflict"):
        apply(fixture,changed)


@pytest.mark.parametrize("change,reason", [
    ({"cash_debit":"12.34"},"cash_debit"), ({"account_currency":"GBP"},"currency"),
    ({"occurred_at_source":"2026-10-23T11:59:59Z"},"time"),
    ({"occurred_at_source":"2026-10-23T12:00:02"},"source_time"),
    ({"source_reference":"missing"},"source"), ({"position_id":"1"},"identity"),
])
def test_external_opening_identity_currency_debit_time_fail_closed(fixture,change,reason):
    ledger,_,_,_,proof=fixture;before=database_rows(ledger)
    with pytest.raises(external.ExternalLifecycleError,match=reason):
        apply(fixture,replace(proof,openings=(dict(proof.openings[0],**change),)))
    assert database_rows(ledger)==before


def test_cash_residual_unqualified_carrying_and_stale_account_rejected(fixture):
    ledger,_,_,_,proof=fixture;before=database_rows(ledger)
    with pytest.raises(external.ExternalLifecycleError,match="cash_residual"):
        apply(fixture,replace(proof,cash_events=()))
    with pytest.raises(ValueError,match="stale"):
        apply(fixture,now=NOW+timedelta(hours=1))
    carrying=replace(proof.account.positions[0],carrying_fees=D("1"))
    account=Account(cash=proof.account.cash,positions=(carrying,),now=proof.account.capture_completed_at)
    with pytest.raises(external.ExternalLifecycleError,match="carrying"):
        apply(fixture,replace(proof,account=account))
    assert database_rows(ledger)==before


def test_loss_stop_latches_on_external_account_mark_and_never_resets(fixture):
    ledger,_,_,_,proof=fixture
    # Capital > loss keeps account exposure positive, allowing a valid broker
    # source fixture while still demonstrating an attributed loss below -25.
    p=replace(proof.account.positions[0],principal=D("100"),pnl=D("-30"),exposure=D("70"))
    account=Account(cash=D("17926.56"),positions=(p,),now=proof.account.capture_completed_at)
    changed=replace(proof,account=account,openings=(dict(proof.openings[0],cash_debit="100.03"),))
    record=apply(fixture,changed);recover(fixture,record)
    assert ledger.snapshot()["loss_stopped"] and ledger.snapshot()["pilot_pnl"]==D("-30.03")
    positive=recapture(account,NOW+timedelta(seconds=6),positions=(replace(p,pnl=D("3"),exposure=D("103")),))
    assert ledger.refresh_marks(positive,NOW+timedelta(seconds=6))["loss_stopped"]


def test_journal_failure_after_economic_commit_holds_next_claim_until_recovery(fixture,monkeypatch):
    ledger,_,_,journal,proof=fixture
    record=apply(fixture);append=journal._append
    def fail(*args,**kwargs):raise OSError("synthetic journal disk failure")
    monkeypatch.setattr(journal,"_append",fail)
    with pytest.raises(OSError):recover(fixture,record)
    with pytest.raises(PilotLedgerError,match="unresolved"):
        ledger.reserve_and_claim("new",D("10"),D("0"),proof.account,NOW+timedelta(seconds=5))
    assert not ledger.snapshot()["submissions"]
    monkeypatch.setattr(journal,"_append",append)
    recover(fixture,record)
    assert not ledger.snapshot()["unresolved"]


def test_crash_after_journal_commit_is_recovered_without_duplicate_event(fixture,monkeypatch):
    ledger,_,_,journal,_=fixture;record=apply(fixture)
    complete=ledger._mark_external_journal_complete
    def fail(*args,**kwargs):raise OSError("synthetic completion crash")
    monkeypatch.setattr(ledger,"_mark_external_journal_complete",fail)
    with pytest.raises(OSError):recover(fixture,record)
    assert ledger.snapshot()["unresolved"] and len(journal.events())==1
    monkeypatch.setattr(ledger,"_mark_external_journal_complete",complete)
    recover(fixture,record)
    assert not ledger.snapshot()["unresolved"] and len(journal.events())==1


def test_journal_corruption_or_wrong_reference_never_releases_gate(fixture):
    ledger,_,_,journal,_=fixture;record=apply(fixture)
    journal.record_external_import(record["event_id"])
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE head SET digest=?",("0"*64,))
    before=database_rows(ledger)
    with pytest.raises(ExecutionJournalError):recover(fixture,record)
    assert database_rows(ledger)==before and ledger.snapshot()["unresolved"]


def test_atomic_db_failure_rolls_back_entire_external_import(fixture,monkeypatch):
    ledger=fixture[0];before=database_rows(ledger);append=ledger._append
    def fail(db,kind,command,value):
        append(db,kind,command,value)
        if kind=="external_lifecycle":raise OSError("synthetic precommit crash")
    monkeypatch.setattr(ledger,"_append",fail)
    with pytest.raises(OSError):apply(fixture)
    assert database_rows(ledger)==before


def test_switch_disengagement_or_change_before_commit_rolls_back(fixture,monkeypatch):
    ledger,_,switch,_,_=fixture;before=database_rows(ledger);append=ledger._append
    def reset(db,kind,command,value):
        append(db,kind,command,value)
        if kind=="external_lifecycle":
            switch.reset(operator_id="synthetic",reason="fault",acknowledgement=RESET_ACKNOWLEDGEMENT,now=NOW+timedelta(seconds=4))
    monkeypatch.setattr(ledger,"_append",reset)
    with pytest.raises(external.ExternalLifecycleError,match="switch_changed_before_commit"):
        apply(fixture)
    assert database_rows(ledger)==before
    with pytest.raises(external.ExternalLifecycleError,match="engaged_switch"):
        apply(fixture)


def test_global_decimal_context_does_not_change_external_money(fixture):
    with localcontext() as context:
        context.prec=3
        record=apply(fixture)
    assert fixture[0].snapshot()["committed_capital"]==D("12.37")
    assert fixture[0].snapshot()["pilot_pnl"]==D("0.24")
    recover(fixture,record)


def test_valid_old_journal_rollback_blocks_readiness_and_direct_next_claim(fixture):
    ledger,_,_,journal,proof=fixture
    old=journal.path.read_bytes()
    record=apply(fixture);recover(fixture,record)
    journal.path.write_bytes(old)
    with pytest.raises(PilotLedgerError,match="completed_journal_unavailable"):
        journal.ensure_ready()
    before=database_rows(ledger)
    with pytest.raises(PilotLedgerError,match="completed_journal_unavailable"):
        ledger.reserve_and_claim("new",D("10"),D("0"),proof.account,NOW+timedelta(seconds=6))
    with pytest.raises(PilotLedgerError,match="completed_journal_unavailable"):
        recover(fixture,record)
    assert database_rows(ledger)==before


@pytest.mark.parametrize("kind", ["cash_adjustment", "dividend", "fee", "carrying_fee"])
def test_balanced_unknown_cash_semantics_are_never_a_cash_plug(fixture,kind):
    before=database_rows(fixture[0]);proof=fixture[4]
    changed=replace(proof,cash_events=(dict(proof.cash_events[0],kind=kind),))
    with pytest.raises(external.ExternalLifecycleError,match="cash_event_identity_or_kind"):
        apply(fixture,changed)
    assert database_rows(fixture[0])==before


@pytest.mark.parametrize("changes,reason", [
    ({"total_commitment":"20","per_order_cap":"20","loss_stop":"5"},"combined_capital_cap"),
    ({"max_positions":1},"position_cap"),
    ({"max_per_day":1,"max_submissions":1},"daily_submission_budget"),
])
def test_next_finite_claim_counts_external_capital_positions_and_same_day_budget(fixture,changes,reason):
    ledger,_,_,_,proof=fixture;record=apply(fixture);recover(fixture,record)
    at=NOW+timedelta(seconds=6)
    enable(ledger,at)
    _,mandate=make(ledger.path.parent,ledger=ledger,at=at,**changes)
    with pytest.raises(PilotLedgerError,match=reason):
        ledger.reserve_under_mandate(mandate,candidate(),recapture(proof.account,at),at)
    assert ledger.snapshot()["submissions"]==()


def original_resolved_claim(fixture, *, order="700", finite=False):
    ledger,_,_,_,proof=fixture
    pre=Account(positions=tuple(Position(position_id=str(i),instrument_id=9+i) for i in range(1,9)))
    if finite:
        enable(ledger)
        _,mandate=make(ledger.path.parent,ledger=ledger,loss_stop="5")
        ledger.reserve_under_mandate(mandate,candidate("original",amount="10"),pre,NOW)
    else:
        ledger.reserve_and_claim("original",D("10"),D("0"),pre,NOW,instrument_id=1)
    fill=replace(execution("100","10","0",NOW+timedelta(microseconds=100000)),broker_order_id=order)
    post=post_capture(pre,fill,NOW+timedelta(microseconds=200000))
    ledger.reconcile_open("original",(fill,),pre,post,NOW+timedelta(microseconds=200000))
    if finite:
        ledger.mark_journal_complete("original",journal_reference="synthetic-original-receipt",now=NOW+timedelta(seconds=1))
    current=Account(cash=proof.account.cash-D("10"),positions=(*proof.account.positions,next(p for p in post.positions if p.position_id=="100")),
        now=proof.account.capture_completed_at)
    return replace(proof,account=current,prior_account_hash=payload_hash(external._clean(post)))


def test_nonempty_original_claim_and_history_are_preserved_and_combined(fixture):
    ledger=fixture[0];proof=original_resolved_claim(fixture)
    before=database_rows(ledger)
    record=apply(fixture,proof);recover(fixture,record)
    after=database_rows(ledger)
    assert after["submissions"]==before["submissions"] and len(after["submissions"])==1
    assert after["inception"]==before["inception"] and after["events"][:len(before["events"])]==before["events"]
    state=ledger.snapshot()
    assert state["position_count"]==2 and state["committed_capital"]==D("22.37")
    assert len(state["external_openings"])==1 and len(state["submissions"])==1


def test_existing_own_order_cannot_be_relabelled_as_external(fixture):
    proof=original_resolved_claim(fixture,order="500");before=database_rows(fixture[0])
    with pytest.raises(PilotLedgerError,match="external_lifecycle_integrity"):
        apply(fixture,proof)
    assert database_rows(fixture[0])==before


def test_prior_stricter_finite_loss_limit_latches_on_external_import(fixture):
    ledger=fixture[0];proof=original_resolved_claim(fixture,finite=True)
    p=replace(next(p for p in proof.account.positions if p.position_id=="101"),pnl=D("-6"),exposure=D("6.34"))
    owned=next(p for p in proof.account.positions if p.position_id=="100")
    proof=replace(proof,account=recapture(proof.account,proof.account.capture_completed_at,
        positions=(p,owned)))
    record=apply(fixture,proof);recover(fixture,record)
    assert ledger.snapshot()["loss_stopped"] and ledger.snapshot()["pilot_pnl"]==D("-6.03")
    positive=recapture(proof.account,NOW+timedelta(seconds=6),positions=(replace(p,pnl=D("3")),owned))
    assert ledger.refresh_marks(positive,NOW+timedelta(seconds=6))["loss_stopped"]


def next_proof(fixture, *, order="501", cash_id="second-deposit", event_time=None):
    old=fixture[4]
    extra=replace(old.account.positions[0],position_id="102")
    at=NOW+timedelta(seconds=8)
    account=Account(cash=old.account.cash-D("12.37")+D("2"),positions=(*old.account.positions,extra),now=at)
    opening=dict(old.openings[0],position_id="102",order_id=order,
        occurred_at_source=(event_time or NOW+timedelta(seconds=7)).isoformat())
    cash=dict(old.cash_events[0],event_id=cash_id,kind="deposit",amount="2",
        occurred_at_source=(event_time or NOW+timedelta(seconds=7)).isoformat())
    return replace(old,account=account,prior_account_hash=payload_hash(external._clean(old.account)),
        closures=(),openings=(opening,),cash_events=(cash,))


@pytest.mark.parametrize("changes", [{"order":"500"},{"cash_id":"synthetic-withdrawal"}])
def test_separate_imports_cannot_reuse_order_or_cash_event_identity(fixture,changes):
    record=apply(fixture);recover(fixture,record)
    before=database_rows(fixture[0])
    with pytest.raises(PilotLedgerError,match="external_lifecycle_integrity"):
        apply(fixture,next_proof(fixture,**changes),now=NOW+timedelta(seconds=9))
    assert database_rows(fixture[0])==before


def test_later_import_and_marks_cannot_regress_account_source_cutoff(fixture):
    ledger,_,_,_,proof=fixture;record=apply(fixture);recover(fixture,record)
    with pytest.raises(external.ExternalLifecycleError,match="source_currency_or_time"):
        apply(fixture,next_proof(fixture,event_time=NOW+timedelta(seconds=2)),now=NOW+timedelta(seconds=9))
    # Same composition and money cannot hide an older capture.
    with pytest.raises(PilotLedgerError,match="source_precedes_external_checkpoint"):
        ledger.refresh_marks(recapture(proof.account,NOW+timedelta(seconds=2)),NOW+timedelta(seconds=6))


def test_future_finite_opening_combines_economics_without_external_order_reuse(fixture):
    ledger,_,_,_,proof=fixture;record=apply(fixture);recover(fixture,record)
    at=NOW+timedelta(seconds=6);enable(ledger,at)
    _,mandate=make(ledger.path.parent,ledger=ledger,at=at)
    pre=recapture(proof.account,at)
    ledger.reserve_under_mandate(mandate,candidate("future"),pre,at)
    own=replace(execution("200","10","0",at+timedelta(seconds=1)),broker_order_id="500")
    with pytest.raises(PilotLedgerError,match="already_attributed_external"):
        ledger.reconcile_open("future",(own,),pre,post_capture(pre,own,at+timedelta(seconds=2)),at+timedelta(seconds=2))
    own=replace(own,broker_order_id="900")
    state=ledger.reconcile_open("future",(own,),pre,post_capture(pre,own,at+timedelta(seconds=2)),at+timedelta(seconds=2))
    assert state["committed_capital"]==D("22.37") and state["position_count"]==2
    assert state["pilot_pnl"]==D("0.24") and state["unresolved"]  # future command still owes its ordinary journal


def test_nested_same_runtime_journal_can_complete_and_recover(fixture):
    ledger,store,_,_,_=fixture;record=apply(fixture)
    nested=ExecutionJournal.create(ledger.path.parent/"audit"/"journal.sqlite3",ledger=ledger,store=store,runtime_root=ledger.path.parent)
    external.recover_external_journal(ledger,nested,event_id=record["event_id"],now=NOW+timedelta(seconds=5))
    assert not ledger.snapshot()["unresolved"] and nested.ensure_ready()


def test_completion_readback_failure_rolls_back_marker_and_remains_recoverable(fixture,monkeypatch):
    ledger,_,_,journal,_=fixture;record=apply(fixture)
    journal.record_external_import(record["event_id"])
    before=database_rows(ledger)
    with monkeypatch.context() as patch:
        def fail(**kwargs):raise ExecutionJournalError("synthetic reverse readback failure")
        patch.setattr(ExecutionJournal,"verify_external_receipts",fail)
        with pytest.raises(PilotLedgerError,match="completed_journal_unavailable"):
            recover(fixture,record)
    assert database_rows(ledger)==before and ledger.snapshot()["unresolved"]
    recover(fixture,record)
    assert not ledger.snapshot()["unresolved"]


def test_distinct_second_event_adds_capital_and_recovers_without_rewriting_history(fixture):
    ledger=fixture[0];record=apply(fixture);recover(fixture,record)
    before=database_rows(ledger)
    record2=apply(fixture,next_proof(fixture),now=NOW+timedelta(seconds=9))
    external.recover_external_journal(ledger,fixture[3],event_id=record2["event_id"],now=NOW+timedelta(seconds=10))
    state=ledger.snapshot()
    assert len(state["external_imports"])==2 and state["position_count"]==2
    assert state["committed_capital"]==D("24.74") and state["pilot_pnl"]==D("0.48")
    assert not state["unresolved"] and state["submissions"]==()
    assert database_rows(ledger)["events"][:len(before["events"])]==before["events"]


def test_mark_cannot_absorb_unqualified_cash_change_into_next_external_checkpoint(fixture):
    ledger,_,_,_,proof=fixture;record=apply(fixture);recover(fixture,record)
    at=NOW+timedelta(seconds=6)
    changed=recapture(proof.account,at,cash=proof.account.cash-D("1"))
    with pytest.raises(PilotLedgerError,match="external_cash_movement_evidence_required"):
        ledger.refresh_marks(changed,at)
    assert ledger.snapshot()["unresolved"]
    with closing(ledger._connect()) as db:
        assert ledger._state(db)["_account_checkpoint"]["cash"]==str(proof.account.cash)
