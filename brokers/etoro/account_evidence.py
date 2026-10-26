"""Strict read-only pre-trade capture; no credentials, stores or writes owned here.

Contracts: official eToro API v1.398.0, checked 2026-10-21 using
getTradingInfoDemoPortfolio, postAggregatedPortfolioDemo and getSecurities.
Reference: https://api-portal.etoro.com/api-reference
Aggregate guide:
https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot

The detailed portfolio enumerates every pending-order family. Most active-order
schemas are untyped, so this initial scope requires *all* explicit collections
to be empty. Account valuation covers long, leverage-one stocks, funds and
crypto with real-asset or CFD settlement, in a USD account (USD/GBX assets).
Broker-provided account-currency exposure is distinct from margin/liquidation.
This read scope grants no permission to trade those holdings. Copies, shorts,
leverage and other asset shapes fail closed instead of disappearing from risk.
Execution-history completeness, daily profit/loss and peak equity remain absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from brokers.base import BrokerAccountSnapshot, BrokerDataUnavailable, BrokerPosition
from brokers.etoro.schemas import (
    DEMO_PENDING_COLLECTIONS, EtoroDetailedPortfolio, EtoroInstrumentAggregate,
    EtoroSecurity, EtoroPositionValuation,
)
from execution.domain import Environment, payload_hash

if TYPE_CHECKING:
    from brokers.etoro.client import EtoroReadOnlyClient
    from brokers.etoro.pnl_currency_metadata import EtoroPositionCurrencyMetadata


ACCOUNT_EVIDENCE_SCOPE = "usd_account_long_1x_stocks_etfs_crypto_real_cfd_usd_gbx_no_mirrors_no_pending"
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
    instrument_valuations: tuple[EtoroInstrumentAggregate, ...]
    security_metadata: tuple[EtoroSecurity, ...]
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
        # Official detailed-portfolio enum: 0=CFD, 1=Real Asset. Other
        # derivatives need separate valuation/accounting qualification.
        if position.mirror_id or not position.is_buy or position.leverage != 1 or position.settlement_type_id not in {0, 1}:
            raise BrokerDataUnavailable("account_scope_not_supported: mirrored, short, leveraged or unsupported settlement")


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
        if item.currency not in {"USD", "GBX"} or item.average_leverage != 1 or item.units <= 0 or item.current_exposure <= 0:
            raise BrokerDataUnavailable("account_scope_not_supported: asset currency, direction or leverage")
        if metadata[instrument_id].instrument_type not in {"Stocks", "ETF", "Crypto"}:
            raise BrokerDataUnavailable("account_scope_not_supported: instrument type")
        if abs(units - item.units) > UNITS_TOLERANCE:
            raise BrokerDataUnavailable("account_position_units_mismatch")
        if item.margin < 0 or abs(item.margin - margin_by_instrument[instrument_id]) > MONEY_TOLERANCE:
            raise BrokerDataUnavailable("account_margin_evidence_mismatch")
        # This identity is explicitly documented for liquidation value. Market
        # exposure is a separate broker field: collateral, fees and currency
        # treatment mean it must never be replaced by margin plus P&L.
        if abs(item.liquidation_value - item.margin - item.pnl) > MONEY_TOLERANCE:
            raise BrokerDataUnavailable("account_liquidation_evidence_mismatch")

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
        instrument_valuations=aggregate.instrument_aggregates,
        security_metadata=securities,
    )


@dataclass(frozen=True, slots=True)
class EtoroPnlAccountEvidence:
    """Bracketed observations, deliberately not a BrokerAccountSnapshot.

    A collection of per-position valuation times cannot supply the single
    observation required by risk/pretrade.py or the prospective baseline ledger.
    Unqualified valuations are retained as evidence, never promoted to freshness.
    """

    demo_customer_id: str
    detailed_snapshot_id: str
    pnl_snapshot_id: str
    closing_detailed_snapshot_id: str
    security_metadata_checksum: str
    currency_metadata_checksum: str
    currency_metadata: tuple["EtoroPositionCurrencyMetadata", ...]
    capture_started_at: datetime
    capture_completed_at: datetime
    positions: tuple[BrokerPosition, ...]
    position_valuations: tuple["EtoroPositionValuation", ...]
    valuation_ages_seconds: tuple[Decimal, ...]
    valuation_freshness: tuple[bool, ...]
    valuation_max_age_seconds: Decimal
    security_metadata: tuple[EtoroSecurity, ...]
    available_cash: Decimal
    invested_amount: Decimal
    broker_margin: Decimal
    current_pnl: Decimal
    equity: Decimal
    checked_pending_collections: tuple[str, ...] = DEMO_PENDING_COLLECTIONS
    observed_at: None = None
    daily_pnl: None = None
    peak_equity: None = None
    scope: str = ACCOUNT_EVIDENCE_SCOPE
    limitations: tuple[str, ...] = (
        "whole_account_observation_time_unavailable",
        "numeric_asset_and_margin_currency_id_mapping_unverified",
        "bracketed_reads_are_not_an_atomic_broker_snapshot",
        "full_daily_pnl_unavailable", "peak_equity_unavailable",
        "execution_history_not_complete",
    )

    @property
    def valuations_fresh(self) -> bool:
        return bool(self.valuation_freshness) and all(self.valuation_freshness)


def _structural_state(detail: EtoroDetailedPortfolio) -> tuple:
    # Raw payload hashes retain provenance but may include changing valuation
    # fields. Compare the required economic structure independently of marks.
    return (detail.credit, detail.bonus_credit, detail.mirror_count,
            detail.pending_collection_counts,
            tuple(sorted(detail.positions, key=lambda item: item.position_id)))


def capture_pnl_account_evidence(
    client: "EtoroReadOnlyClient", *, expected_demo_customer_id: str,
    now: datetime | None = None, account_max_age_seconds: int = MAX_CAPTURE_SECONDS,
) -> EtoroPnlAccountEvidence:
    """Detailed -> P&L -> exact metadata -> detailed, one bounded read bracket.

    The result distinguishes reconciliation from valuation qualification. A
    stale/future mark is retained with a false freshness result and cannot be
    passed as an account snapshot to the baseline or pre-trade risk evaluator.
    """
    from brokers.etoro.schemas import exact_datetime_epoch_seconds

    if client.environment != Environment.DEMO or not client.read_only:
        raise BrokerDataUnavailable("account_capture_requires_demo_read_only_client")
    if isinstance(expected_demo_customer_id, bool):
        raise BrokerDataUnavailable("expected_demo_account_identity_required")
    expected = str(expected_demo_customer_id).strip()
    if not expected.isascii() or not expected.isdecimal() or int(expected) <= 0:
        raise BrokerDataUnavailable("expected_demo_account_identity_required")
    expected = str(int(expected))
    if (isinstance(account_max_age_seconds, bool)
            or not isinstance(account_max_age_seconds, int)
            or not 0 < account_max_age_seconds <= MAX_CAPTURE_SECONDS):
        raise BrokerDataUnavailable("account_valuation_limit_not_valid")
    started = _moment(now or datetime.now(timezone.utc))
    first = client.get_demo_detailed_portfolio()
    _check_details(first, expected)
    # This route has no independent top-level customer identity. With no
    # positions, selection cannot be corroborated; do not accept vacuously.
    if not first.positions:
        raise BrokerDataUnavailable("pnl_account_identity_unproven_without_positions")
    pnl = client.get_demo_pnl_portfolio()
    _check_details(pnl.detailed, expected)
    securities = client.get_security_metadata(tuple(sorted({p.instrument_id for p in first.positions})))
    currencies = client.get_demo_position_currencies(expected, first.positions)
    last = client.get_demo_detailed_portfolio()
    completed = _moment(now or datetime.now(timezone.utc))
    _check_details(last, expected)
    if completed < started or (completed - started).total_seconds() > MAX_CAPTURE_SECONDS:
        raise BrokerDataUnavailable("account_capture_timing_not_valid")
    if _structural_state(first) != _structural_state(last):
        raise BrokerDataUnavailable("account_changed_during_capture")
    if _structural_state(first) != _structural_state(pnl.detailed):
        raise BrokerDataUnavailable("pnl_account_structure_or_cash_mismatch")
    if pnl.account_currency_id != 1:
        raise BrokerDataUnavailable("account_scope_not_supported: account currency")
    by_id = {item.position_id: item for item in pnl.valuations}
    if len(by_id) != len(pnl.valuations) or set(by_id) != {p.position_id for p in first.positions}:
        raise BrokerDataUnavailable("account_position_coverage_mismatch")
    metadata = {item.instrument_id: item for item in securities}
    if len(metadata) != len(securities) or set(metadata) != {p.instrument_id for p in first.positions}:
        raise BrokerDataUnavailable("account_security_metadata_coverage_mismatch")
    if any(item.instrument_type not in {"Stocks", "ETF", "Crypto"} for item in securities):
        raise BrokerDataUnavailable("account_scope_not_supported: instrument type")
    currency_by_id = {item.position_id: item for item in currencies}
    if len(currency_by_id) != len(currencies) or set(currency_by_id) != set(by_id):
        raise BrokerDataUnavailable("account_currency_metadata_coverage_mismatch")
    code_by_numeric_id = {}
    numeric_id_by_code = {}
    code_by_instrument = {}
    for position in first.positions:
        valuation = by_id[position.position_id]
        currency = currency_by_id[position.position_id]
        if (currency.instrument_id != position.instrument_id
                or currency.asset_currency not in {"USD", "GBX"}):
            raise BrokerDataUnavailable("account_currency_metadata_inconsistent")
        code = currency.asset_currency
        numeric_id = valuation.asset_currency_id
        # USD is the only numeric currency ID explicitly established by the
        # official contract. Other IDs do not establish a GBX mapping.
        if (code == "USD") != (numeric_id == 1):
            raise BrokerDataUnavailable("account_currency_metadata_inconsistent")
        # No numeric GBX mapping is invented. Reject contradictory pairings
        # within this bracket without asserting a global ID namespace.
        if (code_by_numeric_id.setdefault(numeric_id, code) != code
                or numeric_id_by_code.setdefault(code, numeric_id) != numeric_id
                or code_by_instrument.setdefault(position.instrument_id, code) != code):
            raise BrokerDataUnavailable("account_currency_metadata_inconsistent")
        if (valuation.exposure_account_currency <= 0 or valuation.exposure_asset_currency <= 0
                or valuation.margin_account_currency < 0):
            raise BrokerDataUnavailable("account_scope_not_supported: exposure or margin currency")
    current_pnl = sum((item.pnl for item in pnl.valuations), Decimal("0"))
    if abs(current_pnl - pnl.total_pnl) > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_unrealized_pnl_evidence_mismatch")
    # Official calculate-equity / calculate-available-cash formulas, reduced
    # only after proving every pending collection and mirrors explicitly empty.
    # Fees are preserved in valuations; the formula adds no second deduction
    # of position fees. Exposure and broker margin are never used as invested.
    invested = sum((item.amount for item in first.positions), Decimal("0"))
    equity = first.credit + invested + current_pnl
    if equity < 0:
        raise BrokerDataUnavailable("account_equity_identity_mismatch")
    completion_epoch = exact_datetime_epoch_seconds(completed)
    ages = tuple(completion_epoch - item.timestamp_epoch_seconds for item in pnl.valuations)
    max_age = Decimal(account_max_age_seconds)
    return EtoroPnlAccountEvidence(
        demo_customer_id=expected, detailed_snapshot_id=first.snapshot_id,
        pnl_snapshot_id=pnl.snapshot_id, closing_detailed_snapshot_id=last.snapshot_id,
        security_metadata_checksum=payload_hash(securities),
        currency_metadata_checksum=payload_hash(currencies), currency_metadata=currencies,
        capture_started_at=started, capture_completed_at=completed,
        positions=tuple(BrokerPosition(
            broker_position_id=p.position_id, broker_instrument_id=p.instrument_id,
            units=p.units, current_exposure=by_id[p.position_id].exposure_account_currency,
            average_leverage=p.leverage, pnl=by_id[p.position_id].pnl, currency="USD",
        ) for p in first.positions),
        position_valuations=pnl.valuations, valuation_ages_seconds=ages,
        valuation_freshness=tuple(Decimal("0") <= age <= max_age for age in ages),
        valuation_max_age_seconds=max_age, security_metadata=securities,
        available_cash=first.credit, invested_amount=invested,
        broker_margin=sum((item.margin_account_currency for item in pnl.valuations), Decimal("0")),
        current_pnl=current_pnl, equity=equity,
    )
