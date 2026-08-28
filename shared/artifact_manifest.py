from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from shared.paths import RUNTIME_RUNS_DIR, RUNTIME_STATE_DIR, run_path
from shared.schema_registry import FileSchemaRegistryEntry, get_file_schema


MANIFEST_VERSION = "1.0"
MANIFEST_FILE_NAME = "artifact_manifest.json"
PRE_ECONOMIC_CHECKSUMS_FILE_NAME = "pre_economic_state.json"

REQUIRED_ARTIFACTS = [
    "portfolio_state.csv",
    "portfolio_monitor.csv",
    "position_alerts.csv",
    "cash_state.csv",
    "cash_ledger.csv",
    "trade_fills.csv",
    "processed_fills.csv",
    "portfolio_equity_history.csv",
    "performance_summary.csv",
    "lifecycle_integrity_report.csv",
    "data_source_health.csv",
    "event_log.csv",
    "run_history.csv",
    "run_reconciliation_summary.csv",
]

ECONOMIC_ARTIFACTS = [
    "portfolio_state.csv",
    "cash_state.csv",
    "cash_ledger.csv",
    "trade_fills.csv",
    "processed_fills.csv",
]

_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ArtifactValidationError(RuntimeError):
    pass


def validate_run_id(run_id: object) -> str:
    normalised = str(run_id).strip()
    if not _SAFE_RUN_ID.fullmatch(normalised):
        raise ArtifactValidationError(
            "run_id must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return normalised


@dataclass(frozen=True)
class ArtifactRecord:
    relative_path: str
    producer: str
    schema_version: str
    produced_at_utc: str
    sha256: str
    row_count: int
    required: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "producer": self.producer,
            "schema_version": self.schema_version,
            "produced_at_utc": self.produced_at_utc,
            "sha256": self.sha256,
            "row_count": self.row_count,
            "required": self.required,
        }


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw_temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}.",
        suffix=".json.tmp",
    )
    temp_path = Path(raw_temp_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        path.chmod(0o600)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"Cannot read valid JSON artifact {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"JSON artifact {path.name} must contain an object")
    return payload


def _validate_dataframe_schema(
    file_name: str,
    frame: pd.DataFrame,
    schema: FileSchemaRegistryEntry,
) -> None:
    missing = sorted(set(schema.required_columns) - set(frame.columns))
    if missing:
        raise ArtifactValidationError(
            f"{file_name} is missing required schema columns: {missing}"
        )

    for column in schema.non_nullable_columns():
        if column not in frame.columns or frame.empty:
            continue
        values = frame[column]
        invalid = values.isna() | values.astype(str).str.strip().eq("")
        if invalid.any():
            raise ArtifactValidationError(
                f"{file_name} contains blank non-nullable field {column}"
            )

    numeric_types = {"int", "integer", "float", "number", "numeric"}
    for column, expected_type in schema.expected_types.items():
        if column not in frame.columns or frame.empty:
            continue
        if expected_type.strip().lower() in numeric_types:
            non_blank = frame[column].notna() & frame[column].astype(str).str.strip().ne("")
            if non_blank.any() and pd.to_numeric(frame.loc[non_blank, column], errors="coerce").isna().any():
                raise ArtifactValidationError(
                    f"{file_name} contains malformed numeric field {column}"
                )


def validate_csv_artifact(path: Path, *, required: bool = True) -> ArtifactRecord:
    if not path.exists():
        if required:
            raise ArtifactValidationError(f"Required artifact is missing: {path.name}")
        return ArtifactRecord(
            relative_path=path.name,
            producer="",
            schema_version="",
            produced_at_utc="",
            sha256="",
            row_count=0,
            required=False,
        )
    if not path.is_file() or path.stat().st_size == 0:
        raise ArtifactValidationError(f"Artifact is not a non-empty regular file: {path.name}")

    schema = get_file_schema(path.name)
    try:
        frame = pd.read_csv(path, keep_default_na=False)
    except Exception as exc:
        raise ArtifactValidationError(f"Cannot parse required CSV {path.name}: {exc}") from exc
    _validate_dataframe_schema(path.name, frame, schema)

    produced_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    return ArtifactRecord(
        relative_path=f"state/{path.name}",
        producer=schema.owner_agent,
        schema_version=schema.schema_version,
        produced_at_utc=produced_at,
        sha256=sha256_file(path),
        row_count=len(frame),
        required=required,
    )


def capture_economic_checksums(
    state_dir: Path = RUNTIME_STATE_DIR,
    names: Iterable[str] = ECONOMIC_ARTIFACTS,
) -> dict[str, str | None]:
    return {
        name: sha256_file(state_dir / name) if (state_dir / name).is_file() else None
        for name in names
    }


def capture_pre_economic_state(
    run_id: str,
    *,
    state_dir: Path = RUNTIME_STATE_DIR,
    runs_dir: Path = RUNTIME_RUNS_DIR,
    now_func: Callable[[], str] = utc_now_iso,
) -> Path:
    run_id = validate_run_id(run_id)
    target = runs_dir / run_id / PRE_ECONOMIC_CHECKSUMS_FILE_NAME
    if target.exists():
        raise ArtifactValidationError(
            f"Pre-run economic checksum record already exists for run_id={run_id}"
        )
    payload = {
        "record_version": "1.0",
        "run_id": run_id,
        "captured_at_utc": now_func(),
        "checksums": capture_economic_checksums(state_dir),
    }
    _atomic_write_json(target, payload)
    return target


def load_pre_economic_checksums(
    run_id: str,
    *,
    runs_dir: Path = RUNTIME_RUNS_DIR,
) -> dict[str, str | None]:
    run_id = validate_run_id(run_id)
    path = runs_dir / run_id / PRE_ECONOMIC_CHECKSUMS_FILE_NAME
    payload = _read_json_object(path)
    if payload.get("run_id") != run_id or not isinstance(payload.get("checksums"), dict):
        raise ArtifactValidationError(
            f"Invalid pre-run economic checksum record for run_id={run_id}"
        )
    return {str(key): value for key, value in payload["checksums"].items()}


def build_artifact_manifest(
    run_id: str,
    *,
    pre_economic_checksums: dict[str, str | None],
    state_dir: Path = RUNTIME_STATE_DIR,
    artifact_names: Iterable[str] = REQUIRED_ARTIFACTS,
    validation_checks: Iterable[str] = (),
    now_func: Callable[[], str] = utc_now_iso,
) -> dict[str, Any]:
    run_id = validate_run_id(run_id)
    records = [
        validate_csv_artifact(state_dir / file_name, required=True)
        for file_name in artifact_names
    ]
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "generated_at_utc": now_func(),
        "artifacts": [record.as_dict() for record in records],
        "pre_economic_state_checksums": dict(pre_economic_checksums),
        "post_economic_state_checksums": capture_economic_checksums(state_dir),
        "terminal_validation": {
            "result": "passed",
            "checks": list(validation_checks),
            "validated_at_utc": now_func(),
        },
    }


def write_artifact_manifest(
    manifest: dict[str, Any],
    *,
    runs_dir: Path = RUNTIME_RUNS_DIR,
) -> Path:
    run_id = validate_run_id(manifest.get("run_id", ""))
    target = runs_dir / run_id / MANIFEST_FILE_NAME
    _atomic_write_json(target, manifest)
    return target


def validate_artifact_manifest(
    path: Path,
    *,
    state_dir: Path = RUNTIME_STATE_DIR,
) -> dict[str, Any]:
    manifest = _read_json_object(path)
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ArtifactValidationError("Unsupported artifact manifest version")
    run_id = validate_run_id(manifest.get("run_id", ""))
    if path.parent.name != run_id:
        raise ArtifactValidationError("Artifact manifest directory does not match run_id")
    terminal = manifest.get("terminal_validation")
    if not isinstance(terminal, dict) or terminal.get("result") != "passed":
        raise ArtifactValidationError("Artifact manifest lacks a passed terminal validation")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ArtifactValidationError("Artifact manifest artifacts must be a list")
    paths = [
        str(item.get("relative_path", ""))
        for item in artifacts
        if isinstance(item, dict)
    ]
    if len(paths) != len(set(paths)):
        raise ArtifactValidationError("Artifact manifest contains duplicate artifact paths")
    required_paths = {f"state/{file_name}" for file_name in REQUIRED_ARTIFACTS}
    if set(paths) != required_paths:
        missing = sorted(required_paths - set(paths))
        extra = sorted(set(paths) - required_paths)
        raise ArtifactValidationError(
            f"Artifact manifest required set mismatch: missing={missing}, extra={extra}"
        )
    for item in artifacts:
        if not isinstance(item, dict):
            raise ArtifactValidationError("Artifact manifest contains a malformed artifact entry")
        relative_path = str(item.get("relative_path", ""))
        if not relative_path.startswith("state/") or Path(relative_path).name != relative_path[6:]:
            raise ArtifactValidationError(f"Unsafe artifact relative path: {relative_path}")
        current = validate_csv_artifact(
            state_dir / Path(relative_path).name,
            required=bool(item.get("required", False)),
        )
        schema = get_file_schema(Path(relative_path).name)
        if item.get("producer") != schema.owner_agent:
            raise ArtifactValidationError(
                f"Artifact producer mismatch: {Path(relative_path).name}"
            )
        if item.get("schema_version") != schema.schema_version:
            raise ArtifactValidationError(
                f"Artifact schema version mismatch: {Path(relative_path).name}"
            )
        if item.get("required") is not True:
            raise ArtifactValidationError(
                f"Required artifact is not marked required: {Path(relative_path).name}"
            )
        if current.sha256 != item.get("sha256"):
            raise ArtifactValidationError(
                f"Artifact checksum mismatch after manifest creation: {Path(relative_path).name}"
            )
        if current.row_count != item.get("row_count"):
            raise ArtifactValidationError(
                f"Artifact row-count mismatch after manifest creation: {Path(relative_path).name}"
            )

    expected_post = manifest.get("post_economic_state_checksums")
    if not isinstance(expected_post, dict):
        raise ArtifactValidationError("Manifest lacks post-economic checksums")
    if capture_economic_checksums(state_dir) != expected_post:
        raise ArtifactValidationError("Post-economic state checksums do not match the manifest")
    expected_pre = manifest.get("pre_economic_state_checksums")
    if not isinstance(expected_pre, dict) or set(expected_pre) != set(ECONOMIC_ARTIFACTS):
        raise ArtifactValidationError("Manifest lacks complete pre-economic checksums")
    return manifest


def default_manifest_path(run_id: str) -> Path:
    return run_path(run_id, MANIFEST_FILE_NAME)
