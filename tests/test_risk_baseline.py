"""Synthetic ledger arithmetic, never evidence of broker-qualified history."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from brokers.base import BrokerAccountSnapshot
from execution.domain import Environment
from execution.risk_baseline import (
    ExternalCashFlow, FlowInterval, RiskBaselineError, RiskBaselineStore,
)


D = Decimal
T0 = datetime(2026, 10, 20, 23, tzinfo=timezone.utc)  # Midnight BST.


def snapshot(value="1000", at=T0, identifier=None):
    return BrokerAccountSnapshot(
        snapshot_id=identifier or at.isoformat(), environment=Environment.DEMO,
        observed_at=at, currency="USD", equity=D(value), available_cash=D(value),
        balance=D(value), frozen_cash=D(0), current_pnl=D(0), used_margin=D(0),
        positions=(), pending_orders=(), pending_orders_complete=True,
        executions_complete=False,
    )


def interval(start, end, *, flows=(), start_cursor="c0", end_cursor="c1", complete=True):
    return FlowInterval(start_at=start, end_at=end, start_cursor=start_cursor,
                        end_cursor=end_cursor, source_reference="synthetic-evidence",
                        complete=complete, external_flows=flows)


def record(store, snap, flows=None):
    return store.record(snap, account_id="synthetic-account", flow_interval=flows,
                        captured_at=snap.observed_at)


def store(tmp_path):
    return RiskBaselineStore(tmp_path / "risk.sqlite3", account_id="synthetic-account")


def test_durable_baseline_drawdown_and_exact_replay_survive_restart(tmp_path):
    ledger = store(tmp_path)
    first = record(ledger, snapshot())
    peak = snapshot("1100", T0 + timedelta(hours=1))
    record(ledger, peak, interval(T0, peak.observed_at))
    now = snapshot("990", T0 + timedelta(hours=2))
    coverage = interval(peak.observed_at, now.observed_at, start_cursor="c1", end_cursor="c2")
    evidence = record(ledger, now, coverage)
    assert evidence.inception_pnl == D("-10")
    assert evidence.daily_pnl == D("-10")
    assert evidence.peak_equity_index == D("1.1")
    assert evidence.drawdown_fraction == D("0.1")
    assert evidence.baseline_at == T0
    assert not evidence.broker_history_verified
    assert not evidence.qualification_credit and not evidence.submission_available
    reopened = store(tmp_path)
    assert reopened.latest() == evidence
    # Historical exact replay is idempotent, retaining its original time.
    assert reopened.record(snapshot(), account_id="synthetic-account") == first
    assert reopened.latest() == evidence
    assert record(reopened, now, coverage) == evidence
    assert reopened.latest().observation_count == 3
    with pytest.raises(RiskBaselineError, match="replay_conflict"):
        record(reopened, snapshot("995", now.observed_at), coverage)


@pytest.mark.parametrize("kind,amount,equity", [("deposit", "500", "1400"), ("withdrawal", "-200", "700")])
def test_external_flows_cannot_hide_loss_or_fabricate_drawdown(tmp_path, kind, amount, equity):
    ledger = store(tmp_path)
    record(ledger, snapshot())
    at = T0 + timedelta(hours=1)
    flow = ExternalCashFlow("cashflow1", at - timedelta(minutes=1), D(amount), "USD", kind)
    evidence = record(ledger, snapshot(equity, at), interval(T0, at, flows=(flow,)))
    assert evidence.daily_pnl == D("-100")
    assert evidence.inception_pnl == D("-100")
    assert evidence.drawdown_fraction is None
    assert evidence.equity_index is None and evidence.peak_equity_index is None
    assert "external_flows_require_preflow_valuations_for_drawdown" in evidence.limitations
    later = at + timedelta(hours=1)
    evidence = record(ledger, snapshot(equity, later), interval(at, later, start_cursor="c1", end_cursor="c2"))
    assert evidence.drawdown_fraction is None  # Never silently rebase after a flow.


def test_offsetting_external_flows_still_disable_drawdown(tmp_path):
    ledger = store(tmp_path)
    record(ledger, snapshot())
    end = T0 + timedelta(hours=1)
    flows = (ExternalCashFlow("in", T0 + timedelta(minutes=1), D("100"), "USD", "deposit"),
             ExternalCashFlow("out", end, D("-100"), "USD", "withdrawal"))
    evidence = record(ledger, snapshot("950", end), interval(T0, end, flows=flows))
    assert evidence.inception_pnl == D("-50")
    assert evidence.drawdown_fraction is None


def test_missing_incomplete_gapped_or_discontinuous_flows_do_not_append(tmp_path):
    ledger = store(tmp_path)
    first = record(ledger, snapshot())
    at = T0 + timedelta(hours=1)
    for coverage in (None, interval(T0, at, complete=False), interval(T0 + timedelta(seconds=1), at)):
        with pytest.raises(RiskBaselineError):
            record(ledger, snapshot("990", at), coverage)
        assert ledger.latest() == first
    record(ledger, snapshot("990", at), interval(T0, at))
    later = at + timedelta(hours=1)
    with pytest.raises(RiskBaselineError, match="cursor_discontinuity"):
        record(ledger, snapshot("980", later), interval(at, later, start_cursor="wrong"))
    assert ledger.latest().observation_count == 2


@pytest.mark.parametrize("boundary", [T0, datetime(2026, 10, 26, tzinfo=timezone.utc)])
def test_day_boundaries_honour_bst_and_gmt_without_inventing_midnight_equity(tmp_path, boundary):
    ledger = store(tmp_path)
    start = boundary - timedelta(hours=1)
    record(ledger, snapshot("1000", start))
    partial = boundary + timedelta(minutes=1)
    evidence = record(ledger, snapshot("990", partial), interval(start, partial))
    assert evidence.daily_pnl is None
    assert evidence.daily_baseline_at is None
    assert evidence.inception_pnl == D("-10")
    assert evidence.risk_day == boundary.astimezone(ledger.zone).date().isoformat()


def test_exact_new_day_boundary_resets_daily_measurement_only(tmp_path):
    ledger = store(tmp_path)
    record(ledger, snapshot())
    next_boundary = T0 + timedelta(days=1)
    record(ledger, snapshot("900", next_boundary), interval(T0, next_boundary))
    later = next_boundary + timedelta(hours=1)
    evidence = record(ledger, snapshot("950", later), interval(next_boundary, later, start_cursor="c1", end_cursor="c2"))
    assert evidence.daily_pnl == D("50")
    assert evidence.inception_pnl == D("-50")
    assert evidence.peak_equity_index == D("1")
    assert evidence.baseline_at == T0


def test_midday_inception_is_never_labelled_full_day(tmp_path):
    ledger = store(tmp_path)
    evidence = record(ledger, snapshot(at=T0 + timedelta(hours=12)))
    assert evidence.daily_pnl is None
    assert not evidence.flow_coverage_declared_complete
    assert evidence.inception_pnl == 0


def test_identity_environment_currency_and_time_checks(tmp_path):
    ledger = store(tmp_path)
    snap = snapshot()
    with pytest.raises(RiskBaselineError, match="identity"):
        ledger.record(snap, account_id="other", captured_at=T0)
    with pytest.raises(RiskBaselineError, match="identity"):
        record(ledger, replace(snap, environment=Environment.REAL))
    with pytest.raises(RiskBaselineError, match="currency"):
        record(ledger, replace(snap, currency="GBP"))
    with pytest.raises(RiskBaselineError, match="timezone"):
        record(ledger, replace(snap, observed_at=T0.replace(tzinfo=None)))
    for captured_at in (T0 + timedelta(seconds=16), T0 - timedelta(seconds=1)):
        with pytest.raises(RiskBaselineError, match="stale_or_future"):
            ledger.record(snap, account_id="synthetic-account", captured_at=captured_at)
    record(ledger, snap)
    with pytest.raises(RiskBaselineError, match="strictly_increasing"):
        record(ledger, snapshot(at=T0 - timedelta(seconds=1)))
    with pytest.raises(RiskBaselineError, match="identity"):
        RiskBaselineStore(ledger.path, account_id="other")
    assert ledger.latest().observation_count == 1


@pytest.mark.parametrize("changes", [
    {"equity": D("NaN")}, {"current_pnl": D("Infinity")}, {"equity": D("0")},
    {"equity": D("1001")}, {"equity": 1000}, {"pending_orders_complete": False},
    {"pending_orders_complete": 1}, {"positions": []},
])
def test_malformed_or_unreconciled_snapshot_never_establishes_baseline(tmp_path, changes):
    ledger = store(tmp_path)
    with pytest.raises(ValueError):
        record(ledger, replace(snapshot(), **changes))
    assert ledger.latest() is None


def test_flow_validation_excludes_costs_and_duplicate_cash_movements(tmp_path):
    with pytest.raises(RiskBaselineError, match="kind"):
        ExternalCashFlow("fee", T0, D("-5"), "USD", "fee")
    with pytest.raises(RiskBaselineError, match="sign"):
        ExternalCashFlow("in", T0, D("-5"), "USD", "deposit")
    flow = ExternalCashFlow("in", T0 + timedelta(minutes=1), D("5"), "USD", "deposit")
    with pytest.raises(RiskBaselineError, match="duplicate"):
        interval(T0, T0 + timedelta(hours=1), flows=(flow, flow))
    with pytest.raises(RiskBaselineError, match="outside"):
        interval(T0 + timedelta(hours=1), T0 + timedelta(hours=2), flows=(flow,))
    ledger = store(tmp_path)
    record(ledger, snapshot())
    at = T0 + timedelta(hours=1)
    record(ledger, snapshot("1005", at), interval(T0, at, flows=(flow,)))
    later = at + timedelta(hours=1)
    reused = replace(flow, occurred_at=later)
    with pytest.raises(RiskBaselineError, match="duplicate"):
        record(ledger, snapshot("1010", later), interval(at, later, flows=(reused,), start_cursor="c1"))


def test_append_only_database_and_hash_chain_integrity(tmp_path):
    ledger = store(tmp_path)
    record(ledger, snapshot())
    with sqlite3.connect(ledger.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM risk_observations")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE risk_identity SET payload='{}'")
        db.execute("DROP TRIGGER risk_observations_no_update")
        db.execute("UPDATE risk_observations SET payload='{}'")
    with pytest.raises(RiskBaselineError, match="integrity"):
        ledger.latest()
    with pytest.raises(RiskBaselineError, match="integrity"):
        store(tmp_path)


def test_holdings_require_invested_principal_and_never_substitute_margin(tmp_path):
    from brokers.base import BrokerPosition
    holding = BrokerPosition("p1", 1, D("1"), D("250"), D("1"), D("10"), "USD")
    snap = replace(snapshot("1110"), available_cash=D("1000"),
                   current_pnl=D("10"), used_margin=D("40"), positions=(holding,))
    ledger = store(tmp_path)
    with pytest.raises(RiskBaselineError, match="invested_amount_required"):
        record(ledger, snap)
    with pytest.raises(RiskBaselineError, match="equity_not_reconciled"):
        record(ledger, replace(snap, invested_amount=snap.used_margin))
    result = record(ledger, replace(snap, invested_amount=D("100")))
    assert result.baseline_at == snap.observed_at
    assert not result.submission_available


def test_cent_difference_is_not_silently_reconciled(tmp_path):
    ledger = store(tmp_path)
    with pytest.raises(RiskBaselineError, match="equity_not_reconciled"):
        record(ledger, replace(snapshot(), equity=D("1000.01")))
    assert ledger.latest() is None
