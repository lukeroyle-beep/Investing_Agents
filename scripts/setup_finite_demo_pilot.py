"""Save an explicit finite Demo authority against the original private runtime.

This setup does not request credentials, reset a kill switch, start a worker or
send an order. An incomplete setup is preserved for review, never overwritten.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.fill_agent.pilot_ledger import PilotLedger
from agents.journal_agent.execution_journal import ExecutionJournal
from execution.domain import canonical_json, payload_hash
from execution.finite_demo_mandate import FiniteDemoMandate, validate_scope
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitch
from scripts.run_autonomous_demo_pilot import verify_reviewed_checkout
from scripts.setup_autonomous_demo_pilot import _owned_directory, _read_private, PilotSetupError

MANDATE_FILE = "finite-mandate.json"
CATALOG_FILE = "finite-catalog.json"
JOURNAL_FILE = "execution-journal.sqlite3"


@contextmanager
def operator_lock(root):
    """Use the original operator lock without following a replacement symlink."""
    descriptor = os.open(root / "operator.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise PilotSetupError("owned_0600_operator_lock_required")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotSetupError("pilot_operator_is_active") from None
        yield


def existing_runtime(runtime):
    """Read original state; opening this function cannot create a replacement."""
    root = _owned_directory(runtime)
    for name in ("pilot.sqlite3", "execution.sqlite3", "operator.sqlite3"):
        path = root / name
        try:
            info = path.lstat()
        except OSError:
            raise PilotSetupError("existing_pilot_state_missing_never_initialize") from None
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise PilotSetupError("existing_owned_private_pilot_state_required")
    marker = _read_private(root / "pilot-inception.json")
    if (not isinstance(marker, dict) or set(marker) != {"policy", "account_id", "inception_hash"}
            or marker["policy"] != "demo-pilot-v1"):
        raise PilotSetupError("original_pilot_inception_marker_required")
    ledger = PilotLedger(root / "pilot.sqlite3", expected_account_id=marker["account_id"])
    snapshot = ledger.snapshot()
    if (snapshot["account_id"] != marker["account_id"]
            or payload_hash(snapshot["inception"]) != marker["inception_hash"]):
        raise PilotSetupError("original_pilot_inception_marker_mismatch")
    return root, ledger


def classifications(rows, scope, state):
    """Review inputs cover the whole account and bind every permitted entry."""
    keys = {"instrument_id", "symbol", "issuer", "sector", "asset_type", "source_reference"}
    if not isinstance(rows, list) or not rows:
        raise PilotSetupError("whole_account_catalog_required")
    items = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != keys
                or type(row["instrument_id"]) is not int or row["instrument_id"] < 1
                or any(not isinstance(row[k], str) or not row[k].strip() for k in keys - {"instrument_id"})
                or row["asset_type"] not in {"equity", "etf", "crypto"}
                or row["instrument_id"] in items):
            raise PilotSetupError("reviewed_catalog_classification_invalid")
        items[row["instrument_id"]] = dict(row)
    required = {int(row["instrument_id"]) for row in state["inception"]["account"]["positions"]}
    required.update(int(row["instrument_id"]) for row in state["submissions"])
    required.update(int(row["instrument_id"]) for row in state["positions"])
    for spec in scope["instruments"]:
        required.add(spec["instrument_id"])
        expected = {key: spec[key] for key in keys - {"source_reference"}}
        expected["source_reference"] = spec["class_source_reference"]
        if items.get(spec["instrument_id"]) != expected:
            raise PilotSetupError("catalog_mandate_classification_mismatch")
    if not required <= set(items):
        raise PilotSetupError("whole_account_classification_incomplete")
    return [items[key] for key in sorted(items)]


def _save(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_catalog(root, mandate, ledger):
    record = _read_private(root / CATALOG_FILE)
    if not isinstance(record, dict) or set(record) != {"catalog", "sha256"}:
        raise PilotSetupError("finite_catalog_record_invalid")
    body = record["catalog"]
    if (not isinstance(body, dict) or set(body) != {"schema", "mandate_hash", "reviewed_commit",
            "source_reference", "classifications"} or payload_hash(body) != record["sha256"]
            or body["schema"] != "finite-demo-catalog-v2"
            or body["mandate_hash"] != mandate.mandate_hash
            or body["reviewed_commit"] != mandate.reviewed_commit
            or body["source_reference"] != mandate.source_reference):
        raise PilotSetupError("finite_catalog_binding_mismatch")
    rows = classifications(body["classifications"], mandate.scope, ledger.snapshot())
    if rows != body["classifications"]:
        raise PilotSetupError("finite_catalog_not_canonical")
    return rows, record["sha256"]


def setup(*, runtime, authorization_reference, reviewed_commit, scope, catalog,
          issued_at, expires_at, checkout_verifier=verify_reviewed_checkout):
    checkout_verifier(reviewed_commit)
    root, _ = existing_runtime(runtime)
    with operator_lock(root):
        root, ledger = existing_runtime(root)
        state = ledger.snapshot()
        if state["unresolved"] or state["loss_stopped"]:
            raise PilotSetupError("existing_pilot_requires_reconciliation_before_expansion")
        switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
        before_switch = switch.status()
        if not before_switch.integrity_valid or not before_switch.engaged:
            raise PilotSetupError("finite_setup_requires_existing_engaged_kill_switch")
        if any((root / name).exists() for name in (MANDATE_FILE, CATALOG_FILE, JOURNAL_FILE)):
            raise PilotSetupError("finite_setup_exists_never_overwrite")
        normalized = validate_scope(scope)
        rows = classifications(catalog, normalized, state)
        # Authority is retained before schema migration; a crash never grants an
        # unrecorded extension, and retry cannot recreate an empty allowance.
        mandate = FiniteDemoMandate.create(root / MANDATE_FILE, account_id=state["account_id"],
            source_reference=authorization_reference, issued_at=issued_at, expires_at=expires_at,
            ledger=ledger, reviewed_commit=reviewed_commit, scope=normalized)
        body = {"schema": "finite-demo-catalog-v2", "mandate_hash": mandate.mandate_hash,
            "reviewed_commit": reviewed_commit, "source_reference": authorization_reference,
            "classifications": rows}
        _save(root / CATALOG_FILE, {"catalog": body, "sha256": payload_hash(body)})
        ledger.enable_finite_mandates(source_reference=authorization_reference,
            reviewed_commit=reviewed_commit, now=issued_at)
        store = ExecutionStore(root / "execution.sqlite3")
        ExecutionJournal.create(root / JOURNAL_FILE, ledger=ledger, store=store, runtime_root=root)
        if switch.status() != before_switch:
            raise PilotSetupError("kill_switch_changed_during_finite_setup")
        return {"status": "FINITE_SETUP_SAVED_SWITCH_REMAINS_ENGAGED", "mandate_hash": mandate.mandate_hash,
            "catalog_hash": payload_hash(body), "worker_started": False, "order_submitted": False}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PilotSetupError("invalid_finite_setup_arguments")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--reviewed-commit", required=True)
    parser.add_argument("--authorization-reference", required=True)
    parser.add_argument("--scope-file", type=Path, required=True,
        help="Owner-only JSON containing explicit scope, catalog, issued_at and expires_at")
    try:
        args = parser.parse_args(argv)
        request = _read_private(args.scope_file)
        if not isinstance(request, dict) or set(request) != {"scope", "catalog", "issued_at", "expires_at"}:
            raise PilotSetupError("finite_scope_request_shape_invalid")
        result = setup(runtime=args.runtime, reviewed_commit=args.reviewed_commit,
            authorization_reference=args.authorization_reference, **request)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum() else "finite_setup_held"
        print(canonical_json({"status": "FINITE_SETUP_BLOCKED", "reason": reason,
            "worker_started": False, "order_submitted": False}), file=sys.stderr)
        return 1
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
