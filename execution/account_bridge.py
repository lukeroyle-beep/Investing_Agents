"""Explicit account-wide projection of reviewed eToro component evidence.

No source timestamps are invented. A fully qualified clock/coverage proof is
required before projection; missing performance remains unknown and cannot pass
risk. Mapping the entire account is mandatory even for one tiny order.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

from brokers.etoro.account_evidence import AccountComponentTimeEvidence
from execution.accounting import PerformanceEvidence
from execution.domain import Environment
from execution.instruments import Instrument, InstrumentRegistry
from execution.account_source import RiskAccountSource, SourcePosition
from risk.pretrade import AccountEvidence, PositionEvidence, RiskEvidenceError


def component_risk_account(
    evidence: AccountComponentTimeEvidence, *,
    registry: InstrumentRegistry,
    instruments_by_broker_id: Mapping[int, Instrument],
    performance: PerformanceEvidence | None,
    estimated_order_costs: Decimal,
    issuer_by_instrument: Mapping[str, str],
) -> AccountEvidence:
    if not isinstance(evidence, AccountComponentTimeEvidence):
        raise RiskEvidenceError("typed_component_time_required")
    source = evidence.evidence
    projection = RiskAccountSource(
        model_version=evidence.model_version, source_evidence_sha256=evidence.source_evidence_sha256,
        time_evidence_hash=evidence.evidence_hash, demo_customer_id=evidence.demo_customer_id,
        currency=evidence.currency, equity=evidence.equity, available_cash=evidence.available_cash,
        invested_amount=evidence.invested_amount, broker_margin=evidence.broker_margin,
        current_pnl=evidence.current_pnl,
        positions=tuple(SourcePosition(x.broker_position_id, x.broker_instrument_id, x.current_exposure) for x in source.positions),
        earliest_component_at=evidence.earliest_component_at, latest_component_at=evidence.latest_component_at,
        assessed_at=evidence.assessed_at, deadline_epoch=evidence.freshness_deadline_epoch_seconds(),
        original_blockers=evidence.validate_at(evidence.assessed_at),
    )
    held_ids = {position.broker_instrument_id for position in source.positions}
    if set(instruments_by_broker_id) != held_ids:
        raise RiskEvidenceError("whole_account_instrument_mapping_required")
    metadata = {security.instrument_id: security for security in source.security_metadata}
    positions = []
    for position in source.positions:
        instrument = instruments_by_broker_id[position.broker_instrument_id]
        mapping = registry.broker_mapping(
            instrument.internal_instrument_id, broker="etoro", environment=Environment.DEMO)
        if (registry.get(instrument.internal_instrument_id) != instrument
                or mapping.broker_instrument_id != position.broker_instrument_id
                or position.broker_instrument_id not in metadata
                or metadata[position.broker_instrument_id].symbol != instrument.canonical_symbol):
            raise RiskEvidenceError("account_instrument_mapping_mismatch")
        if position.currency != evidence.currency:
            raise RiskEvidenceError("account_exposure_currency_mismatch")
        # PnL evidence validates the supported direct long-only book. Current
        # exposure already includes leverage; broker margin is never substituted.
        positions.append(PositionEvidence(instrument.internal_instrument_id,
            instrument.sector, position.current_exposure,
            position.broker_position_id, position.broker_instrument_id))
    return AccountEvidence(
        snapshot_id=evidence.source_evidence_sha256, environment=Environment.DEMO,
        observed_at=None, currency=evidence.currency, equity=evidence.equity,
        cash=evidence.available_cash, daily_pnl=None, peak_equity=None,
        positions=tuple(positions), pending_orders=(), pending_orders_complete=True,
        contract_version="account-risk-v2", account_id=evidence.demo_customer_id,
        component_time=projection, performance=performance,
        invested_amount=evidence.invested_amount, unrealised_pnl=evidence.current_pnl,
        estimated_order_costs=estimated_order_costs, positions_complete=True,
        issuer_by_instrument=tuple(sorted(issuer_by_instrument.items())),
    )
