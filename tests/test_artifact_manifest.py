from __future__ import annotations

import json

import pytest

from shared.artifact_manifest import (
    ArtifactValidationError,
    validate_artifact_manifest,
    validate_run_id,
)
from shared.run_finalizer import (
    RunFinalizationError,
    redact_failure_message,
    validate_finalization_record,
)
from shared.runtime_bootstrap import BOOTSTRAP_RUN_ID, bootstrap_runtime


def test_bootstrap_manifest_and_finalization_record_validate(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")

    manifest = validate_artifact_manifest(
        result.manifest_path,
        state_dir=result.runtime_dir / "state",
    )
    finalization = validate_finalization_record(
        result.finalization_record_path,
        state_dir=result.runtime_dir / "state",
    )

    assert manifest["run_id"] == BOOTSTRAP_RUN_ID
    assert finalization.run_id == BOOTSTRAP_RUN_ID
    assert finalization.outcome == "succeeded"


def test_artifact_manifest_detects_post_production_tampering(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")
    cash_path = result.runtime_dir / "state" / "cash_state.csv"
    cash_path.write_text(
        cash_path.read_text(encoding="utf-8").replace("100000.0", "999999.0"),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactValidationError, match="checksum mismatch"):
        validate_artifact_manifest(
            result.manifest_path,
            state_dir=result.runtime_dir / "state",
        )

    with pytest.raises(RunFinalizationError, match="checksum mismatch"):
        validate_finalization_record(
            result.finalization_record_path,
            state_dir=result.runtime_dir / "state",
        )


def test_manifest_rejects_missing_required_artifact_and_reduced_required_set(tmp_path) -> None:
    result = bootstrap_runtime(tmp_path / "runtime")
    (result.runtime_dir / "state" / "trade_fills.csv").unlink()

    with pytest.raises(ArtifactValidationError, match="Required artifact is missing"):
        validate_artifact_manifest(
            result.manifest_path,
            state_dir=result.runtime_dir / "state",
        )

    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"] = payload["artifacts"][:-1]
    result.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ArtifactValidationError, match="required set mismatch"):
        validate_artifact_manifest(
            result.manifest_path,
            state_dir=result.runtime_dir / "state",
        )


@pytest.mark.parametrize("run_id", ["../escape", "/absolute", "has space", ""])
def test_manifest_run_id_rejects_path_traversal(run_id: str) -> None:
    with pytest.raises(ArtifactValidationError):
        validate_run_id(run_id)


def test_finalization_failure_redaction_handles_headers_and_json() -> None:
    message = redact_failure_message(
        'request failed: x-user-key=alpha {"x-api-key":"beta"} token:gamma'
    )
    assert "alpha" not in message
    assert "beta" not in message
    assert "gamma" not in message
    assert message.count("[REDACTED]") == 3
