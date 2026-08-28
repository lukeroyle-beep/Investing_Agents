from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from uuid import UUID, uuid4


SCHEMA_VERSION = "1.0"


class DomainValidationError(ValueError):
    """Raised when immutable execution-domain evidence is malformed."""


class InvalidLifecycleTransition(DomainValidationError):
    """Raised when a broker command attempts an invalid state transition."""


class Environment(StrEnum):
    OFFLINE = "offline"
    DEMO = "demo"
    REAL = "real"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class SizingMethod(StrEnum):
    FIXED_NOTIONAL = "fixed_notional"
    PERCENT_EQUITY = "percent_equity"
    UNITS = "units"


class RiskOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class BrokerOperation(StrEnum):
    SUBMIT_ORDER = "submit_order"
    CLOSE_POSITION = "close_position"
    PARTIAL_CLOSE = "partial_close"
    CANCEL_ORDER = "cancel_order"
    RECONCILE = "reconcile"


class CommandState(StrEnum):
    PROPOSED = "proposed"
    RISK_REJECTED = "risk_rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUBMISSION_PENDING = "submission_pending"
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    RECONCILING = "reconciling"
    RECONCILED = "reconciled"


ALLOWED_TRANSITIONS: Mapping[CommandState, frozenset[CommandState]] = MappingProxyType(
    {
        CommandState.PROPOSED: frozenset(
            {CommandState.RISK_REJECTED, CommandState.AWAITING_APPROVAL}
        ),
        CommandState.RISK_REJECTED: frozenset({CommandState.RECONCILED}),
        CommandState.AWAITING_APPROVAL: frozenset(
            {CommandState.APPROVED, CommandState.RISK_REJECTED}
        ),
        CommandState.APPROVED: frozenset({CommandState.SUBMISSION_PENDING}),
        CommandState.SUBMISSION_PENDING: frozenset(
            {
                CommandState.ACKNOWLEDGED,
                CommandState.REJECTED,
                CommandState.UNKNOWN,
            }
        ),
        CommandState.ACKNOWLEDGED: frozenset(
            {
                CommandState.PARTIALLY_FILLED,
                CommandState.FILLED,
                CommandState.REJECTED,
                CommandState.CANCEL_PENDING,
                CommandState.UNKNOWN,
            }
        ),
        CommandState.PARTIALLY_FILLED: frozenset(
            {
                CommandState.PARTIALLY_FILLED,
                CommandState.FILLED,
                CommandState.CANCEL_PENDING,
                CommandState.UNKNOWN,
                CommandState.RECONCILED,
            }
        ),
        CommandState.FILLED: frozenset({CommandState.RECONCILED}),
        CommandState.REJECTED: frozenset({CommandState.RECONCILED}),
        CommandState.CANCEL_PENDING: frozenset(
            {
                CommandState.CANCELLED,
                CommandState.PARTIALLY_FILLED,
                CommandState.FILLED,
                CommandState.UNKNOWN,
            }
        ),
        CommandState.CANCELLED: frozenset({CommandState.RECONCILED}),
        CommandState.UNKNOWN: frozenset({CommandState.RECONCILING}),
        CommandState.RECONCILING: frozenset(
            {
                CommandState.RECONCILED,
                CommandState.ACKNOWLEDGED,
                CommandState.PARTIALLY_FILLED,
                CommandState.FILLED,
                CommandState.REJECTED,
                CommandState.CANCELLED,
                CommandState.UNKNOWN,
            }
        ),
        CommandState.RECONCILED: frozenset(),
    }
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _uuid(value: UUID | str, field_name: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise DomainValidationError(f"{field_name} must be a UUID") from exc


def decimal_value(value: Decimal | str | int | float, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DomainValidationError(f"{field_name} must be decimal-compatible") from exc
    if not result.is_finite():
        raise DomainValidationError(f"{field_name} must be finite")
    return result


def _primitive(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _aware_utc(value, "datetime").isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list, frozenset)):
        return [_primitive(item) for item in value]
    if is_dataclass(value):
        return {
            field.name: _primitive(getattr(value, field.name))
            for field in fields(value)
        }
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def immutable_pairs(
    values: Mapping[str, Any] | Iterable[tuple[str, Any]],
) -> tuple[tuple[str, str], ...]:
    items = values.items() if isinstance(values, Mapping) else values
    return tuple(sorted((str(key), str(value)) for key, value in items))


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: UUID
    strategy_id: str
    run_id: str
    internal_instrument_id: UUID
    environment: Environment
    side: OrderSide
    order_type: OrderType
    sizing_method: SizingMethod
    sizing_value: Decimal
    currency: str
    expires_at: datetime
    intent_hash: str
    target_leverage: Decimal = Decimal("1")
    limit_price: Decimal | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _uuid(self.intent_id, "intent_id"))
        object.__setattr__(
            self,
            "internal_instrument_id",
            _uuid(self.internal_instrument_id, "internal_instrument_id"),
        )
        object.__setattr__(self, "environment", Environment(self.environment))
        object.__setattr__(self, "side", OrderSide(self.side))
        object.__setattr__(self, "order_type", OrderType(self.order_type))
        object.__setattr__(self, "sizing_method", SizingMethod(self.sizing_method))
        object.__setattr__(
            self, "sizing_value", decimal_value(self.sizing_value, "sizing_value")
        )
        object.__setattr__(
            self,
            "target_leverage",
            decimal_value(self.target_leverage, "target_leverage"),
        )
        if self.limit_price is not None:
            object.__setattr__(
                self, "limit_price", decimal_value(self.limit_price, "limit_price")
            )
        object.__setattr__(self, "expires_at", _aware_utc(self.expires_at, "expires_at"))
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        for name in ("strategy_id", "run_id", "currency"):
            if not str(getattr(self, name)).strip():
                raise DomainValidationError(f"{name} must not be blank")
        if self.sizing_value <= 0:
            raise DomainValidationError("sizing_value must be positive")
        if self.target_leverage <= 0:
            raise DomainValidationError("target_leverage must be positive")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise DomainValidationError("limit orders require limit_price")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise DomainValidationError("market orders must not include limit_price")
        if self.intent_hash != self.compute_hash():
            raise DomainValidationError("intent_hash does not match the immutable payload")

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        run_id: str,
        internal_instrument_id: UUID | str,
        environment: Environment | str,
        side: OrderSide | str,
        order_type: OrderType | str,
        sizing_method: SizingMethod | str,
        sizing_value: Decimal | str | int | float,
        currency: str,
        expires_at: datetime,
        target_leverage: Decimal | str | int | float = Decimal("1"),
        limit_price: Decimal | str | int | float | None = None,
        intent_id: UUID | str | None = None,
    ) -> "OrderIntent":
        values = {
            "strategy_id": str(strategy_id).strip(),
            "run_id": str(run_id).strip(),
            "internal_instrument_id": _uuid(
                internal_instrument_id, "internal_instrument_id"
            ),
            "environment": Environment(environment),
            "side": OrderSide(side),
            "order_type": OrderType(order_type),
            "sizing_method": SizingMethod(sizing_method),
            "sizing_value": decimal_value(sizing_value, "sizing_value"),
            "currency": str(currency).strip().upper(),
            "expires_at": _aware_utc(expires_at, "expires_at"),
            "target_leverage": decimal_value(target_leverage, "target_leverage"),
            "limit_price": (
                decimal_value(limit_price, "limit_price")
                if limit_price is not None
                else None
            ),
            "schema_version": SCHEMA_VERSION,
        }
        return cls(
            intent_id=_uuid(intent_id or uuid4(), "intent_id"),
            intent_hash=payload_hash(values),
            **values,
        )

    def hash_payload(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "internal_instrument_id": self.internal_instrument_id,
            "environment": self.environment,
            "side": self.side,
            "order_type": self.order_type,
            "sizing_method": self.sizing_method,
            "sizing_value": self.sizing_value,
            "currency": self.currency,
            "expires_at": self.expires_at,
            "target_leverage": self.target_leverage,
            "limit_price": self.limit_price,
            "schema_version": self.schema_version,
        }

    def compute_hash(self) -> str:
        return payload_hash(self.hash_payload())


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    observed: str
    limit: str


@dataclass(frozen=True, slots=True)
class RiskDecision:
    decision_id: UUID
    intent_hash: str
    account_snapshot_id: str
    quote_id: str
    quote_observed_at: datetime
    computed_exposures: tuple[tuple[str, str], ...]
    checks: tuple[RiskCheck, ...]
    outcome: RiskOutcome
    reasons: tuple[str, ...]
    decided_at: datetime
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _uuid(self.decision_id, "decision_id"))
        object.__setattr__(self, "outcome", RiskOutcome(self.outcome))
        object.__setattr__(
            self,
            "quote_observed_at",
            _aware_utc(self.quote_observed_at, "quote_observed_at"),
        )
        object.__setattr__(self, "decided_at", _aware_utc(self.decided_at, "decided_at"))
        object.__setattr__(
            self, "computed_exposures", immutable_pairs(self.computed_exposures)
        )
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "reasons", tuple(str(item) for item in self.reasons))
        for name in ("intent_hash", "account_snapshot_id", "quote_id"):
            if not str(getattr(self, name)).strip():
                raise DomainValidationError(f"{name} must not be blank")
        expected = RiskOutcome.ACCEPTED if all(item.passed for item in self.checks) else RiskOutcome.REJECTED
        if self.outcome != expected:
            raise DomainValidationError("risk outcome conflicts with recorded checks")


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: UUID
    intent_hash: str
    approver: str
    environment: Environment
    limits: tuple[tuple[str, str], ...]
    issued_at: datetime
    expires_at: datetime
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", _uuid(self.approval_id, "approval_id"))
        object.__setattr__(self, "environment", Environment(self.environment))
        object.__setattr__(self, "limits", immutable_pairs(self.limits))
        object.__setattr__(self, "issued_at", _aware_utc(self.issued_at, "issued_at"))
        object.__setattr__(self, "expires_at", _aware_utc(self.expires_at, "expires_at"))
        if not self.intent_hash.strip() or not self.approver.strip():
            raise DomainValidationError("approval identity fields must not be blank")
        if self.expires_at <= self.issued_at:
            raise DomainValidationError("approval expiry must be after issue time")


@dataclass(frozen=True, slots=True)
class CommandAttempt:
    attempt_number: int
    request_id: UUID
    attempted_at: datetime
    outcome: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _uuid(self.request_id, "request_id"))
        object.__setattr__(
            self, "attempted_at", _aware_utc(self.attempted_at, "attempted_at")
        )
        if self.attempt_number < 1 or not self.outcome.strip():
            raise DomainValidationError("command attempt is invalid")


@dataclass(frozen=True, slots=True)
class BrokerCommand:
    command_id: UUID
    logical_request_id: UUID
    intent_hash: str
    operation: BrokerOperation
    broker: str
    environment: Environment
    payload_hash: str
    state: CommandState
    attempt_history: tuple[CommandAttempt, ...] = ()
    broker_order_id: str | None = None
    broker_reference_id: str | None = None
    broker_position_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "command_id", _uuid(self.command_id, "command_id"))
        object.__setattr__(
            self,
            "logical_request_id",
            _uuid(self.logical_request_id, "logical_request_id"),
        )
        object.__setattr__(self, "operation", BrokerOperation(self.operation))
        object.__setattr__(self, "environment", Environment(self.environment))
        object.__setattr__(self, "state", CommandState(self.state))
        object.__setattr__(self, "attempt_history", tuple(self.attempt_history))
        if not self.intent_hash.strip() or not self.broker.strip() or not self.payload_hash.strip():
            raise DomainValidationError("broker command identity fields must not be blank")
        numbers = [attempt.attempt_number for attempt in self.attempt_history]
        if numbers != list(range(1, len(numbers) + 1)):
            raise DomainValidationError("attempt history must be contiguous and ordered")

    @classmethod
    def create(
        cls,
        *,
        intent_hash: str,
        operation: BrokerOperation | str,
        broker: str,
        environment: Environment | str,
        broker_payload: Mapping[str, Any],
        command_id: UUID | str | None = None,
        logical_request_id: UUID | str | None = None,
    ) -> "BrokerCommand":
        return cls(
            command_id=_uuid(command_id or uuid4(), "command_id"),
            logical_request_id=_uuid(
                logical_request_id or uuid4(), "logical_request_id"
            ),
            intent_hash=str(intent_hash),
            operation=BrokerOperation(operation),
            broker=str(broker).strip().lower(),
            environment=Environment(environment),
            payload_hash=payload_hash(broker_payload),
            state=CommandState.PROPOSED,
        )

    def transition(
        self,
        new_state: CommandState | str,
        *,
        broker_order_id: str | None = None,
        broker_reference_id: str | None = None,
        broker_position_id: str | None = None,
    ) -> "BrokerCommand":
        target = CommandState(new_state)
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidLifecycleTransition(f"{self.state} -> {target} is not allowed")
        return replace(
            self,
            state=target,
            broker_order_id=broker_order_id or self.broker_order_id,
            broker_reference_id=broker_reference_id or self.broker_reference_id,
            broker_position_id=broker_position_id or self.broker_position_id,
        )

    def with_attempt(self, attempt: CommandAttempt) -> "BrokerCommand":
        if attempt.attempt_number != len(self.attempt_history) + 1:
            raise DomainValidationError("attempt number is not the next contiguous value")
        return replace(self, attempt_history=(*self.attempt_history, attempt))


def dataclass_json(value: Any) -> str:
    """Stable JSON representation used by the operational store."""
    return canonical_json(value)
