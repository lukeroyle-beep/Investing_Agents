"""User-run, one-time dedicated-key setup. Never submits or launches a worker."""
from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import stat
import sys
import warnings
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.autonomous_api import DirectDemoClient, DirectDemoError, verify_profile
from brokers.etoro.client import EtoroCredentials
from execution.domain import canonical_json, payload_hash
from execution.pilot_mandate import StandingPilotMandate, PilotMandateError
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT

CREDENTIAL_FILENAME = "demo-api-credentials.json"
MANDATE_FILENAME = "standing-mandate.json"


class PilotSetupError(ValueError):
    """Fixed error codes only: never include credentials or remote responses."""


def _owned_directory(path):
    path = Path(path).absolute()
    try:
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise PilotSetupError("existing_owned_0700_runtime_required")
    except OSError:
        raise PilotSetupError("existing_owned_0700_runtime_required") from None
    return path


def _read_private(path, *, max_bytes=65536):
    path = Path(path).absolute()
    _owned_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size > max_bytes):
                raise PilotSetupError("owned_0600_regular_private_file_required")
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise PilotSetupError("private_file_size_exceeded")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise PilotSetupError("private_json_duplicate_key")
                result[key] = value
            return result
        return json.loads(data, object_pairs_hook=unique)
    except PilotSetupError:
        raise
    except (OSError, ValueError, TypeError):
        raise PilotSetupError("private_file_missing_or_invalid") from None


def _credentials(value):
    if (not isinstance(value, dict) or set(value) != {"api_key", "user_key"}
            or any(not isinstance(key, str) or not key.strip() or len(key) > 4096
                   or any(c in key for c in ("\r", "\n", "\x00")) for key in value.values())):
        raise PilotSetupError("dedicated_credentials_invalid")
    return EtoroCredentials(api_key=value["api_key"], user_key=value["user_key"])


def load_dedicated_credentials(path):
    """Load only the explicitly selected owner-only dedicated file; no discovery."""
    return _credentials(_read_private(path, max_bytes=16384))


def read_hidden_credentials():
    """Refuse terminal fallback rather than risking echoed secret input."""
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise PilotSetupError("interactive_hidden_terminal_required")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = getpass.getpass("Dedicated eToro Demo API key (hidden): ")
            user_key = getpass.getpass("Dedicated eToro Demo user key (hidden): ")
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        raise PilotSetupError("hidden_credential_entry_cancelled_or_unavailable") from None
    return _credentials({"api_key": api_key, "user_key": user_key})


def _write_credentials(path, credentials):
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise PilotSetupError("dedicated_credentials_already_exist_never_overwrite") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json({"api_key": credentials.api_key, "user_key": credentials.user_key}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _existing_pilot(root):
    """Read authoritative state only while the operator lock is held."""
    marker = _read_private(root / "pilot-inception.json")
    if (not isinstance(marker, dict) or set(marker) != {"policy", "account_id", "inception_hash"}
            or marker.get("policy") != "demo-pilot-v1"):
        raise PilotSetupError("existing_pilot_inception_marker_required")
    ledger = PilotLedger(root / "pilot.sqlite3", expected_account_id=marker["account_id"])
    state = ledger.snapshot()
    if payload_hash(state["inception"]) != marker["inception_hash"]:
        raise PilotSetupError("existing_pilot_inception_marker_mismatch")
    if state["submissions"] or state["positions"] or state["unresolved"] or state["loss_stopped"]:
        raise PilotSetupError("pilot_already_attempted_or_held_lookup_only")
    return marker, ledger


def _setup_switch_status(switch, *, activate):
    exists = switch.state_path.exists() or switch.audit_path.exists()
    if not exists:
        return None
    status = switch.status()
    if not status.writes_allowed:
        raise PilotSetupError("existing_engaged_or_invalid_kill_switch_requires_review")
    if not activate:
        raise PilotSetupError("existing_writable_kill_switch_requires_explicit_activation")
    return status


def setup(*, runtime, authorization_reference, activate=False, resume_activation=False,
          credential_reader=read_hidden_credentials, client_factory=DirectDemoClient,
          clock=lambda: datetime.now(timezone.utc)):
    """Verify read-only identity, preserve inception, and optionally enable its switch.

    Dependency injection exists for synthetic tests. The CLI always uses hidden
    terminal input and the pinned direct /me client; it has no credential flags.
    """
    root = _owned_directory(runtime)
    if not isinstance(authorization_reference, str) or not authorization_reference.strip():
        raise PilotSetupError("explicit_standing_authorization_reference_required")
    if type(activate) is not bool or type(resume_activation) is not bool or (resume_activation and not activate):
        raise PilotSetupError("explicit_activation_resume_selection_required")
    credentials_path, mandate_path = root / CREDENTIAL_FILENAME, root / MANDATE_FILENAME
    switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
    # Share the existing operator's lock. Setup never races an active runner.
    descriptor = os.open(root / "operator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        info = os.fstat(lock.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise PilotSetupError("owned_operator_lock_required")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotSetupError("pilot_operator_is_active") from None
        _owned_directory(root)
        marker, ledger = _existing_pilot(root)
        if resume_activation:
            if not credentials_path.exists() or not mandate_path.exists():
                raise PilotSetupError("partial_setup_preserved_requires_review")
        elif credentials_path.exists() or mandate_path.exists():
            raise PilotSetupError("setup_exists_use_explicit_resume_activation")
        prior_switch = _setup_switch_status(switch, activate=activate)
        if resume_activation:
            credentials = load_dedicated_credentials(credentials_path)
            mandate = StandingPilotMandate.load(mandate_path, expected_account_id=marker["account_id"])
            if (mandate.source_reference != authorization_reference
                    or mandate.ledger_path != ledger.path.resolve()
                    or mandate.ledger_inception_hash != marker["inception_hash"]):
                raise PilotSetupError("resumed_standing_authorization_mismatch")
        else:
            credentials = credential_reader()
            if not isinstance(credentials, EtoroCredentials):
                raise PilotSetupError("dedicated_injected_credentials_required")
        client = client_factory(credentials=credentials, receipt_dir=root / "direct-api-captures", clock=clock)
        # The only external setup operation is an allowlisted read of /me.
        verify_profile(client.get_profile(), marker["account_id"])
        current_marker, ledger = _existing_pilot(root)
        if current_marker != marker:
            raise PilotSetupError("existing_pilot_inception_changed_during_setup")
        if _setup_switch_status(switch, activate=activate) != prior_switch:
            raise PilotSetupError("kill_switch_changed_during_setup_requires_review")
        if not resume_activation:
            mandate = StandingPilotMandate.create(mandate_path, account_id=marker["account_id"],
                source_reference=authorization_reference, issued_at=clock(), ledger=ledger)
            _write_credentials(credentials_path, credentials)
        # A revocation arriving while identity or artifacts were being written
        # must remain authoritative; activation never resets preexisting state.
        if _setup_switch_status(switch, activate=activate) != prior_switch:
            raise PilotSetupError("kill_switch_changed_during_setup_requires_review")
        if activate:
            # A wholly absent switch may be initialized for this exact human
            # mandate. Existing engaged/corrupt state always requires review.
            if prior_switch is None:
                initialized = switch.initialize_engaged(operator_id="standing-mandate:" + mandate.mandate_id,
                    reason="standing mandate activation: " + mandate.mandate_hash, now=clock())
                if switch.status() != initialized:
                    raise PilotSetupError("kill_switch_changed_during_setup_requires_review")
                switch.reset(operator_id="standing-mandate:" + mandate.mandate_id,
                    reason="explicit standing mandate activation: " + mandate.mandate_hash,
                    acknowledgement=RESET_ACKNOWLEDGEMENT, now=clock())
            mandate.validate(ledger=ledger, kill_switch=switch, now=clock())
        return {"status": "SETUP_SAVED_WRITES_ENABLED" if activate else "SETUP_SAVED_ACTIVATION_PENDING",
                "mandate_hash": mandate.mandate_hash, "worker_started": False, "order_submitted": False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Never echo an accidentally supplied secret from unsupported argv.
        raise PilotSetupError("invalid_setup_arguments")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True, help="Existing private pilot runtime; never initialized here")
    parser.add_argument("--authorization-reference", required=True, help="Current human standing-mandate source reference, never a credential")
    parser.add_argument("--activate", action="store_true", help="Explicitly enable a newly initialized Demo kill switch for this mandate")
    parser.add_argument("--resume-activation", action="store_true", help="Use existing saved credentials/mandate without overwriting or prompting")
    try:
        result = setup(**vars(parser.parse_args(argv)))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, (PilotSetupError, DirectDemoError, PilotMandateError)) else "setup_not_verified_private_evidence_preserved"
        print(canonical_json({"status":"SETUP_BLOCKED", "reason":reason,
                              "worker_started":False, "order_submitted":False}), file=sys.stderr)
        return 1
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
