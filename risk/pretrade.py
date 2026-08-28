from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable
from uuid import UUID, uuid4

import yaml

from execution.domain import (
    DomainValidationError,
    Environment,
    OrderIntent,
    OrderSide,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
    SizingMethod,
    _aware_utc,
    _uuid,
    decimal_value,
)
from execution.instruments import Instrument


class RiskConfigurationError(RuntimeError):
    """Missing or malformed risk configuration disables broker writes."""


class RiskEvidenceError(ValueError):
    """Malformed or contradictory account evidence fails closed."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    schema_version: str
    environment: Environment
    max_order_equity_fraction: Decimal
    max_issuer_fraction: Decimal
    max_sector_fraction: Decimal
    max_gross_fraction: Decimal
    max_net_fraction: Decimal
    min_cash_buffer_fraction: Decimal
    required_leverage: Decimal
    daily_loss_stop_fraction: Decimal
    drawdown_stop_fraction: Decimal
    quote_max_age_seconds: int
    account_max_age_seconds: int
    approval_max_age_seconds: int
    regular_hours_only: bool
    allowed_asset_types: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str) -> "RiskLimits":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RiskConfigurationError("risk configuration is missing or invalid") from exc
        if not isinstance(raw, dict):
            raise RiskConfigurationError("risk configuration must be a mapping")
        required = {
            "schema_version",
            "environment",
            "max_order_equity_fraction",
            "max_issuer_fraction",
            "max_sector_fraction",
            "max_gross_fraction",
            "max_net_fraction",
            "min_cash_buffer_fraction",
            "required_leverage",
            "daily_loss_stop_fraction",
            "drawdown_stop_fraction",
            "quote_max_age_seconds",
            "account_max_age_seconds",
            "approval_max_age_seconds",
            "regular_hours_only",
            "allowed_asset_types",
        }
        if missing := sorted(required - set(raw)):
            raise RiskConfigurationError(f"risk configuration missing keys: {missing}")
        try:
            limits = cls(
                schema_version=str(raw["schema_version"]),
                environment=Environment(str(raw["environment"]).strip().lower()),
                max_order_equity_fraction=Decimal(
                    str(raw["max_order_equity_fraction"])
                ),
                max_issuer_fraction=Decimal(str(raw["max_issuer_fraction"])),
                max_sector_fraction=Decimal(str(raw["max_sector_fraction"])),
                max_gross_fraction=Decimal(str(raw["max_gross_fraction"])),
                max_net_fraction=Decimal(str(raw["max_net_fraction"])),
                min_cash_buffer_fraction=Decimal(
                    str(raw["min_cash_buffer_fraction"])
                ),
                required_leverage=Decimal(str(raw["required_leverage"])),
                daily_loss_stop_fraction=Decimal(
                    str(raw["daily_loss_stop_fraction"])
                ),
                drawdown_stop_fraction=Decimal(str(raw["drawdown_stop_fraction"])),
                quote_max_age_seconds=int(raw["quote_max_age_seconds"]),
                account_max_age_seconds=int(raw["account_max_age_seconds"]),
                approval_max_age_seconds=int(raw["approval_max_age_seconds"]),
                regular_hours_only=raw["regular_hours_only"] is True,
                allowed_asset_types=tuple(
                    str(value).strip().lower() for value in raw["allowed_asset_types"]
                ),
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise RiskConfigurationError("risk configuration values are invalid") from exc
        fractions = (
            limits.max_order_equity_fraction,
            limits.max_issuer_fraction,
            limits.max_sector_fraction,
            limits.max_gross_fraction,
            limits.max_net_fraction,
            limits.min_cash_buffer_fraction,
            limits.daily_loss_stop_fraction,
            limits.drawdown_stop_fraction,
        )
        if any(value <= 0 or value > 1 for value in fractions):
            raise RiskConfigurationError("risk fractions must be within (0, 1]")
        if limits.required_leverage != Decimal("1"):
            raise RiskConfigurationError("initial Demo scope requires leverage exactly 1")
        if limits.environment != Environment.DEMO:
            raise RiskConfigurationError("only Demo risk configuration is authorized")
        if min(
            limits.quote_max_age_seconds,
            limits.account_max_age_seconds,
            limits.approval_max_age_seconds,
        ) <= 0:
            raise RiskConfigurationError("freshness windows must be positive")
        if not limits.regular_hours_only:
            raise RiskConfigurationError("initial Demo scope requires regular hours")
        return limits


@dataclass(frozen=True, slots=True)
class PositionEvidence:
    internal_instrument_id: UUID
    sector: str
    signed_market_value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "internal_instrument_id",
            _uuid(self.internal_instrument_id, "internal_instrument_id"),
        )
        object.__setattr__(
            self,
            "signed_market_value",
            decimal_value(self.signed_market_value, "signed_market_value"),
        )
        object.__setattr__(self, "sector", str(self.sector).strip().lower() or "unknown")


@dataclass(frozen=True, slots=True)
class PendingOrderEvidence:
    broker_order_id: str
    internal_instrument_id: UUID
    sector: str
    side: OrderSide
    remaining_notional: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "internal_instrument_id",
            _uuid(self.internal_instrument_id, "internal_instrument_id"),
        )
        object.__setattr__(self, "side", OrderSide(self.side))
        object.__setattr__(
            self,
            "remaining_notional",
            decimal_value(self.remaining_notional, "remaining_notional"),
        )
        object.__setattr__(self, "sector", str(self.sector).strip().lower() or "unknown")
        if not self.broker_order_id.strip() or self.remaining_notional < 0:
            raise RiskEvidenceError("pending order evidence is invalid")


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    snapshot_id: str
    environment: Environment
    observed_at: datetime
    currency: str
    equity: Decimal
    cash: Decimal
    daily_pnl: Decimal
    peak_equity: Decimal
    positions: tuple[PositionEvidence, ...]
    pending_orders: tuple[PendingOrderEvidence, ...]
    pending_orders_complete: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", Environment(self.environment))
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        for name in ("equity", "cash", "daily_pnl", "peak_equity"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name))
        object.__setattr__(self, "positions", tuple(self.positions))
        object.__setattr__(self, "pending_orders", tuple(self.pending_orders))
        if not self.snapshot_id.strip() or not self.currency:
            raise RiskEvidenceError("account snapshot identity is incomplete")
        if self.equity <= 0 or self.peak_equity <= 0 or self.cash < 0:
            raise RiskEvidenceError("account balances are invalid")


@dataclass(frozen=True, slots=True)
class QuoteEvidence:
    quote_id: str
    internal_instrument_id: UUID
    observed_at: datetime
    bid: Decimal
    ask: Decimal
    regular_session_open: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "internal_instrument_id",
            _uuid(self.internal_instrument_id, "internal_instrument_id"),
        )
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        object.__setattr__(self, "bid", decimal_value(self.bid, "bid"))
        object.__setattr__(self, "ask", decimal_value(self.ask, "ask"))
        if not self.quote_id.strip() or self.bid <= 0 or self.ask <= 0:
            raise RiskEvidenceError("quote evidence is invalid")
        if self.ask < self.bid:
            raise RiskEvidenceError("quote ask is below bid")


def _fraction(numerator: Decimal, denominator: Decimal) -> Decimal:
    return numerator / denominator if denominator else Decimal("Infinity")


def _order_notional(
    intent: OrderIntent, account: AccountEvidence, quote: QuoteEvidence
) -> Decimal:
    if intent.sizing_method == SizingMethod.FIXED_NOTIONAL:
        return intent.sizing_value
    if intent.sizing_method == SizingMethod.PERCENT_EQUITY:
        return account.equity * intent.sizing_value
    if intent.sizing_method == SizingMethod.UNITS:
        return quote.ask * intent.sizing_value
    raise RiskEvidenceError("unsupported sizing method")


class PreTradeRiskEvaluator:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(
        self,
        *,
        intent: OrderIntent,
        instrument: Instrument,
        account: AccountEvidence,
        quote: QuoteEvidence,
        now: datetime,
    ) -> RiskDecision:
        moment = _aware_utc(now, "now")
        if quote.internal_instrument_id != instrument.internal_instrument_id:
            raise RiskEvidenceError("quote instrument identity mismatch")
        if intent.internal_instrument_id != instrument.internal_instrument_id:
            raise RiskEvidenceError("intent instrument identity mismatch")
        pending_by_id: dict[str, PendingOrderEvidence] = {}
        contradictory_pending = False
        for pending in account.pending_orders:
            existing = pending_by_id.get(pending.broker_order_id)
            if existing is not None and existing != pending:
                contradictory_pending = True
            pending_by_id[pending.broker_order_id] = pending
        pending_orders = tuple(pending_by_id.values())

        order_notional = _order_notional(intent, account, quote)
        signed_order = order_notional if intent.side == OrderSide.BUY else -order_notional
        position_values = [item.signed_market_value for item in account.positions]
        pending_values = [
            item.remaining_notional
            if item.side == OrderSide.BUY
            else -item.remaining_notional
            for item in pending_orders
        ]
        issuer_existing = sum(
            (abs(item.signed_market_value) for item in account.positions
             if item.internal_instrument_id == instrument.internal_instrument_id),
            Decimal("0"),
        )
        issuer_pending = sum(
            (abs(value) for item, value in zip(pending_orders, pending_values, strict=True)
             if item.internal_instrument_id == instrument.internal_instrument_id),
            Decimal("0"),
        )
        sector_existing = sum(
            (abs(item.signed_market_value) for item in account.positions
             if item.sector == instrument.sector),
            Decimal("0"),
        )
        sector_pending = sum(
            (abs(value) for item, value in zip(pending_orders, pending_values, strict=True)
             if item.sector == instrument.sector),
            Decimal("0"),
        )
        projected_issuer = issuer_existing + issuer_pending + abs(order_notional)
        projected_sector = sector_existing + sector_pending + abs(order_notional)
        projected_gross = (
            sum((abs(value) for value in position_values), Decimal("0"))
            + sum((abs(value) for value in pending_values), Decimal("0"))
            + abs(order_notional)
        )
        projected_net = (
            sum(position_values, Decimal("0"))
            + sum(pending_values, Decimal("0"))
            + signed_order
        )
        pending_buys = sum(
            (
                item.remaining_notional
                for item in pending_orders
                if item.side == OrderSide.BUY
            ),
            Decimal("0"),
        )
        projected_cash = account.cash - pending_buys - (
            order_notional if intent.side == OrderSide.BUY else Decimal("0")
        )
        drawdown = max(
            Decimal("0"), _fraction(account.peak_equity - account.equity, account.peak_equity)
        )
        daily_loss = max(
            Decimal("0"), -_fraction(account.daily_pnl, account.equity)
        )
        quote_age = (moment - quote.observed_at).total_seconds()
        account_age = (moment - account.observed_at).total_seconds()

        checks: list[RiskCheck] = []

        def add(name: str, passed: bool, observed: object, limit: object) -> None:
            checks.append(
                RiskCheck(
                    name=name,
                    passed=bool(passed),
                    observed=str(observed),
                    limit=str(limit),
                )
            )

        add("environment", intent.environment == account.environment == self.limits.environment,
            f"{intent.environment}/{account.environment}", self.limits.environment)
        add("asset_type", instrument.asset_type in self.limits.allowed_asset_types,
            instrument.asset_type, self.limits.allowed_asset_types)
        add("long_only", intent.side == OrderSide.BUY, intent.side, OrderSide.BUY)
        add("leverage", intent.target_leverage == self.limits.required_leverage,
            intent.target_leverage, self.limits.required_leverage)
        add("currency", intent.currency == account.currency == instrument.currency,
            f"{intent.currency}/{account.currency}/{instrument.currency}", "exact match")
        add("intent_expiry", moment < intent.expires_at, intent.expires_at.isoformat(), moment.isoformat())
        add("quote_freshness", 0 <= quote_age <= self.limits.quote_max_age_seconds,
            quote_age, self.limits.quote_max_age_seconds)
        add("account_freshness", 0 <= account_age <= self.limits.account_max_age_seconds,
            account_age, self.limits.account_max_age_seconds)
        add("regular_hours", (not self.limits.regular_hours_only) or quote.regular_session_open,
            quote.regular_session_open, True)
        add("pending_orders_complete", account.pending_orders_complete,
            account.pending_orders_complete, True)
        add("pending_orders_consistent", not contradictory_pending,
            not contradictory_pending, True)
        add("order_size", _fraction(order_notional, account.equity) <= self.limits.max_order_equity_fraction,
            _fraction(order_notional, account.equity), self.limits.max_order_equity_fraction)
        add("issuer_exposure", abs(_fraction(projected_issuer, account.equity)) <= self.limits.max_issuer_fraction,
            _fraction(projected_issuer, account.equity), self.limits.max_issuer_fraction)
        add("sector_exposure", _fraction(projected_sector, account.equity) <= self.limits.max_sector_fraction,
            _fraction(projected_sector, account.equity), self.limits.max_sector_fraction)
        add("gross_exposure", _fraction(projected_gross, account.equity) <= self.limits.max_gross_fraction,
            _fraction(projected_gross, account.equity), self.limits.max_gross_fraction)
        add("net_exposure", abs(_fraction(projected_net, account.equity)) <= self.limits.max_net_fraction,
            _fraction(projected_net, account.equity), self.limits.max_net_fraction)
        add("cash_buffer", _fraction(projected_cash, account.equity) >= self.limits.min_cash_buffer_fraction,
            _fraction(projected_cash, account.equity), self.limits.min_cash_buffer_fraction)
        add("daily_loss_stop", daily_loss < self.limits.daily_loss_stop_fraction,
            daily_loss, self.limits.daily_loss_stop_fraction)
        add("drawdown_stop", drawdown < self.limits.drawdown_stop_fraction,
            drawdown, self.limits.drawdown_stop_fraction)

        reasons = tuple(check.name for check in checks if not check.passed)
        outcome = RiskOutcome.ACCEPTED if not reasons else RiskOutcome.REJECTED
        exposures = {
            "order_notional": order_notional,
            "projected_issuer": projected_issuer,
            "projected_sector": projected_sector,
            "projected_gross": projected_gross,
            "projected_net": projected_net,
            "projected_cash": projected_cash,
            "daily_loss_fraction": daily_loss,
            "drawdown_fraction": drawdown,
        }
        return RiskDecision(
            decision_id=uuid4(),
            intent_hash=intent.intent_hash,
            account_snapshot_id=account.snapshot_id,
            quote_id=quote.quote_id,
            quote_observed_at=quote.observed_at,
            computed_exposures=tuple((key, str(value)) for key, value in exposures.items()),
            checks=tuple(checks),
            outcome=outcome,
            reasons=reasons,
            decided_at=moment,
        )
