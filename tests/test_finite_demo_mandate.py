"""Finite authority and same-ledger lifecycle tests use synthetic broker data."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import sqlite3

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger, PilotLedgerError
from brokers.base import BrokerReconciliationResult
from execution.domain import canonical_json, payload_hash
from execution.finite_demo_mandate import FiniteDemoMandate, FiniteDemoMandateError, FiniteOrderCandidate
from tests.test_pilot_ledger import Account, NOW, execution, fill_order, post_capture, recapture

D = Decimal
COMMIT = "a" * 40


def scope(**changes):
    value = {"instruments": [
        {"instrument_id": 1, "symbol": "SYNTH", "exchange": "NASDAQ", "asset_type": "equity",
         "economic_class": "equity", "currency": "USD", "issuer": "Synthetic issuer",
         "sector": "technology", "class_source_reference": "synthetic:instrument-1"},
        {"instrument_id": 2, "symbol": "SYNTHBOND", "exchange": "NYSE", "asset_type": "etf",
         "economic_class": "fixed_income", "currency": "USD", "issuer": "Synthetic fund",
         "sector": "bond-fund", "class_source_reference": "synthetic:instrument-2-fund-holdings"}],
        "max_submissions": 3, "max_per_day": 4, "per_order_cap": "100", "total_commitment": "500",
        "max_positions": 5, "min_cash_fraction": "0.20", "loss_stop": "25", "order_types": ["market"]}
    value["max_per_day"] = 3
    value.update(changes)
    return value


def make(tmp_path, *, ledger=None, at=NOW, **changes):
    ledger = ledger or PilotLedger.create(tmp_path / "pilot.sqlite3", Account(), [], NOW)
    mandate = FiniteDemoMandate.create(tmp_path / "finite.json", account_id="123", ledger=ledger,
        source_reference="synthetic finite authorization", issued_at=at, expires_at=at+timedelta(days=3),
        reviewed_commit=COMMIT, scope=scope(**changes))
    return ledger, mandate


def enable(ledger, at=NOW):
    return ledger.enable_finite_mandates(source_reference="synthetic reviewed migration", reviewed_commit=COMMIT, now=at)


def candidate(command="first", instrument=1, amount="10", fees="0"):
    return FiniteOrderCandidate(command, instrument, D(amount), D(fees), "b" * 64)


def terminal(command, *, fill=None, status="3", remainder="true"):
    lifecycle = {"3": "filled", "5": "partially_filled", "9": "partially_filled_terminal",
                 "10": "partially_filled_terminal", "7": "cancelled", "8": "rejected", "4": "rejected"}[status]
    return BrokerReconciliationResult(command, True, lifecycle, fill.broker_order_id if fill else "order-none",
        fill.broker_position_id if fill else None,
        (("status_id", status), ("status_name", "synthetic-terminal"), ("remainder_terminal", remainder),
         ("lookup_sha256", "c" * 64), ("connector_broker_reference_id", fill.broker_reference_id if fill else "reference-none")),
        (fill,) if fill else ())


def complete(ledger, mandate, pre, command, at, *, instrument=1, partial=False, journal=True):
    pre = recapture(pre, at)
    ledger.reserve_under_mandate(mandate, candidate(command, instrument), pre, at)
    fill = execution(str(100+len(ledger.snapshot()["submissions"])), "9" if partial else "10", "0",
                     at+timedelta(seconds=1), instrument_id=instrument)
    post = post_capture(pre, fill, at+timedelta(seconds=2))
    lookup = terminal(command, fill=fill, status="9" if partial else "3")
    ledger.reconcile_open(command, (fill,), pre, post, at+timedelta(seconds=2),
                          terminal_partial=partial, terminal_evidence=lookup)
    if journal:
        ledger.mark_journal_complete(command, journal_reference="synthetic durable journal:"+command,
                                    now=at+timedelta(seconds=2))
    return post, fill, pre


def test_create_expiring_immutable_authority_and_scope_copy(tmp_path):
    ledger, mandate = make(tmp_path)
    original = mandate.path.read_bytes()
    assert FiniteDemoMandate.load(mandate.path, expected_account_id="123") == mandate
    assert mandate.path.stat().st_mode & 0o777 == 0o600
    modified = mandate.scope
    modified["max_submissions"] = 100
    assert mandate.scope["max_submissions"] == 3
    with pytest.raises(FiniteDemoMandateError, match="never_overwrite"):
        FiniteDemoMandate.create(mandate.path, account_id="123", ledger=ledger, source_reference="new",
            issued_at=NOW, expires_at=NOW+timedelta(days=1), reviewed_commit=COMMIT, scope=scope())
    assert mandate.path.read_bytes() == original
    for now in (NOW-timedelta(seconds=1), mandate.expires_at):
        with pytest.raises(FiniteDemoMandateError, match="authorized_time"):
            mandate.validate_binding(ledger=ledger, now=now)
    with pytest.raises(FiniteDemoMandateError, match="reviewed_commit"):
        mandate.validate_binding(ledger=ledger, now=NOW, reviewed_commit="d"*40)


@pytest.mark.parametrize("changes", [
    {"max_submissions": True}, {"max_per_day": 0}, {"max_positions": 6},
    {"per_order_cap": "100.01"}, {"total_commitment": "500.01"}, {"loss_stop": "25.01"},
    {"min_cash_fraction": "0.1999"}, {"per_order_cap": 10.0}, {"order_types": ["limitIOC"]},
    {"max_submissions": 0}, {"loss_stop": "0"}, {"min_cash_fraction": "NaN"}])
def test_request_cannot_weaken_code_bounds(tmp_path, changes):
    with pytest.raises(FiniteDemoMandateError):
        make(tmp_path, **changes)
    assert not (tmp_path / "finite.json").exists()


def test_instruments_need_explicit_supported_classification(tmp_path):
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", Account(), [], NOW)
    for changes in ({"currency": "GBP"}, {"asset_type": "crypto"}, {"economic_class": "crypto"},
                    {"class_source_reference": ""}):
        instruments = scope()["instruments"]
        instruments[0].update(changes)
        with pytest.raises(FiniteDemoMandateError):
            make(tmp_path, ledger=ledger, instruments=instruments)


def test_migration_preserves_inception_claim_bytes_and_v1_limits(tmp_path):
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", Account(), [], NOW)
    post, _, _ = fill_order(ledger, amount="10", fees="0")
    with sqlite3.connect(ledger.path) as db:
        before = list(db.execute("SELECT rowid,* FROM submissions"))
        inception = list(db.execute("SELECT * FROM inception"))
    at = NOW+timedelta(seconds=3)
    _, mandate = make(tmp_path, ledger=ledger, at=at)
    assert mandate.baseline_claims[0][0] == "first"
    receipt = enable(ledger, at)
    assert enable(ledger, at) == receipt
    with sqlite3.connect(ledger.path) as db:
        assert list(db.execute("SELECT rowid,* FROM submissions")) == before
        assert list(db.execute("SELECT * FROM inception")) == inception
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM submissions")
    with pytest.raises(PilotLedgerError, match="one_opening_submission"):
        ledger.reserve_and_claim("legacy-two", D("10"), D("0"), recapture(post, at), at)
    ledger.reserve_under_mandate(mandate, candidate("second", 2), recapture(post, at), at)
    assert len(ledger.snapshot()["submissions"]) == 2
    mandate.validate_binding(ledger=ledger, now=at)


def test_no_implicit_migration_and_no_scope_or_float_bypass(tmp_path):
    ledger, mandate = make(tmp_path)
    with pytest.raises(PilotLedgerError, match="explicit_finite_migration"):
        ledger.reserve_under_mandate(mandate, candidate(), Account(), NOW)
    enable(ledger)
    for bad in (replace(candidate(), instrument_id=1142), replace(candidate(), amount=1.0),
                replace(candidate(), side="sell"), replace(candidate(), intent_hash="")):
        with pytest.raises(FiniteDemoMandateError):
            ledger.reserve_under_mandate(mandate, bad, Account(), NOW)
    assert ledger.snapshot()["submissions"] == ()


def test_multimarket_same_day_only_after_reconcile_and_journal(tmp_path):
    ledger, mandate = make(tmp_path)
    enable(ledger)
    post, _, _ = complete(ledger, mandate, Account(), "first", NOW, journal=False)
    at = NOW+timedelta(seconds=3)
    with pytest.raises(PilotLedgerError, match="unresolved.*journal"):
        ledger.reserve_under_mandate(mandate, candidate("second", 2), recapture(post, at), at)
    receipt = ledger.reconciliation_receipt("first")
    assert "journal_complete" not in receipt["claim"] and "reconciled" not in receipt["claim"]
    ledger.mark_journal_complete("first", journal_reference="synthetic hash", now=at)
    assert ledger.reconciliation_receipt("first") == receipt
    post, _, _ = complete(ledger, mandate, post, "second", at, instrument=2)
    assert [s["instrument_id"] for s in ledger.snapshot()["submissions"]] == [1, 2]
    assert ledger.snapshot()["committed_capital"] == 20
    assert not ledger.snapshot()["unresolved"]


def test_one_atomic_claim_across_concurrent_workers(tmp_path):
    ledger, mandate = make(tmp_path)
    enable(ledger)
    def claim(name):
        try:
            PilotLedger(ledger.path, "123").reserve_under_mandate(mandate, candidate(name), Account(), NOW)
            return True
        except PilotLedgerError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(claim, ["one", "two"])) == 1
    assert len(ledger.snapshot()["submissions"]) == 1


def test_unknown_consumes_attempt_and_cannot_be_replaced(tmp_path):
    ledger, mandate = make(tmp_path)
    enable(ledger)
    ledger.reserve_under_mandate(mandate, candidate(), Account(), NOW)
    ledger.record_outcome("first", {"status": "unknown"})
    for proposed in (candidate(), candidate("replacement")):
        with pytest.raises(PilotLedgerError, match="lookup_only"):
            ledger.reserve_under_mandate(mandate, proposed, Account(), NOW)
    with pytest.raises(PilotLedgerError, match="economic_reconciliation"):
        ledger.mark_journal_complete("first", journal_reference="forged completion", now=NOW)


def test_terminal_partial_requires_broker_and_actual_cash_evidence(tmp_path):
    ledger, mandate = make(tmp_path)
    enable(ledger)
    pre = Account()
    ledger.reserve_under_mandate(mandate, candidate(), pre, NOW)
    fill = execution("101", "9", "0", NOW+timedelta(seconds=1))
    post = post_capture(pre, fill, NOW+timedelta(seconds=2))
    for result in (None, terminal("first", fill=fill, status="5"),
                   terminal("first", fill=fill, status="9", remainder="false"),
                   terminal("other", fill=fill, status="9")):
        with pytest.raises(PilotLedgerError):
            ledger.reconcile_open("first", (fill,), pre, post, NOW+timedelta(seconds=2),
                terminal_partial=True, terminal_evidence=result)
    with pytest.raises(PilotLedgerError, match="terminal_full_requested"):
        ledger.reconcile_open("first", (fill,), pre, post, NOW+timedelta(seconds=2))
    lookup = terminal("first", fill=fill, status="9")
    result = ledger.reconcile_open("first", (fill,), pre, post, NOW+timedelta(seconds=2),
        terminal_partial=True, terminal_evidence=lookup)
    assert result["committed_capital"] == 9
    assert ledger.reconciliation_receipt("first")["reconciliation"]["terminal_partial"] is True
    assert result["unresolved"]  # Journal acknowledgement still pending.


def test_terminal_partial_never_exceeds_claim_and_no_fill_needs_unchanged_cash(tmp_path):
    ledger, mandate = make(tmp_path, max_submissions=1, max_per_day=1)
    enable(ledger)
    pre = Account()
    ledger.reserve_under_mandate(mandate, candidate(), pre, NOW)
    oversized = execution("101", "11", "0", NOW+timedelta(seconds=1))
    post = post_capture(pre, oversized, NOW+timedelta(seconds=2))
    with pytest.raises(PilotLedgerError, match="outside_claim"):
        ledger.reconcile_open("first", (oversized,), pre, post, NOW+timedelta(seconds=2),
            terminal_partial=True, terminal_evidence=terminal("first", fill=oversized, status="9"))
    at = NOW+timedelta(seconds=2)
    lookup = terminal("first", status="7")
    with pytest.raises(PilotLedgerError, match="no_fill_cash_changed"):
        ledger.reconcile_no_fill("first", pre, recapture(pre, at, cash=pre.cash-1), at, terminal_evidence=lookup)
    ledger.reconcile_no_fill("first", pre, recapture(pre, at), at, terminal_evidence=lookup)
    assert ledger.snapshot()["committed_capital"] == 0
    ledger.mark_journal_complete("first", journal_reference="durable reject journal", now=at)
    with pytest.raises(PilotLedgerError, match="submission_budget_exhausted"):
        ledger.reserve_under_mandate(mandate, candidate("second"), recapture(pre, at), at)


def test_new_manifest_cannot_reset_total_cap_or_existing_positions(tmp_path):
    ledger, first = make(tmp_path, total_commitment="100", per_order_cap="100")
    enable(ledger)
    post, _, _ = complete(ledger, first, Account(), "first", NOW)
    second = FiniteDemoMandate.create(tmp_path / "second.json", account_id="123", ledger=ledger,
        source_reference="synthetic second scope", issued_at=NOW+timedelta(seconds=3),
        expires_at=NOW+timedelta(days=1), reviewed_commit=COMMIT,
        scope=scope(total_commitment="15", per_order_cap="15"))
    at = NOW+timedelta(seconds=3)
    with pytest.raises(PilotLedgerError, match="combined_capital_cap"):
        ledger.reserve_under_mandate(second, candidate("second"), recapture(post, at), at)
    assert ledger.snapshot()["position_count"] == 1


def test_migration_and_authority_detect_disappeared_original_history(tmp_path):
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", Account(), [], NOW)
    fill_order(ledger, amount="10", fees="0")
    _, mandate = make(tmp_path, ledger=ledger, at=NOW+timedelta(seconds=3))
    # Directly supplied projected state cannot conceal the bound baseline.
    state = deepcopy(ledger.snapshot())
    state["submissions"] = ()
    with pytest.raises(FiniteDemoMandateError, match="original_claim_history"):
        mandate.validate_binding(ledger=ledger, now=NOW+timedelta(seconds=3), state=state)
    enable(ledger, NOW+timedelta(seconds=3))
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TRIGGER submissions_no_update")
        encoded, = db.execute("SELECT payload FROM submissions").fetchone()
        claim = json.loads(encoded)
        claim["intent_hash"] = "f"*64
        db.execute("UPDATE submissions SET payload=?,digest=?", (canonical_json(claim), payload_hash(claim)))
    with pytest.raises(PilotLedgerError, match="original_submission_history"):
        ledger.snapshot()


def test_tighter_loss_limit_latches_during_mark_and_survives_new_authority(tmp_path):
    ledger, mandate = make(tmp_path, loss_stop="5")
    enable(ledger)
    post, _, _ = complete(ledger, mandate, Account(), "first", NOW)
    at = NOW+timedelta(seconds=3)
    positions = tuple(replace(p, pnl=D("-5")) if p.position_id == "101" else p for p in post.positions)
    marked = recapture(post, at, positions=positions)
    state = ledger.refresh_marks(marked, at)
    assert state["loss_stopped"]
    rebound = FiniteDemoMandate.create(tmp_path / "second.json", account_id="123", ledger=ledger,
        source_reference="synthetic later scope cannot clear latched loss", issued_at=at,
        expires_at=at+timedelta(days=1), reviewed_commit=COMMIT, scope=scope(loss_stop="25"))
    with pytest.raises(PilotLedgerError, match="loss_stop_latched"):
        ledger.reserve_under_mandate(rebound, candidate("second"), marked, at)


def test_journal_completion_is_replayable_only_for_same_durable_reference(tmp_path):
    ledger, mandate = make(tmp_path)
    enable(ledger)
    complete(ledger, mandate, Account(), "first", NOW)
    state = ledger.snapshot()
    assert ledger.mark_journal_complete("first", journal_reference="synthetic durable journal:first",
                                       now=NOW+timedelta(seconds=3)) == state
    with pytest.raises(PilotLedgerError, match="reference_changed"):
        ledger.mark_journal_complete("first", journal_reference="other journal", now=NOW+timedelta(seconds=3))
