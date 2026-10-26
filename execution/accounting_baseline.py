"""Durable immutable inception for the versioned account-performance contract."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from execution.accounting import (
    AccountingEvidenceError, AccountValuation, HistoryCoverage, PerformanceEvidence,
    _at, _history_payload, _sum, evaluate_performance,
)
from execution.domain import canonical_json, payload_hash


class AccountPerformanceStore:
    """Append-only reconciled valuations; a caller cannot replace a loss baseline.

    History is complete from immutable inception on every new observation. Its
    prior events must remain identical. The ledger supplies every prior sampled
    valuation to performance, and does not infer complete historical valuation
    coverage from the mere existence of this local ledger.
    """

    def __init__(self, path: Path | str, *, account_id: str):
        if not isinstance(account_id, str) or not account_id.strip():
            raise AccountingEvidenceError("account_identity_required")
        self.path = Path(path)
        self.account_id = account_id
        self.identity = {"schema": "account-performance-store-v1", "account_id": account_id,
                         "currency": "USD", "risk_timezone": "Europe/London", "environment": "demo"}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS performance_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS performance_records (
                    sequence INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, previous_hash TEXT NOT NULL, record_hash TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS performance_no_update BEFORE UPDATE ON performance_records
                    BEGIN SELECT RAISE(ABORT, 'append-only performance evidence'); END;
                CREATE TRIGGER IF NOT EXISTS performance_no_delete BEFORE DELETE ON performance_records
                    BEGIN SELECT RAISE(ABORT, 'append-only performance evidence'); END;
                CREATE TRIGGER IF NOT EXISTS performance_identity_no_update BEFORE UPDATE ON performance_identity
                    BEGIN SELECT RAISE(ABORT, 'immutable performance identity'); END;
                CREATE TRIGGER IF NOT EXISTS performance_identity_no_delete BEFORE DELETE ON performance_identity
                    BEGIN SELECT RAISE(ABORT, 'immutable performance identity'); END;
            """)
            db.execute("INSERT OR IGNORE INTO performance_identity VALUES (1,?)", (canonical_json(self.identity),))
            self._read(db)
        self.path.chmod(0o600)

    def _read(self, db):
        if db.execute("SELECT payload FROM performance_identity WHERE singleton=1").fetchone() != (canonical_json(self.identity),):
            raise AccountingEvidenceError("performance_ledger_identity_mismatch")
        rows = []
        previous = payload_hash(self.identity)
        for index, row in enumerate(db.execute("SELECT sequence,snapshot_id,payload,previous_hash,record_hash FROM performance_records ORDER BY sequence"), 1):
            sequence, snapshot_id, encoded, predecessor, digest = row
            try:
                body = json.loads(encoded)
                valid = (sequence == index and predecessor == previous
                         and body["valuation"]["snapshot_id"] == snapshot_id
                         and payload_hash({"previous_hash": previous, "payload": body}) == digest)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise AccountingEvidenceError("performance_ledger_integrity_failure")
            rows.append((body, digest))
            previous = digest
        return rows

    def record(self, valuation: AccountValuation, *, now: datetime,
               history: HistoryCoverage | None = None,
               component_evidence=None) -> PerformanceEvidence:
        if not isinstance(valuation, AccountValuation) or valuation.account_id != self.account_id:
            raise AccountingEvidenceError("performance_account_identity_mismatch")
        now = _at(now)
        request = json.loads(canonical_json({"valuation": valuation, "history": _history_payload(history)}))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._read(db)
            for existing, _ in rows:
                if existing["valuation"]["snapshot_id"] == valuation.snapshot_id:
                    if {key: existing[key] for key in request} != request:
                        raise AccountingEvidenceError("performance_snapshot_replay_conflict")
                    # Replay returns the original dated result; no new record or
                    # fresh qualification is created by this read operation.
                    return _performance(existing["performance"])
            if valuation.valuation_at is None:
                from brokers.etoro.account_evidence import AccountComponentTimeEvidence
                if not isinstance(component_evidence, AccountComponentTimeEvidence):
                    raise AccountingEvidenceError("qualified_component_evidence_required_for_baseline")
                if (component_evidence.validate_at(now)
                        or component_evidence.evidence_hash != valuation.time_evidence_hash
                        or component_evidence.model_version != valuation.time_model
                        or component_evidence.demo_customer_id != valuation.account_id
                        or component_evidence.currency != valuation.currency
                        or component_evidence.source_evidence_sha256 != valuation.snapshot_id
                        or component_evidence.earliest_component_at != valuation.component_start_at
                        or component_evidence.latest_component_at != valuation.component_end_at
                        or component_evidence.available_cash != valuation.cash
                        or component_evidence.invested_amount != valuation.invested_amount
                        or component_evidence.current_pnl != valuation.unrealised_pnl
                        or component_evidence.broker_margin != valuation.broker_margin
                        or component_evidence.equity != valuation.equity
                        or _sum(item.current_exposure.copy_abs() for item in component_evidence.positions) != valuation.gross_exposure):
                    raise AccountingEvidenceError("component_baseline_evidence_unqualified_or_unbound")
            ages = ((now - valuation.freshness_at).total_seconds(),
                    (now - valuation.coverage_at).total_seconds())
            if min(ages) < 0 or max(ages) > 15:
                raise AccountingEvidenceError("performance_valuation_stale_or_future")
            if not rows:
                if history is not None:
                    raise AccountingEvidenceError("new_baseline_cannot_invent_preinception_history")
                opening = valuation
                prior = ()
            else:
                opening = _valuation(rows[0][0]["valuation"])
                prior = tuple(_valuation(row["valuation"]) for row, _ in rows[1:])
                previous = _valuation(rows[-1][0]["valuation"])
                if valuation.coverage_at <= previous.coverage_at:
                    raise AccountingEvidenceError("performance_valuation_not_strictly_increasing")
                if history is None:
                    raise AccountingEvidenceError("complete_verified_history_required")
                earlier = rows[-1][0]["history"]
                if earlier is not None:
                    older_events = {item["event_id"]: item for item in earlier["events"]}
                    repeated_events = {item["event_id"]: item for item in request["history"]["events"]
                                       if datetime.fromisoformat(item["occurred_at"]) <= previous.coverage_at}
                    if (older_events != repeated_events or earlier["start_cursor"] != history.start_cursor
                            or earlier["source_contract"] != history.source_contract):
                        raise AccountingEvidenceError("historical_accounting_evidence_changed")
            evidence = evaluate_performance(opening=opening, current=valuation, history=history,
                                            observations=prior, now=now)
            if rows and not evidence.history_complete:
                raise AccountingEvidenceError("complete_verified_history_required")
            body = {**request, "performance": json.loads(canonical_json(evidence))}
            predecessor = rows[-1][1] if rows else payload_hash(self.identity)
            digest = payload_hash({"previous_hash": predecessor, "payload": body})
            db.execute("INSERT INTO performance_records VALUES (?,?,?,?,?)",
                       (len(rows) + 1, valuation.snapshot_id, canonical_json(body), predecessor, digest))
            return evidence

    def latest(self) -> PerformanceEvidence | None:
        with closing(sqlite3.connect(self.path)) as db:
            rows = self._read(db)
        return _performance(rows[-1][0]["performance"]) if rows else None


def _valuation(body):
    values = dict(body)
    for key in ("valuation_at", "component_start_at", "component_end_at"):
        values[key] = datetime.fromisoformat(values[key]) if values[key] is not None else None
    for key in ("cash", "invested_amount", "unrealised_pnl", "equity", "broker_margin", "gross_exposure"):
        values[key] = Decimal(values[key])
    return AccountValuation(**values)


def _performance(body):
    values = dict(body)
    for key in ("coverage_start_at", "coverage_end_at"):
        values[key] = datetime.fromisoformat(values[key]) if values[key] is not None else None
    for key in ("daily_pnl", "daily_loss_denominator", "daily_loss_fraction", "equity_index", "peak_equity_index", "drawdown_fraction"):
        values[key] = Decimal(values[key]) if values[key] is not None else None
    values["reasons"] = tuple(values["reasons"])
    values["current_valuation"] = _valuation(values["current_valuation"]) if values.get("current_valuation") else None
    return PerformanceEvidence(**values)
