"""Prospective Demo risk arithmetic with durable, explicitly unqualified evidence.

This ledger does not verify broker transaction history or authorize orders. A
caller's declaration of complete cash-flow coverage remains a declaration.
Nonzero external flows disable drawdown arithmetic because snapshots alone do
not supply valuations immediately before each flow. Nothing rebases the ledger.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from brokers.base import BrokerAccountSnapshot, BrokerPosition
from execution.domain import Environment, canonical_json, decimal_value, payload_hash


class RiskBaselineError(ValueError):
    """Missing, conflicting or unsupported evidence; never a trading permission."""


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RiskBaselineError("timezone_aware_timestamp_required")
    return value.astimezone(timezone.utc)


def _text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RiskBaselineError("nonempty_evidence_identity_required")
    return value


def _number(value: Decimal) -> Decimal:
    if isinstance(value, bool):
        raise RiskBaselineError("finite_decimal_required")
    return decimal_value(value, "risk_evidence_number")


@dataclass(frozen=True, slots=True)
class ExternalCashFlow:
    flow_id: str
    occurred_at: datetime
    amount: Decimal
    currency: str
    kind: str  # deposit or withdrawal; fees/dividends/financing are performance.

    def __post_init__(self):
        _text(self.flow_id)
        object.__setattr__(self, "occurred_at", _time(self.occurred_at))
        object.__setattr__(self, "amount", _number(self.amount))
        if self.kind not in {"deposit", "withdrawal"} or self.amount == 0:
            raise RiskBaselineError("external_flow_kind_or_amount_invalid")
        if (self.kind == "deposit") != (self.amount > 0):
            raise RiskBaselineError("external_flow_sign_invalid")
        _text(self.currency)


@dataclass(frozen=True, slots=True)
class FlowInterval:
    start_at: datetime
    end_at: datetime
    start_cursor: str
    end_cursor: str
    source_reference: str
    complete: bool
    external_flows: tuple[ExternalCashFlow, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "start_at", _time(self.start_at))
        object.__setattr__(self, "end_at", _time(self.end_at))
        for value in (self.start_cursor, self.end_cursor, self.source_reference):
            _text(value)
        if self.end_at <= self.start_at or type(self.complete) is not bool:
            raise RiskBaselineError("flow_interval_invalid")
        if not isinstance(self.external_flows, tuple) or not all(
            isinstance(flow, ExternalCashFlow) for flow in self.external_flows
        ):
            raise RiskBaselineError("typed_external_flows_required")
        if len({flow.flow_id for flow in self.external_flows}) != len(self.external_flows):
            raise RiskBaselineError("duplicate_external_flow")
        if any(not self.start_at < flow.occurred_at <= self.end_at for flow in self.external_flows):
            raise RiskBaselineError("flow_outside_covered_interval")


@dataclass(frozen=True, slots=True)
class RiskBaselineEvidence:
    baseline_at: datetime
    observed_at: datetime
    snapshot_id: str
    record_hash: str
    observation_count: int
    risk_day: str
    risk_timezone: str
    inception_pnl: Decimal
    daily_pnl: Decimal | None
    daily_baseline_at: datetime | None
    equity_index: Decimal | None
    peak_equity_index: Decimal | None
    drawdown_fraction: Decimal | None
    flow_coverage_declared_complete: bool
    broker_history_verified: bool = False
    qualification_credit: bool = False
    submission_available: bool = False
    limitations: tuple[str, ...] = ()


class RiskBaselineStore:
    """Append-only local observations pinned to one account and risk-day zone.

    Existing records can be replayed exactly, including after restart; an old
    record's result retains its original observed time and is not fresh evidence.
    Record hashes detect inconsistent local evidence, not a malicious database
    owner capable of rewriting the entire database and its hashes.
    """

    def __init__(self, path: Path | str, *, account_id: str, currency: str = "USD",
                 risk_timezone: str = "Europe/London"):
        self.path = Path(path)
        self.account_id = _text(account_id)
        if currency != "USD" or risk_timezone != "Europe/London":
            raise RiskBaselineError("unsupported_ledger_currency_or_timezone")
        self.currency = currency
        self.risk_timezone = risk_timezone
        self.zone = ZoneInfo(risk_timezone)
        self.identity = {
            "schema_version": 1, "environment": "demo", "account_id": account_id,
            "currency": currency, "risk_timezone": risk_timezone,
        }
        self.identity_hash = payload_hash(self.identity)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS risk_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS risk_observations (
                    sequence INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, previous_hash TEXT NOT NULL, record_hash TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS risk_observations_no_update BEFORE UPDATE ON risk_observations
                    BEGIN SELECT RAISE(ABORT, 'append-only risk evidence'); END;
                CREATE TRIGGER IF NOT EXISTS risk_observations_no_delete BEFORE DELETE ON risk_observations
                    BEGIN SELECT RAISE(ABORT, 'append-only risk evidence'); END;
                CREATE TRIGGER IF NOT EXISTS risk_identity_no_update BEFORE UPDATE ON risk_identity
                    BEGIN SELECT RAISE(ABORT, 'immutable risk identity'); END;
                CREATE TRIGGER IF NOT EXISTS risk_identity_no_delete BEFORE DELETE ON risk_identity
                    BEGIN SELECT RAISE(ABORT, 'immutable risk identity'); END;
            """)
            db.execute("INSERT OR IGNORE INTO risk_identity VALUES (1, ?)", (canonical_json(self.identity),))
            self._read(db)
        self.path.chmod(0o600)

    def _read(self, db):
        identity = db.execute("SELECT payload FROM risk_identity WHERE singleton=1").fetchone()
        if identity != (canonical_json(self.identity),):
            raise RiskBaselineError("ledger_identity_mismatch")
        rows = db.execute("SELECT sequence,snapshot_id,payload,previous_hash,record_hash "
                          "FROM risk_observations ORDER BY sequence").fetchall()
        previous = self.identity_hash
        parsed = []
        for index, (sequence, snapshot_id, encoded, predecessor, record_hash) in enumerate(rows, 1):
            try:
                body = json.loads(encoded)
                valid = (sequence == index and predecessor == previous
                         and body["snapshot"]["snapshot_id"] == snapshot_id
                         and payload_hash({"previous_hash": previous, "payload": body}) == record_hash)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise RiskBaselineError("risk_ledger_integrity_failure")
            parsed.append((body, record_hash))
            previous = record_hash
        return parsed

    def _validate_snapshot(self, snapshot, account_id):
        if not isinstance(snapshot, BrokerAccountSnapshot):
            raise RiskBaselineError("typed_broker_snapshot_required")
        if account_id != self.account_id or snapshot.environment != Environment.DEMO:
            raise RiskBaselineError("demo_account_identity_mismatch")
        if snapshot.currency != self.currency:
            raise RiskBaselineError("account_currency_mismatch")
        _text(snapshot.snapshot_id)
        _time(snapshot.observed_at)
        for name in ("equity", "available_cash", "balance", "frozen_cash", "current_pnl", "used_margin"):
            if not isinstance(getattr(snapshot, name), Decimal):
                raise RiskBaselineError("decimal_account_values_required")
            _number(getattr(snapshot, name))
        if snapshot.equity <= 0 or snapshot.used_margin < 0 or snapshot.frozen_cash < 0:
            raise RiskBaselineError("invalid_account_equity_or_margin")
        if abs(snapshot.equity - snapshot.available_cash - snapshot.used_margin - snapshot.current_pnl) > Decimal("0.02"):
            raise RiskBaselineError("account_equity_not_reconciled")
        if (snapshot.pending_orders_complete is not True or snapshot.pending_orders
                or not isinstance(snapshot.pending_orders, tuple)):
            raise RiskBaselineError("complete_empty_pending_orders_required")
        if not isinstance(snapshot.positions, tuple):
            raise RiskBaselineError("typed_positions_required")
        for position in snapshot.positions:
            if not isinstance(position, BrokerPosition):
                raise RiskBaselineError("typed_positions_required")
            if position.currency != self.currency:
                raise RiskBaselineError("position_account_currency_mismatch")
            for name in ("units", "current_exposure", "average_leverage", "pnl"):
                if not isinstance(getattr(position, name), Decimal):
                    raise RiskBaselineError("decimal_position_values_required")
                _number(getattr(position, name))

    def record(self, snapshot: BrokerAccountSnapshot, *, account_id: str,
               flow_interval: FlowInterval | None = None,
               captured_at: datetime | None = None) -> RiskBaselineEvidence:
        self._validate_snapshot(snapshot, account_id)
        if flow_interval is not None and not isinstance(flow_interval, FlowInterval):
            raise RiskBaselineError("typed_flow_interval_required")
        body = json.loads(canonical_json({"snapshot": snapshot, "flow_interval": flow_interval}))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = self._read(db)
            for index, (existing, _) in enumerate(rows):
                if existing["snapshot"]["snapshot_id"] == snapshot.snapshot_id:
                    if existing != body:
                        raise RiskBaselineError("snapshot_replay_conflict")
                    return self._evidence(rows[:index + 1])
            now = _time(captured_at or datetime.now(timezone.utc))
            age = (now - _time(snapshot.observed_at)).total_seconds()
            if age < 0 or age > 15:
                raise RiskBaselineError("snapshot_stale_or_future")
            if not rows:
                if flow_interval is not None:
                    raise RiskBaselineError("baseline_cannot_claim_preinception_history")
            else:
                prior = rows[-1][0]
                prior_time = datetime.fromisoformat(prior["snapshot"]["observed_at"])
                if snapshot.observed_at <= prior_time:
                    raise RiskBaselineError("snapshot_not_strictly_increasing")
                if flow_interval is None or flow_interval.complete is not True:
                    raise RiskBaselineError("complete_flow_interval_required")
                if flow_interval.start_at != prior_time or flow_interval.end_at != snapshot.observed_at:
                    raise RiskBaselineError("flow_coverage_gap")
                previous_interval = prior["flow_interval"]
                if previous_interval and previous_interval["end_cursor"] != flow_interval.start_cursor:
                    raise RiskBaselineError("flow_cursor_discontinuity")
                seen = {flow["flow_id"] for row, _ in rows for flow in
                        (row["flow_interval"] or {}).get("external_flows", [])}
                if any(flow.flow_id in seen for flow in flow_interval.external_flows):
                    raise RiskBaselineError("duplicate_external_flow")
                if any(flow.currency != self.currency for flow in flow_interval.external_flows):
                    raise RiskBaselineError("external_flow_currency_mismatch")
            predecessor = rows[-1][1] if rows else self.identity_hash
            record_hash = payload_hash({"previous_hash": predecessor, "payload": body})
            db.execute("INSERT INTO risk_observations VALUES (?,?,?,?,?)",
                       (len(rows) + 1, snapshot.snapshot_id, canonical_json(body), predecessor, record_hash))
            return self._evidence([*rows, (body, record_hash)])

    def latest(self) -> RiskBaselineEvidence | None:
        with closing(sqlite3.connect(self.path)) as db, db:
            rows = self._read(db)
            return self._evidence(rows) if rows else None

    def _evidence(self, rows):
        first = rows[0][0]["snapshot"]
        last = rows[-1][0]["snapshot"]
        start = datetime.fromisoformat(first["observed_at"])
        observed = datetime.fromisoformat(last["observed_at"])
        risk_day = observed.astimezone(self.zone).date()
        boundary = datetime.combine(risk_day, time.min, self.zone).astimezone(timezone.utc)
        all_flows = [flow for row, _ in rows for flow in (row["flow_interval"] or {}).get("external_flows", [])]
        net_flows = sum((Decimal(flow["amount"]) for flow in all_flows), Decimal("0"))
        baseline_equity = Decimal(first["equity"])
        current_equity = Decimal(last["equity"])
        daily_baseline = next((row["snapshot"] for row, _ in rows
                               if datetime.fromisoformat(row["snapshot"]["observed_at"]) == boundary), None)
        daily_pnl = None
        if daily_baseline is not None:
            day_flows = sum((Decimal(flow["amount"]) for flow in all_flows
                             if datetime.fromisoformat(flow["occurred_at"]) > boundary), Decimal("0"))
            daily_pnl = current_equity - Decimal(daily_baseline["equity"]) - day_flows
        limitations = ["broker_transaction_history_not_verified", "local_evidence_is_not_trading_authority",
                       "peak_is_since_inception_and_only_at_observation_times"]
        index = peak = drawdown = None
        if all_flows:
            limitations.append("external_flows_require_preflow_valuations_for_drawdown")
        else:
            index = current_equity / baseline_equity
            peak = max(Decimal(row["snapshot"]["equity"]) / baseline_equity for row, _ in rows)
            drawdown = Decimal("1") - index / peak
        if daily_baseline is None:
            limitations.append("exact_risk_day_boundary_snapshot_missing")
        return RiskBaselineEvidence(
            baseline_at=start, observed_at=observed, snapshot_id=last["snapshot_id"],
            record_hash=rows[-1][1], observation_count=len(rows), risk_day=risk_day.isoformat(),
            risk_timezone=self.risk_timezone,
            inception_pnl=current_equity - baseline_equity - net_flows,
            daily_pnl=daily_pnl, daily_baseline_at=boundary if daily_baseline else None,
            equity_index=index, peak_equity_index=peak, drawdown_fraction=drawdown,
            flow_coverage_declared_complete=len(rows) > 1,
            limitations=tuple(limitations),
        )
