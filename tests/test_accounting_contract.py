"""Synthetic performance contract tests; no broker qualification is claimed."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from execution.accounting import (
    AccountValuation, AccountingEvent, AccountingEvidenceError, EventKind,
    HistoryCoverage, REQUIRED_EVENT_KINDS, COMPONENT_TIME_MODEL,
    evaluate_performance,
)
from execution.accounting_baseline import AccountPerformanceStore

D = Decimal
T0 = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)  # 01:00 BST; DST change day.
MIDNIGHT = datetime(2026, 10, 26, 0, tzinfo=timezone.utc)


def valuation(equity="1000", at=MIDNIGHT, **kwargs):
    values = dict(account_id="demo-synthetic", snapshot_id=at.isoformat(), currency="USD",
                  cash=D(equity), invested_amount=D("0"), unrealised_pnl=D("0"),
                  equity=D(equity), broker_margin=D("0"), gross_exposure=D("0"),
                  reconciliation_reference="synthetic-reconciled-account",
                  time_evidence_hash="synthetic-clock-digest", valuation_at=at)
    values.update(kwargs)
    return AccountValuation(**values)


def history(start=MIDNIGHT, end=None, events=(), **kwargs):
    values = dict(account_id="demo-synthetic", currency="USD", start_at=start,
                  end_at=end or start + timedelta(hours=1), start_cursor="c0", end_cursor="c1",
                  source_reference="synthetic-history", source_contract="synthetic-contract-v1",
                  raw_evidence_hash="synthetic-history-digest", events=events,
                  covered_kinds=REQUIRED_EVENT_KINDS, complete=True,
                  broker_history_verified=True, valuation_path_complete=True)
    values.update(kwargs)
    return HistoryCoverage(**values)


def event(kind, amount, at, identifier=None, **kwargs):
    return AccountingEvent(identifier or str(kind), kind, at, D(amount), "USD", "synthetic-event", **kwargs)


def measure(opening, current, covered=None, path=(), **kwargs):
    return evaluate_performance(opening=opening, current=current, history=covered,
                                observations=path, now=current.coverage_at, **kwargs)


def test_equity_identity_separates_invested_margin_and_exposure():
    good = valuation("1010", cash=D("900"), invested_amount=D("100"),
                     unrealised_pnl=D("10"), broker_margin=D("40"), gross_exposure=D("250"))
    assert good.equity == D("1010")
    with pytest.raises(AccountingEvidenceError, match="identity"):
        replace(good, invested_amount=good.broker_margin)
    with pytest.raises(AccountingEvidenceError, match="finite_decimal"):
        replace(good, cash=900.0)
    # Preserve small input digits beside large balances; default Decimal
    # context precision must not truncate the authoritative equity identity.
    exact = "1000000000000000000000000000000.000000000001"
    valuation(exact, cash=D("1000000000000000000000000000000"),
              invested_amount=D("0.000000000001"))


def test_daily_loss_is_cash_flow_adjusted_with_fixed_opening_denominator():
    start = valuation()
    end = start.coverage_at + timedelta(hours=1)
    deposit_time = end - timedelta(minutes=1)
    preflow = valuation("900", at=deposit_time)
    deposit = event(EventKind.DEPOSIT, "500", deposit_time, pre_flow_valuation=preflow)
    current = valuation("1400", end)
    result = measure(start, current, history(end=end, events=(deposit,)))
    assert result.daily_pnl == D("-100")
    assert result.daily_loss_denominator == D("1000")
    assert result.daily_loss_fraction == D("0.1")
    assert result.drawdown_fraction == D("0.1")
    assert result.qualified


def test_withdrawal_does_not_create_loss_or_hide_earlier_loss():
    start = valuation()
    end = MIDNIGHT + timedelta(hours=1)
    at = end - timedelta(minutes=1)
    withdrawn = event(EventKind.WITHDRAWAL, "-200", at, pre_flow_valuation=valuation("900", at))
    result = measure(start, valuation("700", end), history(end=end, events=(withdrawn,)))
    assert result.daily_pnl == D("-100")
    assert abs(result.drawdown_fraction - D("0.1")) < D("1e-48")


@pytest.mark.parametrize("kind,amount,extras", [
    (EventKind.DIVIDEND, "5", {}), (EventKind.FEE, "-5", {}),
    (EventKind.FINANCING, "-5", {}), (EventKind.PURCHASE, "-100", {}),
    (EventKind.SALE, "100", {}),
    (EventKind.CURRENCY_CONVERSION, "5", {"original_amount": D("4"), "original_currency": "GBP", "conversion_rate": D("1.25")}),
])
def test_internal_movements_and_performance_events_are_not_external_flows(kind, amount, extras):
    start = valuation()
    end = MIDNIGHT + timedelta(hours=1)
    item = event(kind, amount, end, **extras)
    result = measure(start, valuation("995", end), history(end=end, events=(item,)))
    assert item.external_amount == 0
    assert result.daily_pnl == D("-5")


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"broker_history_verified": False},
    {"covered_kinds": REQUIRED_EVENT_KINDS - {EventKind.RESET}},
    {"start_at": MIDNIGHT - timedelta(seconds=1)},
    {"end_at": MIDNIGHT + timedelta(hours=1, seconds=1)},
])
def test_incomplete_history_never_becomes_zero_performance(changes):
    end = MIDNIGHT + timedelta(hours=1)
    result = measure(valuation(), valuation("990", end), history(end=end, **changes)) if "end_at" not in changes else measure(valuation(), valuation("990", end), history(**changes))
    assert not result.qualified and not result.history_complete
    assert result.daily_pnl is None and result.drawdown_fraction is None


def test_missing_history_reset_or_midday_baseline_remains_unknown():
    end = MIDNIGHT + timedelta(hours=1)
    current = valuation("990", end)
    missing = measure(valuation(), current)
    assert missing.daily_pnl is None and "account_history_missing" in missing.reasons
    reset = event(EventKind.RESET, "0", end)
    result = measure(valuation(), current, history(end=end, events=(reset,)))
    assert result.daily_pnl is None and "account_reset_breaks_continuity" in result.reasons
    start = MIDNIGHT + timedelta(minutes=1)
    result = measure(valuation(at=start), current, history(start=start, end=end))
    assert result.daily_pnl is None
    assert result.drawdown_fraction == D("0.01")
    assert "exact_risk_day_boundary_valuation_missing" in result.reasons


def test_flow_needs_preflow_valuation_even_if_net_flows_offset():
    end = MIDNIGHT + timedelta(hours=1)
    events = (event(EventKind.DEPOSIT, "100", end - timedelta(minutes=1)),
              event(EventKind.WITHDRAWAL, "-100", end - timedelta(seconds=1)))
    result = measure(valuation(), valuation("950", end), history(end=end, events=events))
    assert result.daily_pnl == D("-50")
    assert result.drawdown_fraction is None
    assert "external_flow_immediate_preflow_valuation_missing" in result.reasons


def test_full_transaction_history_does_not_prove_full_peak_valuation_history():
    end = MIDNIGHT + timedelta(hours=1)
    result = measure(valuation(), valuation("990", end), history(end=end, valuation_path_complete=False))
    assert result.daily_pnl == D("-10")
    assert result.peak_equity_index == D("1")
    assert result.drawdown_fraction is None and not result.qualified


def test_daily_boundary_changes_without_erasing_inception_peak():
    start = MIDNIGHT - timedelta(days=1, hours=1)  # Midnight BST before DST change.
    opening = valuation(at=start)
    daily_boundary = valuation("800", MIDNIGHT)
    current = valuation("850", MIDNIGHT + timedelta(hours=1))
    result = measure(opening, current, history(start=start, end=current.coverage_at), (daily_boundary,))
    assert result.daily_pnl == D("50")
    assert result.daily_loss_denominator == D("800")
    assert result.drawdown_fraction == D("0.15")
    assert result.coverage_start_at == start


def test_component_clock_cannot_claim_midnight_or_hide_staleness():
    start = valuation()
    end = MIDNIGHT + timedelta(hours=1)
    component = valuation("990", end, valuation_at=None, time_model=COMPONENT_TIME_MODEL,
                          component_start_at=end - timedelta(seconds=2), component_end_at=end)
    result = measure(start, component, history(end=end))
    assert result.qualified and component.valuation_at is None
    stale = evaluate_performance(opening=start, current=component, history=history(end=end),
                                 now=end + timedelta(seconds=14))
    assert not stale.qualified and "current_valuation_stale_or_future" in stale.reasons
    with pytest.raises(AccountingEvidenceError, match="cannot_claim_atomic"):
        replace(component, valuation_at=end)
    component_boundary = replace(start, valuation_at=None, time_model=COMPONENT_TIME_MODEL,
                                 component_start_at=MIDNIGHT - timedelta(seconds=1), component_end_at=MIDNIGHT)
    result = measure(component_boundary, component, history(end=end))
    assert result.daily_pnl is None


def test_duplicates_foreign_currency_and_incomplete_fx_evidence_reject():
    end = MIDNIGHT + timedelta(hours=1)
    item = event(EventKind.FEE, "-1", end)
    with pytest.raises(AccountingEvidenceError, match="duplicate"):
        history(end=end, events=(item, item))
    with pytest.raises(AccountingEvidenceError, match="event_account_currency"):
        replace(item, currency="GBP")
    with pytest.raises(AccountingEvidenceError, match="conversion_evidence"):
        event(EventKind.CURRENCY_CONVERSION, "0", end)
    with pytest.raises(AccountingEvidenceError, match="complete_currency"):
        replace(item, original_amount=D("1"))


def test_append_only_performance_store_pins_losses_and_replay_times(tmp_path):
    path = tmp_path / "performance.sqlite3"
    store = AccountPerformanceStore(path, account_id="demo-synthetic")
    start = valuation()
    first = store.record(start, now=MIDNIGHT)
    assert first.daily_pnl is None and not first.qualified
    peak_at = MIDNIGHT + timedelta(hours=1)
    peak = valuation("1100", peak_at)
    store.record(peak, now=peak_at, history=history(end=peak_at))
    end = peak_at + timedelta(hours=1)
    current = valuation("990", end)
    evidence = store.record(current, now=end, history=history(end=end, end_cursor="c2"))
    assert evidence.daily_pnl == D("-10") and evidence.drawdown_fraction == D("0.1")
    reopened = AccountPerformanceStore(path, account_id="demo-synthetic")
    assert reopened.latest() == evidence
    assert reopened.record(start, now=end + timedelta(days=7)) == first
    assert reopened.latest() == evidence
    with pytest.raises(AccountingEvidenceError, match="replay_conflict"):
        reopened.record(replace(start, equity=D("900"), cash=D("900")), now=MIDNIGHT)
    with pytest.raises(AccountingEvidenceError, match="stale"):
        reopened.record(valuation("980", end + timedelta(seconds=1)), now=end + timedelta(seconds=17))
    with sqlite3.connect(path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM performance_records")
    assert reopened.latest() == evidence


def test_store_rejects_missing_history_and_rewritten_prior_events(tmp_path):
    store = AccountPerformanceStore(tmp_path / "risk.sqlite3", account_id="demo-synthetic")
    store.record(valuation(), now=MIDNIGHT)
    t1 = MIDNIGHT + timedelta(hours=1)
    with pytest.raises(AccountingEvidenceError, match="history"):
        store.record(valuation("990", t1), now=t1)
    fee = event(EventKind.FEE, "-1", t1)
    store.record(valuation("990", t1), now=t1, history=history(end=t1, events=(fee,)))
    t2 = t1 + timedelta(hours=1)
    with pytest.raises(AccountingEvidenceError, match="historical_accounting"):
        store.record(valuation("980", t2), now=t2, history=history(end=t2))
    assert store.latest().coverage_end_at == t1


def test_historical_observation_cannot_follow_current_valuation():
    end = MIDNIGHT + timedelta(hours=1)
    with pytest.raises(AccountingEvidenceError, match="outside_current_interval"):
        measure(valuation(), valuation("990", end), history(end=end),
                (valuation("2000", end + timedelta(minutes=1)),))


def test_store_integrity_and_identity_are_verified(tmp_path):
    path = tmp_path / "risk.sqlite3"
    store = AccountPerformanceStore(path, account_id="demo-synthetic")
    store.record(valuation(), now=MIDNIGHT)
    with pytest.raises(AccountingEvidenceError, match="identity_mismatch"):
        AccountPerformanceStore(path, account_id="other-demo")
    with sqlite3.connect(path) as db:
        db.execute("DROP TRIGGER performance_no_update")
        db.execute("UPDATE performance_records SET payload='{}'")
    with pytest.raises(AccountingEvidenceError, match="integrity"):
        store.latest()


def test_cash_flow_and_account_valuation_at_same_time_need_ordering_proof():
    end = MIDNIGHT + timedelta(hours=1)
    item = event(EventKind.DEPOSIT, "100", end, pre_flow_valuation=valuation("990", end))
    result = measure(valuation(), valuation("1090", end), history(end=end, events=(item,)))
    assert result.daily_pnl is None and result.drawdown_fraction is None
    assert "external_flow_valuation_order_unproven" in result.reasons


def test_cash_flow_at_midnight_does_not_assume_boundary_pre_or_post_flow():
    start = MIDNIGHT - timedelta(hours=1)
    end = MIDNIGHT + timedelta(hours=1)
    item = event(EventKind.DEPOSIT, "100", MIDNIGHT, pre_flow_valuation=valuation("900", MIDNIGHT))
    result = measure(valuation(at=start), valuation("990", end),
                     history(start=start, end=end, events=(item,)), (valuation("1000", MIDNIGHT),))
    assert result.daily_pnl is None and result.drawdown_fraction is None
    assert "external_flow_valuation_order_unproven" in result.reasons


def test_component_baseline_needs_underlying_revalidated_source_proof(tmp_path):
    value = valuation(valuation_at=None, time_model=COMPONENT_TIME_MODEL,
                      component_start_at=MIDNIGHT - timedelta(seconds=1), component_end_at=MIDNIGHT)
    store = AccountPerformanceStore(tmp_path / "risk.sqlite3", account_id="demo-synthetic")
    with pytest.raises(AccountingEvidenceError, match="qualified_component_evidence_required"):
        store.record(value, now=MIDNIGHT)
    assert store.latest() is None


def test_performance_freshness_limit_cannot_be_relaxed():
    current = valuation(at=MIDNIGHT + timedelta(hours=1))
    with pytest.raises(AccountingEvidenceError, match="freshness_window"):
        measure(valuation(), current, history(end=current.coverage_at), max_age_seconds=16)


def test_performance_retains_exact_current_values_and_clock_binding():
    end = MIDNIGHT + timedelta(hours=1)
    value = valuation("995", end)
    result = measure(valuation(), value, history(end=end))
    assert result.current_valuation == value
    assert result.current_valuation.equity == D("995")
    assert result.current_valuation.time_evidence_hash == value.time_evidence_hash


def test_component_conversion_and_baseline_revalidate_source_proof(tmp_path):
    from execution.accounting import valuation_from_component_time
    from tests.contracts.etoro.test_etoro_component_time import _assessment
    proof = _assessment()
    now = proof.assessed_at
    value = valuation_from_component_time(proof, now=now, reconciliation_reference="synthetic-reconcile")
    assert value.valuation_at is None
    assert value.snapshot_id == proof.source_evidence_sha256
    assert value.time_evidence_hash == proof.evidence_hash
    store = AccountPerformanceStore(tmp_path / "risk.sqlite3", account_id=proof.demo_customer_id)
    result = store.record(value, now=now, component_evidence=proof)
    assert not result.qualified
    assert store.latest().current_valuation == value
    with pytest.raises(AccountingEvidenceError, match="component_valuation_unqualified"):
        valuation_from_component_time(proof, now=now + timedelta(seconds=16), reconciliation_reference="replay")
    other = AccountPerformanceStore(tmp_path / "other.sqlite3", account_id=proof.demo_customer_id)
    with pytest.raises(AccountingEvidenceError, match="unqualified_or_unbound"):
        other.record(replace(value, gross_exposure=value.gross_exposure + D("1")), now=now, component_evidence=proof)
    assert other.latest() is None


@pytest.mark.parametrize("changes", [
    {"daily_pnl": D("999")}, {"daily_loss_fraction": D("0")},
    {"qualified": False}, {"history_complete": False}, {"evidence_hash": "changed"},
])
def test_performance_projection_seal_rejects_metric_or_provenance_tampering(changes):
    end = MIDNIGHT + timedelta(hours=1)
    result = measure(valuation(), valuation("990", end), history(end=end))
    assert result.projection_valid()
    with pytest.raises(AccountingEvidenceError, match="projection_digest"):
        replace(result, **changes)


def test_performance_projection_verifies_even_bypassed_frozen_mutation():
    end = MIDNIGHT + timedelta(hours=1)
    result = measure(valuation(), valuation("990", end), history(end=end))
    object.__setattr__(result, "drawdown_fraction", D("0"))
    assert not result.projection_valid()
