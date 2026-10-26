"""Inspectable operator lifecycle, currently restricted to offline Demo fixtures.

Neutral risk evidence supplied by a caller is not proof of its provenance.
Live eToro operation remains blocked until a reviewed collector supplies the
complete account, daily-loss baseline, peak-equity and exchange-session proof.
This module never invokes Fill Agent or awards live qualification credit.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from brokers.base import BrokerAdapter
from brokers.etoro.schemas import EtoroSchemaError, _pnl_timestamp, exact_datetime_epoch_seconds
from execution.coordinator import ExecutionCoordinator
from execution.domain import (
    Approval, BrokerCommand, CommandState, Environment, OrderIntent, OrderSide,
    OrderType, SizingMethod, _aware_utc, canonical_json, payload_hash,
)
from execution.fill_ingestion import stage_broker_executions
from execution.accounting import _sum as exact_sum
from risk.approval import ApprovalAuthority, ApprovalError
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import AccountEvidence, PreTradeRiskEvaluator, QuoteEvidence, RiskLimits
from risk.submission_gate import GovernanceWritePolicy, SubmissionGate
from scheduler.lock import ScheduleLockError, ScheduleRunLock


class OperatorEvidenceError(RuntimeError):
    """Missing, contradictory or unqualified workflow evidence blocks progress."""


class OperatorEvidenceStore:
    """Checksummed preparation and append-only observations, not economic state."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS preparations (
                    command_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    command_id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL
                );
            """)
        self.path.chmod(0o600)

    @staticmethod
    def _verified(row):
        if row is None:
            raise OperatorEvidenceError("required operator evidence is missing")
        value = json.loads(row[0])
        if payload_hash(value) != row[1]:
            raise OperatorEvidenceError("operator evidence checksum mismatch")
        return value

    def _put(self, table, command_id, value):
        if table not in {"preparations", "approvals"}:
            raise ValueError("unsupported evidence table")
        encoded, digest = canonical_json(value), payload_hash(value)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT payload,digest FROM {table} WHERE command_id=?", (str(command_id),)
            ).fetchone()
            if row:
                if canonical_json(self._verified(row)) != encoded:
                    raise OperatorEvidenceError("operator evidence is immutable")
                return
            connection.execute(f"INSERT INTO {table} VALUES (?,?,?)", (str(command_id), encoded, digest))

    def save_preparation(self, command_id, value):
        self._put("preparations", command_id, value)

    def read_preparation(self, command_id):
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT payload,digest FROM preparations WHERE command_id=?", (str(command_id),)).fetchone()
        return self._verified(row)

    def save_approval(self, command_id, approval):
        self._put("approvals", command_id, approval)

    def read_approval(self, command_id):
        with closing(sqlite3.connect(self.path)) as connection:
            row = connection.execute("SELECT payload,digest FROM approvals WHERE command_id=?", (str(command_id),)).fetchone()
        return self._verified(row)

    def append_observation(self, command_id, value):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO observations(command_id,payload,digest) VALUES (?,?,?)", (str(command_id), canonical_json(value), payload_hash(value)))

    def read_observations(self, command_id):
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute("SELECT payload,digest FROM observations WHERE command_id=? ORDER BY sequence", (str(command_id),)).fetchall()
        return tuple(self._verified(row) for row in rows)


@dataclass(frozen=True, slots=True)
class OperatorReceipt:
    command_id: str
    source_kind: str
    command_state: str
    broker_terminal: bool
    staged_execution_ids: tuple[str, ...]
    economic_reconciliation_verified: bool = False
    live_demo_qualified: bool = False
    qualification_credit: bool = False


class _CapturedLookup:
    def __init__(self, adapter, result):
        self.adapter, self.result = adapter, result

    def __getattr__(self, name):
        return getattr(self.adapter, name)

    def reconcile_command(self, command):
        return self.result


class DemoOperator:
    broker_name = "fake"
    source_kind = "fixture"
    def __init__(self, *, coordinator: ExecutionCoordinator, limits: RiskLimits,
                 evidence_store: OperatorEvidenceStore, staged_fills_path: Path | str):
        self.coordinator, self.limits = coordinator, limits
        self.evidence = evidence_store
        self.staged_fills_path = Path(staged_fills_path)
        self.risk = PreTradeRiskEvaluator(limits)

    @staticmethod
    def _fixture_source(source_kind):
        if source_kind != "fixture":
            raise OperatorEvidenceError(
                "live Demo workflow blocked: reviewed account, daily-loss, peak-equity "
                "and exchange-session provenance is not available; a source label is not proof"
            )

    @staticmethod
    def _fixture_adapter(adapter):
        # Checking the entire ancestry prevents an eToro subclass renaming
        # itself 'fake' from crossing this intentionally offline boundary.
        if (adapter.broker_name != "fake" or adapter.environment != Environment.DEMO
                or any(cls.__module__.startswith("brokers.etoro") for cls in type(adapter).__mro__)):
            raise OperatorEvidenceError("fixture workflow requires a fake Demo adapter, never eToro")

    def prepare(self, *, intent: OrderIntent, account: AccountEvidence,
                quote: QuoteEvidence, source_kind: str, now: datetime) -> BrokerCommand:
        self._fixture_source(source_kind)
        existing_intent = self.coordinator.store.get_intent(intent.intent_hash)
        if existing_intent is not None:
            if existing_intent != intent:
                raise OperatorEvidenceError("existing intent identity differs from preparation retry")
            commands = [item for item in self.coordinator.store.list_commands()
                        if item.intent_hash == intent.intent_hash]
            if commands:
                if len(commands) != 1:
                    raise OperatorEvidenceError("intent has multiple commands; operator review required")
                proof, existing, _, _ = self._bound(commands[0].command_id)
                if (canonical_json(account) != canonical_json(proof["account"])
                        or canonical_json(quote) != canonical_json(proof["quote"])):
                    raise OperatorEvidenceError("preparation retry cannot replace original evidence")
                return existing
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        if (intent.environment != Environment.DEMO or intent.side != OrderSide.BUY
                or intent.order_type != OrderType.MARKET
                or intent.sizing_method != SizingMethod.FIXED_NOTIONAL
                or intent.currency != "USD" or intent.target_leverage != Decimal("1")
                or instrument.asset_type not in {"equity", "etf"}):
            raise OperatorEvidenceError("operator scope is Demo unleveraged USD long equity/ETF market orders")
        mapping = self.coordinator.registry.broker_mapping(
            intent.internal_instrument_id, broker=self.broker_name, environment=Environment.DEMO)
        decision = self.risk.evaluate(intent=intent, instrument=instrument,
                                      account=account, quote=quote, now=now)
        if existing_intent is None:
            self.coordinator.prepare_intent(intent)
        broker_payload = {"action": "open", "transaction": "buy",
                          "instrumentId": mapping.broker_instrument_id,
                          "settlementType": "real", "orderType": "mkt", "leverage": 1,
                          "amount": str(intent.sizing_value), "orderCurrency": "usd"}
        command = self.coordinator.record_risk_decision(
            intent=intent, decision=decision, broker_payload=broker_payload, broker=self.broker_name)
        self.evidence.save_preparation(command.command_id, {
            "source_kind": source_kind, "prepared_at": _aware_utc(now, "now"),
            "command_id": command.command_id, "intent": intent, "instrument": instrument,
            "mapping": mapping, "account": account, "quote": quote, "decision": decision,
            "broker_payload": broker_payload, "qualification_credit": False,
        })
        return command

    def _bound(self, command_id):
        proof = self.evidence.read_preparation(command_id)
        self._fixture_source(proof["source_kind"])
        command = self.coordinator.store.get_command(command_id)
        if command is None or command.broker != self.broker_name or command.environment != Environment.DEMO:
            raise OperatorEvidenceError("persisted fake Demo command is missing")
        intent = self.coordinator.store.get_intent(command.intent_hash)
        decision = self.coordinator.store.get_command_risk_decision(command.command_id)
        if (intent is None or decision is None or proof["command_id"] != str(command.command_id)
                or canonical_json(intent) != canonical_json(proof["intent"])
                or canonical_json(decision) != canonical_json(proof["decision"])
                or command.payload_hash != payload_hash(proof["broker_payload"])):
            raise OperatorEvidenceError("persisted command, intent or risk binding differs from preparation")
        mapping = self.coordinator.registry.broker_mapping(
            intent.internal_instrument_id, broker=self.broker_name, environment=Environment.DEMO)
        if canonical_json(mapping) != canonical_json(proof["mapping"]):
            raise OperatorEvidenceError("immutable instrument mapping changed")
        if canonical_json(self.coordinator.registry.get(intent.internal_instrument_id)) != canonical_json(proof["instrument"]):
            raise OperatorEvidenceError("instrument registration changed after preparation")
        return proof, command, intent, decision

    def approve(self, command_id, *, approver: str, human_confirmed: bool,
                now: datetime) -> Approval:
        if human_confirmed is not True:
            raise ApprovalError("explicit human confirmation is required per order")
        _, command, intent, decision = self._bound(command_id)
        if command.state == CommandState.APPROVED:
            # Recover the crash boundary after the coordinator committed its
            # approval but before this separate evidence ledger recorded it.
            # Never issue a replacement authorization or extend its lifetime.
            approval = self.coordinator.store.get_command_approval(command.command_id)
            if approval is None or approval.approver != str(approver).strip():
                raise ApprovalError("approval recovery requires the original approver")
            ApprovalAuthority.verify(approval=approval, intent=intent, now=now)
            bindings = dict(approval.limits)
            if (bindings.get("risk_decision_id") != str(decision.decision_id)
                    or bindings.get("account_snapshot_id") != decision.account_snapshot_id
                    or bindings.get("quote_id") != decision.quote_id):
                raise ApprovalError("approval recovery risk binding mismatch")
            self.evidence.save_approval(command_id, approval)
            return approval
        approval = ApprovalAuthority(self.limits).issue(
            intent=intent, decision=decision, approver=approver,
            human_confirmed=human_confirmed, now=now)
        self.coordinator.record_approval(command=command, approval=approval, now=now)
        self.evidence.save_approval(command_id, approval)
        return approval

    def submit(self, command_id, *, adapter: BrokerAdapter, kill_switch: KillSwitchStatus,
               regular_session_open: bool, governance: GovernanceWritePolicy,
               now: datetime) -> BrokerCommand:
        self._fixture_adapter(adapter)
        proof, command, intent, decision = self._bound(command_id)
        approval = self.coordinator.store.get_command_approval(command.command_id)
        if approval is None or canonical_json(approval) != canonical_json(self.evidence.read_approval(command_id)):
            raise OperatorEvidenceError("persisted approval differs from operator evidence")
        # The decision timestamp is not the source account timestamp. Preserve
        # that distinction so preparation cannot extend an ageing account's TTL.
        moment = _aware_utc(now, "now")
        for name, max_age in (("account", self.limits.account_max_age_seconds),
                              ("quote", self.limits.quote_max_age_seconds)):
            observed = datetime.fromisoformat(proof[name]["observed_at"].replace("Z", "+00:00"))
            if not 0 <= (moment - observed).total_seconds() <= max_age:
                raise OperatorEvidenceError(f"original {name} evidence is stale; prepare and approve again")
        pending = self.coordinator.mark_submission_pending(
            command=command, intent=intent, decision=decision, approval=approval,
            kill_switch=kill_switch, regular_session_open=regular_session_open,
            submission_gate=SubmissionGate(self.limits), governance=governance, now=moment)
        return self.coordinator.submit_pending_command(
            command=pending, intent=intent, adapter=adapter, now=moment)

    def _validate_lookup(self, proof, command, intent, result, now):
        if result.command_id != command.command_id:
            raise OperatorEvidenceError("lookup command identity mismatch")
        if command.broker_order_id and result.broker_order_id != command.broker_order_id:
            raise OperatorEvidenceError("lookup broker order identity mismatch")
        if result.executions and not result.broker_order_id:
            raise OperatorEvidenceError("executions require a broker order identity")
        records: dict[str, Any] = {}
        for observation in self.evidence.read_observations(command.command_id):
            for execution in observation.get("executions", []):
                if execution.get("executed_amount") is None:
                    execution.pop("executed_amount", None)
                if execution.get("executed_at_source") is None:
                    execution.pop("executed_at_source", None)
                records[execution["broker_execution_id"]] = execution
        seen = set()
        submitted_at = None
        if result.executions:
            send_attempts = [
                item for item in command.attempt_history
                if item.request_id == command.logical_request_id and item.outcome == "send_started"
            ]
            if not send_attempts:
                raise OperatorEvidenceError("broker executions have no persisted send attempt")
            submitted_at = min(item.attempted_at for item in send_attempts)
        for execution in result.executions:
            if (execution.environment != Environment.DEMO
                    or execution.broker_order_id != result.broker_order_id
                    or execution.broker_reference_id != (command.broker_reference_id or str(command.logical_request_id))
                    or execution.broker_instrument_id != proof["mapping"]["broker_instrument_id"]
                    or execution.currency != intent.currency or execution.side != "long"
                    or execution.action != "buy" or not execution.broker_execution_id
                    or not execution.broker_position_id or not execution.price_rate_id):
                raise OperatorEvidenceError("broker execution does not match the approved command")
            if result.broker_position_id and execution.broker_position_id != result.broker_position_id:
                raise OperatorEvidenceError("broker position identity mismatch")
            for name in ("units", "price", "fees", "taxes"):
                value = Decimal(str(getattr(execution, name)))
                if not value.is_finite() or value < 0 or (name in {"units", "price"} and value == 0):
                    raise OperatorEvidenceError("broker execution has invalid numeric values")
            if execution.executed_amount is not None and (
                    not execution.executed_amount.is_finite() or execution.executed_amount <= 0):
                raise OperatorEvidenceError("broker execution has invalid invested principal")
            if self.broker_name == "etoro" and execution.executed_amount is None:
                raise OperatorEvidenceError("authoritative broker executed principal required")
            when = _aware_utc(execution.executed_at, "executed_at")
            when_epoch = exact_datetime_epoch_seconds(when)
            if execution.executed_at_source is not None:
                try:
                    _, source_epoch = _pnl_timestamp(execution.executed_at_source)
                except EtoroSchemaError:
                    raise OperatorEvidenceError("broker execution source timestamp invalid") from None
                if not Decimal("0") <= source_epoch - when_epoch < Decimal("0.000001"):
                    raise OperatorEvidenceError("broker execution source timestamp view mismatch")
                when_epoch = source_epoch
            elif self.broker_name == "etoro":
                raise OperatorEvidenceError("exact broker execution source timestamp required")
            prepared = datetime.fromisoformat(proof["prepared_at"].replace("Z", "+00:00"))
            if not exact_datetime_epoch_seconds(max(prepared, submitted_at)) <= when_epoch <= exact_datetime_epoch_seconds(now):
                raise OperatorEvidenceError("broker execution timestamp is outside the command lifetime")
            serial = json.loads(canonical_json(execution))
            # Keep old fixture receipts comparable without rewriting their bytes.
            if execution.executed_amount is None:
                serial.pop("executed_amount", None)
            if execution.executed_at_source is None:
                serial.pop("executed_at_source", None)
            if execution.broker_execution_id in records and records[execution.broker_execution_id] != serial:
                raise OperatorEvidenceError("broker execution identity has contradictory evidence")
            records[execution.broker_execution_id] = serial
            seen.add(execution.broker_execution_id)
        if not set(records).issubset(seen):
            raise OperatorEvidenceError("lookup dropped previously confirmed executions")
        terminal_states = {"filled", "rejected", "cancelled"}
        if (result.lifecycle_state not in terminal_states | {"acknowledged", "partially_filled", "unknown", "cancel_pending"}
                or result.reconciled != (result.lifecycle_state in terminal_states)):
            raise OperatorEvidenceError("lookup terminal status is contradictory")
        for previous in self.evidence.read_observations(command.command_id):
            if "result" in previous and previous["result"]["reconciled"] and previous["result"]["lifecycle_state"] != result.lifecycle_state:
                raise OperatorEvidenceError("terminal broker state changed")
        if result.lifecycle_state in {"filled", "partially_filled"} and not result.executions:
            raise OperatorEvidenceError("filled status lacks broker-confirmed executions")
        if exact_sum(Decimal(x["executed_amount"]) if x.get("executed_amount") is not None
                     else Decimal(x["units"]) * Decimal(x["price"]) for x in records.values()) > intent.sizing_value:
            raise OperatorEvidenceError("broker executions exceed approved notional")
        return records

    def _check_staged_collisions(self, executions, *, ticker):
        if not self.staged_fills_path.exists():
            return
        with self.staged_fills_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        existing = {row["fill_id"]: row for row in rows}
        if len(rows) != len(existing):
            raise OperatorEvidenceError("duplicate staged execution identity")
        for execution in executions:
            row = existing.get(f"{self.broker_name}:demo:{execution.broker_execution_id}")
            if not row:
                continue
            numeric = {"quantity": execution.units, "fill_price": execution.price,
                       "broker_fee": execution.fees, "broker_tax": execution.taxes,
                       "fees": exact_sum((execution.fees, execution.taxes))}
            if execution.executed_amount is not None:
                if not row.get("executed_amount"):
                    raise OperatorEvidenceError("staged execution lacks authoritative principal")
                numeric["executed_amount"] = execution.executed_amount
            elif row.get("executed_amount"):
                raise OperatorEvidenceError("staged execution principal evidence changed")
            strings = {"ticker": ticker, "broker": self.broker_name, "environment": "demo",
                       "broker_execution_id": execution.broker_execution_id,
                       "broker_order_id": execution.broker_order_id,
                       "broker_position_id": execution.broker_position_id,
                       "broker_reference_id": execution.broker_reference_id,
                       "broker_instrument_id": str(execution.broker_instrument_id),
                       "broker_rate_id": execution.price_rate_id, "currency": execution.currency,
                       "side": execution.side, "action": execution.action}
            if (any(Decimal(row[name]) != value for name, value in numeric.items())
                    or any(row[name] != value for name, value in strings.items())
                    or (execution.executed_at_source is not None and row["fill_timestamp"] != execution.executed_at_source)
                    or datetime.fromisoformat(row["fill_timestamp"].replace("Z", "+00:00")) != execution.executed_at):
                raise OperatorEvidenceError("staged execution identity has contradictory values")

    def reconcile(self, command_id, *, adapter: BrokerAdapter, now: datetime) -> OperatorReceipt:
        self._fixture_adapter(adapter)
        # The shared resource is the staging CSV, not a workflow's own ledger.
        # Different operator databases targeting it therefore contend on the
        # same exclusive lock. A process crash leaves the lock for inspection;
        # neither time nor a missing process automatically proves recovery safe.
        staging_path = self.staged_fills_path.resolve()
        lock = ScheduleRunLock(staging_path.with_name(staging_path.name + ".operator.lock"))
        try:
            lease = lock.acquire(run_id=str(command_id), now=now)
        except ScheduleLockError as exc:
            raise OperatorEvidenceError(
                "operator staging lock exists or is invalid; inspect and recover it manually"
            ) from exc
        try:
            return self._reconcile_locked(command_id, adapter=adapter, now=now)
        finally:
            lock.release(lease)

    def _reconcile_locked(self, command_id, *, adapter: BrokerAdapter, now: datetime) -> OperatorReceipt:
        proof, command, intent, _ = self._bound(command_id)
        allowed = {CommandState.SUBMISSION_PENDING, CommandState.UNKNOWN,
                   CommandState.RECONCILING, CommandState.ACKNOWLEDGED,
                   CommandState.PARTIALLY_FILLED, CommandState.CANCEL_PENDING,
                   CommandState.RECONCILED}
        if command.state not in allowed:
            raise OperatorEvidenceError("command has no submitted or ambiguous order to reconcile")
        moment = _aware_utc(now, "now")
        result = adapter.reconcile_command(command)
        self._validate_lookup(proof, command, intent, result, moment)
        if command.state == CommandState.RECONCILED and not result.reconciled:
            raise OperatorEvidenceError("terminal broker evidence regressed")
        self._check_staged_collisions(result.executions, ticker=proof["instrument"]["canonical_symbol"])
        # Durable lookup evidence precedes lifecycle transitions and staging.
        # A crash at either boundary can be recovered by another read, not send.
        self.evidence.append_observation(command_id, {
            "source_kind": self.source_kind, "observed_at": moment, "result": result,
            "executions": result.executions, "economic_reconciliation_verified": False,
            "qualification_credit": False,
        })
        if command.state != CommandState.RECONCILED:
            command, _ = self.coordinator.reconcile_command(
                command=command, adapter=_CapturedLookup(adapter, result))
        unique = {item.broker_execution_id: item for item in result.executions}
        stage_broker_executions(
            self.staged_fills_path, executions=tuple(unique.values()), broker=self.broker_name,
            ticker_by_instrument_id={proof["mapping"]["broker_instrument_id"]: proof["instrument"]["canonical_symbol"]})
        return OperatorReceipt(
            command_id=str(command.command_id), source_kind=self.source_kind,
            command_state=str(command.state), broker_terminal=result.reconciled,
            staged_execution_ids=tuple(sorted(unique)))
