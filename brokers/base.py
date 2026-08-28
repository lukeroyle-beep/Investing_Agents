from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID

from execution.domain import BrokerCommand, Environment, OrderIntent
from execution.instruments import BrokerInstrumentMapping, Instrument


class BrokerError(RuntimeError):
    """Base error for broker-adapter failures."""


class BrokerWriteDisabled(BrokerError):
    """Raised when a mutation is requested from a read-only adapter."""


class BrokerDataUnavailable(BrokerError):
    """Raised when a required authoritative read is unavailable or incomplete."""


@dataclass(frozen=True, slots=True)
class BrokerQuote:
    quote_id: str
    internal_instrument_id: UUID
    broker_instrument_id: int
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    broker_position_id: str | None
    broker_instrument_id: int
    units: Decimal
    current_exposure: Decimal
    average_leverage: Decimal
    pnl: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    broker_order_id: str
    broker_instrument_id: int
    side: str
    remaining_notional: Decimal
    status: str


@dataclass(frozen=True, slots=True)
class BrokerExecution:
    broker_execution_id: str
    broker_order_id: str
    broker_instrument_id: int
    broker_position_id: str
    broker_reference_id: str
    environment: Environment
    side: str
    action: str
    units: Decimal
    price: Decimal
    executed_at: datetime
    price_rate_id: str
    fees: Decimal
    taxes: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class BrokerAccountSnapshot:
    snapshot_id: str
    environment: Environment
    observed_at: datetime
    currency: str
    equity: Decimal
    available_cash: Decimal
    balance: Decimal
    frozen_cash: Decimal
    current_pnl: Decimal
    used_margin: Decimal
    positions: tuple[BrokerPosition, ...]
    pending_orders: tuple[BrokerOrder, ...]
    pending_orders_complete: bool
    executions_complete: bool


@dataclass(frozen=True, slots=True)
class BrokerBalancesAndPnl:
    snapshot_id: str
    observed_at: datetime
    currency: str
    equity: Decimal
    available_cash: Decimal
    balance: Decimal
    current_pnl: Decimal


@dataclass(frozen=True, slots=True)
class BrokerSubmissionResult:
    request_id: UUID
    accepted_for_processing: bool
    broker_order_id: str | None
    broker_reference_id: str | None
    raw_status: str
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class BrokerReconciliationResult:
    command_id: UUID
    reconciled: bool
    lifecycle_state: str
    broker_order_id: str | None
    broker_position_id: str | None
    details: tuple[tuple[str, str], ...]
    executions: tuple[BrokerExecution, ...] = ()


class BrokerAdapter(ABC):
    """Broker-neutral capability surface owned only by execution coordination."""

    broker_name: str
    environment: Environment
    read_only: bool

    @abstractmethod
    def resolve_instrument(self, instrument: Instrument) -> BrokerInstrumentMapping:
        raise NotImplementedError

    @abstractmethod
    def get_rates(
        self, internal_instrument_ids: Sequence[UUID]
    ) -> tuple[BrokerQuote, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_balances_and_pnl(self) -> BrokerBalancesAndPnl:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> tuple[BrokerPosition, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_pending_orders(self) -> tuple[BrokerOrder, ...]:
        raise NotImplementedError

    @abstractmethod
    def get_executions(self) -> tuple[BrokerExecution, ...]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, intent: OrderIntent, command: BrokerCommand) -> BrokerSubmissionResult:
        raise NotImplementedError

    @abstractmethod
    def close_position(
        self,
        *,
        broker_position_id: str,
        broker_instrument_id: int | None = None,
        command: BrokerCommand,
    ) -> BrokerSubmissionResult:
        raise NotImplementedError

    @abstractmethod
    def partial_close_position(
        self,
        *,
        broker_position_id: str,
        broker_instrument_id: int | None = None,
        units: Decimal,
        command: BrokerCommand,
    ) -> BrokerSubmissionResult:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(
        self, *, broker_order_id: str, command: BrokerCommand
    ) -> BrokerSubmissionResult:
        raise NotImplementedError

    @abstractmethod
    def reconcile_command(self, command: BrokerCommand) -> BrokerReconciliationResult:
        raise NotImplementedError
