from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Sequence
from uuid import UUID

from brokers.base import (
    BrokerAccountSnapshot,
    BrokerAdapter,
    BrokerBalancesAndPnl,
    BrokerDataUnavailable,
    BrokerExecution,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    BrokerReconciliationResult,
    BrokerSubmissionResult,
    BrokerWriteDisabled,
)
from brokers.etoro.client import EtoroReadOnlyClient
from brokers.etoro.execution_client import EtoroDemoExecutionClient
from brokers.etoro.read_model import write_reconciliation_read_model
from brokers.etoro.schemas import EtoroPortfolioSnapshot, EtoroSchemaError
from execution.domain import BrokerCommand, Environment, OrderIntent
from execution.instruments import (
    BrokerInstrumentMapping,
    Instrument,
    InstrumentMappingError,
    InstrumentNotFoundError,
    InstrumentRegistry,
)


class EtoroDemoReadOnlyAdapter(BrokerAdapter):
    broker_name = "etoro"
    environment = Environment.DEMO
    read_only = True

    def __init__(
        self,
        *,
        client: EtoroReadOnlyClient,
        registry: InstrumentRegistry,
    ) -> None:
        if not client.read_only or client.environment != Environment.DEMO:
            raise ValueError("Gate A requires a Demo read-only client")
        self.client = client
        self.registry = registry

    def resolve_instrument(self, instrument: Instrument) -> BrokerInstrumentMapping:
        try:
            return self.registry.broker_mapping(
                instrument.internal_instrument_id,
                broker=self.broker_name,
                environment=self.environment,
            )
        except InstrumentNotFoundError:
            pass
        response = self.client.search_instruments(instrument.canonical_symbol)
        matches = [
            item
            for item in response.items
            if item.symbol == instrument.canonical_symbol
            and item.exchange_name == instrument.exchange
        ]
        if len(matches) != 1:
            raise InstrumentMappingError(
                "eToro search did not return exactly one symbol/exchange match"
            )
        match = matches[0]
        if match.is_delisted or not match.is_currently_tradable:
            raise InstrumentMappingError("eToro instrument is delisted or not tradable")
        mapping = BrokerInstrumentMapping.from_resolution(
            instrument=instrument,
            broker=self.broker_name,
            environment=self.environment,
            broker_instrument_id=match.instrument_id,
            exact_match_symbol=match.symbol,
            metadata=match.metadata_mapping(),
        )
        self.registry.record_mapping(mapping)
        return mapping

    def get_rates(
        self, internal_instrument_ids: Sequence[UUID]
    ) -> tuple[BrokerQuote, ...]:
        mappings = [
            self.registry.broker_mapping(
                internal_id,
                broker=self.broker_name,
                environment=self.environment,
            )
            for internal_id in internal_instrument_ids
        ]
        response = self.client.get_market_rates(
            tuple(mapping.broker_instrument_id for mapping in mappings)
        )
        rate_by_id = {rate.instrument_id: rate for rate in response.rates}
        return tuple(
            BrokerQuote(
                quote_id=rate_by_id[mapping.broker_instrument_id].price_rate_id,
                internal_instrument_id=mapping.internal_instrument_id,
                broker_instrument_id=mapping.broker_instrument_id,
                bid=rate_by_id[mapping.broker_instrument_id].bid,
                ask=rate_by_id[mapping.broker_instrument_id].ask,
                observed_at=rate_by_id[mapping.broker_instrument_id].observed_at,
                source="etoro_v1_market_rates",
            )
            for mapping in mappings
        )

    @staticmethod
    def _positions(snapshot: EtoroPortfolioSnapshot) -> tuple[BrokerPosition, ...]:
        return tuple(
            BrokerPosition(
                broker_position_id=None,
                broker_instrument_id=item.instrument_id,
                units=item.units,
                current_exposure=item.current_exposure,
                average_leverage=item.average_leverage,
                pnl=item.pnl,
                currency=item.currency,
            )
            for item in snapshot.instrument_aggregates
        )

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        snapshot = self.client.get_demo_portfolio()
        return BrokerAccountSnapshot(
            snapshot_id=snapshot.snapshot_id,
            environment=self.environment,
            observed_at=snapshot.observed_at,
            currency=snapshot.account_currency,
            equity=snapshot.totals.total_value,
            available_cash=snapshot.totals.available_cash,
            balance=snapshot.totals.balance,
            frozen_cash=snapshot.totals.frozen_cash,
            current_pnl=snapshot.totals.current_pnl,
            used_margin=snapshot.totals.used_margin,
            positions=self._positions(snapshot),
            pending_orders=(),
            pending_orders_complete=False,
            executions_complete=False,
        )

    def get_balances_and_pnl(self) -> BrokerBalancesAndPnl:
        snapshot = self.client.get_demo_portfolio()
        return BrokerBalancesAndPnl(
            snapshot_id=snapshot.snapshot_id,
            observed_at=snapshot.observed_at,
            currency=snapshot.account_currency,
            equity=snapshot.totals.total_value,
            available_cash=snapshot.totals.available_cash,
            balance=snapshot.totals.balance,
            current_pnl=snapshot.totals.current_pnl,
        )

    def get_positions(self) -> tuple[BrokerPosition, ...]:
        return self._positions(self.client.get_demo_portfolio())

    def capture_reconciliation_read_model(
        self, path: Path | str
    ) -> BrokerAccountSnapshot:
        snapshot = self.get_account_snapshot()
        write_reconciliation_read_model(snapshot, path)
        return snapshot

    def get_pending_orders(self) -> tuple[BrokerOrder, ...]:
        raise BrokerDataUnavailable(
            "Gate A aggregate portfolio does not prove pending-order completeness"
        )

    def get_executions(self) -> tuple[BrokerExecution, ...]:
        raise BrokerDataUnavailable(
            "Gate A aggregate portfolio does not prove execution completeness"
        )

    @staticmethod
    def _write_disabled(operation: str) -> BrokerWriteDisabled:
        return BrokerWriteDisabled(
            f"{operation} is unavailable: eToro Gate A is Demo read-only"
        )

    def submit_order(
        self, intent: OrderIntent, command: BrokerCommand
    ) -> BrokerSubmissionResult:
        raise self._write_disabled("submit_order")

    def close_position(
        self,
        *,
        broker_position_id: str,
        broker_instrument_id: int | None = None,
        command: BrokerCommand,
    ) -> BrokerSubmissionResult:
        raise self._write_disabled("close_position")

    def partial_close_position(
        self,
        *,
        broker_position_id: str,
        broker_instrument_id: int | None = None,
        units: Decimal,
        command: BrokerCommand,
    ) -> BrokerSubmissionResult:
        raise self._write_disabled("partial_close_position")

    def cancel_order(
        self, *, broker_order_id: str, command: BrokerCommand
    ) -> BrokerSubmissionResult:
        raise self._write_disabled("cancel_order")

    def reconcile_command(self, command: BrokerCommand) -> BrokerReconciliationResult:
        raise BrokerDataUnavailable(
            "command reconciliation is unavailable until Gate B lookup contracts are enabled"
        )


class EtoroDemoExecutionAdapter(EtoroDemoReadOnlyAdapter):
    """Gate B adapter: Demo mutations plus REST-authoritative reconciliation."""

    read_only = False

    def __init__(
        self,
        *,
        client: EtoroReadOnlyClient,
        execution_client: EtoroDemoExecutionClient,
        registry: InstrumentRegistry,
    ) -> None:
        super().__init__(client=client, registry=registry)
        if execution_client.environment != Environment.DEMO or execution_client.read_only:
            raise ValueError("Gate B requires a Demo execution client")
        self.execution_client = execution_client

    @staticmethod
    def _validate_command(command: BrokerCommand) -> None:
        if command.environment != Environment.DEMO or command.broker != "etoro":
            raise BrokerWriteDisabled("command is not bound to eToro Demo")

    def submit_order(
        self, intent: OrderIntent, command: BrokerCommand
    ) -> BrokerSubmissionResult:
        self._validate_command(command)
        mapping = self.registry.broker_mapping(
            intent.internal_instrument_id,
            broker=self.broker_name,
            environment=self.environment,
        )
        payload = {
            "action": "open",
            "transaction": "buy",
            "instrumentId": mapping.broker_instrument_id,
            "settlementType": "real",
            "orderType": "mkt",
            "leverage": 1,
            "amount": str(intent.sizing_value),
            "orderCurrency": intent.currency.lower(),
        }
        accepted = self.execution_client.submit_open_order(
            payload,
            request_id=command.logical_request_id,
        )
        return BrokerSubmissionResult(
            request_id=command.logical_request_id,
            accepted_for_processing=True,
            broker_order_id=accepted.order_id,
            broker_reference_id=accepted.reference_id,
            raw_status="queued",
        )

    def close_position(
        self,
        *,
        broker_position_id: str,
        broker_instrument_id: int | None = None,
        command: BrokerCommand,
    ) -> BrokerSubmissionResult:
        self._validate_command(command)
        if broker_instrument_id is None:
            raise ValueError("close requires broker_instrument_id")
        accepted = self.execution_client.close_position(
            position_id=broker_position_id,
            instrument_id=broker_instrument_id,
            units=None,
            request_id=command.logical_request_id,
        )
        return BrokerSubmissionResult(
            request_id=command.logical_request_id,
            accepted_for_processing=True,
            broker_order_id=accepted.order_id,
            broker_reference_id=str(command.logical_request_id),
            raw_status="submitted",
        )

    def partial_close_position(
        self,
        *,
        broker_position_id: str,
        broker_instrument_id: int | None = None,
        units: Decimal,
        command: BrokerCommand,
    ) -> BrokerSubmissionResult:
        self._validate_command(command)
        if broker_instrument_id is None:
            raise ValueError("partial close requires broker_instrument_id")
        accepted = self.execution_client.close_position(
            position_id=broker_position_id,
            instrument_id=broker_instrument_id,
            units=units,
            request_id=command.logical_request_id,
        )
        return BrokerSubmissionResult(
            request_id=command.logical_request_id,
            accepted_for_processing=True,
            broker_order_id=accepted.order_id,
            broker_reference_id=str(command.logical_request_id),
            raw_status="submitted",
        )

    def cancel_order(
        self, *, broker_order_id: str, command: BrokerCommand
    ) -> BrokerSubmissionResult:
        self._validate_command(command)
        accepted = self.execution_client.cancel_order(
            order_id=broker_order_id,
            request_id=command.logical_request_id,
        )
        return BrokerSubmissionResult(
            request_id=command.logical_request_id,
            accepted_for_processing=True,
            broker_order_id=accepted.order_id,
            broker_reference_id=str(command.logical_request_id),
            raw_status="cancel_pending",
        )

    def reconcile_command(self, command: BrokerCommand) -> BrokerReconciliationResult:
        self._validate_command(command)
        lookup = self.execution_client.lookup_order(
            order_id=command.broker_order_id,
            reference_id=(
                None
                if command.broker_order_id
                else command.broker_reference_id or str(command.logical_request_id)
            ),
        )
        status_map = {
            1: "acknowledged",
            2: "acknowledged",
            3: "filled",
            4: "rejected",
            5: "partially_filled",
            6: "cancel_pending",
            7: "cancelled",
            8: "rejected",
            9: "cancelled",
            10: "partially_filled",
            11: "acknowledged",
            12: "acknowledged",
        }
        terminal = lookup.status.status_id in {3, 4, 7, 8, 9, 10}
        reference = command.broker_reference_id or str(command.logical_request_id)
        executions = tuple(
            BrokerExecution(
                broker_execution_id=item.execution_id,
                broker_order_id=lookup.order_id,
                broker_instrument_id=lookup.instrument_id,
                broker_position_id=item.position_id,
                broker_reference_id=reference,
                environment=Environment.DEMO,
                side="long",
                action="buy" if lookup.transaction == "buy" else "sell",
                units=item.units,
                price=item.price,
                executed_at=item.executed_at,
                price_rate_id=item.price_rate_id,
                fees=item.fees,
                taxes=item.taxes,
                currency=lookup.currency,
            )
            for item in lookup.executions
        )
        position_id = executions[-1].broker_position_id if executions else None
        details = (
            ("status_id", str(lookup.status.status_id)),
            ("status_name", lookup.status.name),
            ("error_code", str(lookup.status.error_code)),
            ("error_message", lookup.status.error_message or ""),
        )
        return BrokerReconciliationResult(
            command_id=command.command_id,
            reconciled=terminal,
            lifecycle_state=status_map[lookup.status.status_id],
            broker_order_id=lookup.order_id,
            broker_position_id=position_id,
            details=details,
            executions=executions,
        )
