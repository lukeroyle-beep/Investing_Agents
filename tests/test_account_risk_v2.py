"""Synthetic regression evidence, never broker qualification."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from execution.accounting import AccountValuation, PerformanceEvidence
from execution.domain import OrderIntent, RiskOutcome
from execution.instruments import Instrument
from risk.pretrade import AccountEvidence, PositionEvidence, PreTradeRiskEvaluator, QuoteEvidence, RiskLimits
from shared.paths import RISK_CONFIG_PATH


NOW = datetime(2026, 10, 26, 14, 30, tzinfo=timezone.utc)
INSTRUMENT = Instrument.create(canonical_symbol="AAPL", exchange="NASDAQ",
    asset_type="equity", currency="USD", sector="technology")


def performance(**changes):
    valuation = AccountValuation(account_id="synthetic-demo", snapshot_id="snapshot", currency="USD",
        cash=D("10000"), invested_amount=D("0"), unrealised_pnl=D("0"), equity=D("10000"),
        broker_margin=D("0"), gross_exposure=D("0"), reconciliation_reference="synthetic",
        time_evidence_hash="b" * 64, valuation_at=NOW)
    value = PerformanceEvidence(current_snapshot_id="snapshot", account_id="synthetic-demo",
        currency="USD", risk_day="2026-10-26", risk_timezone="Europe/London",
        coverage_start_at=NOW.replace(hour=0, minute=0), coverage_end_at=NOW,
        daily_pnl=D("0"), daily_loss_denominator=D("10000"), daily_loss_fraction=D("0"),
        equity_index=D("1"), peak_equity_index=D("1"), drawdown_fraction=D("0"),
        history_complete=True, qualified=True, reasons=(), evidence_hash="a" * 64, current_valuation=valuation)
    return replace(value, projection_hash="", **changes)


def account(**changes):
    value = AccountEvidence(snapshot_id="snapshot", environment="demo", observed_at=NOW,
        currency="USD", equity=D("10000"), cash=D("10000"), daily_pnl=None, peak_equity=None,
        positions=(), pending_orders=(), pending_orders_complete=True,
        contract_version="account-risk-v2", account_id="synthetic-demo",
        performance=performance(), invested_amount=D("0"), unrealised_pnl=D("0"),
        positions_complete=True, estimated_order_costs=D("0.01"),
        issuer_by_instrument=((str(INSTRUMENT.internal_instrument_id), "issuer:apple"),))
    return replace(value, **changes)


def assess(value):
    intent = OrderIntent.create(strategy_id="manual-test", run_id="test", environment="demo",
        internal_instrument_id=INSTRUMENT.internal_instrument_id, side="buy", order_type="market",
        sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=NOW + timedelta(minutes=2))
    quote = QuoteEvidence("quote", INSTRUMENT.internal_instrument_id, NOW, D("99"), D("100"), True)
    return PreTradeRiskEvaluator(RiskLimits.load(RISK_CONFIG_PATH)).evaluate(
        intent=intent, instrument=INSTRUMENT, account=value, quote=quote, now=NOW)


def test_complete_explicit_v2_contract_passes_synthetic_check():
    assert assess(account()).outcome == RiskOutcome.ACCEPTED


def test_missing_history_is_unknown_and_blocks_both_performance_limits():
    result = assess(account(performance=None))
    assert {"performance_coverage", "daily_loss_stop", "drawdown_stop"} <= set(result.reasons)
    assert dict(result.computed_exposures)["daily_loss_fraction"] == "None"


def test_cashflow_adjusted_loss_and_drawdown_used_without_raw_peak_substitution():
    result = assess(account(performance=performance(daily_pnl=D("-100"),
        daily_loss_fraction=D("0.01"), drawdown_fraction=D("0.05"))))
    assert {"daily_loss_stop", "drawdown_stop"} <= set(result.reasons)


def test_performance_snapshot_account_currency_and_age_are_bound():
    for change in ({"current_snapshot_id": "other"}, {"account_id": "other"},
                   {"currency": "GBP"}, {"coverage_end_at": NOW - timedelta(seconds=16)},
                   {"history_complete": False}, {"qualified": False}):
        assert "performance_coverage" in assess(account(performance=performance(**change))).reasons


def test_existing_account_exposure_blocks_even_tiny_order_in_another_issuer():
    held = Instrument.create(canonical_symbol="EXISTING", exchange="NASDAQ", asset_type="equity",
                             currency="USD", sector="industrials")
    result = assess(account(equity=D("10000"), cash=D("3000"),
        invested_amount=D("5000"), unrealised_pnl=D("2000"),
        positions=(PositionEvidence(held.internal_instrument_id, held.sector, D("7000")),)))
    assert {"gross_exposure", "net_exposure", "all_issuer_exposure", "all_sector_exposure"} <= set(result.reasons)


def test_existing_unrelated_issuer_breach_is_not_hidden_by_small_target_order():
    held = Instrument.create(canonical_symbol="HELD", exchange="NASDAQ", asset_type="equity",
                             currency="USD", sector="industrials")
    result = assess(account(cash=D("9400"), invested_amount=D("600"),
        positions=(PositionEvidence(held.internal_instrument_id, held.sector, D("600")),)))
    assert "all_issuer_exposure" in result.reasons
    assert "issuer_exposure" not in result.reasons


def test_missing_holdings_coverage_classification_or_equity_reconciliation_fails():
    assert "positions_complete" in assess(account(positions_complete=False)).reasons
    assert "equity_identity" in assess(account(invested_amount=None)).reasons
    unknown = PositionEvidence(INSTRUMENT.internal_instrument_id, "unknown", D("1"))
    assert "exposure_classification" in assess(account(positions=(unknown,))).reasons


def test_costs_are_reserved_in_projected_cash():
    result = assess(account(estimated_order_costs=D("0.123456789123456789")))
    assert dict(result.computed_exposures)["projected_cash"] == "9989.876543210876543211"


def test_same_snapshot_label_cannot_hide_a_different_performance_valuation():
    proof = performance()
    changed = replace(proof.current_valuation, cash=D("10001"), equity=D("10001"))
    assert "performance_coverage" in assess(account(performance=replace(proof, current_valuation=changed, projection_hash=""))).reasons


def test_share_classes_group_under_reviewed_issuer_identity():
    other = Instrument.create(canonical_symbol="OTHER", exchange="NASDAQ", asset_type="equity",
                              currency="USD", sector="technology")
    positions = (PositionEvidence(INSTRUMENT.internal_instrument_id, "technology", D("300")),
                 PositionEvidence(other.internal_instrument_id, "technology", D("300")))
    value = account(cash=D("9400"), invested_amount=D("600"), positions=positions,
        issuer_by_instrument=((str(INSTRUMENT.internal_instrument_id), "issuer:shared"),
                              (str(other.internal_instrument_id), "ISSUER:SHARED ")))
    result = assess(value)
    assert "all_issuer_exposure" in result.reasons
    assert "issuer_exposure" not in result.reasons
