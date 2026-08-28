from __future__ import annotations

import re
import sqlite3
import warnings
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from shared.paths import data_path


SQLITE_DB_PATH = data_path("trading_system.sqlite3")


def _coerce_value(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _coerce_value(value) for key, value in row.items()}


def get_connection(
    db_path: Path | str | None = None,
    *,
    journal_mode: str = "WAL",
) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else SQLITE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    normalised_journal_mode = str(journal_mode).strip().upper()
    if normalised_journal_mode not in {"WAL", "DELETE"}:
        connection.close()
        raise ValueError(f"Unsupported SQLite journal mode: {journal_mode}")
    connection.execute(f"PRAGMA journal_mode = {normalised_journal_mode}")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection):
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_log (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            event_time TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            ticker TEXT,
            position_id TEXT,
            order_id TEXT,
            severity TEXT NOT NULL,
            message TEXT,
            before_json TEXT,
            after_json TEXT,
            metadata_json TEXT
        );

        CREATE TABLE IF NOT EXISTS run_history (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            failed_agent TEXT,
            error_message TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS run_reconciliation_summary (
            run_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            failed_agent TEXT,
            fills_processed REAL NOT NULL,
            positions_opened REAL NOT NULL,
            positions_closed REAL NOT NULL,
            positions_marked_exit_required REAL NOT NULL,
            cash_delta REAL NOT NULL,
            realised_pnl_delta REAL NOT NULL,
            unrealised_pnl_delta REAL NOT NULL,
            equity_delta REAL NOT NULL,
            exposure_delta REAL NOT NULL,
            validation_warning_count REAL NOT NULL,
            validation_failure_count REAL NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS cash_ledger (
            ledger_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            position_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            amount REAL NOT NULL,
            fees REAL NOT NULL,
            cash_balance_after REAL NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS processed_fills (
            fill_id TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            run_id TEXT
        );

        CREATE TABLE IF NOT EXISTS trade_fills (
            fill_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            action TEXT NOT NULL,
            quantity REAL NOT NULL,
            fill_price REAL NOT NULL,
            fees REAL NOT NULL,
            fill_timestamp TEXT NOT NULL,
            run_id TEXT NOT NULL,
            broker TEXT,
            environment TEXT,
            broker_execution_id TEXT,
            broker_order_id TEXT,
            broker_position_id TEXT,
            broker_reference_id TEXT,
            broker_instrument_id TEXT,
            broker_rate_id TEXT,
            broker_fee REAL,
            broker_tax REAL,
            currency TEXT
        );

        CREATE TABLE IF NOT EXISTS cash_state (
            as_of TEXT PRIMARY KEY,
            cash_balance REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS portfolio_state (
            position_id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            status TEXT NOT NULL,
            quantity REAL NOT NULL,
            entry_price REAL NOT NULL,
            entry_date TEXT NOT NULL,
            capital_allocated REAL,
            stop_loss REAL,
            take_profit REAL,
            regime_at_entry TEXT,
            sector TEXT,
            signal_score REAL,
            highest_price_since_entry REAL,
            lowest_price_since_entry REAL,
            current_price REAL,
            market_value REAL,
            pnl_abs REAL,
            pnl_pct REAL,
            exit_flag TEXT,
            exit_reason TEXT,
            last_updated TEXT,
            run_id TEXT,
            realised_pnl_abs REAL,
            fees_total REAL,
            entry_fees_remaining REAL,
            closed_at TEXT,
            exit_price REAL
        );

        CREATE TABLE IF NOT EXISTS portfolio_equity_history (
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            cash_balance REAL NOT NULL,
            open_market_value REAL NOT NULL,
            gross_exposure REAL NOT NULL,
            net_exposure REAL NOT NULL,
            unrealised_pnl_abs REAL NOT NULL,
            realised_pnl_abs REAL NOT NULL,
            total_equity REAL NOT NULL,
            open_positions REAL NOT NULL,
            closed_positions REAL NOT NULL,
            peak_equity REAL NOT NULL,
            drawdown_abs REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            PRIMARY KEY (run_id, timestamp)
        );
        """
    )
    existing_trade_fill_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(trade_fills)")
    }
    for column, column_type in {
        "broker": "TEXT",
        "environment": "TEXT",
        "broker_execution_id": "TEXT",
        "broker_order_id": "TEXT",
        "broker_position_id": "TEXT",
        "broker_reference_id": "TEXT",
        "broker_instrument_id": "TEXT",
        "broker_rate_id": "TEXT",
        "broker_fee": "REAL",
        "broker_tax": "REAL",
        "currency": "TEXT",
    }.items():
        if column not in existing_trade_fill_columns:
            connection.execute(f"ALTER TABLE trade_fills ADD COLUMN {column} {column_type}")

    existing_portfolio_state_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(portfolio_state)")
    }
    if "entry_fees_remaining" not in existing_portfolio_state_columns:
        connection.execute(
            "ALTER TABLE portfolio_state ADD COLUMN entry_fees_remaining REAL"
        )


def initialise_db(
    db_path: Path | str | None = None,
    *,
    journal_mode: str = "WAL",
) -> None:
    with get_connection(db_path=db_path, journal_mode=journal_mode) as connection:
        with transaction(connection):
            _create_schema(connection)


def _validated_identifier(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(identifier)):
        raise ValueError(f"Invalid SQLite identifier: {identifier}")
    return str(identifier)


def _execute_best_effort(action: str, fn) -> None:
    try:
        with get_connection() as connection:
            with transaction(connection):
                _create_schema(connection)
                fn(connection)
    except Exception as exc:
        warnings.warn(
            f"SQLite dual-write skipped for {action}: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )


def append_event_log_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO event_log (
                event_id, run_id, event_time, agent_name, event_type, entity_type,
                entity_id, ticker, position_id, order_id, severity, message,
                before_json, after_json, metadata_json
            ) VALUES (
                :event_id, :run_id, :event_time, :agent_name, :event_type, :entity_type,
                :entity_id, :ticker, :position_id, :order_id, :severity, :message,
                :before_json, :after_json, :metadata_json
            )
            """,
            payload,
        )

    _execute_best_effort("event_log", _write)


def upsert_run_history_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO run_history (
                run_id, started_at, completed_at, status, failed_agent, error_message, notes
            ) VALUES (
                :run_id, :started_at, :completed_at, :status, :failed_agent, :error_message, :notes
            )
            ON CONFLICT(run_id) DO UPDATE SET
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                status=excluded.status,
                failed_agent=excluded.failed_agent,
                error_message=excluded.error_message,
                notes=excluded.notes
            """,
            payload,
        )

    _execute_best_effort("run_history", _write)


def upsert_run_reconciliation_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO run_reconciliation_summary (
                run_id, started_at, completed_at, status, failed_agent, fills_processed,
                positions_opened, positions_closed, positions_marked_exit_required,
                cash_delta, realised_pnl_delta, unrealised_pnl_delta, equity_delta,
                exposure_delta, validation_warning_count, validation_failure_count, notes
            ) VALUES (
                :run_id, :started_at, :completed_at, :status, :failed_agent, :fills_processed,
                :positions_opened, :positions_closed, :positions_marked_exit_required,
                :cash_delta, :realised_pnl_delta, :unrealised_pnl_delta, :equity_delta,
                :exposure_delta, :validation_warning_count, :validation_failure_count, :notes
            )
            ON CONFLICT(run_id) DO UPDATE SET
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                status=excluded.status,
                failed_agent=excluded.failed_agent,
                fills_processed=excluded.fills_processed,
                positions_opened=excluded.positions_opened,
                positions_closed=excluded.positions_closed,
                positions_marked_exit_required=excluded.positions_marked_exit_required,
                cash_delta=excluded.cash_delta,
                realised_pnl_delta=excluded.realised_pnl_delta,
                unrealised_pnl_delta=excluded.unrealised_pnl_delta,
                equity_delta=excluded.equity_delta,
                exposure_delta=excluded.exposure_delta,
                validation_warning_count=excluded.validation_warning_count,
                validation_failure_count=excluded.validation_failure_count,
                notes=excluded.notes
            """,
            payload,
        )

    _execute_best_effort("run_reconciliation_summary", _write)


def append_cash_ledger_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO cash_ledger (
                ledger_id, run_id, timestamp, event_type, position_id, ticker, side,
                action, amount, fees, cash_balance_after, notes
            ) VALUES (
                :ledger_id, :run_id, :timestamp, :event_type, :position_id, :ticker, :side,
                :action, :amount, :fees, :cash_balance_after, :notes
            )
            """,
            payload,
        )

    _execute_best_effort("cash_ledger", _write)


def append_processed_fill_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO processed_fills (
                fill_id, processed_at, run_id
            ) VALUES (
                :fill_id, :processed_at, :run_id
            )
            """,
            payload,
        )

    _execute_best_effort("processed_fills", _write)


def append_trade_fill_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO trade_fills (
                fill_id, ticker, side, action, quantity, fill_price, fees,
                fill_timestamp, run_id, broker, environment, broker_execution_id,
                broker_order_id, broker_position_id, broker_reference_id,
                broker_instrument_id, broker_rate_id, broker_fee, broker_tax, currency
            ) VALUES (
                :fill_id, :ticker, :side, :action, :quantity, :fill_price, :fees,
                :fill_timestamp, :run_id, :broker, :environment, :broker_execution_id,
                :broker_order_id, :broker_position_id, :broker_reference_id,
                :broker_instrument_id, :broker_rate_id, :broker_fee, :broker_tax, :currency
            )
            """,
            payload,
        )

    _execute_best_effort("trade_fills", _write)


def replace_cash_state_rows(rows: Iterable[dict[str, Any]]) -> None:
    payloads = [_normalise_row(row) for row in rows]

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM cash_state")
        connection.executemany(
            "INSERT INTO cash_state (as_of, cash_balance) VALUES (:as_of, :cash_balance)",
            payloads,
        )

    _execute_best_effort("cash_state", _write)


def replace_portfolio_state_rows(rows: Iterable[dict[str, Any]]) -> None:
    columns = [
        "position_id",
        "ticker",
        "side",
        "status",
        "quantity",
        "entry_price",
        "entry_date",
        "capital_allocated",
        "stop_loss",
        "take_profit",
        "regime_at_entry",
        "sector",
        "signal_score",
        "highest_price_since_entry",
        "lowest_price_since_entry",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "exit_flag",
        "exit_reason",
        "last_updated",
        "run_id",
        "realised_pnl_abs",
        "fees_total",
        "entry_fees_remaining",
        "closed_at",
        "exit_price",
    ]
    payloads = [
        _normalise_row({column: row.get(column) for column in columns})
        for row in rows
    ]
    column_sql = ", ".join(columns)
    value_sql = ", ".join(f":{column}" for column in columns)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM portfolio_state")
        if payloads:
            connection.executemany(
                f"INSERT INTO portfolio_state ({column_sql}) VALUES ({value_sql})",
                payloads,
            )

    _execute_best_effort("portfolio_state", _write)


def upsert_portfolio_equity_history_row(row: dict[str, Any]) -> None:
    payload = _normalise_row(row)

    def _write(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            INSERT INTO portfolio_equity_history (
                run_id, timestamp, cash_balance, open_market_value, gross_exposure,
                net_exposure, unrealised_pnl_abs, realised_pnl_abs, total_equity,
                open_positions, closed_positions, peak_equity, drawdown_abs, drawdown_pct
            ) VALUES (
                :run_id, :timestamp, :cash_balance, :open_market_value, :gross_exposure,
                :net_exposure, :unrealised_pnl_abs, :realised_pnl_abs, :total_equity,
                :open_positions, :closed_positions, :peak_equity, :drawdown_abs, :drawdown_pct
            )
            ON CONFLICT(run_id, timestamp) DO UPDATE SET
                cash_balance=excluded.cash_balance,
                open_market_value=excluded.open_market_value,
                gross_exposure=excluded.gross_exposure,
                net_exposure=excluded.net_exposure,
                unrealised_pnl_abs=excluded.unrealised_pnl_abs,
                realised_pnl_abs=excluded.realised_pnl_abs,
                total_equity=excluded.total_equity,
                open_positions=excluded.open_positions,
                closed_positions=excluded.closed_positions,
                peak_equity=excluded.peak_equity,
                drawdown_abs=excluded.drawdown_abs,
                drawdown_pct=excluded.drawdown_pct
            """,
            payload,
        )

    _execute_best_effort("portfolio_equity_history", _write)


def fetch_row_count(table_name: str, db_path: Path | str | None = None) -> int:
    with get_connection(db_path=db_path) as connection:
        _create_schema(connection)
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {_validated_identifier(table_name)}").fetchone()
        return int(row["count"])


def fetch_all_rows(table_name: str, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    with get_connection(db_path=db_path) as connection:
        _create_schema(connection)
        query = f"SELECT * FROM {_validated_identifier(table_name)}"
        rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]


def fetch_table_df(
    table_name: str,
    db_path: Path | str | None = None,
    order_by: Iterable[str] | None = None,
) -> pd.DataFrame:
    with get_connection(db_path=db_path) as connection:
        _create_schema(connection)
        query = f"SELECT * FROM {_validated_identifier(table_name)}"
        if order_by:
            order_clause = ", ".join(_validated_identifier(column) for column in order_by)
            query = f"{query} ORDER BY {order_clause}"
        rows = connection.execute(query).fetchall()
        return pd.DataFrame([dict(row) for row in rows])
