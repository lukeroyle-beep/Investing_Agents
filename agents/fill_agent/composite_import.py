"""Public exact-plan application and recovery for reviewed composite evidence.

The legacy all-event importer stays closed. This entry point supports only the
bounded historical net-effects source contract in composite_source. It cannot
reset a store, change the kill switch, create a submission or activate trading.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess

from agents.fill_agent import external_lifecycle as lifecycle
from agents.fill_agent.composite_source import build_composite_plan, require
from agents.fill_agent.external_reconciliation import _read, _json, _hash
from agents.fill_agent.pilot_ledger import PilotLedger
from agents.journal_agent.execution_journal import ExecutionJournal
from execution.domain import payload_hash
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitch


def _require_reviewed_checkout(commit):
    require(isinstance(commit, str) and len(commit) == 40 and all(c in "0123456789abcdef" for c in commit),
            "reviewed_commit_required")
    root = Path(__file__).resolve().parents[2]
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                               check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        require(False, "reviewed_checkout_unavailable")
    require(head == commit and not dirty, "clean_exact_reviewed_checkout_required")


def _stores(runtime):
    root = Path(runtime)
    require(root.is_absolute() and root.resolve() == root, "canonical_runtime_path")
    marker = _json(_read(root / "pilot-inception.json"))
    require(marker.get("policy") == "demo-pilot-v1", "original_inception_policy")
    # Constructors must not bootstrap a missing operational or economic store.
    for name in ("pilot.sqlite3", "execution.sqlite3", "operator.sqlite3", "connector.sqlite3"):
        _read(root / name)
    switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
    status = switch.status()
    require(status.integrity_valid and status.engaged, "existing_engaged_switch_required")
    ledger = PilotLedger(root / "pilot.sqlite3", expected_account_id=marker["account_id"])
    require(payload_hash(ledger.snapshot()["inception"]) == marker["inception_hash"], "original_inception_binding")
    return root, ledger, switch


def _journal(root, ledger, *, allow_create):
    # The normal ExecutionStore constructor bootstraps/migrates schemas and
    # enables WAL. Historical reconciliation must never invoke that path.
    execution_path = root / "execution.sqlite3"
    raw = _read(execution_path)
    require(len(raw) >= 100 and raw.startswith(b"SQLite format 3\x00"), "existing_execution_store_invalid")
    for suffix in ("-wal", "-journal"):
        path = Path(str(execution_path) + suffix)
        require(not path.exists() or not _read(path), "execution_store_uncheckpointed_state")
    try:
        with closing(sqlite3.connect(execution_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "execution_store_corrupt")
            required = {"order_intents": {"intent_id", "intent_hash", "payload_json"},
                        "broker_commands": {"command_id", "intent_hash", "state", "revision", "payload_json"},
                        "command_events": {"event_id", "command_id", "revision", "details_json"}}
            for table, columns in required.items():
                require(columns <= {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")},
                        "existing_execution_schema_required")
    except sqlite3.Error:
        require(False, "existing_execution_store_invalid")
    require(_read(execution_path) == raw, "execution_store_changed_during_validation")
    store = object.__new__(ExecutionStore)
    store.path = execution_path
    path = root / "execution-journal.sqlite3"
    require(not path.is_symlink(), "journal_symlink_never_recreate")
    if path.exists():
        _read(path)
        return ExecutionJournal(path, ledger=ledger, store=store, runtime_root=root)
    require(allow_create, "journal_missing_never_recreate")
    return ExecutionJournal.create(path, ledger=ledger, store=store, runtime_root=root)


def _old_rows(ledger):
    with closing(ledger._connect()) as db:
        return {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("inception", "submissions", "events")}


def apply_composite_import(*, runtime, manifest, expected_manifest_sha256, expected_plan_sha256,
                           reviewed_commit, review_reference, now):
    """Apply the independently reviewed exact historical plan once.

    A stopped original runtime and its engaged switch are prerequisites. A
    failed response after economic commit must use recover_composite_import;
    rebuilding a changed source plan or repeating economic application is not
    recovery. The durable ledger event carries the complete safe source bundle.
    """
    _require_reviewed_checkout(reviewed_commit)
    lifecycle._sha(expected_manifest_sha256)
    lifecycle._sha(expected_plan_sha256)
    require(_hash(_read(Path(manifest))) == expected_manifest_sha256, "reviewed_manifest_changed")
    proof, record, report = build_composite_plan(runtime=runtime, manifest=manifest,
        reviewed_commit=reviewed_commit, review_reference=review_reference, now=now)
    require(report["manifest_sha256"] == expected_manifest_sha256
            and payload_hash(record) == expected_plan_sha256, "exact_reviewed_plan_changed")
    root, ledger, switch = _stores(runtime)
    old = _old_rows(ledger)
    with lifecycle._original_lock(ledger, switch):
        journal = _journal(root, ledger, allow_create=True)
    committed = lifecycle._apply_qualified(ledger, proof, expected_plan_hash=expected_plan_sha256,
        reviewed_commit=reviewed_commit, review_reference=review_reference, kill_switch=switch,
        now=now, historical_accounting=True)
    with lifecycle._original_lock(ledger, switch):
        terminal = lifecycle.recover_external_journal(ledger, journal, event_id=committed["event_id"], now=now)
        after = _old_rows(ledger)
        require(all(after[name][:len(rows)] == rows for name, rows in old.items())
                and after["inception"] == old["inception"] and after["submissions"] == old["submissions"],
                "historical_rows_changed")
    snapshot = ledger.snapshot()
    require(not snapshot["unresolved"] and switch.status().engaged, "post_import_still_held")
    return {**report, "status": "COMPOSITE_NET_RECONCILIATION_APPLIED", "applied": True,
            "original_bytes_unchanged": False, "source_snapshot_matches_reviewed_plan": True,
            "event_id": committed["event_id"], "journal_chain_hash": terminal["chain_hash"],
            "journal_complete": True, "historical_rows_unchanged": True,
            "new_submission_claims": 0, "trading_ready": False, "kill_switch_engaged": True}


def recover_composite_import(*, runtime, event_id, expected_plan_sha256, reviewed_commit, now):
    """Lookup-only local journal recovery; never resend or reapply economics."""
    _require_reviewed_checkout(reviewed_commit)
    lifecycle._sha(event_id)
    lifecycle._sha(expected_plan_sha256)
    root, ledger, switch = _stores(runtime)
    receipt = ledger.external_lifecycle_receipt(event_id)
    require(receipt["record"]["schema"] == lifecycle.COMPOSITE_SCHEMA
            and payload_hash(receipt["record"]) == expected_plan_sha256
            and receipt["record"]["reviewed_commit"] == reviewed_commit, "recovery_exact_existing_record_required")
    with lifecycle._original_lock(ledger, switch):
        journal = _journal(root, ledger, allow_create=False)
        terminal = lifecycle.recover_external_journal(ledger, journal, event_id=event_id, now=now)
    return {"status": "COMPOSITE_JOURNAL_RECOVERED", "event_id": event_id,
            "journal_chain_hash": terminal["chain_hash"], "economic_reapplied": False,
            "new_submission_claims": 0, "kill_switch_engaged": switch.status().engaged,
            "trading_ready": False}
