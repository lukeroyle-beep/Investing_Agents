"""Versioned, full-account performance arithmetic; no trading/economic writes.

All monetary inputs are account-currency Decimals. Performance uses equity
change less deposits/withdrawals; execution principal is an internal transfer.
History/provenance is input evidence, never inferred from a missing collection.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, time, timezone
from decimal import Decimal, localcontext
from enum import StrEnum
from fractions import Fraction
from zoneinfo import ZoneInfo

from execution.domain import payload_hash


MODEL_VERSION = "account-performance-v1"
ATOMIC_TIME_MODEL = "authoritative-account-v1"
COMPONENT_TIME_MODEL = "etoro-component-time-v1"
ZERO = Decimal("0")
ONE = Decimal("1")


class AccountingEvidenceError(ValueError):
    """Contradictory evidence cannot become a performance observation."""


def _at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AccountingEvidenceError("timezone_aware_accounting_time_required")
    return value.astimezone(timezone.utc)


def _decimal(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AccountingEvidenceError("finite_decimal_accounting_value_required")
    return value


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountingEvidenceError("accounting_evidence_identity_required")
    return value


def _sum(values) -> Decimal:
    # Retain every supplied coefficient digit, including small fractions next
    # to large balances. Division uses the separately documented 50-digit model.
    values = tuple(values)
    if not values:
        return ZERO
    precision = max(value.adjusted() for value in values) - min(value.as_tuple().exponent for value in values) + len(str(len(values))) + 3
    with localcontext() as ctx:
        ctx.prec = max(50, precision)
        return sum(values, ZERO)


class EventKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    RESET = "reset"
    DIVIDEND = "dividend"
    FEE = "fee"
    FINANCING = "financing"
    CURRENCY_CONVERSION = "currency_conversion"
    PURCHASE = "purchase"
    SALE = "sale"


REQUIRED_EVENT_KINDS = frozenset(EventKind)


@dataclass(frozen=True, slots=True)
class AccountValuation:
    """Reconciled account values, with atomic and component clocks distinct.

    component_end_at bounds the history interval; it is never an atomic account
    observation. A component bracket cannot establish exact midnight equity.
    Source contract and reconciliation references must be inspectable by the
    caller. Construction validates accounting, not the truth of provenance.
    """

    account_id: str
    snapshot_id: str
    currency: str
    cash: Decimal
    invested_amount: Decimal
    unrealised_pnl: Decimal
    equity: Decimal
    broker_margin: Decimal
    gross_exposure: Decimal
    reconciliation_reference: str
    time_evidence_hash: str
    valuation_at: datetime | None = None
    component_start_at: datetime | None = None
    component_end_at: datetime | None = None
    time_model: str = ATOMIC_TIME_MODEL

    def __post_init__(self):
        for name in ("account_id", "snapshot_id", "reconciliation_reference", "time_evidence_hash"):
            _identity(getattr(self, name))
        if self.currency != "USD":
            raise AccountingEvidenceError("unsupported_account_currency")
        for name in ("cash", "invested_amount", "unrealised_pnl", "equity", "broker_margin", "gross_exposure"):
            _decimal(getattr(self, name))
        if self.equity <= ZERO or min(self.cash, self.invested_amount, self.broker_margin, self.gross_exposure) < ZERO:
            raise AccountingEvidenceError("invalid_account_valuation")
        if self.equity != _sum((self.cash, self.invested_amount, self.unrealised_pnl)):
            raise AccountingEvidenceError("equity_cash_invested_unrealised_identity_failed")
        if self.time_model == ATOMIC_TIME_MODEL:
            object.__setattr__(self, "valuation_at", _at(self.valuation_at))
            if self.component_start_at is not None or self.component_end_at is not None:
                raise AccountingEvidenceError("atomic_and_component_clocks_must_not_be_mixed")
        elif self.time_model == COMPONENT_TIME_MODEL:
            if self.valuation_at is not None:
                raise AccountingEvidenceError("component_time_cannot_claim_atomic_account_time")
            object.__setattr__(self, "component_start_at", _at(self.component_start_at))
            object.__setattr__(self, "component_end_at", _at(self.component_end_at))
            if self.component_start_at > self.component_end_at:
                raise AccountingEvidenceError("component_clock_order_invalid")
        else:
            raise AccountingEvidenceError("unsupported_valuation_time_model")

    @property
    def coverage_at(self) -> datetime:
        """History coverage endpoint, never a replacement for valuation_at."""
        return self.valuation_at if self.valuation_at is not None else self.component_end_at

    @property
    def freshness_at(self) -> datetime:
        return self.valuation_at if self.valuation_at is not None else self.component_start_at


@dataclass(frozen=True, slots=True)
class AccountingEvent:
    event_id: str
    kind: EventKind
    occurred_at: datetime
    amount: Decimal
    currency: str
    source_reference: str
    pre_flow_valuation: AccountValuation | None = None
    # Foreign-currency rows require an authoritative account-currency amount;
    # original amounts/rates are retained for audit, not recomputed with a guess.
    original_amount: Decimal | None = None
    original_currency: str | None = None
    conversion_rate: Decimal | None = None

    def __post_init__(self):
        _identity(self.event_id)
        _identity(self.source_reference)
        object.__setattr__(self, "kind", EventKind(self.kind))
        object.__setattr__(self, "occurred_at", _at(self.occurred_at))
        _decimal(self.amount)
        if self.currency != "USD":
            raise AccountingEvidenceError("event_account_currency_required")
        if self.kind == EventKind.DEPOSIT and self.amount <= ZERO:
            raise AccountingEvidenceError("deposit_sign_invalid")
        if self.kind == EventKind.WITHDRAWAL and self.amount >= ZERO:
            raise AccountingEvidenceError("withdrawal_sign_invalid")
        if self.pre_flow_valuation is not None and self.kind not in {EventKind.DEPOSIT, EventKind.WITHDRAWAL}:
            raise AccountingEvidenceError("preflow_valuation_only_for_external_flows")
        foreign = (self.original_amount, self.original_currency, self.conversion_rate)
        if any(value is not None for value in foreign):
            if any(value is None for value in foreign):
                raise AccountingEvidenceError("complete_currency_conversion_evidence_required")
            _decimal(self.original_amount)
            _decimal(self.conversion_rate)
            _identity(self.original_currency)
            if self.conversion_rate <= ZERO:
                raise AccountingEvidenceError("positive_conversion_rate_required")
        if self.kind == EventKind.CURRENCY_CONVERSION and all(value is None for value in foreign):
            raise AccountingEvidenceError("currency_conversion_evidence_required")

    @property
    def external_amount(self) -> Decimal:
        return self.amount if self.kind in {EventKind.DEPOSIT, EventKind.WITHDRAWAL} else ZERO


@dataclass(frozen=True, slots=True)
class HistoryCoverage:
    account_id: str
    currency: str
    start_at: datetime
    end_at: datetime
    start_cursor: str
    end_cursor: str
    source_reference: str
    source_contract: str
    raw_evidence_hash: str
    events: tuple[AccountingEvent, ...]
    covered_kinds: frozenset[EventKind]
    complete: bool
    broker_history_verified: bool
    # Historical peak beyond the supplied valuations is otherwise unknown.
    valuation_path_complete: bool = False

    def __post_init__(self):
        for name in ("account_id", "start_cursor", "end_cursor", "source_reference", "source_contract", "raw_evidence_hash"):
            _identity(getattr(self, name))
        object.__setattr__(self, "start_at", _at(self.start_at))
        object.__setattr__(self, "end_at", _at(self.end_at))
        if self.end_at <= self.start_at or self.currency != "USD":
            raise AccountingEvidenceError("invalid_history_interval_or_currency")
        if any(type(getattr(self, name)) is not bool for name in ("complete", "broker_history_verified", "valuation_path_complete")):
            raise AccountingEvidenceError("typed_history_coverage_flags_required")
        if not isinstance(self.events, tuple) or not all(isinstance(item, AccountingEvent) for item in self.events):
            raise AccountingEvidenceError("typed_accounting_events_required")
        if not isinstance(self.covered_kinds, frozenset) or not self.covered_kinds <= REQUIRED_EVENT_KINDS or not all(isinstance(kind, EventKind) for kind in self.covered_kinds):
            raise AccountingEvidenceError("typed_history_category_coverage_required")
        if len({item.event_id for item in self.events}) != len(self.events):
            raise AccountingEvidenceError("duplicate_accounting_event")
        if any(not self.start_at < item.occurred_at <= self.end_at for item in self.events):
            raise AccountingEvidenceError("accounting_event_outside_history_interval")
        if any(item.currency != self.currency or item.kind not in self.covered_kinds for item in self.events):
            raise AccountingEvidenceError("event_outside_currency_or_category_coverage")


@dataclass(frozen=True, slots=True)
class PerformanceEvidence:
    current_snapshot_id: str
    account_id: str
    currency: str
    risk_day: str
    risk_timezone: str
    coverage_start_at: datetime | None
    coverage_end_at: datetime
    daily_pnl: Decimal | None
    daily_loss_denominator: Decimal | None
    daily_loss_fraction: Decimal | None
    equity_index: Decimal | None
    peak_equity_index: Decimal | None
    drawdown_fraction: Decimal | None
    history_complete: bool
    qualified: bool
    reasons: tuple[str, ...]
    current_valuation: AccountValuation | None = None
    model_version: str = MODEL_VERSION
    evidence_hash: str = ""
    projection_hash: str = ""

    def __post_init__(self):
        digest = self._projection_digest()
        if self.projection_hash and self.projection_hash != digest:
            raise AccountingEvidenceError("performance_projection_digest_mismatch")
        object.__setattr__(self, "projection_hash", digest)

    def _projection_digest(self):
        return payload_hash({item.name: getattr(self, item.name) for item in fields(self)
                             if item.name != "projection_hash"})

    def projection_valid(self) -> bool:
        return self.projection_hash == self._projection_digest()


def evaluate_performance(*, opening: AccountValuation | None,
                         current: AccountValuation,
                         history: HistoryCoverage | None,
                         observations: tuple[AccountValuation, ...] = (),
                         now: datetime,
                         max_age_seconds: int = 15,
                         risk_timezone: str = "Europe/London") -> PerformanceEvidence:
    """Compute covered daily performance and a flow-unitised observed path.

    Purchases/sales/FX are internal; dividends, fees and financing remain in
    equity performance. Resets invalidate continuity. A complete valuation path
    must be proven separately from complete transaction history. Unknowns never
    turn into zero. A new day resets only daily P&L, never an earlier drawdown.
    The opening is the immutable drawdown inception, not a movable daily reset.
    """
    if not isinstance(current, AccountValuation) or (opening is not None and not isinstance(opening, AccountValuation)):
        raise AccountingEvidenceError("typed_account_valuations_required")
    if history is not None and not isinstance(history, HistoryCoverage):
        raise AccountingEvidenceError("typed_history_coverage_required")
    if not isinstance(observations, tuple) or not all(isinstance(item, AccountValuation) for item in observations):
        raise AccountingEvidenceError("typed_valuation_path_required")
    if risk_timezone != "Europe/London" or type(max_age_seconds) is not int or not 0 < max_age_seconds <= 15:
        raise AccountingEvidenceError("unsupported_risk_day_or_freshness_window")
    now = _at(now)
    zone = ZoneInfo(risk_timezone)
    risk_day = current.coverage_at.astimezone(zone).date()
    boundary = datetime.combine(risk_day, time.min, zone).astimezone(timezone.utc)
    reasons: list[str] = []
    ages = ((now - current.freshness_at).total_seconds(), (now - current.coverage_at).total_seconds())
    if min(ages) < 0 or max(ages) > max_age_seconds:
        reasons.append("current_valuation_stale_or_future")
    if opening is None:
        reasons.append("immutable_opening_valuation_missing")
    if history is None:
        reasons.append("account_history_missing")
    path = tuple(sorted((*observations, current), key=lambda item: item.coverage_at))
    if opening is not None:
        path = (opening, *(item for item in path if item != opening))
    if any(item.coverage_at >= current.coverage_at for item in observations):
        raise AccountingEvidenceError("historical_valuation_outside_current_interval")
    if len({item.snapshot_id for item in path}) != len(path) or len({item.coverage_at for item in path}) != len(path):
        raise AccountingEvidenceError("duplicate_or_ambiguous_valuation_path")
    if any(item.account_id != current.account_id or item.currency != current.currency for item in path):
        raise AccountingEvidenceError("valuation_account_or_currency_mismatch")
    if any(left.coverage_at >= right.coverage_at for left, right in zip(path, path[1:])):
        raise AccountingEvidenceError("valuation_path_not_strictly_increasing")
    complete = False
    if history is not None:
        if history.account_id != current.account_id or history.currency != current.currency:
            raise AccountingEvidenceError("history_account_or_currency_mismatch")
        complete = (history.complete and history.broker_history_verified
                    and history.covered_kinds == REQUIRED_EVENT_KINDS
                    and opening is not None and history.start_at == opening.coverage_at
                    and history.end_at == current.coverage_at)
        if not complete:
            reasons.append("complete_verified_all_category_history_required")
        if any(item.kind == EventKind.RESET for item in history.events):
            complete = False
            reasons.append("account_reset_breaks_continuity")
    day_open = next((item for item in path if item.valuation_at == boundary), None)
    daily = denominator = daily_loss = index = peak = drawdown = None
    if day_open is None:
        reasons.append("exact_risk_day_boundary_valuation_missing")
    flow_order_unknown = (history is not None and any(
        item.external_amount != ZERO and item.occurred_at in {value.coverage_at for value in path}
        for item in history.events))
    if flow_order_unknown:
        reasons.append("external_flow_valuation_order_unproven")
    if complete and day_open is not None and not flow_order_unknown:
        flows = _sum(item.external_amount for item in history.events if item.occurred_at > boundary)
        daily = _sum((current.equity, day_open.equity.copy_negate(), flows.copy_negate()))
        denominator = day_open.equity
        with localcontext() as ctx:
            ctx.prec = 50
            daily_loss = max(ZERO, -daily / denominator)
    if complete and not flow_order_unknown:
        index, peak, drawdown, drawdown_reasons = _unitised_path(opening, path[1:], history)
        reasons.extend(drawdown_reasons)
    result_fields = dict(
        current_snapshot_id=current.snapshot_id, account_id=current.account_id,
        currency=current.currency, risk_day=risk_day.isoformat(), risk_timezone=risk_timezone,
        coverage_start_at=opening.coverage_at if opening else None,
        coverage_end_at=current.coverage_at, daily_pnl=daily,
        daily_loss_denominator=denominator, daily_loss_fraction=daily_loss,
        equity_index=index, peak_equity_index=peak, drawdown_fraction=drawdown,
        history_complete=complete, qualified=not reasons,
        reasons=tuple(reasons), current_valuation=current, model_version=MODEL_VERSION,
    )
    return PerformanceEvidence(**result_fields, evidence_hash=payload_hash({
        "result": result_fields, "opening": opening, "current": current,
        "history": _history_payload(history), "observations": observations,
    }))


def _unitised_path(opening, path, history):
    flows = tuple(sorted((item for item in history.events if item.external_amount != ZERO), key=lambda item: item.occurred_at))
    if len({item.occurred_at for item in flows}) != len(flows):
        return None, None, None, ["same_time_external_flow_order_unproven"]
    reasons = []
    for flow in flows:
        value = flow.pre_flow_valuation
        if value is None or value.valuation_at != flow.occurred_at:
            return None, None, None, ["external_flow_immediate_preflow_valuation_missing"]
        if value.account_id != opening.account_id or value.currency != opening.currency:
            raise AccountingEvidenceError("preflow_valuation_identity_mismatch")
        if _sum((value.equity, flow.external_amount)) <= ZERO:
            return None, None, None, ["external_flow_exhausts_unitised_equity"]
    if not history.valuation_path_complete:
        reasons.append("complete_valuation_path_not_proven")
    # Exact rational units avoid accumulating rounding in a deposit followed
    # by a withdrawal. Only the public ratio is converted to 50-digit Decimal.
    units = Fraction(opening.equity)
    peak = Fraction(1)
    index = Fraction(1)
    remaining = iter(flows)
    flow = next(remaining, None)
    for value in path:
        while flow is not None and flow.occurred_at <= value.coverage_at:
            before_index = Fraction(flow.pre_flow_valuation.equity) / units
            peak = max(peak, before_index)
            units += Fraction(flow.external_amount) / before_index
            flow = next(remaining, None)
        index = Fraction(value.equity) / units
        peak = max(peak, index)
    observed_drawdown = max(Fraction(0), 1 - index / peak)
    with localcontext() as ctx:
        ctx.prec = 50
        convert = lambda ratio: Decimal(ratio.numerator) / Decimal(ratio.denominator)
        decimal_index, decimal_peak, decimal_drawdown = map(convert, (index, peak, observed_drawdown))
    # Retain the observable indexes, but do not call the observed peak the full
    # covered peak when historical valuation coverage is missing.
    return decimal_index, decimal_peak, None if reasons else decimal_drawdown, reasons


def _history_payload(history):
    if history is None:
        return None
    body = {item.name: getattr(history, item.name) for item in fields(history)}
    body["covered_kinds"] = tuple(sorted(history.covered_kinds))
    body["events"] = tuple(sorted(history.events, key=lambda item: (item.occurred_at, item.event_id)))
    return body


def valuation_from_component_time(proof, *, now: datetime, reconciliation_reference: str) -> AccountValuation:
    """Convert only a currently qualified source; never grants history coverage.

    The caller retains the source proof and must revalidate it at subsequent
    baseline/risk admission. The local bracket remains explicitly non-atomic.
    """
    from brokers.etoro.account_evidence import AccountComponentTimeEvidence
    if not isinstance(proof, AccountComponentTimeEvidence):
        raise AccountingEvidenceError("typed_component_evidence_required")
    if blockers := proof.validate_at(_at(now)):
        raise AccountingEvidenceError("component_valuation_unqualified:" + ",".join(blockers))
    return AccountValuation(
        account_id=proof.demo_customer_id, snapshot_id=proof.source_evidence_sha256,
        currency=proof.currency, cash=proof.available_cash, invested_amount=proof.invested_amount,
        unrealised_pnl=proof.current_pnl, equity=proof.equity, broker_margin=proof.broker_margin,
        gross_exposure=_sum(item.current_exposure.copy_abs() for item in proof.positions),
        reconciliation_reference=reconciliation_reference, time_evidence_hash=proof.evidence_hash,
        valuation_at=None, component_start_at=proof.earliest_component_at,
        component_end_at=proof.latest_component_at, time_model=proof.model_version,
    )
