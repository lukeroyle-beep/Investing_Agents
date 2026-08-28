from __future__ import annotations

import hashlib
import os
import stat
import zipfile
from collections import namedtuple
from datetime import UTC, datetime

import pandas as pd
import pytest

from scripts import nightly_checklist
from shared.artifact_manifest import validate_artifact_manifest
from shared.runtime_archive import (
    RuntimeArchiveError,
    create_runtime_backup,
    restore_runtime_backup,
    verify_runtime_backup,
)
from shared.runtime_bootstrap import (
    BOOTSTRAP_RUN_ID,
    RuntimeBootstrapError,
    bootstrap_runtime,
)
from shared.runtime_recovery import resolve_interrupted_run
from shared.runtime_schema_migration import migrate_runtime_schemas
from shared.sqlite_parity import validate_sqlite_dual_write_parity


def test_bootstrap_passes_operational_checks_without_pipeline(tmp_path, monkeypatch) -> None:
    result = bootstrap_runtime(
        tmp_path / "runtime",
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )
    monkeypatch.setattr(nightly_checklist, "DATA_DIR", result.runtime_dir / "state")

    assert nightly_checklist.main() == 0
    assert validate_sqlite_dual_write_parity(
        state_dir=result.runtime_dir / "state"
    ).passed
    assert validate_artifact_manifest(
        result.manifest_path,
        state_dir=result.runtime_dir / "state",
    )["run_id"] == BOOTSTRAP_RUN_ID


def test_bootstrap_is_atomic_and_refuses_nonempty_target(tmp_path) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    marker = runtime_dir / "operator-data.txt"
    marker.write_text("preserve", encoding="utf-8")

    with pytest.raises(RuntimeBootstrapError, match="non-empty"):
        bootstrap_runtime(runtime_dir)

    assert marker.read_text(encoding="utf-8") == "preserve"
    assert not list(tmp_path.glob(".runtime-bootstrap-*"))


def test_backup_restore_is_checksum_verified_and_preserves_pre_restore_copy(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")
    backup = create_runtime_backup(result.runtime_dir, label="test")
    target = result.runtime_dir / "state" / "cash_state.csv"
    original = target.read_bytes()
    target.write_text("corrupt-local-state\n", encoding="utf-8")

    pre_restore = restore_runtime_backup(
        backup.archive_path,
        runtime_dir=result.runtime_dir,
    )

    assert target.read_bytes() == original
    assert pre_restore.is_file()
    assert stat.S_IMODE(backup.archive_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.checksum_path.stat().st_mode) == 0o600


def test_backup_rejects_corruption_external_paths_and_insufficient_space(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")
    backup = create_runtime_backup(result.runtime_dir, label="secure")
    backup.archive_path.write_bytes(backup.archive_path.read_bytes() + b"tamper")
    with pytest.raises(RuntimeArchiveError, match="checksum mismatch"):
        verify_runtime_backup(backup.archive_path, runtime_dir=result.runtime_dir)

    external = tmp_path / "external.zip"
    external.write_bytes(b"not-a-backup")
    with pytest.raises(RuntimeArchiveError, match="inside the configured"):
        verify_runtime_backup(external, runtime_dir=result.runtime_dir)

    Usage = namedtuple("Usage", "total used free")
    with pytest.raises(RuntimeArchiveError, match="Insufficient disk space"):
        create_runtime_backup(
            result.runtime_dir,
            label="no-space",
            disk_usage_func=lambda _path: Usage(1, 1, 0),
        )


def test_restore_rejects_archive_path_traversal(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")
    archive = result.runtime_dir / "backups" / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("backup_manifest.json", "{}")
        handle.writestr("../escape.txt", "no")
    archive.chmod(0o600)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    checksum.chmod(0o600)

    with pytest.raises(RuntimeArchiveError, match="Unsafe archive path"):
        restore_runtime_backup(archive, runtime_dir=result.runtime_dir)
    assert not (tmp_path / "escape.txt").exists()


def test_interrupted_run_defaults_failed_unless_success_proof_validates(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")

    proven = resolve_interrupted_run(result.runtime_dir, BOOTSTRAP_RUN_ID)
    assert proven.resolved_status == "succeeded"
    assert proven.used_finalization_proof

    history_path = result.runtime_dir / "state" / "run_history.csv"
    history = pd.read_csv(history_path, dtype=str, keep_default_na=False)
    history.loc[0, "status"] = "validating"
    history.loc[0, "completed_at"] = ""
    history.to_csv(history_path, index=False)
    ambiguous = resolve_interrupted_run(result.runtime_dir, BOOTSTRAP_RUN_ID)

    assert ambiguous.resolved_status == "failed"
    assert not ambiguous.used_finalization_proof
    assert pd.read_csv(history_path).iloc[0]["status"] == "failed"


def test_schema_migration_is_idempotent_and_always_creates_verified_backup(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")
    history_path = result.runtime_dir / "state" / "run_history.csv"
    history = pd.read_csv(history_path, dtype=str, keep_default_na=False)
    history.loc[0, "status"] = "success"
    history.to_csv(history_path, index=False)
    health_path = result.runtime_dir / "state" / "data_source_health.csv"
    pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "source": "legacy",
                "error": "",
                "stale": False,
                "retry_count": 0,
                "fetched_at": "2026-08-28T12:00:00+00:00",
                "as_of": "2026-08-27T00:00:00+00:00",
            }
        ]
    ).to_csv(health_path, index=False)

    first = migrate_runtime_schemas(result.runtime_dir)
    second = migrate_runtime_schemas(result.runtime_dir)

    assert first.backup.archive_path.is_file()
    assert second.backup.archive_path.is_file()
    assert first.backup.archive_path != second.backup.archive_path
    assert pd.read_csv(history_path).iloc[0]["status"] == "succeeded"
    health = pd.read_csv(health_path)
    assert health.iloc[0]["mode"] == "normal"
    assert health.iloc[0]["data_kind"] == "daily_research_price"
    assert second.changed_files == ()
    assert validate_sqlite_dual_write_parity(
        state_dir=result.runtime_dir / "state"
    ).passed
