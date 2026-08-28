from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import yaml

from brokers.base import BrokerAdapter, BrokerReconciliationResult

from execution.domain import (
    Approval,
    BrokerCommand,
    BrokerOperation,
    CommandAttempt,
    CommandState,
    DomainValidationError,
    Environment,
    OrderIntent,
    OrderSide,
    OrderType,
    RiskDecision,
    RiskOutcome,
    SizingMethod,
)
from execution.instruments import InstrumentRegistry
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitchStatus
from risk.submission_gate import GovernanceWritePolicy, SubmissionGate


class ExecutionDisabledError(RuntimeError):
    """Raised when deny-by-default execution configuration blocks an action."""


class IntentImportError(ValueError):
    """Raised when advisory rows lack immutable instrument identity or sizing."""


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    schema_version: str
    intent_import_enabled: bool
    broker_writes_enabled: bool
    adapter: str
    environment: Environment
    require_internal_instrument_id: bool
    allowed_asset_types: tuple[str, ...]
    allowed_sides: tuple[str, ...]
    allowed_leverage: Decimal

    @classmethod
    def load(cls, path: Path | str) -> "ExecutionConfig":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ExecutionDisabledError(
                "execution configuration is missing or invalid"
            ) from exc
        if not isinstance(raw, dict):
            raise ExecutionDisabledError("execution configuration must be a mapping")
        required = {
            "schema_version",
            "intent_import_enabled",
            "broker_writes_enabled",
            "adapter",
            "environment",
            "require_internal_instrument_id",
            "allowed_asset_types",
            "allowed_sides",
            "allowed_leverage",
        }
        if missing := sorted(required - set(raw)):
            raise ExecutionDisabledError(f"execution configuration missing keys: {missing}")
        try:
            config = cls(
                schema_version=str(raw["schema_version"]),
                intent_import_enabled=raw["intent_import_enabled"] is True,
                broker_writes_enabled=raw["broker_writes_enabled"] is True,
                adapter=str(raw["adapter"]).strip().lower(),
                environment=Environment(str(raw["environment"]).strip().lower()),
                require_internal_instrument_id=(
                    raw["require_internal_instrument_id"] is True
                ),
                allowed_asset_types=tuple(
                    str(item).strip().lower() for item in raw["allowed_asset_types"]
                ),
                allowed_sides=tuple(
                    str(item).strip().lower() for item in raw["allowed_sides"]
                ),
                allowed_leverage=Decimal(str(raw["allowed_leverage"])),
            )
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ExecutionDisabledError(
                "execution configuration has invalid values"
            ) from exc
        if config.environment == Environment.REAL:
            raise ExecutionDisabledError("Real execution is outside the authorized scope")
        if config.adapter not in {
            "offline",
            "fake",
            "etoro_demo_read_only",
            "etoro_demo_manual",
        }:
            raise ExecutionDisabledError("unapproved execution adapter")
        if not set(config.allowed_asset_types).issubset({"equity", "etf"}):
            raise ExecutionDisabledError("only equities and ETFs are supported")
        if set(config.allowed_sides) != {"buy"}:
            raise ExecutionDisabledError("initial Demo scope is buy/long only")
        if config.allowed_leverage != Decimal("1"):
            raise ExecutionDisabledError("only unleveraged intents are supported")
        if not config.require_internal_instrument_id:
            raise ExecutionDisabledError("ticker-only identity is forbidden")
        return config


class ExecutionCoordinator:
    """Persists lifecycle evidence but cannot mutate portfolio economics."""

    def __init__(
        self,
        *,
        store: ExecutionStore,
        registry: InstrumentRegistry,
        config: ExecutionConfig,
    ) -> None:
        self.store = store
        self.registry = registry
        self.config = config

    def prepare_intent(self, intent: OrderIntent) -> OrderIntent:
        self._validate_intent(intent)
        self.store.save_intent(intent)
        return intent

    def _validate_intent(self, intent: OrderIntent) -> None:
        instrument = self.registry.get(intent.internal_instrument_id)
        if intent.environment != self.config.environment:
            raise IntentImportError("intent environment does not match coordinator")
        if instrument.asset_type not in self.config.allowed_asset_types:
            raise IntentImportError("asset type is outside the long-only Demo scope")
        if str(intent.side) not in self.config.allowed_sides:
            raise IntentImportError("side is outside the long-only Demo scope")
        if intent.target_leverage != self.config.allowed_leverage:
            raise IntentImportError("leverage must be exactly 1")
        if instrument.currency != intent.currency:
            raise IntentImportError("intent currency conflicts with instrument registry")

    def record_risk_decision(
        self,
        *,
        intent: OrderIntent,
        decision: RiskDecision,
        broker_payload: Mapping[str, Any],
        broker: str,
        operation: BrokerOperation = BrokerOperation.SUBMIT_ORDER,
    ) -> BrokerCommand:
        if decision.intent_hash != intent.intent_hash:
            raise DomainValidationError("risk decision does not bind to this intent")
        self.store.save_risk_decision(decision)
        command = BrokerCommand.create(
            intent_hash=intent.intent_hash,
            operation=operation,
            broker=broker,
            environment=intent.environment,
            broker_payload=broker_payload,
        )
        self.store.save_command(command)
        target = (
            CommandState.AWAITING_APPROVAL
            if decision.outcome == RiskOutcome.ACCEPTED
            else CommandState.RISK_REJECTED
        )
        return self.store.transition_command(command.command_id, target)

    def record_approval(
        self,
        *,
        command: BrokerCommand,
        approval: Approval,
        now: datetime,
    ) -> BrokerCommand:
        if command.state != CommandState.AWAITING_APPROVAL:
            raise DomainValidationError("command is not awaiting approval")
        if approval.intent_hash != command.intent_hash:
            raise DomainValidationError("approval does not bind to the command intent")
        if approval.environment != command.environment:
            raise DomainValidationError("approval environment mismatch")
        moment = now.astimezone(timezone.utc)
        if moment < approval.issued_at or moment >= approval.expires_at:
            raise DomainValidationError("approval is not currently valid")
        self.store.save_approval(approval)
        return self.store.transition_command(command.command_id, CommandState.APPROVED)

    def mark_submission_pending(
        self,
        *,
        command: BrokerCommand,
        intent: OrderIntent,
        decision: RiskDecision,
        approval: Approval,
        kill_switch: KillSwitchStatus,
        regular_session_open: bool,
        submission_gate: SubmissionGate,
        governance: GovernanceWritePolicy,
        now: datetime,
    ) -> BrokerCommand:
        if command.state != CommandState.APPROVED:
            raise DomainValidationError("command is not approved")
        submission_gate.authorize(
            intent=intent,
            decision=decision,
            approval=approval,
            kill_switch=kill_switch,
            regular_session_open=regular_session_open,
            broker_writes_enabled=self.config.broker_writes_enabled,
            governance=governance,
            now=now,
        )
        self.store.consume_approval(approval.approval_id, consumed_at=now)
        return self.store.transition_command(
            command.command_id, CommandState.SUBMISSION_PENDING
        )

    def submit_pending_command(
        self,
        *,
        command: BrokerCommand,
        intent: OrderIntent,
        adapter: BrokerAdapter,
        now: datetime,
    ) -> BrokerCommand:
        """Persist the request identity before the only broker send attempt."""
        if command.state != CommandState.SUBMISSION_PENDING:
            raise DomainValidationError("command is not submission_pending")
        if adapter.read_only:
            raise ExecutionDisabledError("selected broker adapter is read-only")
        if adapter.environment != command.environment:
            raise ExecutionDisabledError("broker adapter environment mismatch")
        attempt = CommandAttempt(
            attempt_number=len(command.attempt_history) + 1,
            request_id=command.logical_request_id,
            attempted_at=now,
            outcome="send_started",
        )
        command = self.store.record_attempt(command.command_id, attempt)
        try:
            result = adapter.submit_order(intent, command)
        except Exception as exc:
            # Once the transport call begins, an exception is never permission
            # to invent another logical request. Reconciliation owns recovery.
            self.store.transition_command(
                command.command_id,
                CommandState.UNKNOWN,
                details={"error_type": exc.__class__.__name__},
                broker_reference_id=str(command.logical_request_id),
            )
            raise
        if not result.accepted_for_processing:
            return self.store.transition_command(
                command.command_id,
                CommandState.REJECTED,
                details={"broker_status": result.raw_status},
                broker_order_id=result.broker_order_id,
                broker_reference_id=result.broker_reference_id,
            )
        return self.store.transition_command(
            command.command_id,
            CommandState.ACKNOWLEDGED,
            details={"broker_status": result.raw_status},
            broker_order_id=result.broker_order_id,
            broker_reference_id=result.broker_reference_id,
        )

    def reconcile_command(
        self,
        *,
        command: BrokerCommand,
        adapter: BrokerAdapter,
    ) -> tuple[BrokerCommand, BrokerReconciliationResult]:
        """Resolve ambiguous/in-flight state only through authoritative reads."""
        current = self.store.get_command(command.command_id)
        if current is None:
            raise DomainValidationError("command is not persisted")
        if current.state == CommandState.SUBMISSION_PENDING:
            current = self.store.transition_command(
                current.command_id, CommandState.UNKNOWN
            )
        if current.state in {
            CommandState.ACKNOWLEDGED,
            CommandState.PARTIALLY_FILLED,
            CommandState.CANCEL_PENDING,
        }:
            current = self.store.transition_command(
                current.command_id, CommandState.UNKNOWN
            )
        if current.state == CommandState.UNKNOWN:
            current = self.store.transition_command(
                current.command_id, CommandState.RECONCILING
            )
        if current.state != CommandState.RECONCILING:
            raise DomainValidationError(
                f"command state {current.state} cannot be reconciled"
            )
        result = adapter.reconcile_command(current)
        target = CommandState(result.lifecycle_state)
        current = self.store.transition_command(
            current.command_id,
            target,
            details=dict(result.details),
            broker_order_id=result.broker_order_id,
            broker_position_id=result.broker_position_id,
        )
        if result.reconciled:
            current = self.store.transition_command(
                current.command_id,
                CommandState.RECONCILED,
                details={"terminal_broker_state": str(target)},
            )
        return current, result

    def import_advisory_orders(
        self,
        frame: pd.DataFrame,
        *,
        strategy_id: str,
        expires_at: datetime,
    ) -> tuple[OrderIntent, ...]:
        if not self.config.intent_import_enabled:
            raise ExecutionDisabledError("advisory intent import is disabled")
        if frame.empty:
            return ()
        required = {
            "run_id",
            "internal_instrument_id",
            "direction",
            "asset_type",
            "currency",
            "capital_allocated",
        }
        missing = sorted(required - set(frame.columns))
        if missing:
            raise IntentImportError(
                f"portfolio orders lack execution identity fields: {missing}"
            )
        prepared: list[OrderIntent] = []
        for row in frame.to_dict(orient="records"):
            internal_id = str(row["internal_instrument_id"]).strip()
            if not internal_id:
                raise IntentImportError("ticker-only portfolio order rejected")
            instrument = self.registry.get(internal_id)
            asset_type = str(row["asset_type"]).strip().lower()
            if asset_type != instrument.asset_type:
                raise IntentImportError(
                    "portfolio order asset type conflicts with registry"
                )
            direction = str(row["direction"]).strip().lower()
            if direction != "long":
                raise IntentImportError("only long portfolio orders are supported")
            intent = OrderIntent.create(
                strategy_id=strategy_id,
                run_id=str(row["run_id"]).strip(),
                internal_instrument_id=internal_id,
                environment=self.config.environment,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                sizing_method=SizingMethod.FIXED_NOTIONAL,
                sizing_value=str(row["capital_allocated"]),
                currency=str(row["currency"]),
                expires_at=expires_at,
                target_leverage=Decimal("1"),
            )
            self._validate_intent(intent)
            prepared.append(intent)
        self.store.save_intents(tuple(prepared))
        return tuple(prepared)
