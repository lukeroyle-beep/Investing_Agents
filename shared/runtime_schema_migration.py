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

from shared.artifact_manifest import validate_csv_artifact
from shared.runtime_archive import BackupResult, create_runtime_backup
from shared.schema_registry import get_file_schema
from shared.sqlite_parity import _parity_tables, validate_sqlite_dual_write_parity
from shared.sqlite_sidecar import get_connection, initialise_db, transaction


SCHEMA_MIGRATION_VERSION = "2.0"
_STATUS_MAP = {"running": "started", "success": "succeeded"}


class RuntimeSchemaMigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSchemaMigrationResult:
    runtime_dir: Path
    backup: BackupResult
    report_path: Path
    changed_files: tuple[str, ...]


def _atomic_write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".csv.tmp",
    )
    temp = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            frame.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        path.chmod(0o600)
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def _canonical_frame(path: Path) -> pd.DataFrame:
    schema = get_file_schema(path.name)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in schema.canonical_column_order:
        if column not in frame.columns:
            frame[column] = ""
    return frame[schema.canonical_column_order].fillna("").astype(str)


def _migrate_status_file(path: Path) -> bool:
    if not path.is_file():
        return False
    before = path.read_bytes()
    frame = _canonical_frame(path)
    frame["status"] = frame["status"].map(
        lambda value: _STATUS_MAP.get(
            str(value).strip().lower(),
            str(value).strip().lower(),
        )
    )
    _atomic_write_csv(frame, path)
    return path.read_bytes() != before


def _parse_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _migrate_health(path: Path) -> bool:
    if not path.is_file():
        return False
    before = path.read_bytes()
    original = pd.read_csv(path, dtype=str, keep_default_na=False)
    schema = get_file_schema(path.name)
    output = original.copy()
    for column in schema.canonical_column_order:
        if column not in output.columns:
            output[column] = ""
    output["data_kind"] = output["data_kind"].replace("", "daily_research_price")
    output["observation_time"] = output["observation_time"].where(
        output["observation_time"].str.strip().ne(""),
        output.get("as_of", ""),
    )
    output["retrieval_time"] = output["retrieval_time"].where(
        output["retrieval_time"].str.strip().ne(""),
        output.get("fetched_at", ""),
    )
    output["calendar"] = output["calendar"].replace("", "XNYS")
    output["contradiction_status"] = output["contradiction_status"].replace(
        "", "not_checked"
    )
    for index, row in output.iterrows():
        has_error = bool(str(row.get("error", "")).strip())
        stale = _parse_bool(row.get("stale", ""))
        if not str(row.get("freshness_outcome", "")).strip():
            output.at[index, "freshness_outcome"] = (
                "missing" if has_error else "stale" if stale else "fresh"
            )
        if not str(row.get("mode", "")).strip():
            output.at[index, "mode"] = "no_trade" if has_error or stale else "normal"
        if not str(row.get("reason", "")).strip():
            output.at[index, "reason"] = "Migrated from legacy provider-health schema."
        observation = str(output.at[index, "observation_time"]).strip()
        if observation and not str(row.get("market_session", "")).strip():
            output.at[index, "market_session"] = observation[:10]
    _atomic_write_csv(output[schema.canonical_column_order], path)
    return path.read_bytes() != before


def _migrate_trade_fills(state_dir: Path) -> bool:
    path = state_dir / "trade_fills.csv"
    if not path.is_file():
        return False
    before = path.read_bytes()
    frame = _canonical_frame(path)
    processed_path = state_dir / "processed_fills.csv"
    run_ids: dict[str, str] = {}
    if processed_path.is_file():
        processed = pd.read_csv(processed_path, dtype=str, keep_default_na=False)
        if {"fill_id", "run_id"}.issubset(processed.columns):
            run_ids = dict(zip(processed["fill_id"], processed["run_id"]))
    frame["run_id"] = [
        str(value).strip() or str(run_ids.get(fill_id, "")).strip() or "LEGACY_UNASSIGNED"
        for fill_id, value in zip(frame["fill_id"], frame["run_id"])
    ]
    _atomic_write_csv(frame, path)
    return path.read_bytes() != before


def _rebuild_sqlite_mirror(state_dir: Path) -> None:
    target = state_dir / "trading_system.sqlite3"
    migrated = state_dir / ".trading_system.migrated.sqlite3"
    migrated.unlink(missing_ok=True)
    initialise_db(db_path=migrated, journal_mode="DELETE")
    with get_connection(db_path=migrated, journal_mode="DELETE") as connection:
        with transaction(connection):
            for config in _parity_tables(state_dir=state_dir):
                if not config.csv_path.is_file():
                    continue
                frame = pd.read_csv(config.csv_path, keep_default_na=False)
                for column in config.compare_columns:
                    if column not in frame.columns:
                        frame[column] = ""
                rows = frame[config.compare_columns].to_dict(orient="records")
                connection.execute(f"DELETE FROM {config.table_name}")
                if rows:
                    column_sql = ", ".join(config.compare_columns)
                    placeholder_sql = ", ".join(
                        f":{column}" for column in config.compare_columns
                    )
                    connection.executemany(
                        f"INSERT INTO {config.table_name} ({column_sql}) "
                        f"VALUES ({placeholder_sql})",
                        rows,
                    )
    os.replace(migrated, target)
    target.with_name(f"{target.name}-wal").unlink(missing_ok=True)
    target.with_name(f"{target.name}-shm").unlink(missing_ok=True)
    migrated.with_name(f"{migrated.name}-wal").unlink(missing_ok=True)
    migrated.with_name(f"{migrated.name}-shm").unlink(missing_ok=True)
    target.chmod(0o600)


def migrate_runtime_schemas(runtime_dir: Path) -> RuntimeSchemaMigrationResult:
    """Migrate schema-control artifacts on a copy, then atomically install the copy."""
    runtime_dir = runtime_dir.expanduser().resolve()
    state_dir = runtime_dir / "state"
    if not state_dir.is_dir():
        raise RuntimeSchemaMigrationError("Runtime state directory does not exist")
    backup = create_runtime_backup(runtime_dir, label="pre-schema-v2-migration")
    staging_root = Path(
        tempfile.mkdtemp(prefix=f".{runtime_dir.name}-schema-migration-", dir=runtime_dir.parent)
    )
    staged_state = staging_root / "state"
    rollback_state = runtime_dir / f"state-schema-rollback-{uuid.uuid4().hex}"
    changed: list[str] = []
    try:
        shutil.copytree(state_dir, staged_state)
        for file_name in ("run_history.csv", "run_reconciliation_summary.csv"):
            if _migrate_status_file(staged_state / file_name):
                changed.append(file_name)
        if _migrate_health(staged_state / "data_source_health.csv"):
            changed.append("data_source_health.csv")
        if _migrate_trade_fills(staged_state):
            changed.append("trade_fills.csv")

        for file_name in (
            "run_history.csv",
            "run_reconciliation_summary.csv",
            "data_source_health.csv",
            "trade_fills.csv",
        ):
            path = staged_state / file_name
            if path.is_file():
                validate_csv_artifact(path)

        _rebuild_sqlite_mirror(staged_state)
        present_required = all(
            config.csv_path.is_file() for config in _parity_tables(state_dir=staged_state)
        )
        if present_required:
            parity = validate_sqlite_dual_write_parity(state_dir=staged_state)
            if not parity.passed:
                raise RuntimeSchemaMigrationError("Migrated SQLite mirror failed parity")

        os.replace(state_dir, rollback_state)
        try:
            os.replace(staged_state, state_dir)
        except Exception:
            os.replace(rollback_state, state_dir)
            raise
        shutil.rmtree(rollback_state)

        report_path = runtime_dir / "control" / "schema_migration_v2.json"
        report_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        report = {
            "migration_version": SCHEMA_MIGRATION_VERSION,
            "completed_at_utc": datetime.now(UTC).isoformat(),
            "backup_archive": backup.archive_path.name,
            "backup_sha256": backup.sha256,
            "changed_files": changed,
            "sqlite_mirror_rebuilt_from_csv_authority": True,
        }
        report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        report_path.chmod(0o600)
        return RuntimeSchemaMigrationResult(
            runtime_dir=runtime_dir,
            backup=backup,
            report_path=report_path,
            changed_files=tuple(changed),
        )
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
        if rollback_state.exists() and not state_dir.exists():
            os.replace(rollback_state, state_dir)
