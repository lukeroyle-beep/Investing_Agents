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
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import re
from typing import TYPE_CHECKING

from brokers.base import BrokerAccountSnapshot, BrokerDataUnavailable, BrokerPosition
from brokers.etoro.schemas import (
    DEMO_PENDING_COLLECTIONS, EtoroDetailedPortfolio, EtoroInstrumentAggregate,
    EtoroPnlPortfolio, EtoroSecurity, EtoroPositionValuation,
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


def _exact_sum(values) -> Decimal:
    """Add source coefficients without losing tiny values beside balances."""
    values = tuple(values)
    if not values:
        return Decimal("0")
    precision = max(value.adjusted() for value in values) - min(value.as_tuple().exponent for value in values) + len(str(len(values))) + 3
    with localcontext() as context:
        context.prec = max(50, precision)
        return sum(values, Decimal("0"))


def _difference(left: Decimal, *right: Decimal) -> Decimal:
    return _exact_sum((left, *(value.copy_negate() for value in right)))


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
    invested_amount = _exact_sum(position.amount for position in first.positions)
    for position in first.positions:
        units_by_instrument[position.instrument_id] = _exact_sum((units_by_instrument.get(position.instrument_id, Decimal("0")), position.units))
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
        if _difference(units, item.units).copy_abs() > UNITS_TOLERANCE:
            raise BrokerDataUnavailable("account_position_units_mismatch")
        if item.margin < 0:
            raise BrokerDataUnavailable("account_margin_evidence_mismatch")
        # This identity is explicitly documented for liquidation value. Market
        # exposure is a separate broker field: collateral, fees and currency
        # treatment mean it must never be replaced by margin plus P&L.
        if _difference(item.liquidation_value, item.margin, item.pnl).copy_abs() > MONEY_TOLERANCE:
            raise BrokerDataUnavailable("account_liquidation_evidence_mismatch")

    totals = aggregate.totals
    if totals.frozen_cash != 0:
        raise BrokerDataUnavailable("empty_pending_orders_contradict_reserved_cash")
    if _difference(totals.available_cash, first.credit).copy_abs() > MONEY_TOLERANCE or _difference(totals.balance, totals.available_cash).copy_abs() > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_cash_evidence_mismatch")
    if totals.used_margin < 0 or _difference(totals.total_value, totals.available_cash, invested_amount, totals.current_pnl).copy_abs() > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_equity_identity_mismatch")
    if _difference(totals.used_margin, _exact_sum(item.margin for item in aggregate.instrument_aggregates)).copy_abs() > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_margin_evidence_mismatch")
    if _difference(totals.current_pnl, _exact_sum(item.pnl for item in aggregate.instrument_aggregates)).copy_abs() > MONEY_TOLERANCE:
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
        invested_amount=invested_amount,
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


def _pnl_expected_identity(expected_demo_customer_id: str) -> str:
    if isinstance(expected_demo_customer_id, bool):
        raise BrokerDataUnavailable("expected_demo_account_identity_required")
    expected = str(expected_demo_customer_id).strip()
    if not expected.isascii() or not expected.isdecimal() or int(expected) <= 0:
        raise BrokerDataUnavailable("expected_demo_account_identity_required")
    return str(int(expected))


def _check_pnl_valuation_limit(account_max_age_seconds: int) -> None:
    if (isinstance(account_max_age_seconds, bool)
            or not isinstance(account_max_age_seconds, int)
            or not 0 < account_max_age_seconds <= MAX_CAPTURE_SECONDS):
        raise BrokerDataUnavailable("account_valuation_limit_not_valid")


def capture_pnl_account_evidence(
    client: "EtoroReadOnlyClient", *, expected_demo_customer_id: str,
    now: datetime | None = None, account_max_age_seconds: int = MAX_CAPTURE_SECONDS,
) -> EtoroPnlAccountEvidence:
    """Detailed -> P&L -> exact metadata -> detailed, one bounded read bracket.

    The result distinguishes reconciliation from valuation qualification. A
    stale/future mark is retained with a false freshness result and cannot be
    passed as an account snapshot to the baseline or pre-trade risk evaluator.
    """
    if client.environment != Environment.DEMO or not client.read_only:
        raise BrokerDataUnavailable("account_capture_requires_demo_read_only_client")
    expected = _pnl_expected_identity(expected_demo_customer_id)
    _check_pnl_valuation_limit(account_max_age_seconds)
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
    return validate_pnl_account_evidence(
        first=first, pnl=pnl, securities=securities, currencies=currencies,
        last=last, expected_demo_customer_id=expected,
        capture_started_at=started, capture_completed_at=completed,
        account_max_age_seconds=account_max_age_seconds,
    )


def validate_pnl_account_evidence(
    *, first: EtoroDetailedPortfolio, pnl: EtoroPnlPortfolio,
    securities: tuple[EtoroSecurity, ...],
    currencies: tuple["EtoroPositionCurrencyMetadata", ...],
    last: EtoroDetailedPortfolio, expected_demo_customer_id: str,
    capture_started_at: datetime, capture_completed_at: datetime,
    account_max_age_seconds: int = MAX_CAPTURE_SECONDS,
) -> EtoroPnlAccountEvidence:
    """Validate strictly parsed bracket inputs without reading or storing data.

    Supplied collection times bound the original capture only. No current
    clock, broker client, baseline or whole-account observation is introduced.
    """
    from brokers.etoro.schemas import exact_datetime_epoch_seconds

    expected = _pnl_expected_identity(expected_demo_customer_id)
    _check_pnl_valuation_limit(account_max_age_seconds)
    started = _moment(capture_started_at)
    completed = _moment(capture_completed_at)
    _check_details(first, expected)
    if not first.positions:
        raise BrokerDataUnavailable("pnl_account_identity_unproven_without_positions")
    _check_details(pnl.detailed, expected)
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
    current_pnl = _exact_sum(item.pnl for item in pnl.valuations)
    if _difference(current_pnl, pnl.total_pnl).copy_abs() > MONEY_TOLERANCE:
        raise BrokerDataUnavailable("account_unrealized_pnl_evidence_mismatch")
    # Official calculate-equity / calculate-available-cash formulas, reduced
    # only after proving every pending collection and mirrors explicitly empty.
    # Fees are preserved in valuations; the formula adds no second deduction
    # of position fees. Exposure and broker margin are never used as invested.
    invested = _exact_sum(item.amount for item in first.positions)
    equity = _exact_sum((first.credit, invested, current_pnl))
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
        broker_margin=_exact_sum(item.margin_account_currency for item in pnl.valuations),
        current_pnl=current_pnl, equity=equity,
    )


# Additive v1 contract: do not add serialized fields to EtoroPnlAccountEvidence.
# Retained PR13 evidence hashes must continue to reproduce byte-for-byte.
COMPONENT_TIME_MODEL_VERSION = "etoro-component-time-v1"


@dataclass(frozen=True, slots=True)
class ComponentCashObservation:
    """A separately sourced broker cash observation, not a collection clock.

    This is an input contract, not an assertion that an eToro endpoint currently
    supplies it. The source adapter must establish the documented timestamp
    semantics and retain the source payload. A digest alone authenticates
    neither a response nor its claimed observation time.
    """

    demo_customer_id: str
    available_cash: Decimal
    observed_at: datetime
    source_endpoint: str
    source_timestamp_field: str
    source_sha256: str
    source_timestamp: str
    currency: str = "USD"

    def __post_init__(self):
        object.__setattr__(self, "demo_customer_id", _pnl_expected_identity(self.demo_customer_id))
        object.__setattr__(self, "observed_at", _moment(self.observed_at))
        from brokers.etoro.schemas import exact_datetime_epoch_seconds
        delta = _difference(self.timestamp_epoch_seconds, exact_datetime_epoch_seconds(self.observed_at))
        if not Decimal("0") <= delta < Decimal("0.000001"):
            raise BrokerDataUnavailable("cash_component_source_time_mismatch")
        if (not isinstance(self.available_cash, Decimal)
                or not self.available_cash.is_finite() or self.available_cash < 0
                or self.currency != "USD"):
            raise BrokerDataUnavailable("cash_component_value_not_valid")
        if (not isinstance(self.source_endpoint, str)
                or not self.source_endpoint.startswith("/api/")
                or "demo" not in self.source_endpoint.split("/")
                or not isinstance(self.source_timestamp_field, str)
                or not self.source_timestamp_field.strip()
                or not isinstance(self.source_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None):
            raise BrokerDataUnavailable("cash_component_source_provenance_required")

    @property
    def timestamp_epoch_seconds(self) -> Decimal:
        # Reuse the exact offset-bearing source timestamp grammar. The datetime
        # field is merely its microsecond-compatible view; qualification and
        # dispatch deadlines use the original text's full precision instead.
        from brokers.etoro.schemas import _pnl_timestamp
        return _pnl_timestamp(self.source_timestamp)[1]


@dataclass(frozen=True, slots=True)
class AccountComponentTimeEvidence:
    """Explicit non-atomic account components with rechecked time bounds.

    The bracket proves matching cash/holdings and enumeration at three reads.
    Its local collection interval does not prove the broker cash observation
    time. A distinct authoritative cash observation is therefore required for
    qualification. No account ``observed_at`` is synthesized by this contract.
    Existing mirrors or pending orders fail the underlying whole-account
    validator; they cannot be dropped from this evidence to fit the scope.
    """

    evidence: EtoroPnlAccountEvidence
    assessed_at: datetime
    source_evidence_sha256: str
    cash_observation: ComponentCashObservation | None = None
    max_component_skew_seconds: Decimal = Decimal("15")
    model_version: str = COMPONENT_TIME_MODEL_VERSION
    observed_at: None = None

    def __post_init__(self):
        if not isinstance(self.evidence, EtoroPnlAccountEvidence):
            raise BrokerDataUnavailable("typed_pnl_account_evidence_required")
        object.__setattr__(self, "assessed_at", _moment(self.assessed_at))
        if (self.model_version != COMPONENT_TIME_MODEL_VERSION or self.observed_at is not None
                or not isinstance(self.max_component_skew_seconds, Decimal)
                or not self.max_component_skew_seconds.is_finite()
                or not Decimal("0") <= self.max_component_skew_seconds <= Decimal(MAX_CAPTURE_SECONDS)):
            raise BrokerDataUnavailable("component_time_model_not_supported")
        if self.source_evidence_sha256 != payload_hash(self.evidence):
            raise BrokerDataUnavailable("component_source_evidence_hash_mismatch")
        if self.cash_observation is not None and not isinstance(self.cash_observation, ComponentCashObservation):
            raise BrokerDataUnavailable("typed_cash_component_required")

    @property
    def earliest_component_at(self) -> datetime:
        """Local bracket start; never an atomic/source account observation."""
        return self.evidence.capture_started_at

    @property
    def latest_component_at(self) -> datetime:
        """Local bracket completion; never an atomic/source observation."""
        return self.evidence.capture_completed_at

    @property
    def evidence_hash(self) -> str:
        return payload_hash(self)

    @property
    def demo_customer_id(self) -> str:
        return self.evidence.demo_customer_id

    @property
    def currency(self) -> str:
        return "USD"

    @property
    def positions(self) -> tuple[BrokerPosition, ...]:
        return self.evidence.positions

    @property
    def available_cash(self) -> Decimal:
        return self.evidence.available_cash

    @property
    def invested_amount(self) -> Decimal:
        return self.evidence.invested_amount

    @property
    def broker_margin(self) -> Decimal:
        return self.evidence.broker_margin

    @property
    def current_pnl(self) -> Decimal:
        return self.evidence.current_pnl

    @property
    def equity(self) -> Decimal:
        return self.evidence.equity

    @property
    def qualified(self) -> bool:
        return not self.validate_at(self.assessed_at)

    def freshness_deadline_epoch_seconds(self, max_age_seconds: int = MAX_CAPTURE_SECONDS) -> Decimal:
        """Earliest component expiry as exact Decimal seconds, not a source time."""
        from brokers.etoro.schemas import exact_datetime_epoch_seconds

        _check_pnl_valuation_limit(max_age_seconds)
        blockers = self.validate_at(self.assessed_at, max_age_seconds)
        if blockers:
            raise BrokerDataUnavailable(";".join(blockers))
        limit = min(Decimal(max_age_seconds), self.evidence.valuation_max_age_seconds)
        clocks = (
            exact_datetime_epoch_seconds(self.evidence.capture_started_at),
            self.cash_observation.timestamp_epoch_seconds,
            *(value.timestamp_epoch_seconds for value in self.evidence.position_valuations),
        )
        return _exact_sum((min(clocks), limit))

    def freshness_deadline(self, max_age_seconds: int = MAX_CAPTURE_SECONDS) -> datetime:
        """Conservative microsecond expiry for host dispatch guards.

        Rounding is only downward for this derived deadline. Original source
        times and exact deadline remain available without precision loss.
        """
        from decimal import ROUND_FLOOR

        deadline = self.freshness_deadline_epoch_seconds(max_age_seconds)
        microseconds = int((deadline * Decimal("1000000")).to_integral_value(rounding=ROUND_FLOOR))
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=microseconds)

    def validate_at(self, now: datetime, age_limit: int = MAX_CAPTURE_SECONDS) -> tuple[str, ...]:
        """Return all blockers using original component clocks at ``now``.

        Reassessment never renews receipt or valuation clocks and cannot repair
        a mark that was future/stale during the original capture. No daily-PnL,
        history, baseline, or execution authority follows from an empty result.
        """
        from brokers.etoro.schemas import exact_datetime_epoch_seconds

        now = _moment(now)
        _check_pnl_valuation_limit(age_limit)
        evidence = self.evidence
        blockers: list[str] = []
        if self.source_evidence_sha256 != payload_hash(evidence):
            blockers.append("component_source_evidence_hash_mismatch")
        if self.model_version != COMPONENT_TIME_MODEL_VERSION or self.observed_at is not None:
            blockers.append("component_time_model_not_supported")
        if evidence.scope != ACCOUNT_EVIDENCE_SCOPE or evidence.observed_at is not None:
            blockers.append("component_account_scope_not_supported")
        if evidence.checked_pending_collections != DEMO_PENDING_COLLECTIONS:
            blockers.append("component_pending_coverage_incomplete")
        started = exact_datetime_epoch_seconds(_moment(evidence.capture_started_at))
        completed = exact_datetime_epoch_seconds(_moment(evidence.capture_completed_at))
        current = exact_datetime_epoch_seconds(now)
        limit = min(Decimal(age_limit), evidence.valuation_max_age_seconds)
        if (not limit.is_finite() or limit <= 0 or limit > MAX_CAPTURE_SECONDS):
            blockers.append("component_valuation_limit_not_valid")
            limit = Decimal(age_limit)
        if completed < started or completed - started > MAX_CAPTURE_SECONDS:
            blockers.append("component_capture_interval_not_valid")
        # Structural evidence is receipt-bounded. This check limits its age;
        # it does not claim that a broker source timestamp was present.
        if current < completed or current - started > limit:
            blockers.append("component_structure_stale_or_future")

        positions = {p.broker_position_id: p for p in evidence.positions}
        valuations = {v.position_id: v for v in evidence.position_valuations}
        currencies = {c.position_id: c for c in evidence.currency_metadata}
        metadata = {s.instrument_id: s for s in evidence.security_metadata}
        if (not positions or len(positions) != len(evidence.positions)
                or None in positions or len(valuations) != len(evidence.position_valuations)
                or len(currencies) != len(evidence.currency_metadata)
                or set(positions) != set(valuations) or set(positions) != set(currencies)
                or len(metadata) != len(evidence.security_metadata)
                or set(metadata) != {p.broker_instrument_id for p in evidence.positions}):
            blockers.append("component_position_coverage_incomplete")
        if (payload_hash(evidence.security_metadata) != evidence.security_metadata_checksum
                or payload_hash(evidence.currency_metadata) != evidence.currency_metadata_checksum):
            blockers.append("component_metadata_hash_mismatch")
        amounts = (evidence.available_cash, evidence.invested_amount,
                   evidence.broker_margin, evidence.current_pnl, evidence.equity)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in amounts):
            blockers.append("component_decimal_account_values_required")
        else:
            if (evidence.available_cash < 0 or evidence.invested_amount < 0
                    or evidence.broker_margin < 0 or evidence.equity <= 0
                    or evidence.equity != _exact_sum((evidence.available_cash, evidence.invested_amount, evidence.current_pnl))):
                blockers.append("component_equity_identity_mismatch")
            if (_exact_sum(v.pnl for v in valuations.values()) != evidence.current_pnl
                    or _exact_sum(v.margin_account_currency for v in valuations.values()) != evidence.broker_margin):
                blockers.append("component_valuation_totals_mismatch")
        for position_id in set(positions) & set(valuations) & set(currencies):
            position, valuation, currency = positions[position_id], valuations[position_id], currencies[position_id]
            if (position.currency != "USD" or position.average_leverage != 1
                    or position.units <= 0 or position.current_exposure <= 0
                    or position.current_exposure != valuation.exposure_account_currency
                    or position.pnl != valuation.pnl
                    or currency.instrument_id != position.broker_instrument_id
                    or currency.asset_currency not in {"USD", "GBX"}):
                blockers.append("component_position_evidence_inconsistent")
        source_times = tuple(v.timestamp_epoch_seconds for v in evidence.position_valuations)
        if not source_times or any(not Decimal("0") <= completed - stamp <= limit for stamp in source_times):
            blockers.append("component_original_valuation_stale_or_future")
        if not source_times or any(not Decimal("0") <= current - stamp <= limit for stamp in source_times):
            blockers.append("component_current_valuation_stale_or_future")
        # Include both collection endpoints, every valuation and independent
        # cash observation in the maximum skew calculation. Decimal preserves
        # seventh-digit source-time boundaries.
        times = [started, completed, *source_times]
        cash = self.cash_observation
        if cash is None:
            blockers.append("cash_component_observation_unproven")
        else:
            cash_time = cash.timestamp_epoch_seconds
            times.append(cash_time)
            if (cash.demo_customer_id != evidence.demo_customer_id or cash.currency != "USD"
                    or cash.available_cash != evidence.available_cash):
                blockers.append("cash_component_identity_or_value_mismatch")
            if (not Decimal("0") <= completed - cash_time <= limit
                    or not Decimal("0") <= current - cash_time <= limit):
                blockers.append("cash_component_stale_or_future")
        if max(times) - min(times) > self.max_component_skew_seconds:
            blockers.append("component_time_skew_exceeded")
        return tuple(dict.fromkeys(blockers))


def assess_pnl_component_time_evidence(
    evidence: EtoroPnlAccountEvidence, *, assessed_at: datetime,
    cash_observation: ComponentCashObservation | None = None,
    max_component_skew_seconds: Decimal = Decimal("15"),
) -> AccountComponentTimeEvidence:
    """Construct an assessment; inspect ``validate_at`` before any admission."""
    return AccountComponentTimeEvidence(
        evidence=evidence, assessed_at=assessed_at,
        source_evidence_sha256=payload_hash(evidence),
        cash_observation=cash_observation,
        max_component_skew_seconds=max_component_skew_seconds,
    )


def qualify_pnl_component_time_evidence(
    evidence: EtoroPnlAccountEvidence, *, assessed_at: datetime,
    cash_observation: ComponentCashObservation | None = None,
    max_component_skew_seconds: Decimal = Decimal("15"),
) -> AccountComponentTimeEvidence:
    """Require complete fresh component proof; never bless receipts as cash."""
    assessment = assess_pnl_component_time_evidence(
        evidence, assessed_at=assessed_at, cash_observation=cash_observation,
        max_component_skew_seconds=max_component_skew_seconds,
    )
    blockers = assessment.validate_at(assessed_at)
    if blockers:
        raise BrokerDataUnavailable(";".join(blockers))
    return assessment
