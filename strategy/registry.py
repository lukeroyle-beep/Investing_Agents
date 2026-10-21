from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from strategy.domain import (
    ExperimentMetrics,
    ExperimentSpec,
    PromotionDecision,
    canonical_json,
)


class ExperimentRegistryError(RuntimeError):
    pass


STABLE_REPLAY_ARTIFACTS = frozenset({"historical_prices.csv", "walk_forward.csv"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _verified_artifact(path: Path, spec: ExperimentSpec) -> dict:
    """Read an immutable manifest and verify every copied evidence file."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            payload.get("experiment_key") != spec.experiment_key
            or payload.get("experiment_id") != str(spec.experiment_id)
            or canonical_json(payload.get("spec")) != canonical_json(spec)
        ):
            raise ExperimentRegistryError("experiment artifact identity collision")
        if not isinstance(payload.get("metrics"), dict):
            raise ExperimentRegistryError("experiment artifact metrics are missing")
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise ExperimentRegistryError("experiment artifact evidence list is missing")
        names: set[str] = set()
        for item in artifacts:
            name = item["file"]
            if (
                not isinstance(name, str)
                or not name
                or Path(name).name != name
                or name in {".", "..", "experiment.json"}
                or name in names
            ):
                raise ExperimentRegistryError("invalid experiment evidence filename")
            names.add(name)
            evidence = path.parent / name
            if not evidence.is_file() or sha256_file(evidence) != item["sha256"]:
                raise ExperimentRegistryError(f"experiment evidence integrity failed: {name}")
        return payload
    except ExperimentRegistryError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        raise ExperimentRegistryError("experiment artifact is missing or malformed") from exc


def _evidence_hashes(payload: dict) -> dict[str, str]:
    return {item["file"]: item["sha256"] for item in payload["artifacts"]}


def write_experiment_artifact(
    root: Path | str,
    *,
    spec: ExperimentSpec,
    metrics: ExperimentMetrics,
    source_artifacts: tuple[Path, ...] = (),
) -> Path:
    directory = Path(root) / str(spec.experiment_id)
    manifest_path = directory / "experiment.json"
    if manifest_path.exists():
        existing = _verified_artifact(manifest_path, spec)
        if canonical_json(existing["metrics"]) != canonical_json(metrics):
            raise ExperimentRegistryError("immutable experiment metrics changed")
        if source_artifacts:
            hashes = _evidence_hashes(existing)
            names = [source.name for source in source_artifacts]
            if len(names) != len(set(names)) or set(names) != set(hashes):
                raise ExperimentRegistryError("immutable experiment source artifacts changed")
            for source in source_artifacts:
                if not source.is_file() or sha256_file(source) != hashes[source.name]:
                    raise ExperimentRegistryError(f"immutable experiment source changed: {source.name}")
        return manifest_path
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    copied: list[dict[str, str]] = []
    try:
        for source in source_artifacts:
            if not source.is_file():
                raise ExperimentRegistryError(f"missing source artifact: {source.name}")
            if source.name == "experiment.json" or any(item["file"] == source.name for item in copied):
                raise ExperimentRegistryError("duplicate or reserved source artifact filename")
            destination = directory / source.name
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
            copied.append({"file": source.name, "sha256": sha256_file(destination)})
        payload = {
            "schema_version": "1.0",
            "experiment_id": str(spec.experiment_id),
            "experiment_key": spec.experiment_key,
            "spec": json.loads(canonical_json(spec)),
            "metrics": json.loads(canonical_json(metrics)),
            "artifacts": copied,
        }
        _atomic_json(manifest_path, payload)
        return manifest_path
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


class ExperimentRegistry:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    experiment_key TEXT NOT NULL UNIQUE,
                    strategy_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promotions (
                    promotion_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    capability TEXT NOT NULL CHECK(capability = 'advisory_only'),
                    operator_id TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiments(experiment_id)
                );
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def resolve_identity(self, spec: ExperimentSpec) -> ExperimentSpec:
        """Reuse the first identity for an identical experiment, across runs."""
        with closing(self._connect()) as connection:
            existing = connection.execute(
                "SELECT experiment_id, payload_json FROM experiments WHERE experiment_key = ?",
                (spec.experiment_key,),
            ).fetchone()
        if existing is None:
            return spec
        try:
            stored = json.loads(existing["payload_json"])
            resolved = replace(
                spec,
                experiment_id=UUID(existing["experiment_id"]),
                created_at=datetime.fromisoformat(stored["created_at"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ExperimentRegistryError("stored experiment identity is malformed") from exc
        if canonical_json(resolved) != existing["payload_json"]:
            raise ExperimentRegistryError("immutable experiment changed")
        return resolved

    def record(self, spec: ExperimentSpec, artifact_path: Path | str) -> None:
        artifact = Path(artifact_path)
        if not artifact.is_file():
            raise ExperimentRegistryError("experiment artifact is missing")
        new_artifact = _verified_artifact(artifact, spec)
        payload = canonical_json(spec)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM experiments WHERE experiment_key = ?",
                (spec.experiment_key,),
            ).fetchone()
            if existing:
                if existing["payload_json"] != payload:
                    raise ExperimentRegistryError("immutable experiment changed")
                previous_path = Path(existing["artifact_path"])
                if (
                    not previous_path.is_file()
                    or sha256_file(previous_path) != existing["artifact_sha256"]
                ):
                    raise ExperimentRegistryError("registered experiment artifact integrity failed")
                previous = _verified_artifact(previous_path, spec)
                if canonical_json(previous["metrics"]) != canonical_json(new_artifact["metrics"]):
                    raise ExperimentRegistryError("immutable experiment metrics changed")
                previous_hashes = {
                    name: digest for name, digest in _evidence_hashes(previous).items()
                    if name in STABLE_REPLAY_ARTIFACTS
                }
                new_hashes = {
                    name: digest for name, digest in _evidence_hashes(new_artifact).items()
                    if name in STABLE_REPLAY_ARTIFACTS
                }
                if previous_hashes != new_hashes:
                    raise ExperimentRegistryError("immutable experiment replay evidence changed")
                return
            connection.execute(
                """
                INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(spec.experiment_id),
                    spec.experiment_key,
                    spec.strategy_version,
                    payload,
                    str(artifact),
                    sha256_file(artifact),
                    datetime.now(UTC).isoformat(),
                ),
            )

    def record_promotion(
        self,
        *,
        spec: ExperimentSpec,
        decision: PromotionDecision,
        operator_id: str,
    ) -> UUID:
        operator = str(operator_id).strip()
        if not operator:
            raise ExperimentRegistryError("promotion requires operator identity")
        if decision.experiment_id != spec.experiment_id:
            raise ExperimentRegistryError("promotion does not bind to experiment")
        with closing(self._connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id = ?",
                (str(spec.experiment_id),),
            ).fetchone()
        if not exists:
            raise ExperimentRegistryError("promotion references unknown experiment")
        promotion_id = uuid4()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO promotions VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(promotion_id),
                    str(spec.experiment_id),
                    spec.strategy_version,
                    1 if decision.eligible else 0,
                    decision.capability,
                    operator,
                    json.dumps(decision.reasons),
                    decision.decided_at.isoformat(),
                ),
            )
        return promotion_id

    def latest_advisory_eligibility(self, strategy_version: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT eligible FROM promotions WHERE strategy_version = ?
                ORDER BY decided_at DESC, promotion_id DESC LIMIT 1
                """,
                (str(strategy_version),),
            ).fetchone()
        return bool(row["eligible"]) if row else False
