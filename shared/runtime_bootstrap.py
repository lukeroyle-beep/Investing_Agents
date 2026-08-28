from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from shared.artifact_manifest import (
    REQUIRED_ARTIFACTS,
    build_artifact_manifest,
    capture_economic_checksums,
    sha256_file,
    validate_artifact_manifest,
    write_artifact_manifest,
)
from shared.run_finalizer import FINALIZATION_RECORD_FILE, FINALIZATION_RECORD_VERSION
from shared.schema_registry import get_file_schema
from shared.sqlite_sidecar import get_connection, initialise_db, transaction


BOOTSTRAP_RUN_ID = "BOOTSTRAP_BASELINE"


class RuntimeBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapResult:
    runtime_dir: Path
    run_id: str
    manifest_path: Path
    finalization_record_path: Path


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    frame.to_csv(path, index=False)
    path.chmod(0o600)


def _registered_frame(file_name: str, rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=get_file_schema(file_name).canonical_column_order).fillna("")


def _event_row(
    event_id: str,
    event_type: str,
    event_time: str,
    message: str,
) -> dict[str, object]:
    metadata = {
        "schema_version": "1.0",
        "entity": {"entity_type": "runtime", "entity_id": BOOTSTRAP_RUN_ID},
        "details": {"synthetic_bootstrap": True},
    }
    return {
        "event_id": event_id,
        "run_id": BOOTSTRAP_RUN_ID,
        "event_time": event_time,
        "agent_name": "Runtime Bootstrap",
        "event_type": event_type,
        "entity_type": "runtime",
        "entity_id": BOOTSTRAP_RUN_ID,
        "ticker": "",
        "position_id": "",
        "order_id": "",
        "severity": "info",
        "message": message,
        "before_json": "",
        "after_json": "",
        "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    }


def _write_baseline_state(runtime_dir: Path, now_iso: str) -> None:
    state_dir = runtime_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    empty_registered = [
        "portfolio_state.csv",
        "portfolio_monitor.csv",
        "position_alerts.csv",
        "cash_ledger.csv",
        "trade_fills.csv",
        "processed_fills.csv",
    ]
    for file_name in empty_registered:
        _write_csv(state_dir / file_name, _registered_frame(file_name, []))

    _write_csv(
        state_dir / "cash_state.csv",
        _registered_frame(
            "cash_state.csv",
            [{"as_of": now_iso, "cash_balance": 100000.0}],
        ),
    )
    equity_row = {
        "timestamp": now_iso,
        "run_id": BOOTSTRAP_RUN_ID,
        "cash_balance": 100000.0,
        "open_market_value": 0.0,
        "gross_exposure": 0.0,
        "net_exposure": 0.0,
        "unrealised_pnl_abs": 0.0,
        "realised_pnl_abs": 0.0,
        "total_equity": 100000.0,
        "open_positions": 0,
        "closed_positions": 0,
        "peak_equity": 100000.0,
        "drawdown_abs": 0.0,
        "drawdown_pct": 0.0,
    }
    _write_csv(
        state_dir / "portfolio_equity_history.csv",
        _registered_frame("portfolio_equity_history.csv", [equity_row]),
    )
    performance_row = {
        "latest_timestamp": now_iso,
        "latest_run_id": BOOTSTRAP_RUN_ID,
        "current_total_equity": 100000.0,
        "peak_equity": 100000.0,
        "peak_equity_timestamp": now_iso,
        "current_drawdown_abs": 0.0,
        "current_drawdown_pct": 0.0,
        "max_drawdown_abs": 0.0,
        "max_drawdown_pct": 0.0,
        "max_drawdown_timestamp": now_iso,
        "observation_count": 1,
    }
    _write_csv(
        state_dir / "performance_summary.csv",
        _registered_frame("performance_summary.csv", [performance_row]),
    )
    lifecycle_row = {
        "checked_at": now_iso,
        "record_type": "summary",
        "severity": "info",
        "invariant_name": "bootstrap_baseline",
        "rule": "synthetic_zero_position_baseline",
        "position_id": "",
        "ticker": "",
        "detail": "Synthetic baseline validated without executing the pipeline.",
        "total_checks": 1,
        "passed_checks": 1,
        "warning_count": 0,
        "failure_count": 0,
    }
    _write_csv(
        state_dir / "lifecycle_integrity_report.csv",
        _registered_frame("lifecycle_integrity_report.csv", [lifecycle_row]),
    )
    health_row = {
        "ticker": "BOOTSTRAP",
        "source": "synthetic_bootstrap",
        "data_kind": "daily_research_price",
        "error": "",
        "retry_count": 0,
        "observation_time": now_iso,
        "retrieval_time": now_iso,
        "market_session": now_iso[:10],
        "calendar": "XNYS",
        "freshness_outcome": "fresh",
        "contradiction_status": "not_checked",
        "mode": "normal",
        "reason": "Synthetic bootstrap health evidence; not market data.",
        "stale": False,
        "fetched_at": now_iso,
        "as_of": now_iso,
    }
    _write_csv(
        state_dir / "data_source_health.csv",
        _registered_frame("data_source_health.csv", [health_row]),
    )

    events = [
        _event_row("EVT_BOOTSTRAP_STARTED", "run_started", now_iso, "Bootstrap started"),
        _event_row("EVT_BOOTSTRAP_VALIDATED", "validation_passed", now_iso, "Bootstrap validated"),
        _event_row("EVT_BOOTSTRAP_SUCCEEDED", "run_completed", now_iso, "Bootstrap completed"),
    ]
    _write_csv(state_dir / "event_log.csv", _registered_frame("event_log.csv", events))
    run_history_row = {
        "run_id": BOOTSTRAP_RUN_ID,
        "started_at": now_iso,
        "completed_at": now_iso,
        "status": "succeeded",
        "failed_agent": "",
        "error_message": "",
        "notes": "Synthetic zero-position runtime bootstrap baseline.",
    }
    _write_csv(
        state_dir / "run_history.csv",
        _registered_frame("run_history.csv", [run_history_row]),
    )
    reconciliation_row = {
        "run_id": BOOTSTRAP_RUN_ID,
        "started_at": now_iso,
        "completed_at": now_iso,
        "status": "succeeded",
        "failed_agent": "",
        "fills_processed": 0,
        "positions_opened": 0,
        "positions_closed": 0,
        "positions_marked_exit_required": 0,
        "cash_delta": 0.0,
        "realised_pnl_delta": 0.0,
        "unrealised_pnl_delta": 0.0,
        "equity_delta": 0.0,
        "exposure_delta": 0.0,
        "validation_warning_count": 0,
        "validation_failure_count": 0,
        "notes": "Synthetic zero-position runtime bootstrap baseline.",
    }
    _write_csv(
        state_dir / "run_reconciliation_summary.csv",
        _registered_frame("run_reconciliation_summary.csv", [reconciliation_row]),
    )

    _write_csv(
        state_dir / "portfolio_state_prev_snapshot.csv",
        _registered_frame("portfolio_state.csv", []),
    )
    custom_headers = {
        "advisory_trades.csv": ["ticker", "action", "direction", "advice_status", "run_id"],
        "exit_advice.csv": ["position_id", "ticker", "exit_action", "status", "run_id"],
        "final_shortlist.csv": ["ticker", "risk_decision", "adjusted_setup_score"],
        "portfolio_orders.csv": [
            "run_id",
            "internal_instrument_id",
            "ticker",
            "exchange",
            "currency",
            "direction",
            "asset_type",
            "execution_environment",
            "order_type",
            "sizing_method",
            "sizing_value",
            "entry_price",
            "position_size_pct",
            "capital_allocated",
        ],
        "signal_setups.csv": ["ticker"],
        "signal_top_setups.csv": ["ticker"],
        "news_flags.csv": ["ticker", "has_news"],
        "macro_regime.csv": ["market_regime"],
    }
    for file_name, columns in custom_headers.items():
        _write_csv(state_dir / file_name, pd.DataFrame(columns=columns))

    db_path = state_dir / "trading_system.sqlite3"
    initialise_db(db_path=db_path)
    with get_connection(db_path=db_path) as connection:
        with transaction(connection):
            connection.execute(
                "INSERT INTO cash_state (as_of, cash_balance) VALUES (?, ?)",
                (now_iso, 100000.0),
            )
            connection.execute(
                """
                INSERT INTO run_history
                (run_id, started_at, completed_at, status, failed_agent, error_message, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(run_history_row[column] for column in get_file_schema("run_history.csv").canonical_column_order),
            )
            connection.execute(
                """
                INSERT INTO run_reconciliation_summary
                (run_id, started_at, completed_at, status, failed_agent, fills_processed,
                 positions_opened, positions_closed, positions_marked_exit_required, cash_delta,
                 realised_pnl_delta, unrealised_pnl_delta, equity_delta, exposure_delta,
                 validation_warning_count, validation_failure_count, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(
                    reconciliation_row[column]
                    for column in get_file_schema("run_reconciliation_summary.csv").canonical_column_order
                ),
            )
            event_columns = get_file_schema("event_log.csv").canonical_column_order
            connection.executemany(
                f"INSERT INTO event_log ({', '.join(event_columns)}) VALUES ({', '.join('?' for _ in event_columns)})",
                [tuple(row[column] for column in event_columns) for row in events],
            )
            equity_columns = get_file_schema("portfolio_equity_history.csv").canonical_column_order
            connection.execute(
                f"INSERT INTO portfolio_equity_history ({', '.join(equity_columns)}) VALUES ({', '.join('?' for _ in equity_columns)})",
                tuple(equity_row[column] for column in equity_columns),
            )
    db_path.chmod(0o600)


def _write_bootstrap_finalization(runtime_dir: Path, now_iso: str) -> tuple[Path, Path]:
    state_dir = runtime_dir / "state"
    runs_dir = runtime_dir / "runs"
    pre_checksums = capture_economic_checksums(state_dir)
    manifest = build_artifact_manifest(
        BOOTSTRAP_RUN_ID,
        pre_economic_checksums=pre_checksums,
        state_dir=state_dir,
        artifact_names=REQUIRED_ARTIFACTS,
        validation_checks=[
            "synthetic_zero_position_baseline",
            "schemas_validated",
            "sqlite_mirror_initialized",
        ],
        now_func=lambda: now_iso,
    )
    manifest_path = write_artifact_manifest(manifest, runs_dir=runs_dir)
    validate_artifact_manifest(manifest_path, state_dir=state_dir)
    record_path = runs_dir / BOOTSTRAP_RUN_ID / FINALIZATION_RECORD_FILE
    record = {
        "record_version": FINALIZATION_RECORD_VERSION,
        "finalization_id": str(uuid.uuid4()),
        "run_id": BOOTSTRAP_RUN_ID,
        "state": "complete",
        "outcome": "succeeded",
        "completed_at_utc": now_iso,
        "manifest_file": manifest_path.name,
        "manifest_sha256": sha256_file(manifest_path),
        "validation_checks": manifest["terminal_validation"]["checks"],
    }
    record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    record_path.chmod(0o600)
    return manifest_path, record_path


def bootstrap_runtime(runtime_dir: Path, *, now: datetime | None = None) -> BootstrapResult:
    runtime_dir = runtime_dir.expanduser().resolve()
    if runtime_dir.exists() and any(runtime_dir.iterdir()):
        raise RuntimeBootstrapError("Runtime bootstrap refuses a non-empty target")
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{runtime_dir.name}-bootstrap-", dir=runtime_dir.parent))
    try:
        staging.chmod(0o700)
        for directory in ("state", "runs", "control", "cache", "logs", "backups"):
            (staging / directory).mkdir(mode=0o700)
        now_iso = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        _write_baseline_state(staging, now_iso)
        manifest_path, record_path = _write_bootstrap_finalization(staging, now_iso)
        if runtime_dir.exists():
            runtime_dir.rmdir()
        os.replace(staging, runtime_dir)
        return BootstrapResult(
            runtime_dir=runtime_dir,
            run_id=BOOTSTRAP_RUN_ID,
            manifest_path=runtime_dir / manifest_path.relative_to(staging),
            finalization_record_path=runtime_dir / record_path.relative_to(staging),
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
