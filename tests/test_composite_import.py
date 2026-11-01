"""Public application/recovery uses only synthetic retained source evidence."""
from datetime import timedelta
import fcntl
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.fill_agent import composite_import as facade
from agents.fill_agent import external_lifecycle as lifecycle
from agents.fill_agent.pilot_ledger import PilotLedger
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from tests.test_composite_source import composite, plan, COMMIT
from tests.test_external_lifecycle import database_rows
from tests.test_external_reconciliation_admission import bytes_under


@pytest.fixture
def ready(composite, monkeypatch):
    f = composite
    f.switch = KillSwitch(state_path=f.runtime / "kill-switch.json",
                          audit_path=f.runtime / "kill-switch.jsonl")
    f.switch.initialize_engaged(operator_id="synthetic-review", reason="accounting only", now=f.now)
    (f.runtime / "operator.lock").touch(mode=0o600)
    f.proof, f.record, f.report = plan(f)
    f.arguments = {"runtime": f.runtime, "manifest": f.path,
        "expected_manifest_sha256": f.report["manifest_sha256"],
        "expected_plan_sha256": f.report["plan_sha256"], "reviewed_commit": COMMIT,
        "review_reference": "synthetic independently reviewed extraction", "now": f.now}
    # The checkout itself is deliberately dirty during test authoring. The
    # real subprocess gate is tested separately below, never bypassed live.
    monkeypatch.setattr(facade, "_require_reviewed_checkout", lambda commit: None)
    return f


def recover(f, **changes):
    arguments = {"runtime": f.runtime, "event_id": f.record["event_id"],
        "expected_plan_sha256": f.report["plan_sha256"], "reviewed_commit": COMMIT,
        "now": f.now + timedelta(seconds=1)}
    arguments.update(changes)
    return facade.recover_composite_import(**arguments)


def test_public_apply_preserves_original_rows_consumed_stores_and_switch(ready):
    f = ready
    before, rows = bytes_under(f.runtime), database_rows(f.ledger)
    result = facade.apply_composite_import(**f.arguments)
    assert result["applied"] and result["journal_complete"] and result["historical_rows_unchanged"]
    assert result["new_submission_claims"] == 0 and result["trading_ready"] is False
    assert result["cash_event_coverage"] == "net_effects_only"
    after = database_rows(f.ledger)
    assert after["inception"] == rows["inception"] and after["submissions"] == rows["submissions"]
    assert after["events"][:len(rows["events"])] == rows["events"]
    changed = bytes_under(f.runtime)
    for name, raw in before.items():
        if name != "pilot.sqlite3":
            assert changed[name] == raw
    assert (f.runtime / "execution-journal.sqlite3").stat().st_mode & 0o777 == 0o600
    assert f.switch.status().integrity_valid and f.switch.status().engaged
    assert len(f.ledger.snapshot()["external_imports"]) == 1
    stable = bytes_under(f.runtime)
    assert recover(f)["economic_reapplied"] is False
    assert bytes_under(f.runtime) == stable
    with pytest.raises(lifecycle.ExternalLifecycleError, match="original_state_not_initial"):
        facade.apply_composite_import(**f.arguments)
    assert bytes_under(f.runtime) == stable


@pytest.mark.parametrize("changes", [
    {"expected_manifest_sha256": "0" * 64}, {"expected_plan_sha256": "0" * 64},
    {"review_reference": "changed review"}, {"expected_manifest_sha256": "bad"},
])
def test_wrong_reviewed_hash_or_review_reference_stops_before_any_store_write(ready, changes):
    before = bytes_under(ready.runtime)
    with pytest.raises(ValueError):
        facade.apply_composite_import(**dict(ready.arguments, **changes))
    assert bytes_under(ready.runtime) == before


@pytest.mark.parametrize("head,dirty", [("c" * 40, ""), (COMMIT, " M source.py\n"),
                                       (COMMIT, "?? unreviewed.py\n")])
def test_clean_exact_checkout_gate_rejects_other_heads_and_changes(monkeypatch, head, dirty):
    calls = []
    def git(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=head if "rev-parse" in args else dirty)
    monkeypatch.setattr(facade.subprocess, "run", git)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="clean_exact_reviewed_checkout"):
        facade._require_reviewed_checkout(COMMIT)
    assert len(calls) == 2


def test_clean_checkout_gate_accepts_only_named_clean_revision(monkeypatch):
    def git(args, **kwargs):
        return SimpleNamespace(stdout=COMMIT if "rev-parse" in args else "")
    monkeypatch.setattr(facade.subprocess, "run", git)
    facade._require_reviewed_checkout(COMMIT)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="reviewed_commit"):
        facade._require_reviewed_checkout("HEAD")


def test_disengaged_switch_stops_before_journal_creation(ready):
    ready.switch.reset(operator_id="synthetic", reason="test disabled switch",
        acknowledgement=RESET_ACKNOWLEDGEMENT, now=ready.now)
    before = bytes_under(ready.runtime)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="engaged_switch"):
        facade.apply_composite_import(**ready.arguments)
    assert bytes_under(ready.runtime) == before


def test_operator_lock_rejects_active_owner_and_wrong_permissions(ready):
    lock = ready.runtime / "operator.lock"
    before = bytes_under(ready.runtime)
    with lock.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(lifecycle.ExternalLifecycleError, match="operator_active"):
            facade.apply_composite_import(**ready.arguments)
    assert bytes_under(ready.runtime) == before
    lock.chmod(0o644)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="private_lock"):
        facade.apply_composite_import(**ready.arguments)
    assert bytes_under(ready.runtime) == before


@pytest.mark.parametrize("name", ["execution.sqlite3", "operator.sqlite3", "connector.sqlite3"])
def test_missing_original_store_is_not_bootstrapped(ready, name):
    (ready.runtime / name).unlink()
    before = bytes_under(ready.runtime)
    with pytest.raises(ValueError):
        facade.apply_composite_import(**ready.arguments)
    assert bytes_under(ready.runtime) == before


def test_empty_existing_execution_store_is_not_initialized(ready):
    path = ready.runtime / "execution.sqlite3"
    path.write_bytes(b"")
    # A read-only plan must not initialize or silently repair a missing schema.
    _, _, report = plan(ready)
    before = bytes_under(ready.runtime)
    with pytest.raises(ValueError):
        facade.apply_composite_import(**dict(ready.arguments,
            expected_manifest_sha256=report["manifest_sha256"], expected_plan_sha256=report["plan_sha256"]))
    assert bytes_under(ready.runtime) == before


@pytest.mark.parametrize("broken", [False, True])
def test_existing_or_dangling_journal_symlink_is_never_followed(ready, broken):
    target = ready.runtime / "unrelated.sqlite3"
    if not broken:
        target.write_bytes(b"preserved unrelated bytes")
        target.chmod(0o600)
    journal = ready.runtime / "execution-journal.sqlite3"
    journal.symlink_to(target.name)
    before = bytes_under(ready.runtime)
    with pytest.raises(ValueError):
        facade.apply_composite_import(**ready.arguments)
    assert journal.is_symlink() and journal.readlink() == Path(target.name)
    assert target.exists() is (not broken)
    assert bytes_under(ready.runtime) == before


def test_existing_failed_journal_is_never_reinitialized(ready):
    path = ready.runtime / "execution-journal.sqlite3"
    path.write_bytes(b"incomplete synthetic journal")
    path.chmod(0o600)
    before = bytes_under(ready.runtime)
    with pytest.raises(Exception):
        facade.apply_composite_import(**ready.arguments)
    assert bytes_under(ready.runtime) == before


@pytest.mark.parametrize("boundary", ["before_journal", "after_journal"])
def test_economic_commit_survives_both_crashes_and_recovery_never_reapplies(ready, monkeypatch, boundary):
    f = ready
    with monkeypatch.context() as patch:
        def failure(*args, **kwargs):
            raise OSError("synthetic crash boundary")
        if boundary == "before_journal":
            patch.setattr(facade.lifecycle, "recover_external_journal", failure)
        else:
            patch.setattr(PilotLedger, "_mark_external_journal_complete", failure)
        with pytest.raises(OSError):
            facade.apply_composite_import(**f.arguments)
    state = f.ledger.snapshot()
    assert len(state["external_imports"]) == 1 and state["unresolved"]
    before = database_rows(f.ledger)
    result = recover(f)
    assert result["economic_reapplied"] is False and result["new_submission_claims"] == 0
    after = database_rows(f.ledger)
    assert after["inception"] == before["inception"] and after["submissions"] == before["submissions"]
    assert after["events"][:len(before["events"])] == before["events"]
    assert [row[1] for row in after["events"]].count("external_lifecycle") == 1
    assert not f.ledger.snapshot()["unresolved"] and f.switch.status().engaged
    assert recover(f)["economic_reapplied"] is False
    assert database_rows(f.ledger) == after


def test_recovery_does_not_recreate_missing_journal_or_accept_another_plan(ready, monkeypatch):
    with monkeypatch.context() as patch:
        def failure(*args, **kwargs):
            raise OSError("synthetic before journal record")
        patch.setattr(facade.lifecycle, "recover_external_journal", failure)
        with pytest.raises(OSError):
            facade.apply_composite_import(**ready.arguments)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="recovery_exact"):
        recover(ready, expected_plan_sha256="0" * 64)
    (ready.runtime / "execution-journal.sqlite3").unlink()
    before = bytes_under(ready.runtime)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="journal_missing"):
        recover(ready)
    assert bytes_under(ready.runtime) == before


def test_cli_failure_emits_no_private_source_exception(monkeypatch, capsys):
    from scripts import import_composite_demo as cli
    def failure(**kwargs):
        raise ValueError("private account and source bytes must stay private")
    monkeypatch.setattr(cli, "apply_composite_import", failure)
    assert cli.main(["apply", "--runtime", "/private/tmp/synthetic-original", "--reviewed-commit", COMMIT]) == 2
    output = capsys.readouterr().out
    assert "COMPOSITE_IMPORT_STOPPED" in output and "private account" not in output
