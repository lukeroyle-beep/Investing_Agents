"""Strict read-only pre-trade capture; no credentials, stores or writes owned here.

Contracts: official eToro API v1.398.0, checked 2026-10-21 using
getTradingInfoDemoPortfolio, getAggregatedPortfolioDemo and getSecurities.
Reference: https://api-portal.etoro.com/api-reference
Aggregate guide:
https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot

The detailed portfolio enumerates every pending-order family. Most active-order
schemas are untyped, so this initial scope requires *all* explicit collections
to be empty. Copies, derivatives, crypto, leverage and non-USD holdings fail
closed instead of disappearing from exposure. Execution-history completeness,
full daily profit/loss and peak equity are not established by these reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from brokers.base import BrokerAccountSnapshot, BrokerDataUnavailable, BrokerPosition
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS, EtoroDetailedPortfolio
from execution.domain import Environment, payload_hash

if TYPE_CHECKING:
    from brokers.etoro.client import EtoroReadOnlyClient


ACCOUNT_EVIDENCE_SCOPE = "usd_long_unleveraged_real_stocks_etfs_no_mirrors_no_pending"
CONTRACT_VERSION = "v1.398.0"
MAX_CAPTURE_SECONDS = 15
MONEY_TOLERANCE = Decimal("0.02")
UNITS_TOLERANCE = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class EtoroPretradeAccountEvidence:
    snapshot: BrokerAccountSnapshot
    demo_customer_id: str
    detailed_snapshot_id: str
    aggregate_snapshot_id: str
    security_metadata_checksum: str
    capture_started_at: datetime
    capture_completed_at: datetime
    checked_pending_collections: tuple[str, ...]
    scope: str = ACCOUNT_EVIDENCE_SCOPE
    contract_version: str = CONTRACT_VERSION
    daily_pnl: None = None
    peak_equity: None = None
    limitations: tuple[str, ...] = (
        "full_daily_pnl_unavailable",
        "peak_equity_unavailable",
        "execution_history_not_complete",
        "bracketed_reads_are_not_an_atomic_broker_snapshot",
    )


def _moment(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BrokerDataUnavailable("account_capture_time_requires_timezone")
    return value.astimezone(timezone.utc)


def _check_details(detail: EtoroDetailedPortfolio, expected_customer_id: str) -> None:
    if detail.mirror_count:
        raise BrokerDataUnavailable("account_scope_not_supported: copy-trading mirrors")
    if any(count for _, count in detail.pending_collection_counts):
        raise BrokerDataUnavailable("account_scope_not_supported: active pending-order collection")
    if detail.bonus_credit:
        raise BrokerDataUnavailable("account_scope_not_supported: bonus credit")
    for position in detail.positions:
        if position.customer_id != expected_customer_id:
            raise BrokerDataUnavailable("demo_account_identity_mismatch")
        if position.mirror_id or not position.is_buy or position.leverage != 1 or position.settlement_type_id != 1:
            raise BrokerDataUnavailable("account_scope_not_supported: mirrored, short, leveraged or derivative position")


def capture_pretrade_account_evidence(
    client: "EtoroReadOnlyClient",
    *,
    expected_demo_customer_id: str,
    now: datetime | None = None,
) -> EtoroPretradeAccountEvidence:
    """Capture complete *empty pending* evidence, otherwise fail closed.

    The expected demo identity must come from independently reviewed account
    selection. `now` is an injectable clock value for deterministic offline tests;
    live callers omit it. This function never initializes an execution store or
    changes broker state. Each read uses the client's already-configured Demo
    credentials. No automatic fallback to another endpoint or account is made.
    """
    if client.environment != Environment.DEMO or not client.read_only:
        raise BrokerDataUnavailable("account_capture_requires_demo_read_only_client")
    if isinstance(expected_demo_customer_id, bool):
        raise BrokerDataUnavailable("expected_demo_account_identity_required")
    expected = str(expected_demo_customer_id).strip()
    if not expected.isascii() or not expected.isdecimal() or int(expected) <= 0:
        raise BrokerDataUnavailable("expected_demo_account_identity_required")
    expected = str(int(expected))
    started = _moment(now or datetime.now(timezone.utc))
    first = client.get_demo_detailed_portfolio()
    _check_details(first, expected)
    aggregate = client.get_demo_portfolio()
    securities = client.get_security_metadata(tuple(sorted({position.instrument_id for position in first.positions})))
    last = client.get_demo_detailed_portfolio()
    completed = _moment(now or datetime.now(timezone.utc))
    _check_details(last, expected)
    if completed < started or (completed - started).total_seconds() > MAX_CAPTURE_SECONDS:
        raise BrokerDataUnavailable("account_capture_timing_not_valid")
    if first.snapshot_id != last.snapshot_id:
        raise BrokerDataUnavailable("account_changed_during_capture")
    if aggregate.customer_id != expected:
        raise BrokerDataUnavailable("demo_account_identity_mismatch")
    age = (completed - aggregate.observed_at).total_seconds()
    if age < 0 or age > MAX_CAPTURE_SECONDS:
        raise BrokerDataUnavailable("aggregate_account_snapshot_stale_or_future")
    if aggregate.mirror_count or aggregate.account_currency != "USD":
        raise BrokerDataUnavailable("account_scope_not_supported: mirrors or account currency")

    by_instrument = {item.instrument_id: item for item in aggregate.instrument_aggregates}
    units_by_instrument: dict[int, Decimal] = {}
    margin_by_instrument: dict[int, Decimal] = {}
    for position in first.positions:
        units_by_instrument[position.instrument_id] = units_by_instrument.get(position.instrument_id, Decimal("0")) + position.units
        margin_by_instrument[position.instrument_id] = margin_by_instrument.get(position.instrument_id, Decimal("0")) + position.amount
    if set(units_by_instrument) != set(by_instrument):
        raise BrokerDataUnavailable("account_position_coverage_mismatch")
    metadata = {security.instrument_id: security for security in securities}
    if len(metadata) != len(securities) or set(metadata) != set(by_instrument):
        raise BrokerDataUnavailable("account_security_metadata_coverage_mismatch")
    for instrument_id, units in units_by_instrument.items():
        item = by_instrument[instrument_id]
        if item.currency != "USD" or item.average_leverage != 1 or item.units <= 0 or item.current_exposure <= 0:
            raise BrokerDataUnavailable("account_scope_not_supported: asset currency, direction or leverage")
        if metadata[instrument_id].instrument_type not in {"Stocks", "ETF"}:
            raise BrokerDataUnavailable("account_scope_not_supported: instrument type")
        if abs(units - item.units) > UNITS_TOLERANCE:
            raise BrokerDataUnavailable("account_position_units_mismatch")
        # This is an additional conservative scope constraint, not a general
        # eToro valuation formula: fees, collateral or other adjustments that
        # separate unleveraged exposure from invested margin plus P&L require
        # a separately qualified valuation model. Never understate exposure.
        if abs(item.current_exposure - margin_by_instrument[instrument_id] - item.pnl) > MONEY_TOLERANCE:
            raise BrokerDataUnavailable("account_exposure_evidence_mismatch")

    totals = aggregate.totals
    if totals.frozen_cash != 0:
        raise BrokerDataUnavailable("empty_pending_orders_contradict_reserved_cash")
    if abs(totals.available_cash - first.credit) > MONEY_TOLERANCE or abs(totals.balance - totals.available_cash) > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_cash_evidence_mismatch")
    if totals.used_margin < 0 or abs(totals.total_value - totals.available_cash - totals.used_margin - totals.current_pnl) > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_equity_identity_mismatch")
    if abs(totals.used_margin - sum((position.amount for position in first.positions), Decimal("0"))) > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_margin_evidence_mismatch")
    if abs(totals.current_pnl - sum((item.pnl for item in aggregate.instrument_aggregates), Decimal("0"))) > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_unrealized_pnl_evidence_mismatch")

    metadata_checksum = payload_hash(securities)
    snapshot = BrokerAccountSnapshot(
        snapshot_id=payload_hash({
            "detailed": first.snapshot_id, "aggregate": aggregate.snapshot_id,
            "metadata": metadata_checksum, "scope": ACCOUNT_EVIDENCE_SCOPE,
        }),
        environment=Environment.DEMO,
        observed_at=aggregate.observed_at,
        currency="USD",
        equity=totals.total_value,
        available_cash=totals.available_cash,
        balance=totals.balance,
        frozen_cash=totals.frozen_cash,
        current_pnl=totals.current_pnl,
        used_margin=totals.used_margin,
        positions=tuple(BrokerPosition(
            broker_position_id=None,
            broker_instrument_id=item.instrument_id,
            units=item.units,
            current_exposure=item.current_exposure,
            average_leverage=item.average_leverage,
            pnl=item.pnl,
            currency="USD",
        ) for item in aggregate.instrument_aggregates),
        pending_orders=(),
        pending_orders_complete=True,
        executions_complete=False,
    )
    return EtoroPretradeAccountEvidence(
        snapshot=snapshot,
        demo_customer_id=expected,
        detailed_snapshot_id=first.snapshot_id,
        aggregate_snapshot_id=aggregate.snapshot_id,
        security_metadata_checksum=metadata_checksum,
        capture_started_at=started,
        capture_completed_at=completed,
        checked_pending_collections=DEMO_PENDING_COLLECTIONS,
    )
