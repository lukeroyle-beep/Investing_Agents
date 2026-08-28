from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import UUID

from execution.domain import (
    Approval,
    BrokerCommand,
    BrokerOperation,
    CommandAttempt,
    CommandState,
    DomainValidationError,
    Environment,
    InvalidLifecycleTransition,
    OrderIntent,
    OrderSide,
    OrderType,
    RiskCheck,
    RiskDecision,
    RiskOutcome,
    SizingMethod,
    dataclass_json,
    _aware_utc,
)
from execution.instruments import (
    BrokerInstrumentMapping,
    Instrument,
    InstrumentCollisionError,
    InstrumentNotFoundError,
)


class ExecutionStoreError(RuntimeError):
    """Base error for durable operational-state failures."""


class DuplicateIntentError(ExecutionStoreError):
    """Raised when an equivalent immutable intent already exists."""


class DuplicateLogicalRequestError(ExecutionStoreError):
    """Raised when a logical broker request is reused."""


class ApprovalReplayError(ExecutionStoreError):
    """Raised when an approval is consumed more than once."""


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class ExecutionStore:
    """SQLite-backed operational ledger; it has no economic-state tables."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS instruments (
                    internal_instrument_id TEXT PRIMARY KEY,
                    canonical_symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    UNIQUE(canonical_symbol, exchange)
                );
                CREATE TABLE IF NOT EXISTS instrument_symbol_history (
                    internal_instrument_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    FOREIGN KEY(internal_instrument_id)
                        REFERENCES instruments(internal_instrument_id),
                    UNIQUE(internal_instrument_id, symbol, effective_at)
                );
                CREATE TABLE IF NOT EXISTS broker_instrument_mappings (
                    internal_instrument_id TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    broker_instrument_id INTEGER NOT NULL,
                    exact_match_symbol TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    metadata_checksum TEXT NOT NULL,
                    PRIMARY KEY(internal_instrument_id, broker, environment),
                    UNIQUE(broker, environment, broker_instrument_id),
                    FOREIGN KEY(internal_instrument_id)
                        REFERENCES instruments(internal_instrument_id)
                );
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    intent_hash TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    persisted_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS risk_decisions (
                    decision_id TEXT PRIMARY KEY,
                    intent_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    persisted_at TEXT NOT NULL,
                    FOREIGN KEY(intent_hash) REFERENCES order_intents(intent_hash)
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    intent_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    persisted_at TEXT NOT NULL,
                    consumed_at TEXT,
                    FOREIGN KEY(intent_hash) REFERENCES order_intents(intent_hash)
                );
                CREATE TABLE IF NOT EXISTS broker_commands (
                    command_id TEXT PRIMARY KEY,
                    logical_request_id TEXT NOT NULL UNIQUE,
                    intent_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    persisted_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(intent_hash) REFERENCES order_intents(intent_hash)
                );
                CREATE TABLE IF NOT EXISTS command_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    previous_state TEXT,
                    new_state TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    FOREIGN KEY(command_id) REFERENCES broker_commands(command_id),
                    UNIQUE(command_id, revision)
                );
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def register_instrument(self, instrument: Instrument) -> Instrument:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM instruments WHERE internal_instrument_id = ?",
                (str(instrument.internal_instrument_id),),
            ).fetchone()
            if existing:
                stored = self._instrument_from_row(existing)
                if stored != instrument:
                    raise InstrumentCollisionError(
                        "internal instrument UUID is immutable and already has another identity"
                    )
                return stored
            try:
                connection.execute(
                    """
                    INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(instrument.internal_instrument_id),
                        instrument.canonical_symbol,
                        instrument.exchange,
                        instrument.asset_type,
                        instrument.currency,
                        instrument.sector,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO instrument_symbol_history
                    (internal_instrument_id, symbol, effective_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        str(instrument.internal_instrument_id),
                        instrument.canonical_symbol,
                        self._now_iso(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InstrumentCollisionError(
                    "canonical symbol and exchange already identify another instrument"
                ) from exc
        return instrument

    @staticmethod
    def _instrument_from_row(row: sqlite3.Row) -> Instrument:
        return Instrument(
            internal_instrument_id=row["internal_instrument_id"],
            canonical_symbol=row["canonical_symbol"],
            exchange=row["exchange"],
            asset_type=row["asset_type"],
            currency=row["currency"],
            sector=row["sector"],
        )

    def get_instrument(self, internal_instrument_id: UUID | str) -> Instrument | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM instruments WHERE internal_instrument_id = ?",
                (str(internal_instrument_id),),
            ).fetchone()
        return self._instrument_from_row(row) if row else None

    def find_instrument(self, *, symbol: str, exchange: str) -> Instrument | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM instruments
                WHERE canonical_symbol = ? AND exchange = ?
                """,
                (str(symbol).strip().upper(), str(exchange).strip().upper()),
            ).fetchone()
        return self._instrument_from_row(row) if row else None

    def find_instruments_by_symbol(self, symbol: str) -> list[Instrument]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM instruments WHERE canonical_symbol = ? ORDER BY exchange",
                (str(symbol).strip().upper(),),
            ).fetchall()
        return [self._instrument_from_row(row) for row in rows]

    def rename_instrument_symbol(
        self,
        internal_instrument_id: UUID | str,
        *,
        new_symbol: str,
        effective_at: datetime,
    ) -> Instrument:
        instrument = self.get_instrument(internal_instrument_id)
        if instrument is None:
            raise InstrumentNotFoundError(str(internal_instrument_id))
        normalised = str(new_symbol).strip().upper()
        if not normalised:
            raise DomainValidationError("new_symbol must not be blank")
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    UPDATE instruments SET canonical_symbol = ?
                    WHERE internal_instrument_id = ?
                    """,
                    (normalised, str(instrument.internal_instrument_id)),
                )
                connection.execute(
                    """
                    INSERT INTO instrument_symbol_history
                    (internal_instrument_id, symbol, effective_at)
                    VALUES (?, ?, ?)
                    """,
                    (
                        str(instrument.internal_instrument_id),
                        normalised,
                        _aware_utc(effective_at, "effective_at").isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InstrumentCollisionError(
                    "ticker change collides with an existing canonical identity"
                ) from exc
        return Instrument(
            internal_instrument_id=instrument.internal_instrument_id,
            canonical_symbol=normalised,
            exchange=instrument.exchange,
            asset_type=instrument.asset_type,
            currency=instrument.currency,
            sector=instrument.sector,
        )

    def save_broker_mapping(self, mapping: BrokerInstrumentMapping) -> None:
        if self.get_instrument(mapping.internal_instrument_id) is None:
            raise InstrumentNotFoundError(str(mapping.internal_instrument_id))
        with self._transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM broker_instrument_mappings
                WHERE internal_instrument_id = ? AND broker = ? AND environment = ?
                """,
                (
                    str(mapping.internal_instrument_id),
                    mapping.broker,
                    str(mapping.environment),
                ),
            ).fetchone()
            if existing:
                stored = self._mapping_from_row(existing)
                if stored != mapping:
                    raise InstrumentCollisionError(
                        "broker mapping is immutable once persisted"
                    )
                return
            try:
                connection.execute(
                    """
                    INSERT INTO broker_instrument_mappings VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(mapping.internal_instrument_id),
                        mapping.broker,
                        str(mapping.environment),
                        mapping.broker_instrument_id,
                        mapping.exact_match_symbol,
                        mapping.resolved_at.isoformat(),
                        mapping.metadata_checksum,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InstrumentCollisionError(
                    "broker instrument ID already maps to another internal instrument"
                ) from exc

    @staticmethod
    def _mapping_from_row(row: sqlite3.Row) -> BrokerInstrumentMapping:
        return BrokerInstrumentMapping(
            internal_instrument_id=row["internal_instrument_id"],
            broker=row["broker"],
            environment=row["environment"],
            broker_instrument_id=int(row["broker_instrument_id"]),
            exact_match_symbol=row["exact_match_symbol"],
            resolved_at=_parse_datetime(row["resolved_at"]),
            metadata_checksum=row["metadata_checksum"],
        )

    def get_broker_mapping(
        self,
        internal_instrument_id: UUID | str,
        *,
        broker: str,
        environment: Environment | str,
    ) -> BrokerInstrumentMapping | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM broker_instrument_mappings
                WHERE internal_instrument_id = ? AND broker = ? AND environment = ?
                """,
                (
                    str(internal_instrument_id),
                    str(broker).strip().lower(),
                    str(Environment(environment)),
                ),
            ).fetchone()
        return self._mapping_from_row(row) if row else None

    def save_intent(self, intent: OrderIntent) -> None:
        self.save_intents((intent,))

    def save_intents(self, intents: tuple[OrderIntent, ...]) -> None:
        if len({intent.intent_hash for intent in intents}) != len(intents):
            raise DuplicateIntentError("duplicate intent in batch")
        with self._transaction() as connection:
            for intent in intents:
                try:
                    connection.execute(
                        """
                        INSERT INTO order_intents
                        (intent_id, intent_hash, run_id, payload_json, persisted_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            str(intent.intent_id),
                            intent.intent_hash,
                            intent.run_id,
                            dataclass_json(intent),
                            self._now_iso(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise DuplicateIntentError(intent.intent_hash) from exc

    @staticmethod
    def _intent_from_json(payload_json: str) -> OrderIntent:
        value = json.loads(payload_json)
        return OrderIntent(
            intent_id=value["intent_id"],
            strategy_id=value["strategy_id"],
            run_id=value["run_id"],
            internal_instrument_id=value["internal_instrument_id"],
            environment=value["environment"],
            side=value["side"],
            order_type=value["order_type"],
            sizing_method=value["sizing_method"],
            sizing_value=value["sizing_value"],
            currency=value["currency"],
            expires_at=_parse_datetime(value["expires_at"]),
            intent_hash=value["intent_hash"],
            target_leverage=value["target_leverage"],
            limit_price=value["limit_price"],
            schema_version=value["schema_version"],
        )

    def get_intent(self, intent_hash: str) -> OrderIntent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM order_intents WHERE intent_hash = ?",
                (intent_hash,),
            ).fetchone()
        return self._intent_from_json(row["payload_json"]) if row else None

    def save_risk_decision(self, decision: RiskDecision) -> None:
        if self.get_intent(decision.intent_hash) is None:
            raise ExecutionStoreError("risk decision references an unknown intent")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO risk_decisions
                (decision_id, intent_hash, payload_json, persisted_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(decision.decision_id),
                    decision.intent_hash,
                    dataclass_json(decision),
                    self._now_iso(),
                ),
            )

    def latest_risk_decision(self, intent_hash: str) -> RiskDecision | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM risk_decisions
                WHERE intent_hash = ? ORDER BY persisted_at DESC LIMIT 1
                """,
                (intent_hash,),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row["payload_json"])
        return RiskDecision(
            decision_id=value["decision_id"],
            intent_hash=value["intent_hash"],
            account_snapshot_id=value["account_snapshot_id"],
            quote_id=value["quote_id"],
            quote_observed_at=_parse_datetime(value["quote_observed_at"]),
            computed_exposures=tuple(map(tuple, value["computed_exposures"])),
            checks=tuple(RiskCheck(**check) for check in value["checks"]),
            outcome=RiskOutcome(value["outcome"]),
            reasons=tuple(value["reasons"]),
            decided_at=_parse_datetime(value["decided_at"]),
            schema_version=value["schema_version"],
        )

    def save_approval(self, approval: Approval) -> None:
        if self.get_intent(approval.intent_hash) is None:
            raise ExecutionStoreError("approval references an unknown intent")
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO approvals
                (approval_id, intent_hash, payload_json, persisted_at, consumed_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (
                    str(approval.approval_id),
                    approval.intent_hash,
                    dataclass_json(approval),
                    self._now_iso(),
                ),
            )

    def consume_approval(self, approval_id: UUID | str, *, consumed_at: datetime) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE approvals SET consumed_at = ?
                WHERE approval_id = ? AND consumed_at IS NULL
                """,
                (_aware_utc(consumed_at, "consumed_at").isoformat(), str(approval_id)),
            )
            if cursor.rowcount != 1:
                raise ApprovalReplayError(str(approval_id))

    def save_command(self, command: BrokerCommand) -> None:
        if self.get_intent(command.intent_hash) is None:
            raise ExecutionStoreError("command references an unknown intent")
        now = self._now_iso()
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO broker_commands
                    (command_id, logical_request_id, intent_hash, state, revision,
                     payload_json, persisted_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        str(command.command_id),
                        str(command.logical_request_id),
                        command.intent_hash,
                        str(command.state),
                        dataclass_json(command),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO command_events
                    (command_id, revision, previous_state, new_state, event_time,
                     details_json)
                    VALUES (?, 0, NULL, ?, ?, ?)
                    """,
                    (str(command.command_id), str(command.state), now, "{}"),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateLogicalRequestError(str(command.logical_request_id)) from exc

    @staticmethod
    def _command_from_json(payload_json: str) -> BrokerCommand:
        value = json.loads(payload_json)
        return BrokerCommand(
            command_id=value["command_id"],
            logical_request_id=value["logical_request_id"],
            intent_hash=value["intent_hash"],
            operation=BrokerOperation(value["operation"]),
            broker=value["broker"],
            environment=Environment(value["environment"]),
            payload_hash=value["payload_hash"],
            state=CommandState(value["state"]),
            attempt_history=tuple(
                CommandAttempt(
                    attempt_number=item["attempt_number"],
                    request_id=item["request_id"],
                    attempted_at=_parse_datetime(item["attempted_at"]),
                    outcome=item["outcome"],
                )
                for item in value["attempt_history"]
            ),
            broker_order_id=value["broker_order_id"],
            broker_reference_id=value["broker_reference_id"],
            broker_position_id=value["broker_position_id"],
            schema_version=value["schema_version"],
        )

    def get_command(self, command_id: UUID | str) -> BrokerCommand | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json FROM broker_commands WHERE command_id = ?",
                (str(command_id),),
            ).fetchone()
        return self._command_from_json(row["payload_json"]) if row else None

    def list_commands(
        self, states: tuple[CommandState | str, ...] | None = None
    ) -> tuple[BrokerCommand, ...]:
        query = "SELECT payload_json FROM broker_commands"
        parameters: tuple[str, ...] = ()
        if states:
            values = tuple(str(CommandState(item)) for item in states)
            placeholders = ",".join("?" for _ in values)
            query += f" WHERE state IN ({placeholders})"
            parameters = values
        query += " ORDER BY persisted_at, command_id"
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._command_from_json(row["payload_json"]) for row in rows)

    def transition_command(
        self,
        command_id: UUID | str,
        new_state: CommandState | str,
        *,
        details: dict[str, str] | None = None,
        broker_order_id: str | None = None,
        broker_reference_id: str | None = None,
        broker_position_id: str | None = None,
    ) -> BrokerCommand:
        now = self._now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT payload_json, revision FROM broker_commands
                WHERE command_id = ?
                """,
                (str(command_id),),
            ).fetchone()
            if not row:
                raise ExecutionStoreError(f"unknown command: {command_id}")
            command = self._command_from_json(row["payload_json"])
            updated = command.transition(
                new_state,
                broker_order_id=broker_order_id,
                broker_reference_id=broker_reference_id,
                broker_position_id=broker_position_id,
            )
            revision = int(row["revision"]) + 1
            cursor = connection.execute(
                """
                UPDATE broker_commands
                SET state = ?, revision = ?, payload_json = ?, updated_at = ?
                WHERE command_id = ? AND revision = ?
                """,
                (
                    str(updated.state),
                    revision,
                    dataclass_json(updated),
                    now,
                    str(command_id),
                    revision - 1,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionStoreError("concurrent command transition rejected")
            cursor = connection.execute(
                """
                INSERT INTO command_events
                (command_id, revision, previous_state, new_state, event_time,
                 details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(command_id),
                    revision,
                    str(command.state),
                    str(updated.state),
                    now,
                    json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionStoreError("command event append rejected")
        return updated

    def record_attempt(
        self,
        command_id: UUID | str,
        attempt: CommandAttempt,
    ) -> BrokerCommand:
        now = self._now_iso()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT payload_json, revision FROM broker_commands
                WHERE command_id = ?
                """,
                (str(command_id),),
            ).fetchone()
            if not row:
                raise ExecutionStoreError(f"unknown command: {command_id}")
            command = self._command_from_json(row["payload_json"])
            updated = command.with_attempt(attempt)
            revision = int(row["revision"]) + 1
            cursor = connection.execute(
                """
                UPDATE broker_commands
                SET revision = ?, payload_json = ?, updated_at = ?
                WHERE command_id = ? AND revision = ?
                """,
                (
                    revision,
                    dataclass_json(updated),
                    now,
                    str(command_id),
                    revision - 1,
                ),
            )
            if cursor.rowcount != 1:
                raise ExecutionStoreError("concurrent command attempt update rejected")
            connection.execute(
                """
                INSERT INTO command_events
                (command_id, revision, previous_state, new_state, event_time,
                 details_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(command_id),
                    revision,
                    str(command.state),
                    str(command.state),
                    now,
                    json.dumps(
                        {"attempt_number": str(attempt.attempt_number)},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
        return updated
