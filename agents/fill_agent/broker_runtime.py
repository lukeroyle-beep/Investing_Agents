"""Fill-owned exact Demo economic CSV runtime, schema version 2.

This is a separate *runtime schema*, not a second economic authority. Its
canonical portfolio/cash/trade CSVs retain Decimal text and are accessed under a
single writer/read lock. The legacy float runtime refuses this manifest. A
recoverable journal makes interrupted multi-file replacement deterministic.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from typing import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from brokers.base import BrokerExecution
from brokers.etoro.account_evidence import AccountComponentTimeEvidence
from brokers.etoro.schemas import EtoroDetailedPortfolio, DEMO_PENDING_COLLECTIONS, exact_datetime_epoch_seconds, _pnl_timestamp
from execution.accounting import _at, _sum, valuation_from_component_time
from execution.domain import BrokerCommand, BrokerOperation, CommandState, Environment, canonical_json, payload_hash


SCHEMA_VERSION = "fill-broker-csv-v2"
MANIFEST = "broker_runtime_manifest.json"
JOURNAL = "broker_transaction.json"
CHECKPOINT = "broker_runtime_checkpoint.json"
POSITION_COLUMNS = ("broker_position_id", "broker_instrument_id", "units", "invested_amount", "currency", "current_exposure", "unrealised_pnl")
CASH_COLUMNS = ("account_id", "currency", "cash_balance", "invested_amount", "unrealised_pnl", "equity", "source_snapshot_id", "source_time_hash", "coverage_at")
FILL_COLUMNS = ("broker_execution_id", "command_id", "execution_hash", "broker_order_id", "broker_reference_id", "broker_position_id", "broker_instrument_id", "units", "price", "principal", "fees", "taxes", "currency", "executed_at", "executed_at_source")
LEDGER_COLUMNS = ("broker_execution_id", "command_id", "principal_debit", "fee_debit", "tax_debit", "cash_change", "currency")
PROCESSED_COLUMNS = ("broker_execution_id", "command_id", "execution_hash")
SCHEMAS = {"portfolio_state.csv": POSITION_COLUMNS, "cash_state.csv": CASH_COLUMNS,
           "trade_fills.csv": FILL_COLUMNS, "cash_ledger.csv": LEDGER_COLUMNS,
           "processed_fills.csv": PROCESSED_COLUMNS}
EVIDENCE_LOG = "broker_reconciliation.jsonl"
RUNTIME_FILES = (*SCHEMAS, EVIDENCE_LOG)


class BrokerEconomicError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrokerHolding:
    broker_position_id: str
    broker_instrument_id: int
    units: Decimal
    invested_amount: Decimal
    currency: str
    current_exposure: Decimal
    unrealised_pnl: Decimal


@dataclass(frozen=True, slots=True)
class ReconciledBrokerAccount:
    component: AccountComponentTimeEvidence
    detailed: EtoroDetailedPortfolio
    detailed_payload: Mapping

    @classmethod
    def from_component(cls, proof, detailed, *, detailed_payload):
        if not isinstance(proof, AccountComponentTimeEvidence):
            raise BrokerEconomicError("typed_component_account_required")
        account = cls(proof, detailed, deepcopy(detailed_payload))
        account.validate(proof.assessed_at)
        return account

    def validate(self, now):
        if not isinstance(self.component, AccountComponentTimeEvidence) or not isinstance(self.detailed, EtoroDetailedPortfolio):
            raise BrokerEconomicError("typed_full_account_sources_required")
        proof, detail = self.component, self.detailed
        if not isinstance(self.detailed_payload, Mapping):
            raise BrokerEconomicError("retained_raw_detailed_payload_required")
        try:
            reparsed = EtoroDetailedPortfolio.parse(self.detailed_payload)
        except (ValueError, TypeError):
            raise BrokerEconomicError("retained_raw_detailed_payload_invalid") from None
        if reparsed != detail:
            raise BrokerEconomicError("detailed_projection_differs_from_retained_source")
        if proof.validate_at(_at(now)):
            raise BrokerEconomicError("fresh_reconciled_account_required")
        if detail.snapshot_id not in {proof.evidence.detailed_snapshot_id, proof.evidence.closing_detailed_snapshot_id}:
            raise BrokerEconomicError("detailed_account_source_binding_mismatch")
        if (detail.mirror_count or detail.bonus_credit or detail.credit != proof.available_cash
                or detail.pending_collection_counts != tuple((name, 0) for name in DEMO_PENDING_COLLECTIONS)):
            raise BrokerEconomicError("complete_direct_empty_pending_account_required")
        positions = {p.broker_position_id: p for p in proof.positions}
        if len(positions) != len(detail.positions) or set(positions) != {p.position_id for p in detail.positions}:
            raise BrokerEconomicError("complete_holdings_required")
        for p in detail.positions:
            source = positions[p.position_id]
            if (p.customer_id != proof.demo_customer_id or p.instrument_id != source.broker_instrument_id
                    or p.units != source.units or p.mirror_id or not p.is_buy or p.leverage != 1
                    or source.currency != "USD" or p.amount < 0):
                raise BrokerEconomicError("holding_identity_units_or_principal_invalid")
        if _sum(p.amount for p in detail.positions) != proof.invested_amount:
            raise BrokerEconomicError("full_invested_principal_not_reconciled")
        return valuation_from_component_time(proof, now=now, reconciliation_reference=detail.snapshot_id)

    def holdings(self):
        positions = {p.broker_position_id: p for p in self.component.positions}
        return tuple(BrokerHolding(p.position_id, p.instrument_id, p.units, p.amount,
                                   "USD", positions[p.position_id].current_exposure, positions[p.position_id].pnl)
                     for p in sorted(self.detailed.positions, key=lambda p: p.position_id))


@dataclass(frozen=True, slots=True)
class EconomicApplicationReceipt:
    transaction_id: str
    applied_execution_ids: tuple[str, ...]
    cash_balance: Decimal
    reconciled: bool
    replay: bool = False


def _execution_epoch(item):
    view_epoch = exact_datetime_epoch_seconds(_at(item.executed_at))
    source = getattr(item, "executed_at_source", None)
    if source is None:
        raise BrokerEconomicError("execution_source_timestamp_required")
    try:
        source_epoch = _pnl_timestamp(source)[1]
    except (ValueError, TypeError):
        raise BrokerEconomicError("execution_source_timestamp_invalid") from None
    if not Decimal("0") <= _sum((source_epoch, view_epoch.copy_negate())) < Decimal("0.000001"):
        raise BrokerEconomicError("execution_source_timestamp_view_mismatch")
    return source_epoch


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _csv(columns, rows):
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def _holding_rows(account):
    return [{name: getattr(item, name) for name in POSITION_COLUMNS} for item in account.holdings()]


def _cash_row(account, value):
    return {"account_id": value.account_id, "currency": value.currency, "cash_balance": value.cash,
            "invested_amount": value.invested_amount, "unrealised_pnl": value.unrealised_pnl,
            "equity": value.equity, "source_snapshot_id": value.snapshot_id,
            "source_time_hash": value.time_evidence_hash, "coverage_at": value.coverage_at.isoformat()}


class BrokerEconomicRuntime:
    """No default money, no reset, no importing an already-used legacy runtime."""

    def __init__(self, root):
        self.root = Path(root)
        if not self.root.is_dir() or not (self.root / MANIFEST).is_file():
            raise BrokerEconomicError("versioned_broker_runtime_required")
        try:
            self.manifest = json.loads((self.root / MANIFEST).read_text())
        except (ValueError, UnicodeError):
            raise BrokerEconomicError("broker_runtime_manifest_invalid") from None
        if (self.manifest.get("schema_version") != SCHEMA_VERSION
                or self.manifest.get("broker") != "etoro" or self.manifest.get("environment") != "demo"
                or self.manifest.get("currency") != "USD" or not self.manifest.get("account_id")):
            raise BrokerEconomicError("broker_runtime_manifest_invalid")
        self.manifest_hash = payload_hash(self.manifest)

    @classmethod
    def initialize(cls, root, *, account: ReconciledBrokerAccount, now):
        if not isinstance(account, ReconciledBrokerAccount):
            raise BrokerEconomicError("typed_reconciled_account_required")
        value = account.validate(now)
        destination = Path(root)
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise BrokerEconomicError("new_empty_runtime_required_no_overwrite")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The requested runtime becomes visible only after all initial CSVs and
        # checkpoint commit. An interrupted initializer retains private staging
        # evidence but cannot expose an apparently usable half-initialized book.
        path = Path(tempfile.mkdtemp(prefix=".broker-init-", dir=destination.parent))
        manifest = {"schema_version": SCHEMA_VERSION, "owner": "Fill Agent", "broker": "etoro",
                    "environment": "demo", "account_id": value.account_id, "currency": "USD",
                    "initial_snapshot_id": value.snapshot_id, "initial_time_hash": value.time_evidence_hash,
                    "initial_coverage_at": value.coverage_at.isoformat(), "canonical_files": list(SCHEMAS)}
        # Exclusive creation prevents two initializers claiming the same account
        # directory. The marker is visible before any economic CSV is created.
        descriptor = os.open(path / MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as target:
            target.write(canonical_json(manifest))
            target.flush()
            os.fsync(target.fileno())
        runtime = cls(path)
        outputs = {name: _csv(columns, []) for name, columns in SCHEMAS.items()}
        outputs["portfolio_state.csv"] = _csv(POSITION_COLUMNS, _holding_rows(account))
        outputs["cash_state.csv"] = _csv(CASH_COLUMNS, [_cash_row(account, value)])
        outputs[EVIDENCE_LOG] = canonical_json({"kind": "initialization", "account": account, "validated_at": _at(now)}) + "\n"
        with runtime._lock():
            runtime._commit(outputs, transaction_id="initialize:" + value.snapshot_id,
                            receipt={"reconciled": True, "applied_execution_ids": []})
        # POSIX directory rename can replace an empty destination, never a
        # nonempty runtime. A concurrently created book therefore fails closed.
        os.rename(path, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return cls(destination)

    @contextmanager
    def _lock(self):
        with (self.root / ".broker-runtime.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if payload_hash(json.loads((self.root / MANIFEST).read_text())) != self.manifest_hash:
                    raise BrokerEconomicError("immutable_account_manifest_changed")
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _atomic_write(self, name, text):
        target = self.root / name
        temporary = self.root / ("." + name + ".pending")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", newline="") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, target)
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _commit(self, outputs, *, transaction_id, receipt):
        before = {name: _digest((self.root / name).read_bytes()) if (self.root / name).exists() else None for name in RUNTIME_FILES}
        checkpoint = {"schema_version": SCHEMA_VERSION, "manifest_hash": self.manifest_hash,
                      "transaction_id": transaction_id, "receipt": receipt,
                      "files": {name: _digest(text.encode()) for name, text in outputs.items()}}
        if (self.root / JOURNAL).exists():
            raise BrokerEconomicError("pending_transaction_requires_recovery")
        body = {"manifest_hash": self.manifest_hash, "before": before, "outputs": outputs,
                "checkpoint": checkpoint}
        self._atomic_write(JOURNAL, canonical_json({"body": body, "journal_hash": payload_hash(body)}))
        self._recover_locked()

    def _recover_locked(self):
        path = self.root / JOURNAL
        if not path.exists():
            return
        try:
            journal = json.loads(path.read_text())
            body = journal["body"]
            if (journal["journal_hash"] != payload_hash(body) or body["manifest_hash"] != self.manifest_hash
                    or set(body["outputs"]) != set(RUNTIME_FILES) or set(body["before"]) != set(RUNTIME_FILES)
                    or body["checkpoint"]["manifest_hash"] != self.manifest_hash
                    or body["checkpoint"]["files"] != {name: _digest(text.encode()) for name, text in body["outputs"].items()}):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise BrokerEconomicError("transaction_journal_integrity_failure") from None
        for name, text in body["outputs"].items():
            current = _digest((self.root / name).read_bytes()) if (self.root / name).exists() else None
            if current not in {body["before"][name], _digest(text.encode())}:
                raise BrokerEconomicError("economic_state_changed_during_transaction")
        for name, text in body["outputs"].items():
            self._atomic_write(name, text)
        self._atomic_write(CHECKPOINT, canonical_json(body["checkpoint"]))
        path.unlink()
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def recover(self):
        with self._lock():
            self._recover_locked()
            self._read_locked()

    def _read_locked(self):
        self._recover_locked()
        try:
            checkpoint = json.loads((self.root / CHECKPOINT).read_text())
            if (checkpoint["manifest_hash"] != self.manifest_hash
                    or set(checkpoint["files"]) != set(RUNTIME_FILES)):
                raise ValueError
            frames = {}
            for name in RUNTIME_FILES:
                raw = (self.root / name).read_bytes()
                if _digest(raw) != checkpoint["files"][name]:
                    raise ValueError
                if name == EVIDENCE_LOG:
                    text = raw.decode()
                    if not text or any(not isinstance(json.loads(line), dict) for line in text.splitlines()):
                        raise ValueError
                    frames[name] = text
                    continue
                columns = SCHEMAS[name]
                reader = csv.DictReader(io.StringIO(raw.decode()), strict=True)
                if tuple(reader.fieldnames or ()) != columns:
                    raise ValueError
                frames[name] = list(reader)
                if any(None in row or any(value is None for value in row.values()) for row in frames[name]):
                    raise ValueError
        except (OSError, ValueError, KeyError, TypeError, csv.Error):
            raise BrokerEconomicError("canonical_broker_csv_integrity_failure") from None
        if len(frames["cash_state.csv"]) != 1:
            raise BrokerEconomicError("single_canonical_cash_snapshot_required")
        cash = frames["cash_state.csv"][0]
        if cash["account_id"] != self.manifest["account_id"] or cash["currency"] != "USD":
            raise BrokerEconomicError("economic_account_identity_mismatch")
        return frames

    def snapshot(self):
        with self._lock():
            frames = self._read_locked()
            return {"cash_balance": Decimal(frames["cash_state.csv"][0]["cash_balance"]),
                    "positions": tuple(BrokerHolding(row["broker_position_id"], int(row["broker_instrument_id"]),
                        Decimal(row["units"]), Decimal(row["invested_amount"]), row["currency"],
                        Decimal(row["current_exposure"]), Decimal(row["unrealised_pnl"]))
                        for row in frames["portfolio_state.csv"]),
                    "processed_execution_ids": tuple(row["broker_execution_id"] for row in frames["processed_fills.csv"])}

    def apply_confirmed(self, *, executions, command: BrokerCommand, broker_payload,
                        post_account: ReconciledBrokerAccount, now):
        if not isinstance(post_account, ReconciledBrokerAccount):
            raise BrokerEconomicError("typed_post_broker_account_required")
        value = post_account.validate(now)
        if value.account_id != self.manifest["account_id"]:
            raise BrokerEconomicError("post_broker_account_identity_mismatch")
        if (not isinstance(command, BrokerCommand) or command.broker != "etoro"
                or command.environment != Environment.DEMO or command.operation != BrokerOperation.SUBMIT_ORDER or command.state not in {CommandState.FILLED, CommandState.RECONCILED}
                or not command.broker_order_id or not command.broker_reference_id
                or command.payload_hash != payload_hash(broker_payload)
                or broker_payload.get("action") != "open" or broker_payload.get("transaction") != "buy"
                or broker_payload.get("leverage") != 1 or broker_payload.get("orderCurrency") != "usd"
                or broker_payload.get("settlementType") != "real"):
            raise BrokerEconomicError("terminal_bound_usd_unleveraged_buy_required")
        if not isinstance(executions, tuple) or not executions or not all(isinstance(x, BrokerExecution) for x in executions):
            raise BrokerEconomicError("typed_terminal_executions_required")
        if len({x.broker_execution_id for x in executions}) != len(executions):
            raise BrokerEconomicError("duplicate_execution_in_terminal_result")
        for item in executions:
            if (item.environment != Environment.DEMO or item.broker_order_id != command.broker_order_id
                    or item.broker_reference_id != command.broker_reference_id
                    or item.broker_instrument_id != broker_payload.get("instrumentId")
                    or item.side != "long" or item.action != "buy" or item.currency != "USD"
                    or any(not isinstance(x, str) or not x.strip() for x in (
                        item.broker_execution_id, item.broker_order_id, item.broker_reference_id,
                        item.broker_position_id, item.price_rate_id))
                    or type(item.broker_instrument_id) is not int or item.broker_instrument_id <= 0
                    or any(not isinstance(x, Decimal) or not x.is_finite() for x in (item.units, item.price, item.fees, item.taxes, item.executed_amount))
                    or min(item.units, item.price, item.executed_amount) <= 0 or min(item.fees, item.taxes) < 0
                    or _execution_epoch(item) > exact_datetime_epoch_seconds(value.coverage_at)):
                raise BrokerEconomicError("execution_identity_or_economic_values_invalid")
        executions = tuple(sorted(executions, key=lambda item: item.broker_execution_id))
        metadata = {item.instrument_id: item.instrument_type for item in post_account.component.evidence.security_metadata}
        if metadata.get(broker_payload["instrumentId"]) not in {"Stocks", "ETF"}:
            raise BrokerEconomicError("new_execution_equity_or_etf_required")
        with self._lock():
            frames = self._read_locked()
            old_cash = Decimal(frames["cash_state.csv"][0]["cash_balance"])
            old = {row["broker_position_id"]: row for row in frames["portfolio_state.csv"]}
            done = {row["broker_execution_id"]: row for row in frames["processed_fills.csv"]}
            command_execution_ids = {row["broker_execution_id"] for row in frames["processed_fills.csv"]
                                     if row["command_id"] == str(command.command_id)}
            if command_execution_ids and command_execution_ids != {x.broker_execution_id for x in executions}:
                raise BrokerEconomicError("terminal_command_execution_set_changed")
            new = []
            for item in executions:
                digest = payload_hash(item)
                if item.broker_execution_id in done:
                    if done[item.broker_execution_id]["execution_hash"] != digest or done[item.broker_execution_id]["command_id"] != str(command.command_id):
                        raise BrokerEconomicError("execution_replay_conflict")
                else:
                    new.append(item)
            if new and len(new) != len(executions):
                raise BrokerEconomicError("partial_terminal_batch_replay_requires_reconciliation")
            expected = {key: (int(row["broker_instrument_id"]), Decimal(row["units"]), Decimal(row["invested_amount"])) for key, row in old.items()}
            debit = Decimal("0")
            for item in new:
                previous = expected.get(item.broker_position_id, (item.broker_instrument_id, Decimal("0"), Decimal("0")))
                if previous[0] != item.broker_instrument_id:
                    raise BrokerEconomicError("position_instrument_changed")
                principal = item.executed_amount
                expected[item.broker_position_id] = (previous[0], _sum((previous[1], item.units)), _sum((previous[2], principal)))
                debit = _sum((debit, principal, item.fees, item.taxes))
            actual = {p.broker_position_id: (p.broker_instrument_id, p.units, p.invested_amount) for p in post_account.holdings()}
            if expected != actual:
                raise BrokerEconomicError("post_broker_all_holdings_not_reconciled")
            expected_cash = _sum((old_cash, debit.copy_negate()))
            if expected_cash != value.cash or expected_cash < 0:
                raise BrokerEconomicError("post_broker_cash_fees_or_tax_not_reconciled")
            transaction_id = payload_hash({"command": str(command.command_id), "executions": tuple(executions)})
            if not new:
                return EconomicApplicationReceipt(transaction_id, (), value.cash, True, True)
            if _sum(x.executed_amount for x in new) != Decimal(str(broker_payload.get("amount"))):
                raise BrokerEconomicError("filled_principal_differs_from_exact_order_amount")
            previous_coverage = datetime.fromisoformat(frames["cash_state.csv"][0]["coverage_at"])
            if any(_execution_epoch(x) <= exact_datetime_epoch_seconds(previous_coverage) for x in new):
                raise BrokerEconomicError("execution_not_after_initial_or_last_reconciled_account")
            # Component clocks remain distinct. Collection completion cannot
            # prove that cash or a changed position was valued after execution.
            cash_epoch = post_account.component.cash_observation.timestamp_epoch_seconds
            marks = {p.position_id: p.timestamp_epoch_seconds
                     for p in post_account.component.evidence.position_valuations}
            if cash_epoch < max(_execution_epoch(x) for x in new):
                raise BrokerEconomicError("post_cash_observation_precedes_execution")
            if any(marks.get(x.broker_position_id, Decimal("-Infinity"))
                   < _execution_epoch(x) for x in new):
                raise BrokerEconomicError("post_position_valuation_precedes_execution")
            for item in new:
                principal = item.executed_amount
                frames["trade_fills.csv"].append({"broker_execution_id": item.broker_execution_id,
                    "command_id": str(command.command_id), "execution_hash": payload_hash(item),
                    "broker_order_id": item.broker_order_id, "broker_reference_id": item.broker_reference_id,
                    "broker_position_id": item.broker_position_id, "broker_instrument_id": item.broker_instrument_id,
                    "units": item.units, "price": item.price, "principal": principal, "fees": item.fees,
                    "taxes": item.taxes, "currency": item.currency, "executed_at": item.executed_at.isoformat(),
                    "executed_at_source": getattr(item, "executed_at_source", None) or ""})
                frames["processed_fills.csv"].append({"broker_execution_id": item.broker_execution_id,
                    "command_id": str(command.command_id), "execution_hash": payload_hash(item)})
                frames["cash_ledger.csv"].append({"broker_execution_id": item.broker_execution_id,
                    "command_id": str(command.command_id), "principal_debit": principal,
                    "fee_debit": item.fees, "tax_debit": item.taxes,
                    "cash_change": _sum((principal, item.fees, item.taxes)).copy_negate(), "currency": "USD"})
            frames["portfolio_state.csv"] = _holding_rows(post_account)
            frames["cash_state.csv"] = [_cash_row(post_account, value)]
            outputs = {name: _csv(columns, frames[name]) for name, columns in SCHEMAS.items()}
            outputs[EVIDENCE_LOG] = frames[EVIDENCE_LOG] + canonical_json({
                "kind": "terminal_execution_reconciled", "account": post_account,
                "command": command, "broker_payload": broker_payload, "executions": executions,
                "validated_at": _at(now), "transaction_id": transaction_id}) + "\n"
            self._commit(outputs,
                         transaction_id=transaction_id,
                         receipt={"reconciled": True, "applied_execution_ids": [x.broker_execution_id for x in new],
                                  "post_snapshot_id": value.snapshot_id, "post_time_hash": value.time_evidence_hash})
            return EconomicApplicationReceipt(transaction_id, tuple(x.broker_execution_id for x in new), value.cash, True)
