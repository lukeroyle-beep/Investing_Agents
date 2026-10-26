"""One-shot private Demo account capture; never creates intents or approvals."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brokers.etoro.capture_transport import RecordingCaptureTransport, private_json, sync_directory
from brokers.etoro.client import EtoroCredentials, EtoroDemoReadOnlyConfig, EtoroReadOnlyClient
from execution.domain import dataclass_json
from risk.pretrade import RiskLimits
from tools.demo_pnl_preflight import pnl_preflight_report
from tools.demo_account_preflight import UNPROVEN_GATES, preflight_report


class PrivateSetupError(ValueError):
    """Messages are fixed codes only; never include paths, values or exceptions."""


def _private_path(path: Path, *, directory=False) -> Path:
    runtime = PROJECT_ROOT / "runtime"
    candidate = path.absolute()
    try:
        relative = candidate.relative_to(runtime)
    except ValueError:
        raise PrivateSetupError("private_setup_must_be_under_runtime") from None
    if ".." in relative.parts:
        raise PrivateSetupError("private_setup_path_invalid")
    current = runtime
    for part in (None, *relative.parts):
        if part is not None:
            current = current / part
        if current.is_symlink():
            raise PrivateSetupError("private_setup_symlink_forbidden")
    info = candidate.stat()
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PrivateSetupError("private_setup_owner_only_required")
    return candidate


def _setup(config_path, identity_path, evidence_parent):
    config_path = _private_path(config_path)
    identity_path = _private_path(identity_path)
    evidence_parent = _private_path(evidence_parent, directory=True)
    config = EtoroDemoReadOnlyConfig.load(config_path)
    if (not config.enabled or config.base_url != "https://public-api.etoro.com"
            or config.max_read_retries != 0 or not math.isfinite(config.timeout_seconds)
            or not 0 < config.timeout_seconds <= 15
            or config.api_key_environment_variable != "ETORO_DEMO_API_KEY"
            or config.user_key_environment_variable != "ETORO_DEMO_USER_KEY"):
        raise PrivateSetupError("one_shot_demo_read_configuration_required")
    raw_config = yaml.safe_load(config_path.read_text())
    if raw_config.get("websocket_enabled") is not False:
        raise PrivateSetupError("websocket_must_remain_disabled")
    execution = yaml.safe_load((PROJECT_ROOT / "config/execution.yaml").read_text())
    scheduler = yaml.safe_load((PROJECT_ROOT / "config/scheduler.yaml").read_text())
    if (execution.get("intent_import_enabled") is not False
            or execution.get("broker_writes_enabled") is not False
            or execution.get("adapter") != "offline" or execution.get("environment") != "demo"
            or scheduler.get("enabled") is not False
            or scheduler.get("broker_submission_enabled") is not False
            or scheduler.get("environment") != "demo"):
        raise PrivateSetupError("execution_and_scheduling_must_remain_disabled")
    identity = json.loads(identity_path.read_text())
    expected = identity["expected_demo_customer_id"]
    if not isinstance(expected, str) or not expected.isascii() or not expected.isdecimal() or int(expected) <= 0:
        raise PrivateSetupError("independent_demo_identity_required")
    if not isinstance(identity.get("verification_source"), str) or not identity["verification_source"].strip():
        raise PrivateSetupError("independent_identity_provenance_required")
    verified = datetime.fromisoformat(identity["verified_at"])
    if verified.tzinfo is None or verified.utcoffset() is None or verified > datetime.now(timezone.utc):
        raise PrivateSetupError("independent_identity_verification_time_invalid")
    limits = RiskLimits.load(PROJECT_ROOT / "config/risk.yaml")
    # Only the exact workflow environment is read; no dotenv, Keychain or
    # connector credential discovery. Missing keys fail before creating output.
    if not all(os.environ.get(name, "").strip() for name in (
            "ETORO_DEMO_API_KEY", "ETORO_DEMO_USER_KEY")):
        raise PrivateSetupError("demo_read_credentials_missing")
    credentials = EtoroCredentials.from_environment(config)
    return config, credentials, expected, limits, evidence_parent, {
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "identity_provenance_sha256": hashlib.sha256(identity_path.read_bytes()).hexdigest(),
    }


def run_capture(*, config_path, identity_path, evidence_parent, attempt, transport=None, account_source="pnl"):
    """Separate broker-backed readiness path. Transport injection is for tests."""
    report = {"status": "BLOCKED", "submission_available": False,
              "preparation_available": False, "approval_available": False,
              "baseline_created": False, "broker_orders_submitted": 0,
              "broker_requests": 0, "qualification_credit": False,
              "blockers": list(UNPROVEN_GATES)}
    if account_source not in {"pnl", "aggregate"}:
        report["blockers"].insert(0, "invalid_account_source")
        return report
    if not attempt or len(attempt) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in attempt):
        report["blockers"].insert(0, "invalid_attempt_name")
        return report
    try:
        config, credentials, expected, limits, parent, provenance = _setup(
            config_path, identity_path, evidence_parent)
    except PrivateSetupError as exc:
        report["blockers"].insert(0, str(exc))
        return report
    except Exception:
        report["blockers"].insert(0, "private_setup_incomplete_or_invalid")
        return report
    destination = parent / attempt
    try:
        destination.mkdir(mode=0o700)  # Exclusive claim before any request.
        sync_directory(parent)
        private_json(destination / "setup-receipt.json", {
            **provenance, "started_at": datetime.now(timezone.utc).isoformat(),
            "identity_source": "independently_supplied_private_file",
            "max_read_retries": 0, "execution_enabled": False,
            "account_source": account_source,
        })
    except FileExistsError:
        report["blockers"].insert(0, "attempt_already_exists_no_retry")
        return report
    except OSError:
        report["blockers"].insert(0, "private_evidence_storage_unavailable")
        return report
    recorder = RecordingCaptureTransport(destination, transport=transport)
    client = EtoroReadOnlyClient(config=config, credentials=credentials, transport=recorder)
    if account_source == "pnl":
        capture = pnl_preflight_report(
            client=client, expected_demo_customer_id=expected, risk_limits=limits,
            evidence_sink=lambda evidence: private_json(
                destination / "account-evidence.json", json.loads(dataclass_json(evidence))),
        )
    else:
        capture = preflight_report(client=client, expected_demo_customer_id=expected, risk_limits=limits)
    report.update(capture)
    report["broker_requests"] = recorder.request_count
    responses = [json.loads(path.read_text()) for path in sorted(destination.glob("*-response.json"))]
    report["transport"] = {
        "requests_attempted": recorder.request_count,
        "responses_received": len(responses),
        "complete_http_200_responses": sum(row["status_code"] == 200 and row["read_outcome"] == "complete" for row in responses),
        "all_attempted_reads_http_200": bool(responses) and len(responses) == recorder.request_count and all(row["status_code"] == 200 and row["read_outcome"] == "complete" for row in responses),
    }
    try:
        private_json(destination / "readiness.json", report)
    except OSError:
        report["blockers"].insert(0, "private_report_storage_failed")
    # preflight has no success-to-intent conversion. No baseline argument is
    # passed and no ExecutionStore, ApprovalAuthority or write adapter is used.
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--evidence-parent", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--account-source", choices=("pnl", "aggregate"), default="pnl")
    args = parser.parse_args(argv)
    report = run_capture(config_path=args.config, identity_path=args.identity_file,
                         evidence_parent=args.evidence_parent, attempt=args.attempt, account_source=args.account_source)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 2  # A successful restricted capture still lacks preparation evidence.


if __name__ == "__main__":
    raise SystemExit(main())
