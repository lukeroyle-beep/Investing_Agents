"""Finite setup reuses the original completed historical execution journal."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import sqlite3

import pytest

from agents.fill_agent import composite_import as facade
from agents.journal_agent.execution_journal import ExecutionJournal
from execution.store import ExecutionStore
from scripts import setup_finite_demo_pilot as setup
from tests.test_composite_import import ready
from tests.test_composite_source import composite
from tests.test_external_lifecycle import database_rows
from tests.test_external_reconciliation_admission import bytes_under


@pytest.fixture
def imported(ready):
    f = ready
    facade.apply_composite_import(**f.arguments)
    specs = [{"instrument_id": iid, "symbol": symbol, "exchange": "NASDAQ", "asset_type": "equity",
        "economic_class": "equity", "currency": "USD", "issuer": "Synthetic " + symbol,
        "sector": "technology", "class_source_reference": "synthetic reviewed classification"}
        for iid, symbol in ((201, "NEXTA"), (202, "NEXTB"))]
    state = f.ledger.snapshot()
    historical_ids = {p["instrument_id"] for p in state["inception"]["account"]["positions"]}
    historical_ids.update(p["instrument_id"] for p in state["positions"])
    catalog = [{"instrument_id": iid, "symbol": "SYN" + str(iid), "issuer": "Synthetic " + str(iid),
        "sector": "industrials", "asset_type": "equity", "source_reference": "synthetic original classification"}
        for iid in sorted(historical_ids)]
    catalog.extend({**{key: spec[key] for key in ("instrument_id", "symbol", "issuer", "sector", "asset_type")},
        "source_reference": spec["class_source_reference"]} for spec in specs)
    scope = {"instruments": specs, "authorized_amounts": {"201": "10", "202": "10"},
        "max_submissions": 2, "max_per_day": 2, "per_order_cap": "10.10", "total_commitment": "33",
        "max_positions": 3, "min_cash_fraction": "0.20", "loss_stop": "5", "order_types": ["market"]}
    f.setup_args = {"runtime": f.runtime, "authorization_reference": "synthetic subsequent finite approval",
        "reviewed_commit": "c"*40, "scope": scope, "catalog": catalog,
        "issued_at": f.now+timedelta(seconds=1), "expires_at": f.now+timedelta(days=1),
        "checkout_verifier": lambda commit: None}
    return f


def no_new_authority(f):
    assert not (f.runtime / setup.MANDATE_FILE).exists()
    assert not (f.runtime / setup.CATALOG_FILE).exists()


def test_composite_import_then_finite_setup_preserves_original_journal_and_rows(imported, monkeypatch):
    f = imported
    before, economic = bytes_under(f.runtime), database_rows(f.ledger)
    old_state = f.ledger.snapshot()
    with monkeypatch.context() as patch:
        patch.setattr(ExecutionJournal, "create", lambda *args, **kwargs: pytest.fail("existing journal recreated"))
        patch.setattr(ExecutionStore, "__init__", lambda *args, **kwargs: pytest.fail("existing execution store initialized"))
        result = setup.setup(**f.setup_args)
    after = bytes_under(f.runtime)
    assert result["worker_started"] is result["order_submitted"] is False
    assert after[setup.JOURNAL_FILE] == before[setup.JOURNAL_FILE]
    assert after["execution.sqlite3"] == before["execution.sqlite3"]
    for name in ("connector.sqlite3", "operator.sqlite3", "pilot-inception.json", "kill-switch.json", "kill-switch.jsonl"):
        assert after[name] == before[name]
    rows = database_rows(f.ledger)
    assert rows["inception"] == economic["inception"] and rows["submissions"] == economic["submissions"]
    assert rows["events"][:len(economic["events"])] == economic["events"]
    assert len(rows["events"]) == len(economic["events"]) + 1
    assert rows["events"][-1][1] == "finite_migration"
    state = f.ledger.snapshot()
    assert state["position_count"] == old_state["position_count"] == 1
    assert state["committed_capital"] == old_state["committed_capital"] == Decimal("12.34")
    assert state["pilot_pnl"] == old_state["pilot_pnl"] == Decimal("0.27")
    assert not state["unresolved"] and f.switch.status().engaged
    mandate = setup.FiniteDemoMandate.load(f.runtime/setup.MANDATE_FILE, expected_account_id="123")
    classes, _ = setup.load_catalog(f.runtime, mandate, f.ledger)
    assert {row["instrument_id"] for row in classes} == set(range(10,18)) | {42,201,202}


@pytest.mark.parametrize("damage", ["missing", "corrupt", "wrong_owner_mode", "truncated_chain", "changed_execution"])
def test_historical_journal_damage_holds_before_any_authority_write(imported, damage):
    f = imported
    path = f.runtime / setup.JOURNAL_FILE
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_bytes(b"synthetic corrupted historical journal")
    elif damage == "wrong_owner_mode":
        path.chmod(0o644)
    elif damage == "changed_execution":
        (f.runtime / "execution.sqlite3").write_bytes(b"synthetic invalid original execution store")
    else:
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE head SET sequence=0")
    before = bytes_under(f.runtime)
    with pytest.raises(Exception):
        setup.setup(**f.setup_args)
    no_new_authority(f)
    assert bytes_under(f.runtime) == before


@pytest.mark.parametrize("identity", [10, 42, 201])
def test_catalog_requires_retired_history_imported_opening_and_each_candidate(imported, identity):
    f = imported
    f.setup_args["catalog"] = [row for row in f.setup_args["catalog"] if row["instrument_id"] != identity]
    before = bytes_under(f.runtime)
    with pytest.raises(ValueError, match="classification_incomplete|classification_mismatch"):
        setup.setup(**f.setup_args)
    no_new_authority(f)
    assert bytes_under(f.runtime) == before


def test_unfinished_import_journal_must_recover_before_finite_authority(ready, monkeypatch):
    f = ready
    with monkeypatch.context() as patch:
        patch.setattr(facade.lifecycle, "recover_external_journal",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic journal crash")))
        with pytest.raises(OSError):
            facade.apply_composite_import(**f.arguments)
    assert f.ledger.snapshot()["unresolved"]
    before = bytes_under(f.runtime)
    with pytest.raises(ValueError, match="requires_reconciliation"):
        setup.setup(runtime=f.runtime, authorization_reference="synthetic new approval",
            reviewed_commit="c"*40, scope={}, catalog=[], issued_at=f.now,
            expires_at=f.now+timedelta(days=1), checkout_verifier=lambda commit: None)
    no_new_authority(f)
    assert bytes_under(f.runtime) == before


def test_bound_ready_empty_journal_is_reused(tmp_path, monkeypatch):
    from tests.test_finite_demo_runner import f as runtime_fixture, save
    f = runtime_fixture.__wrapped__(tmp_path)
    original = ExecutionJournal.create(f.root/setup.JOURNAL_FILE,
        ledger=f.ledger, store=f.store, runtime_root=f.root)
    before = original.path.read_bytes()
    monkeypatch.setattr(ExecutionJournal, "create", lambda *args, **kwargs: pytest.fail("existing journal recreated"))
    assert save(f)["order_submitted"] is False
    assert original.path.read_bytes() == before


def test_wrong_bound_empty_journal_holds_before_new_authority(tmp_path):
    from tests.test_finite_demo_runner import f as runtime_fixture, save
    f = runtime_fixture.__wrapped__(tmp_path)
    (f.root / "operator.lock").touch(mode=0o600)
    original = ExecutionJournal.create(f.root/setup.JOURNAL_FILE,
        ledger=f.ledger, store=f.store, runtime_root=f.root)
    with closing(sqlite3.connect(original.path)) as db, db:
        db.execute("DROP TRIGGER binding_no_update")
        db.execute("UPDATE binding SET digest=?", ("0"*64,))
    before = bytes_under(f.root)
    with pytest.raises(ValueError, match="binding_mismatch"):
        save(f)
    assert not (f.root/setup.MANDATE_FILE).exists()
    assert not (f.root/setup.CATALOG_FILE).exists()
    assert bytes_under(f.root) == before


@pytest.mark.parametrize("dangling", [False, True])
def test_existing_or_dangling_journal_symlink_stops_before_authority(tmp_path, dangling):
    from tests.test_finite_demo_runner import f as runtime_fixture, save
    f = runtime_fixture.__wrapped__(tmp_path)
    (f.root / "operator.lock").touch(mode=0o600)
    target = f.root / "other.sqlite3"
    if not dangling:
        target.write_bytes(b"preserved unrelated synthetic bytes")
        target.chmod(0o600)
    path = f.root / setup.JOURNAL_FILE
    path.symlink_to(target.name)
    before = bytes_under(f.root)
    with pytest.raises(ValueError, match="journal_symlink_refused"):
        save(f)
    assert not (f.root/setup.MANDATE_FILE).exists()
    assert not (f.root/setup.CATALOG_FILE).exists()
    assert path.is_symlink() and bytes_under(f.root) == before


@pytest.mark.parametrize("contents", [b"", b"synthetic corrupted operational store"])
def test_missing_journal_never_allows_operational_store_initialization(tmp_path, contents):
    from tests.test_finite_demo_runner import f as runtime_fixture, save
    f = runtime_fixture.__wrapped__(tmp_path)
    (f.root / "operator.lock").touch(mode=0o600)
    (f.root / "execution.sqlite3").write_bytes(contents)
    before = bytes_under(f.root)
    with pytest.raises(ValueError, match="execution_store_invalid"):
        save(f)
    assert not any((f.root/name).exists() for name in (setup.MANDATE_FILE, setup.CATALOG_FILE, setup.JOURNAL_FILE))
    assert bytes_under(f.root) == before


def test_new_journal_before_authority_crash_is_preserved_and_reused(tmp_path, monkeypatch):
    from tests.test_finite_demo_runner import f as runtime_fixture, save
    f = runtime_fixture.__wrapped__(tmp_path)
    old_state = f.ledger.snapshot()
    with monkeypatch.context() as patch:
        patch.setattr(setup.FiniteDemoMandate, "create", lambda *args, **kwargs:
            (_ for _ in ()).throw(OSError("synthetic authority write failure")))
        with pytest.raises(OSError):
            save(f)
    assert not (f.root/setup.MANDATE_FILE).exists() and not (f.root/setup.CATALOG_FILE).exists()
    assert f.ledger.snapshot() == old_state
    path = f.root/setup.JOURNAL_FILE
    before = path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(ExecutionJournal, "create", lambda *args, **kwargs: pytest.fail("valid empty journal replaced"))
        patch.setattr(ExecutionStore, "__init__", lambda *args, **kwargs: pytest.fail("operational store initialized"))
        assert save(f)["order_submitted"] is False
    assert path.read_bytes() == before and f.switch.status().engaged


def test_missing_historical_journal_after_finite_migration_is_not_recreated(tmp_path):
    from tests.test_finite_demo_runner import f as runtime_fixture, save
    f = runtime_fixture.__wrapped__(tmp_path)
    save(f)
    # Simulate externally lost setup artifacts; retained migration still proves
    # this is not a fresh runtime even when it contains no submission rows.
    for name in (setup.MANDATE_FILE, setup.CATALOG_FILE, setup.JOURNAL_FILE):
        (f.root/name).unlink()
    before = bytes_under(f.root)
    with pytest.raises(ValueError, match="historical_journal_missing_never_recreate"):
        save(f)
    assert bytes_under(f.root) == before
