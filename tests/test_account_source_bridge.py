from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from execution.account_bridge import component_risk_account
from execution.domain import Environment, OrderIntent
from execution.instruments import Instrument, BrokerInstrumentMapping, InstrumentRegistry
from execution.store import ExecutionStore
from risk.pretrade import PreTradeRiskEvaluator, QuoteEvidence, RiskEvidenceError, RiskLimits
from shared.paths import RISK_CONFIG_PATH
from tests.contracts.etoro.test_etoro_component_time import _assessment
from tests.contracts.etoro.test_etoro_account_evidence import NOW


def setup(tmp_path):
    source = _assessment()
    registry = InstrumentRegistry(ExecutionStore(tmp_path / "execution.sqlite3"))
    symbol = source.evidence.security_metadata[0].symbol
    instrument = registry.register(Instrument.create(canonical_symbol=symbol, exchange="NASDAQ",
        currency="USD", asset_type="equity", sector="technology"))
    registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument,
        broker="etoro", environment=Environment.DEMO, broker_instrument_id=1001,
        exact_match_symbol=symbol, metadata={"synthetic": True}))
    account = component_risk_account(source, registry=registry,
        instruments_by_broker_id={1001: instrument}, performance=None,
        estimated_order_costs=D("0.01"),
        issuer_by_instrument={str(instrument.internal_instrument_id): "issuer:apple"})
    return source, registry, instrument, account


def evaluate(instrument, account, now=NOW):
    intent = OrderIntent.create(strategy_id="synthetic", run_id="bridge", environment="demo",
        internal_instrument_id=instrument.internal_instrument_id, side="buy", order_type="market",
        sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=NOW + timedelta(minutes=2))
    quote = QuoteEvidence("synthetic-rate", instrument.internal_instrument_id, now, D("99"), D("100"), True)
    return PreTradeRiskEvaluator(RiskLimits.load(RISK_CONFIG_PATH)).evaluate(
        intent=intent, instrument=instrument, account=account, quote=quote, now=now)


def test_projection_preserves_non_atomic_time_exact_holdings_and_unknown_history(tmp_path):
    source, _, instrument, account = setup(tmp_path)
    assert account.observed_at is None
    assert account.snapshot_id == source.source_evidence_sha256
    assert account.positions[0].broker_position_id == source.positions[0].broker_position_id
    result = evaluate(instrument, account)
    assert "account_freshness" not in result.reasons
    assert "performance_coverage" in result.reasons


def test_completeness_flag_cannot_hide_held_positions(tmp_path):
    _, _, instrument, account = setup(tmp_path)
    for tampered in (replace(account, positions=()),
                     replace(account, positions=(replace(account.positions[0], signed_market_value=D("0")),))):
        result = evaluate(instrument, tampered)
        assert "account_freshness" in result.reasons
        freshness = next(x for x in result.checks if x.name == "account_freshness")
        assert "component_positions_or_pending_coverage_mismatch" in freshness.observed


def test_neutral_projection_never_renews_original_component_deadline(tmp_path):
    source, _, instrument, account = setup(tmp_path)
    assert account.component_time.freshness_deadline() == source.freshness_deadline()
    assert "account_freshness" in evaluate(instrument, account, NOW + timedelta(seconds=16)).reasons
    with pytest.raises(ValueError, match="source_deadline|source_projection"):
        replace(account.component_time, deadline_epoch=account.component_time.deadline_epoch + 1)


def test_missing_whole_account_mapping_is_not_silently_dropped(tmp_path):
    source, registry, _, _ = setup(tmp_path)
    with pytest.raises(RiskEvidenceError, match="whole_account"):
        component_risk_account(source, registry=registry, instruments_by_broker_id={},
            performance=None, estimated_order_costs=D("0"), issuer_by_instrument={})
