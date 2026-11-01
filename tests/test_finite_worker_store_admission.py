"""Original-store admission cannot repair or recreate a finite worker runtime."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from execution.store import ExecutionStore
from execution.finite_demo_mandate import FiniteDemoMandateError
from risk.kill_switch import RESET_ACKNOWLEDGEMENT
from scripts import run_finite_demo_pilot as runner
from scripts import setup_finite_demo_pilot as setup
from tests.test_finite_demo_runner import COMMIT, NOW, _runner_artifacts, f, save


class IntendedWorkBoundary(RuntimeError):
    pass


def bytes_under(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def block_external_work(monkeypatch, *, boundary=None):
    def blocked(name):
        def stop(*args, **kwargs):
            if name == boundary:
                raise IntendedWorkBoundary(name)
            pytest.fail("unexpected startup side effect: " + name)
        return stop
    for name in ("UsageOnlyHost", "load_dedicated_credentials", "DirectBatchReads",
                 "DirectDemoClient", "DirectBindingStore"):
        monkeypatch.setattr(runner, name, blocked(name))
    monkeypatch.setattr(runner, "verify_reviewed_checkout", lambda commit: None)


def pending_runtime(tmp_path):
    from tests.test_finite_demo_operator import workflow, prepare, submit
    value = workflow(tmp_path)
    prepared = prepare(value)
    submit(prepared)  # Synthetic in-process transport only.
    assert len(value.transport.posts) == 1
    assert value.ledger.snapshot()["unresolved"]
    _runner_artifacts(value, tmp_path)
    value.switch.engage(operator_id="synthetic", reason="recovery admission test", now=NOW)
    # Production run owns this existing lock; it must not create test noise.
    (tmp_path / "operator.lock").touch(mode=0o600)
    return value, SimpleNamespace(runtime=tmp_path, reviewed_commit=value.mandate.reviewed_commit,
        instrument_id=value.specs[1]["instrument_id"], amount=Decimal("10"))


@pytest.mark.parametrize("recovery", [False, True], ids=["new-cycle", "pending-claim"])
@pytest.mark.parametrize("damage", ["empty", "corrupt", "missing_execution", "missing_journal"])
def test_worker_refuses_damaged_original_stores_without_changes(f, tmp_path, monkeypatch, recovery, damage):
    if recovery:
        value, args = pending_runtime(tmp_path / "recovery")
    else:
        save(f)
        value = f
        args = SimpleNamespace(runtime=f.root, reviewed_commit=COMMIT,
            instrument_id=101, amount=Decimal("10"))
    root = args.runtime
    assert value.switch.status().engaged
    if damage == "empty":
        (root / "execution.sqlite3").write_bytes(b"")
        reason = "existing_execution_store_invalid"
    elif damage == "corrupt":
        (root / "execution.sqlite3").write_bytes(b"synthetic corrupt original store")
        reason = "existing_execution_store_invalid"
    elif damage == "missing_execution":
        (root / "execution.sqlite3").unlink()
        reason = "existing_pilot_state_missing_never_initialize"
    else:
        (root / setup.JOURNAL_FILE).unlink()
        reason = "journal_missing_never_recreate"
    before = bytes_under(root)
    block_external_work(monkeypatch)
    monkeypatch.setattr(runner, "now", lambda: NOW)
    with pytest.raises(ValueError, match=reason):
        runner.run(args)
    assert bytes_under(root) == before
    assert value.switch.status().engaged
    if recovery:
        assert len(value.transport.posts) == 1
        assert len(value.ledger.snapshot()["submissions"]) == 1


@pytest.mark.parametrize("enabled", [False, True], ids=["stopped", "usage-boundary"])
def test_healthy_startup_preserves_existing_store_until_intended_work(f, monkeypatch, enabled):
    save(f)
    if enabled:
        f.switch.reset(operator_id="synthetic", reason="test only", now=NOW,
            acknowledgement=RESET_ACKNOWLEDGEMENT)
    before = bytes_under(f.root)
    block_external_work(monkeypatch, boundary="UsageOnlyHost" if enabled else None)
    monkeypatch.setattr(runner, "now", lambda: NOW)
    monkeypatch.setattr(ExecutionStore, "__init__",
        lambda *args, **kwargs: pytest.fail("existing store constructor must never run"))
    error, reason = ((IntendedWorkBoundary, "UsageOnlyHost") if enabled
                     else (FiniteDemoMandateError, "kill_switch"))
    with pytest.raises(error, match=reason):
        runner.run(SimpleNamespace(runtime=f.root, reviewed_commit=COMMIT,
            instrument_id=101, amount=Decimal("10")))
    assert bytes_under(f.root) == before


def test_healthy_pending_claim_validates_without_initialization_or_new_work(tmp_path, monkeypatch):
    value, args = pending_runtime(tmp_path)
    before = bytes_under(tmp_path)
    block_external_work(monkeypatch, boundary="load_dedicated_credentials")
    monkeypatch.setattr(runner, "now", lambda: NOW)
    monkeypatch.setattr(ExecutionStore, "__init__",
        lambda *args, **kwargs: pytest.fail("recovery must never initialize an existing store"))
    with pytest.raises(IntendedWorkBoundary, match="load_dedicated_credentials"):
        runner.run(args)
    assert bytes_under(tmp_path) == before
    assert len(value.transport.posts) == 1
    assert len(value.ledger.snapshot()["submissions"]) == 1
