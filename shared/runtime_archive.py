from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable


ARCHIVE_VERSION = "1.0"
BACKUP_MANIFEST_NAME = "backup_manifest.json"
BACKED_UP_DIRECTORIES = ("state", "runs", "control", "cache", "logs")


class RuntimeArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupResult:
    archive_path: Path
    checksum_path: Path
    file_count: int
    sha256: str


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _inventory(runtime_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for directory in BACKED_UP_DIRECTORIES:
        root = runtime_dir / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise RuntimeArchiveError(f"Runtime backup refuses symbolic link: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(runtime_dir).as_posix()
            rows.append(
                {
                    "relative_path": relative,
                    "sha256": sha256_path(path),
                    "size": path.stat().st_size,
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            )
    return rows


def _manifest(runtime_dir: Path, files: list[dict[str, object]]) -> dict[str, object]:
    return {
        "archive_version": ARCHIVE_VERSION,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "runtime_directory_name": runtime_dir.name,
        "files": files,
    }


def create_runtime_backup(
    runtime_dir: Path,
    *,
    label: str = "runtime",
    disk_usage_func: Callable[[Path], Any] = shutil.disk_usage,
) -> BackupResult:
    runtime_dir = runtime_dir.resolve()
    backups_dir = runtime_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backups_dir.chmod(0o700)
    files = _inventory(runtime_dir)
    required_bytes = sum(int(row["size"]) for row in files) + 1024 * 1024
    if disk_usage_func(backups_dir).free < required_bytes:
        raise RuntimeArchiveError("Insufficient disk space for checksum-verified backup")

    safe_label = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in label).strip("._")
    if not safe_label:
        safe_label = "runtime"
    archive_path = backups_dir / f"{_utc_stamp()}-{safe_label}.zip"
    checksum_path = archive_path.with_suffix(".zip.sha256")
    fd, raw_temp_path = tempfile.mkstemp(dir=backups_dir, prefix=".backup-", suffix=".zip.tmp")
    os.close(fd)
    temp_path = Path(raw_temp_path)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            manifest_bytes = (
                json.dumps(_manifest(runtime_dir, files), sort_keys=True, indent=2).encode("utf-8")
                + b"\n"
            )
            archive.writestr(BACKUP_MANIFEST_NAME, manifest_bytes)
            for row in files:
                relative = str(row["relative_path"])
                archive.write(runtime_dir / relative, arcname=relative)
        temp_path.chmod(0o600)
        os.replace(temp_path, archive_path)
        archive_path.chmod(0o600)
        digest = sha256_path(archive_path)
        checksum_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
        checksum_path.chmod(0o600)
        verify_runtime_backup(archive_path, runtime_dir=runtime_dir)
        return BackupResult(
            archive_path=archive_path,
            checksum_path=checksum_path,
            file_count=len(files),
            sha256=digest,
        )
    except Exception:
        temp_path.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)
        checksum_path.unlink(missing_ok=True)
        raise


def _safe_archive_member(info: zipfile.ZipInfo) -> PurePosixPath:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise RuntimeArchiveError(f"Unsafe archive path: {info.filename}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise RuntimeArchiveError(f"Archive contains symbolic link: {info.filename}")
    if path.name == BACKUP_MANIFEST_NAME:
        return path
    if path.parts[0] not in BACKED_UP_DIRECTORIES:
        raise RuntimeArchiveError(f"Archive member is outside approved runtime directories: {info.filename}")
    return path


def verify_runtime_backup(archive_path: Path, *, runtime_dir: Path) -> dict[str, object]:
    archive_path = archive_path.resolve()
    backups_dir = runtime_dir.resolve() / "backups"
    if not _is_within(archive_path, backups_dir):
        raise RuntimeArchiveError("Backup archive must be inside the configured runtime backups directory")
    if not archive_path.is_file():
        raise RuntimeArchiveError("Backup archive does not exist")
    if stat.S_IMODE(archive_path.stat().st_mode) & 0o077:
        raise RuntimeArchiveError("Backup archive permissions are too permissive")
    checksum_path = archive_path.with_suffix(".zip.sha256")
    if not checksum_path.is_file():
        raise RuntimeArchiveError("Backup checksum sidecar is missing")
    if stat.S_IMODE(checksum_path.stat().st_mode) & 0o077:
        raise RuntimeArchiveError("Backup checksum permissions are too permissive")
    checksum_parts = checksum_path.read_text(encoding="utf-8").split()
    if len(checksum_parts) != 2 or checksum_parts[1] != archive_path.name:
        raise RuntimeArchiveError("Backup checksum sidecar is malformed")
    expected = checksum_parts[0]
    if sha256_path(archive_path) != expected:
        raise RuntimeArchiveError("Backup archive checksum mismatch")

    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        for info in infos:
            _safe_archive_member(info)
        try:
            manifest = json.loads(archive.read(BACKUP_MANIFEST_NAME))
        except (KeyError, json.JSONDecodeError) as exc:
            raise RuntimeArchiveError("Backup manifest is missing or malformed") from exc
        if not isinstance(manifest, dict) or manifest.get("archive_version") != ARCHIVE_VERSION:
            raise RuntimeArchiveError("Unsupported backup manifest")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise RuntimeArchiveError("Backup manifest files must be a list")
        info_names = {info.filename for info in infos}
        if len(info_names) != len(infos):
            raise RuntimeArchiveError("Backup archive contains duplicate member names")
        manifest_names = {
            str(row.get("relative_path", ""))
            for row in files
            if isinstance(row, dict)
        }
        if info_names != manifest_names | {BACKUP_MANIFEST_NAME}:
            raise RuntimeArchiveError("Backup archive members do not match its manifest")
        for row in files:
            if not isinstance(row, dict):
                raise RuntimeArchiveError("Backup manifest contains malformed file row")
            relative = str(row.get("relative_path", ""))
            if relative not in info_names:
                raise RuntimeArchiveError(f"Backup file missing from archive: {relative}")
            data = archive.read(relative)
            if _sha256_bytes(data) != row.get("sha256") or len(data) != row.get("size"):
                raise RuntimeArchiveError(f"Backup file checksum mismatch: {relative}")
    return manifest


def restore_runtime_backup(archive_path: Path, *, runtime_dir: Path) -> Path:
    runtime_dir = runtime_dir.resolve()
    manifest = verify_runtime_backup(archive_path, runtime_dir=runtime_dir)
    current_backup = create_runtime_backup(runtime_dir, label="pre-restore")
    staging_dir = Path(tempfile.mkdtemp(prefix="restore-staging-", dir=runtime_dir))
    rollback_dir = runtime_dir / f"restore-rollback-{uuid.uuid4().hex}"
    rollback_dir.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                member = _safe_archive_member(info)
                if member.name == BACKUP_MANIFEST_NAME:
                    continue
                destination = staging_dir.joinpath(*member.parts)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination.write_bytes(archive.read(info))
                destination.chmod(0o600)

        for row in manifest["files"]:
            relative = str(row["relative_path"])
            restored = staging_dir / relative
            if sha256_path(restored) != row["sha256"]:
                raise RuntimeArchiveError(f"Restored file checksum mismatch: {relative}")

        moved: list[str] = []
        installed: list[str] = []
        try:
            for directory in BACKED_UP_DIRECTORIES:
                current = runtime_dir / directory
                staged = staging_dir / directory
                if current.exists():
                    os.replace(current, rollback_dir / directory)
                    moved.append(directory)
                if staged.exists():
                    os.replace(staged, current)
                    installed.append(directory)
            shutil.rmtree(rollback_dir)
        except Exception:
            for directory in reversed(installed):
                target = runtime_dir / directory
                if target.exists():
                    shutil.rmtree(target)
            for directory in reversed(moved):
                prior = rollback_dir / directory
                if prior.exists():
                    os.replace(prior, runtime_dir / directory)
            raise
        return current_backup.archive_path
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
        shutil.rmtree(rollback_dir, ignore_errors=True)
