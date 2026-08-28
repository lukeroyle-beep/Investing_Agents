from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd
import yaml

from shared.run_context import get_or_create_run_id
from shared.schema_registry import get_file_schema, registry_contains
from shared.schemas import SchemaSpec, normalise_to_schema


class ManagedWriteError(RuntimeError):
    """Base error for schema-registry ownership enforcement."""


class UnknownManagedArtifactError(ManagedWriteError):
    """Raised when a managed write targets an unregistered artifact."""


class ProducerOwnershipError(ManagedWriteError):
    """Raised when a producer does not exactly match the registered owner."""


class ManagedSchemaMismatchError(ManagedWriteError):
    """Raised when a managed write supplies a conflicting schema."""


_WRITE_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: dict[Path, threading.RLock] = {}


def _write_lock_for(file_path: Path) -> threading.RLock:
    resolved_path = file_path.resolve()
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(resolved_path, threading.RLock())


def _validate_write_producer(file_path: Path, producer: str | None) -> None:
    file_name = file_path.name
    entry = get_file_schema(file_name) if registry_contains(file_name) else None

    if producer is None:
        if entry is not None and entry.owner_agent == "Fill Agent":
            raise ProducerOwnershipError(
                f"Managed economic artifact {file_name} requires producer='Fill Agent'"
            )
        return

    if entry is None:
        raise UnknownManagedArtifactError(
            f"Managed write rejected because {file_name} has no schema-registry owner"
        )

    if producer != entry.owner_agent:
        raise ProducerOwnershipError(
            f"Managed write rejected for {file_name}: producer={producer!r}, "
            f"registered_owner={entry.owner_agent!r}"
        )


def _validate_managed_schema(file_path: Path, schema: SchemaSpec) -> None:
    entry = get_file_schema(file_path.name)
    if list(schema.column_order) != list(entry.canonical_column_order):
        raise ManagedSchemaMismatchError(
            f"Managed write rejected for {file_path.name}: supplied schema does not "
            "match the registered canonical column order"
        )


def ensure_parent_dir(file_path: Path) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_replace(temp_path: Path, destination_path: Path) -> None:
    """
    Atomically replace a destination with a fully-written temp file.

    The temp file must live in the same directory so `os.replace` stays on the
    same filesystem. On Windows, replacement is atomic but can still fail if
    another process currently holds the destination open without shared delete
    permissions.
    """
    os.replace(temp_path, destination_path)


def _temp_path_for(destination_path: Path) -> tuple[int, Path]:
    fd, raw_path = tempfile.mkstemp(
        dir=destination_path.parent,
        prefix=f".{destination_path.stem}.",
        suffix=f"{destination_path.suffix}.tmp",
    )
    return fd, Path(raw_path)


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    output_df = df.copy()
    output_df.columns = [
        str(col).strip().lower().replace(" ", "_").replace("-", "_")
        for col in output_df.columns
    ]
    return output_df


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def read_csv_file(
    file_path: Path,
    required_columns: Optional[Iterable[str]] = None,
    empty_ok: bool = True,
    normalise: bool = False,
) -> pd.DataFrame:
    if not file_path.exists():
        if empty_ok:
            return pd.DataFrame()
        raise FileNotFoundError(f"CSV file not found: {file_path}")

    df = pd.read_csv(file_path)

    if normalise:
        df = normalise_columns(df)

    if required_columns:
        missing = [col for col in required_columns if col not in df.columns]
        if missing:
            raise ValueError(
                f"Missing required columns in {file_path.name}: {missing}"
            )

    return df


def read_csv(
    file_path: Path | str,
    required_columns: Optional[Iterable[str]] = None,
    empty_ok: bool = True,
    normalise: bool = False,
) -> pd.DataFrame:
    path = Path(file_path)
    return read_csv_file(
        file_path=path,
        required_columns=required_columns,
        empty_ok=empty_ok,
        normalise=normalise,
    )


def read_csv_optional(
    file_path: Path | str,
    normalise: bool = True,
) -> pd.DataFrame:
    path = Path(file_path)
    return read_csv_file(
        file_path=path,
        empty_ok=True,
        normalise=normalise,
    )


def read_csv_required(
    file_path: Path | str,
    required_columns: Optional[Iterable[str]] = None,
    normalise: bool = True,
) -> pd.DataFrame:
    path = Path(file_path)
    return read_csv_file(
        file_path=path,
        required_columns=required_columns,
        empty_ok=False,
        normalise=normalise,
    )


def read_csv_with_schema(
    file_path: Path | str,
    schema: SchemaSpec,
    empty_ok: bool = True,
) -> pd.DataFrame:
    path = Path(file_path)
    df = read_csv_file(path, empty_ok=empty_ok, normalise=False)
    return normalise_to_schema(df, schema)


# -----------------------------
# WRITE FUNCTIONS (BASE)
# -----------------------------

def write_csv_file(
    df: pd.DataFrame,
    file_path: Path,
    sort_columns: Optional[list[str]] = None,
    producer: str | None = None,
) -> None:
    _validate_write_producer(file_path, producer)
    with _write_lock_for(file_path):
        ensure_parent_dir(file_path)

        output_df = df.copy()

        if sort_columns:
            existing_sort_columns = [c for c in sort_columns if c in output_df.columns]
            if existing_sort_columns:
                output_df = output_df.sort_values(by=existing_sort_columns)

        fd, temp_path = _temp_path_for(file_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                output_df.to_csv(handle, index=False)
                handle.flush()
                os.fsync(handle.fileno())

            _atomic_replace(temp_path, file_path)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise


def write_csv(
    df: pd.DataFrame,
    file_path: Path | str,
    sort_columns: Optional[list[str]] = None,
    producer: str | None = None,
) -> None:
    path = Path(file_path)
    write_csv_file(
        df=df,
        file_path=path,
        sort_columns=sort_columns,
        producer=producer,
    )


# -----------------------------
# WRITE FUNCTIONS (SCHEMA ENFORCED)
# -----------------------------

def write_csv_with_schema(
    df: pd.DataFrame,
    file_path: Path | str,
    schema: SchemaSpec,
    sort_columns: Optional[list[str]] = None,
    keep_extra_columns: bool = False,
    producer: str | None = None,
) -> None:
    path = Path(file_path)

    output_df = normalise_to_schema(
        df=df,
        spec=schema,
        keep_extra_columns=keep_extra_columns,
    )

    write_csv_file(
        output_df,
        path,
        sort_columns=sort_columns,
        producer=producer,
    )


def write_managed_csv_with_schema(
    df: pd.DataFrame,
    file_path: Path | str,
    *,
    schema: SchemaSpec,
    producer: str,
    sort_columns: Optional[list[str]] = None,
    keep_extra_columns: bool = False,
) -> None:
    """Atomically write a registered artifact under its exact declared owner."""
    path = Path(file_path)
    _validate_write_producer(path, producer)
    _validate_managed_schema(path, schema)
    write_csv_with_schema(
        df,
        path,
        schema=schema,
        sort_columns=sort_columns,
        keep_extra_columns=keep_extra_columns,
        producer=producer,
    )


def add_run_id_column(
    df: pd.DataFrame,
    run_id: Optional[str] = None,
    column_name: str = "run_id",
    overwrite: bool = True,
) -> pd.DataFrame:
    output_df = df.copy()
    resolved_run_id = run_id or get_or_create_run_id()

    if overwrite or column_name not in output_df.columns:
        output_df[column_name] = resolved_run_id

    return output_df


def write_csv_with_run_id(
    df: pd.DataFrame,
    file_path: Path | str,
    sort_columns: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    column_name: str = "run_id",
    overwrite: bool = True,
    producer: str | None = None,
) -> None:
    path = Path(file_path)

    output_df = add_run_id_column(
        df=df,
        run_id=run_id,
        column_name=column_name,
        overwrite=overwrite,
    )

    write_csv_file(
        output_df,
        path,
        sort_columns=sort_columns,
        producer=producer,
    )


def write_csv_with_schema_and_run_id(
    df: pd.DataFrame,
    file_path: Path | str,
    schema: SchemaSpec,
    sort_columns: Optional[list[str]] = None,
    run_id: Optional[str] = None,
    column_name: str = "run_id",
    overwrite: bool = True,
    keep_extra_columns: bool = False,
    producer: str | None = None,
) -> None:
    path = Path(file_path)

    output_df = add_run_id_column(
        df=df,
        run_id=run_id,
        column_name=column_name,
        overwrite=overwrite,
    )

    output_df = normalise_to_schema(
        df=output_df,
        spec=schema,
        keep_extra_columns=keep_extra_columns,
    )

    write_csv_file(
        output_df,
        path,
        sort_columns=sort_columns,
        producer=producer,
    )


# -----------------------------
# YAML HELPERS
# -----------------------------

def load_yaml(file_path: Path | str, empty_ok: bool = False) -> dict[str, Any]:
    path = Path(file_path)

    if not path.exists():
        if empty_ok:
            return {}
        raise FileNotFoundError(f"YAML file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return data or {}


def save_yaml(data: dict[str, Any], file_path: Path | str) -> None:
    path = Path(file_path)
    ensure_parent_dir(path)

    fd, temp_path = _temp_path_for(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
            handle.flush()
            os.fsync(handle.fileno())

        _atomic_replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


# -----------------------------
# APPEND
# -----------------------------

def append_csv_file(
    new_rows_df: pd.DataFrame,
    file_path: Path | str,
    producer: str | None = None,
) -> pd.DataFrame:
    path = Path(file_path)
    _validate_write_producer(path, producer)
    with _write_lock_for(path):
        ensure_parent_dir(path)

        if path.exists():
            existing_df = pd.read_csv(path)
            combined_df = pd.concat([existing_df, new_rows_df], ignore_index=True)
        else:
            combined_df = new_rows_df.copy()

        write_csv_file(combined_df, path, producer=producer)
        return combined_df
