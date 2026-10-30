"""Reviewed, lossless separation of legacy advice from the keyed journal.

Unkeyed observations have no provable run identity. They stay unresolved in the
exact original byte archive, with an exhaustive row map, never invented IDs.
This module has no broker, economic ledger, or execution journal dependency.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile

import pandas as pd

from shared.schemas import TRADE_JOURNAL_SCHEMA, validate_trade_journal


SCHEMA = "advisory-journal-history-v1"


class JournalMigrationError(ValueError):
    pass


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _path(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise JournalMigrationError("journal_canonical_absolute_source_path_required")
    return path


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise JournalMigrationError("journal_owned_regular_file_required")
            return handle.read()
    except OSError:
        raise JournalMigrationError("journal_source_or_archive_unavailable") from None


def _parse(raw):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True)
        fields = reader.fieldnames
        if (not fields or len(set(fields)) != len(fields)
                or not set(TRADE_JOURNAL_SCHEMA.column_order) <= set(fields)):
            raise JournalMigrationError("journal_complete_unique_header_required")
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise JournalMigrationError("journal_ambiguous_csv_row")
        return fields, rows
    except (csv.Error, UnicodeError):
        raise JournalMigrationError("journal_invalid_csv") from None


def _timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError
        return stamp
    except (ValueError, TypeError):
        raise JournalMigrationError("journal_duplicate_timestamp_requires_timezone") from None


def _plan(path, raw):
    fields, rows = _parse(raw)
    groups = {}
    unresolved = set()
    for index, row in enumerate(rows):
        # Schema normalization matches the live Journal's identity comparison.
        if not row["run_id"].strip() or not row["ticker"].strip():
            unresolved.add(index)
            continue
        normalized = validate_trade_journal(pd.DataFrame([row])).iloc[0]
        key = (normalized["run_id"], normalized["ticker"])
        groups.setdefault(key, []).append(index)
    canonical = []
    output_indices = {}
    representatives = {}
    for indices in groups.values():
        first = rows[indices[0]]
        if len(indices) > 1:
            if any(any(rows[i][field] != first[field] for field in fields if field != "journaled_at")
                   for i in indices):
                raise JournalMigrationError("journal_duplicate_source_or_review_conflict")
            selected = min(indices, key=lambda i: (_timestamp(rows[i]["journaled_at"]), i))
        else:
            selected = indices[0]
        canonical.append(rows[selected])
        for i in indices:
            output_indices[i] = len(canonical)
            representatives[i] = selected
    if canonical:
        validate_trade_journal(pd.DataFrame(canonical))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(canonical)
    operational = output.getvalue().encode("utf-8")
    row_map = []
    for i, row in enumerate(rows):
        row_map.append({"source_row": i + 1, "source_row_sha256": _hash(_json(row)),
            "journaled_at": row["journaled_at"],
            "disposition": "unresolved_missing_identity" if i in unresolved else (
                "retained" if representatives[i] == i else "equivalent_keyed_retry"),
            "operational_row": output_indices.get(i)})
    plan = {"schema": SCHEMA, "source_path": str(path), "source_sha256": _hash(raw),
        "source_bytes": len(raw), "source_rows": len(rows), "operational_sha256": _hash(operational),
        "operational_bytes": len(operational), "operational_rows": len(canonical),
        "unresolved_unkeyed_rows": len(unresolved),
        "equivalent_keyed_retry_rows": len(rows) - len(unresolved) - len(canonical),
        "row_map": row_map}
    return plan, operational


def _summary(plan):
    return {key: value for key, value in plan.items() if key != "row_map"} | {"plan_sha256": _hash(_json(plan))}


def inspect_migration(source):
    """Pure dry run: no directory, lock, archive, or source write."""
    path = _path(source)
    plan, _ = _plan(path, _read(path))
    return {"status": "DRY_RUN_ONLY", **_summary(plan)}


def _history(path):
    return path.with_name(path.name + ".history")


def _directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise JournalMigrationError("journal_owned_private_history_directory_required")


def _sync(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_exact(path, raw):
    """Create-only durable artifact; an interrupted partial file stays held."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read(path) != raw:
            raise JournalMigrationError("journal_existing_archive_conflict_never_overwrite") from None
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    _sync(path.parent)


@contextmanager
def journal_write_lock(source):
    """Serialize the current Journal and migration; a running old writer is unsafe.

    Apply additionally requires an explicit stopped-writers attestation, since
    an older checkout predates this lock and cannot cooperate with it.
    """
    path = Path(source).resolve()
    if not path.parent.is_dir():
        path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise JournalMigrationError("journal_owned_regular_lock_required")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JournalMigrationError("journal_writer_active") from None
        yield


def _validate_archive(directory, source):
    raw = _read(directory / "original.csv")
    plan, operational = _plan(source, raw)
    if (_read(directory / "plan.json") != _json(plan)
            or _read(directory / "operational.csv") != operational
            or directory.name != plan["source_sha256"]):
        raise JournalMigrationError("journal_history_integrity_failure")
    try:
        receipt = json.loads(_read(directory / "prepared.json"))
    except (ValueError, TypeError):
        raise JournalMigrationError("journal_history_receipt_invalid") from None
    if (set(receipt) != {"schema", "source_path", "source_sha256", "plan_sha256", "reviewed_commit", "review_reference"}
            or receipt["schema"] != SCHEMA or receipt["source_path"] != str(source)
            or receipt["source_sha256"] != plan["source_sha256"]
            or receipt["plan_sha256"] != _hash(_json(plan))
            or re.fullmatch(r"[a-f0-9]{40}", receipt["reviewed_commit"]) is None
            or not receipt["review_reference"].strip()):
        raise JournalMigrationError("journal_history_receipt_invalid")
    completed = {"schema": SCHEMA, "prepared_sha256": _hash(_json(receipt)),
                 "operational_sha256": plan["operational_sha256"], "status": "applied"}
    return plan, operational, receipt, completed


def validate_journal_history(source):
    """Do not admit new advice while a migration is partial or its archive lost."""
    source = Path(source).resolve()
    history = _history(source)
    if not history.exists() and not history.is_symlink():
        return
    info = history.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise JournalMigrationError("journal_history_directory_invalid")
    entries = list(history.iterdir())
    if not entries:
        raise JournalMigrationError("journal_history_incomplete")
    for directory in entries:
        if directory.is_symlink() or not directory.is_dir():
            raise JournalMigrationError("journal_history_directory_invalid")
        _, _, _, completed = _validate_archive(directory, source)
        if _read(directory / "completed.json") != _json(completed):
            raise JournalMigrationError("journal_history_incomplete")


def apply_migration(source, *, expected_source_sha256, expected_plan_sha256,
                    reviewed_commit, review_reference, writers_stopped, checkout_verifier):
    """Apply one reviewed plan; crash recovery reuses its exact retained bytes."""
    path = _path(source)
    if (re.fullmatch(r"[a-f0-9]{64}", expected_source_sha256 or "") is None
            or re.fullmatch(r"[a-f0-9]{64}", expected_plan_sha256 or "") is None
            or re.fullmatch(r"[a-f0-9]{40}", reviewed_commit or "") is None
            or not isinstance(review_reference, str) or not review_reference.strip()
            or writers_stopped is not True):
        raise JournalMigrationError("journal_exact_plan_review_and_stopped_writers_required")
    checkout_verifier(reviewed_commit)
    # Existing source required before acquiring a lock; never create a journal.
    _read(path)
    with journal_write_lock(path):
        current = _read(path)
        history = _history(path)
        directory = history / expected_source_sha256
        if directory.exists():
            # A crash may have replaced the source before completion was saved.
            # Rebuild only from the retained original, never from a fresh journal.
            original = _read(directory / "original.csv")
        else:
            original = current
        plan, operational = _plan(path, original)
        if plan["source_sha256"] != expected_source_sha256 or _hash(_json(plan)) != expected_plan_sha256:
            raise JournalMigrationError("journal_reviewed_input_or_plan_changed")
        prepared = {"schema": SCHEMA, "source_path": str(path), "source_sha256": expected_source_sha256,
            "plan_sha256": expected_plan_sha256, "reviewed_commit": reviewed_commit,
            "review_reference": review_reference}
        if (directory / "completed.json").exists():
            validate_journal_history(path)
            _, _, saved, _ = _validate_archive(directory, path)
            if saved != prepared:
                raise JournalMigrationError("journal_migration_review_binding_changed")
            return {"status": "ALREADY_APPLIED", **_summary(plan)}
        if current not in (original, operational):
            raise JournalMigrationError("journal_changed_during_migration_never_overwrite")
        _directory(history)
        # A separate pending migration is never superseded by a new plan.
        for other in history.iterdir():
            if other != directory:
                _, _, _, completed = _validate_archive(other, path)
                if _read(other / "completed.json") != _json(completed):
                    raise JournalMigrationError("journal_other_migration_incomplete")
        _directory(directory)
        _sync(history)
        _sync(history.parent)
        _create_exact(directory / "original.csv", original)
        _create_exact(directory / "operational.csv", operational)
        _create_exact(directory / "plan.json", _json(plan))
        _create_exact(directory / "prepared.json", _json(prepared))
        _, _, _, completed = _validate_archive(directory, path)
        if current == original:
            fd, temporary = tempfile.mkstemp(prefix=".journal-migration-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(operational)
                    handle.flush()
                    os.fsync(handle.fileno())
                if _read(path) != original:
                    raise JournalMigrationError("journal_changed_during_migration_never_overwrite")
                os.replace(temporary, path)
                _sync(path.parent)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if _read(path) != operational:
            raise JournalMigrationError("journal_migration_readback_failed")
        _create_exact(directory / "completed.json", _json(completed))
        validate_journal_history(path)
        return {"status": "APPLIED_HISTORY_PRESERVED", **_summary(plan)}
