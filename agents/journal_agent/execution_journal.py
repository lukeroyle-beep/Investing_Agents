"""Private execution audit, separate from the recommendation journal.

This store never changes economic or operational state. Completion is rebuilt
from the original economic commit, operational command and source account
receipts; callers cannot submit a ``verified=True`` assertion. Transactions,
append-only triggers and a hash chain detect edits, gaps and suffix deletion.
As with any local database, a coordinated rollback of *all* retained stores
requires an external trusted checkpoint to detect; this is not a remote seal.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.account_evidence import _difference, _exact_sum
from brokers.etoro.pilot_account import build_pilot_account
from execution.domain import CommandState, Environment, canonical_json, payload_hash
from execution.store import ExecutionStore


SCHEMA = "demo-execution-journal-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PHASES = {"intent", "dispatch", "outcome", "reconciliation", "external_import"}


class ExecutionJournalError(ValueError):
    """Fail closed: do not begin another trade when audit continuity is lost."""


def _clean(value):
    """Reject secrets, including nested JSON envelopes; never silently drop them."""
    def visit(item):
        if isinstance(item, float):
            raise ExecutionJournalError("journal_exact_decimal_evidence_required")
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(word in normalized for word in (
                    "token", "authorization", "apikey", "userkey", "credential", "secret", "password",
                    "privatekey", "accesskey", "sessionkey", "cookie",
                )):
                    raise ExecutionJournalError("journal_sensitive_field_rejected")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(item, parse_float=Decimal)
            except (ValueError, TypeError):
                pass
            else:
                visit(nested)
    try:
        result = json.loads(canonical_json(value))
        visit(result)
        return result
    except ExecutionJournalError:
        raise
    except (ValueError, TypeError) as exc:
        raise ExecutionJournalError("journal_invalid_payload") from exc


def _digest(value, name):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ExecutionJournalError(f"journal_{name}_sha256_required")
    return value


class ExecutionJournal:
    """Bound append-only audit. Missing storage is never implicitly recreated.

    ``record_*`` methods read retained sources themselves. ``reconstruct`` is
    safe to repeat after a crash; it does not resubmit, reconcile economics or
    turn a pending/unknown order into a terminal one. After final readback the
    caller may mark the economic ledger's journal obligation complete with
    the returned ``chain_hash`` as its durable reference.
    """

    @staticmethod
    def _binding(ledger, store, runtime_root):
        if not isinstance(ledger, PilotLedger) or not isinstance(store, ExecutionStore):
            raise ExecutionJournalError("journal_original_typed_stores_required")
        root = Path(runtime_root).resolve()
        ledger_path, store_path = Path(ledger.path).resolve(), Path(store.path).resolve()
        if not ledger_path.is_relative_to(root) or not store_path.is_relative_to(root):
            raise ExecutionJournalError("journal_original_runtime_binding_required")
        snapshot = ledger.snapshot()
        return _clean({"schema": SCHEMA, "environment": "demo", "account_id": snapshot["account_id"],
            "runtime_root": str(root), "ledger_path": str(ledger_path), "store_path": str(store_path),
            "inception_hash": payload_hash(snapshot["inception"])})

    @classmethod
    def create(cls, path, *, ledger, store, runtime_root):
        path = Path(path).resolve()
        binding = cls._binding(ledger, store, runtime_root)
        if not path.is_relative_to(Path(runtime_root).resolve()):
            raise ExecutionJournalError("journal_original_runtime_binding_required")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ExecutionJournalError("journal_exists_never_reset") from exc
        os.close(fd)
        # If initialization fails, preserve the failed file for investigation.
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE binding(singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE head(singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE events(sequence INTEGER PRIMARY KEY, command_id TEXT NOT NULL,
                    phase TEXT NOT NULL, event_identity TEXT NOT NULL, payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, previous_hash TEXT NOT NULL, chain_hash TEXT NOT NULL,
                    UNIQUE(command_id,phase,event_identity));
                CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END;
                CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append_only'); END;
                CREATE TRIGGER binding_no_update BEFORE UPDATE ON binding BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER binding_no_delete BEFORE DELETE ON binding BEGIN SELECT RAISE(ABORT,'immutable'); END;
                CREATE TRIGGER head_no_delete BEFORE DELETE ON head BEGIN SELECT RAISE(ABORT,'immutable'); END;
            """)
            genesis = payload_hash(binding)
            db.execute("INSERT INTO binding VALUES(1,?,?)", (canonical_json(binding), genesis))
            db.execute("INSERT INTO head VALUES(1,0,?)", (genesis,))
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return cls(path, ledger=ledger, store=store, runtime_root=runtime_root)

    def __init__(self, path, *, ledger, store, runtime_root):
        self.path = Path(path).resolve()
        self.ledger, self.store = ledger, store
        self.binding = self._binding(ledger, store, runtime_root)
        if not self.path.is_relative_to(Path(runtime_root).resolve()):
            raise ExecutionJournalError("journal_original_runtime_binding_required")
        if not self.path.is_file():
            raise ExecutionJournalError("journal_missing_explicit_creation_required")
        self.ensure_ready()

    def _connect(self):
        if not self.path.is_file():
            raise ExecutionJournalError("journal_missing_never_reinitialize")
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=30)
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.DatabaseError as exc:
            raise ExecutionJournalError("journal_storage_unavailable") from exc

    @staticmethod
    def _chain(sequence, command_id, phase, event_identity, digest, previous):
        return payload_hash({"sequence": sequence, "command_id": command_id, "phase": phase,
            "event_identity": event_identity, "payload_hash": digest, "previous_hash": previous})

    def _read(self, db):
        try:
            if not db.in_transaction:
                db.execute("BEGIN")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ExecutionJournalError("journal_storage_integrity_failure")
            row = db.execute("SELECT payload,digest FROM binding WHERE singleton=1").fetchone()
            if not row or json.loads(row[0]) != self.binding or row[1] != payload_hash(self.binding):
                raise ExecutionJournalError("journal_original_binding_mismatch")
            previous, records, phases = row[1], [], {}
            for row in db.execute("SELECT * FROM events ORDER BY sequence"):
                seq, command, phase, identity, raw, digest, prev, chain = row
                data = _clean(json.loads(raw))
                if (seq != len(records) + 1 or phase not in _PHASES or not command or not identity
                        or digest != payload_hash(data) or prev != previous
                        or chain != self._chain(seq, command, phase, identity, digest, previous)):
                    raise ExecutionJournalError("journal_chain_integrity_failure")
                seen = phases.setdefault(command, set())
                if phase == "external_import":
                    if seen or not command.startswith("external:") or identity != "external_import":
                        raise ExecutionJournalError("journal_external_phase_integrity_failure")
                elif (command.startswith("external:") or (phase != "intent" and "intent" not in seen)
                        or (phase in {"outcome", "reconciliation"} and "dispatch" not in seen)
                        or (phase == "reconciliation" and "outcome" not in seen)
                        or (phase != "outcome" and phase in seen)
                        or "reconciliation" in seen):
                    raise ExecutionJournalError("journal_phase_integrity_failure")
                seen.add(phase)
                records.append({"sequence": seq, "command_id": command, "phase": phase,
                    "event_identity": identity, "payload": data, "payload_hash": digest,
                    "previous_hash": prev, "chain_hash": chain})
                previous = chain
            if db.execute("SELECT sequence,digest FROM head WHERE singleton=1").fetchone() != (len(records), previous):
                raise ExecutionJournalError("journal_head_or_truncation_failure")
            return records
        except ExecutionJournalError:
            raise
        except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
            raise ExecutionJournalError("journal_storage_corrupt_never_reinitialize") from exc

    def ensure_ready(self):
        """Validate the entire audit and every terminal record against its sources."""
        if self._binding(self.ledger, self.store, self.binding["runtime_root"]) != self.binding:
            raise ExecutionJournalError("journal_original_binding_mismatch")
        with closing(self._connect()) as db:
            records = self._read(db)
        for record in records:
            if record["phase"] == "external_import":
                identity = record["command_id"].removeprefix("external:")
                actual = self._external_payload(identity)
                if record["payload"] != actual:
                    raise ExecutionJournalError("journal_external_economic_source_changed")
                completion = self.ledger.external_lifecycle_receipt(identity)["journal_receipt"]
                if completion is not None and (completion["journal_reference"] != record["chain_hash"]
                        or completion["journal_path"] != str(self.path.resolve())):
                    raise ExecutionJournalError("journal_external_completion_hash_mismatch")
            if record["phase"] == "intent":
                command, intent = self._command(record["command_id"])
                data = record["payload"]
                if (data["intent"] != _clean(intent) or data["intent_hash"] != intent.intent_hash
                        or data["logical_request_id"] != str(command.logical_request_id)
                        or data["broker_payload_hash"] != command.payload_hash):
                    raise ExecutionJournalError("journal_intent_source_changed")
            if record["phase"] == "dispatch":
                claim, _ = self._economic(record["command_id"])
                if record["payload"]["claim"] != claim:
                    raise ExecutionJournalError("journal_dispatch_source_changed")
            if record["phase"] == "reconciliation":
                actual = self._completion_payload(record["command_id"], records)
                if payload_hash(actual) != record["payload_hash"]:
                    raise ExecutionJournalError("journal_terminal_source_changed")
        return True

    @classmethod
    def verify_external_receipts(cls, *, ledger_path, inception, imports, receipts):
        """Read-only reverse links; a valid older journal must not release a claim.

        This deliberately avoids ledger/store constructors, so ledger replay can
        call it without recursion, initialization, or a writable SQLite handle.
        Pending imports have no receipt and remain recoverable.
        """
        grouped = {}
        for identity, receipt in receipts.items():
            grouped.setdefault(receipt["journal_path"], []).append((identity, receipt))
        for filename, expected in grouped.items():
            path = Path(filename)
            if (not path.is_absolute() or path.resolve() != path or not path.is_file()
                    or not path.is_relative_to(Path(ledger_path).resolve().parent)):
                raise ExecutionJournalError("journal_external_original_path_required")
            try:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
                    row = db.execute("SELECT payload FROM binding WHERE singleton=1").fetchone()
                    binding = json.loads(row[0]) if row else {}
                    if (binding.get("schema") != SCHEMA or binding.get("environment") != "demo"
                            or binding.get("ledger_path") != str(Path(ledger_path).resolve())
                            or binding.get("runtime_root") != str(Path(ledger_path).resolve().parent)
                            or binding.get("account_id") != inception["account_id"]
                            or binding.get("inception_hash") != payload_hash(inception)):
                        raise ExecutionJournalError("journal_external_original_binding_mismatch")
                    reader = object.__new__(cls)
                    reader.binding = binding
                    records = reader._read(db)
                terminals = {r["command_id"]: r for r in records if r["phase"] == "external_import"}
                for identity, receipt in expected:
                    record = terminals.get("external:" + identity)
                    economic = imports[identity]
                    payload = _clean({"origin": "external", "event_id": identity,
                        "economic_record_hash": payload_hash(economic), "economic_record": economic,
                        "qualification_credit": False})
                    if (record is None or record["chain_hash"] != receipt["journal_reference"]
                            or record["payload"] != payload):
                        raise ExecutionJournalError("journal_external_completed_terminal_missing_or_changed")
            except (sqlite3.DatabaseError, ValueError, TypeError, KeyError) as exc:
                if isinstance(exc, ExecutionJournalError):
                    raise
                raise ExecutionJournalError("journal_external_receipt_unreadable") from exc

    def events(self, command_id=None):
        self.ensure_ready()
        with closing(self._connect()) as db:
            records = self._read(db)
        return tuple(r for r in records if command_id is None or r["command_id"] == str(command_id))

    def terminal(self, command_id):
        return next((r for r in self.events(command_id) if r["phase"] == "reconciliation"), None)

    def is_complete(self, command_id):
        return self.terminal(command_id) is not None

    def _append(self, command_id, phase, identity, payload):
        command_id, identity = str(command_id), str(identity)
        data = _clean(payload)
        self.ensure_ready()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            records = self._read(db)
            previous = next((r for r in records if (r["command_id"], r["phase"], r["event_identity"])
                             == (command_id, phase, identity)), None)
            digest = payload_hash(data)
            if previous:
                if previous["payload_hash"] != digest:
                    raise ExecutionJournalError("journal_conflicting_duplicate_event")
                return previous
            seq = len(records) + 1
            prev = records[-1]["chain_hash"] if records else payload_hash(self.binding)
            chain = self._chain(seq, command_id, phase, identity, digest, prev)
            db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)", (
                seq, command_id, phase, identity, canonical_json(data), digest, prev, chain))
            db.execute("UPDATE head SET sequence=?,digest=? WHERE singleton=1", (seq, chain))
            result = self._read(db)[-1]
        return result

    def _command(self, command_id):
        command = self.store.get_command(command_id)
        intent = self.store.get_intent(command.intent_hash) if command else None
        if (command is None or intent is None or command.environment != Environment.DEMO
                or intent.environment != Environment.DEMO or command.broker != "etoro"):
            raise ExecutionJournalError("journal_persisted_demo_intent_required")
        return command, intent

    def record_intent(self, command_id, *, mandate_hash, evidence_refs):
        command, intent = self._command(command_id)
        _digest(mandate_hash, "mandate")
        refs = _clean(evidence_refs)
        if not isinstance(refs, dict) or not refs:
            raise ExecutionJournalError("journal_evidence_references_required")
        for digest in refs.values():
            _digest(digest, "evidence")
        return self._append(command_id, "intent", "intent", {"intent": intent,
            "intent_hash": intent.intent_hash, "mandate_hash": mandate_hash, "evidence_refs": refs,
            "logical_request_id": str(command.logical_request_id), "broker_payload_hash": command.payload_hash})

    def _economic(self, command_id):
        # Read only. The Fill Agent remains the sole economic writer.
        with closing(sqlite3.connect(Path(self.ledger.path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            state = self.ledger._state(db)
            claim = next((s for s in state["submissions"] if s["command_id"] == str(command_id)), None)
            if claim is None:
                raise ExecutionJournalError("journal_economic_dispatch_claim_required")
            claim = {k: v for k, v in claim.items() if k not in {"reconciled", "journal_complete"}}
            outcomes = [(seq, self.ledger._decode((raw, digest))) for seq, raw, digest in db.execute(
                "SELECT sequence,payload,digest FROM events WHERE kind='outcome' AND command_id=? ORDER BY sequence",
                (str(command_id),))]
            return _clean(claim), outcomes

    def record_dispatch(self, command_id):
        command, intent = self._command(command_id)
        claim, _ = self._economic(command_id)
        if claim.get("intent_hash") != intent.intent_hash:
            raise ExecutionJournalError("journal_dispatch_intent_mismatch")
        recorded = next((r for r in self.events(command_id) if r["phase"] == "intent"), None)
        if (recorded is None or claim.get("mandate_hash") is not None
                and claim["mandate_hash"] != recorded["payload"]["mandate_hash"]):
            raise ExecutionJournalError("journal_dispatch_mandate_mismatch")
        return self._append(command_id, "dispatch", "dispatch", {"claim": claim,
            "claim_hash": payload_hash(claim), "intent_hash": intent.intent_hash,
            "status": "durably_claimed_not_proof_of_broker_receipt"})

    def record_outcome(self, command_id):
        command, _ = self._command(command_id)
        _, outcomes = self._economic(command_id)
        existing = self.terminal(command_id)
        if existing:
            return tuple(r for r in self.events(command_id) if r["phase"] == "outcome")
        records = [self._append(command_id, "outcome", f"economic:{seq}",
            {"source_kind": "economic_outcome", "source_sequence": seq, "observation": value})
            for seq, value in outcomes]
        if command.broker_order_id is not None:
            data = _clean(command)
            records.append(self._append(command_id, "outcome", "operational:" + payload_hash(data),
                {"source_kind": "persisted_operational_command", "command": data}))
        return tuple(records)

    @staticmethod
    def _account(data, account_id):
        try:
            sources = data["raw_sources"]
            rebuilt = build_pilot_account(sources["profile"], sources["first"], sources["pnl"],
                sources["last"], account_id, datetime.fromisoformat(data["capture_completed_at"]))
            if _clean(rebuilt) != data:
                raise ExecutionJournalError("journal_account_projection_mismatch")
            return rebuilt
        except ExecutionJournalError:
            raise
        except (KeyError, ValueError, TypeError) as exc:
            raise ExecutionJournalError("journal_original_account_sources_required") from exc

    def _completion_payload(self, command_id, records):
        command, intent = self._command(command_id)
        if command.state != CommandState.RECONCILED or not command.broker_order_id:
            raise ExecutionJournalError("journal_operational_reconciliation_required")
        try:
            receipt = _clean(self.ledger.reconciliation_receipt(str(command_id)))
        except (ValueError, AttributeError) as exc:
            raise ExecutionJournalError("journal_economic_reconciliation_required") from exc
        claim, reconciliation = receipt["claim"], receipt["reconciliation"]
        if (receipt["inception_hash"] != self.binding["inception_hash"]
                or receipt["reconciliation_hash"] != payload_hash(reconciliation)
                or claim["command_id"] != str(command_id) or claim.get("intent_hash") != intent.intent_hash):
            raise ExecutionJournalError("journal_reconciliation_source_mismatch")
        intent_record = next((r for r in records if r["command_id"] == str(command_id) and r["phase"] == "intent"), None)
        if intent_record is None or intent_record["payload"]["intent_hash"] != intent.intent_hash:
            raise ExecutionJournalError("journal_matching_intent_required")
        if claim.get("mandate_hash") is not None and claim["mandate_hash"] != intent_record["payload"]["mandate_hash"]:
            raise ExecutionJournalError("journal_dispatch_mandate_mismatch")
        pre, post = reconciliation["pre_account"], reconciliation["post_account"]
        self._account(pre, self.binding["account_id"])
        self._account(post, self.binding["account_id"])
        if payload_hash(pre) != payload_hash(claim["pre_account"]):
            raise ExecutionJournalError("journal_claimed_pre_account_mismatch")
        fills = reconciliation["executions"]
        no_fill = reconciliation.get("terminal_no_fill", False)
        partial = reconciliation.get("terminal_partial", False)
        if no_fill or partial:
            terminal = reconciliation.get("terminal_evidence", {})
            details = dict(terminal.get("details", []))
            allowed_states = {"partially_filled_terminal"} if partial else {"cancelled", "rejected"}
            allowed_statuses = {"9", "10"} if partial else {"4", "7", "8"}
            if (no_fill and partial or terminal.get("command_id") != str(command_id)
                    or terminal.get("broker_order_id") != command.broker_order_id
                    or terminal.get("reconciled") is not True
                    or terminal.get("lifecycle_state") not in allowed_states
                    or details.get("status_id") not in allowed_statuses
                    or len(details) != len(terminal.get("details", []))
                    or details.get("remainder_terminal") != "true"
                    or not details.get("connector_broker_reference_id")
                    or not _SHA256.fullmatch(details.get("lookup_sha256", ""))
                    or terminal.get("executions") != fills or bool(fills) == no_fill):
                raise ExecutionJournalError("journal_authoritative_terminal_evidence_required")
        if (not no_fill and not fills or fills and {e["broker_order_id"] for e in fills} != {command.broker_order_id}
                or any(e["environment"] != "demo" or e["currency"] != "USD"
                    or e["broker_instrument_id"] != claim["instrument_id"] for e in fills)):
            raise ExecutionJournalError("journal_matching_order_executions_required")
        principal = _exact_sum(Decimal(e["executed_amount"]) for e in fills)
        costs = _exact_sum(_exact_sum((Decimal(e["fees"]), Decimal(e["taxes"]))) for e in fills)
        if ((not no_fill and not 0 < principal <= Decimal(claim["amount"]))
                or (no_fill and (principal != 0 or costs != 0))
                or _difference(Decimal(pre["cash"]), principal, costs) != Decimal(post["cash"])):
            raise ExecutionJournalError("journal_actual_cash_or_principal_mismatch")
        before = {p["position_id"]: p for p in pre["positions"]}
        after = {p["position_id"]: p for p in post["positions"]}
        added = {e["broker_position_id"] for e in fills}
        stable = ("instrument_id", "units", "principal", "fees", "taxes", "carrying_fees",
                  "is_buy", "leverage", "settlement_type")
        if (set(before) & added or set(after) != set(before) | added
                or any(any(p[k] != after[key][k] for k in stable) for key, p in before.items())):
            raise ExecutionJournalError("journal_independent_positions_mismatch")
        for key in added:
            group, position = [e for e in fills if e["broker_position_id"] == key], after[key]
            if (not position["is_buy"] or Decimal(position["leverage"]) != 1
                    or position["settlement_type"] != 1 or Decimal(position["carrying_fees"]) != 0
                    or position["instrument_id"] != claim["instrument_id"]
                    or any(e["side"] != "long" or e["action"] != "buy" for e in group)
                    or any(Decimal(position[field]) != _exact_sum(Decimal(e[source]) for e in group)
                           for field, source in (("units", "units"), ("principal", "executed_amount"),
                                                 ("fees", "fees"), ("taxes", "taxes")))):
                raise ExecutionJournalError("journal_independent_fill_position_mismatch")
        # Partial finality is supplied only by the Fill Agent's committed,
        # source-validated terminal evidence, never inferred from cash alone.
        if not no_fill and principal < Decimal(claim["amount"]) and not partial:
            raise ExecutionJournalError("journal_partial_terminal_evidence_required")
        if partial and not principal < Decimal(claim["amount"]):
            raise ExecutionJournalError("journal_partial_principal_outside_claim")
        return _clean({"intent_hash": intent.intent_hash, "mandate_hash": intent_record["payload"]["mandate_hash"],
            "economic_receipt": receipt, "operational_command": command,
            "pre_source_hash": pre["source_hash"], "post_source_hash": post["source_hash"],
            "broker_order_id": command.broker_order_id, "actual_principal": principal, "actual_costs": costs,
            "outcome": terminal["lifecycle_state"] if no_fill else "partially_filled" if partial else "filled",
            "qualification_credit": False})

    def record_reconciliation(self, command_id):
        with closing(self._connect()) as db:
            records = self._read(db)
        data = self._completion_payload(command_id, records)
        result = self._append(command_id, "reconciliation", "reconciliation", data)
        # A successful return includes complete independent readback.
        return self.terminal(command_id)

    def reconstruct(self, command_id, *, mandate_hash, evidence_refs):
        """Replay only retained evidence; pending/unknown outcomes stay incomplete."""
        self.record_intent(command_id, mandate_hash=mandate_hash, evidence_refs=evidence_refs)
        self.record_dispatch(command_id)
        self.record_outcome(command_id)
        command, _ = self._command(command_id)
        if command.state == CommandState.RECONCILED:
            return self.record_reconciliation(command_id)
        return None

    def _external_payload(self, event_id):
        receipt = self.ledger.external_lifecycle_receipt(event_id)
        return _clean({"origin": "external", "event_id": event_id,
            "economic_record_hash": receipt["record_hash"], "economic_record": receipt["record"],
            "qualification_credit": False})

    def external_terminal(self, event_id):
        _digest(event_id, "external_event")
        return next((r for r in self.events("external:" + event_id) if r["phase"] == "external_import"), None)

    def record_external_import(self, event_id):
        """Journal a committed external origin, without intent/dispatch fiction."""
        _digest(event_id, "external_event")
        self._append("external:" + event_id, "external_import", "external_import", self._external_payload(event_id))
        return self.external_terminal(event_id)
