from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pandas as pd
import pytest

from agents.journal_agent import history_migration as migration
from agents.journal_agent import journal_agent as journal


COMMIT = "a" * 40


def row(**changes):
    result = {"ticker": "TEST", "name": "Synthetic", "market_regime": "neutral",
        "adjusted_setup_score": "82.5", "adjusted_setup_status": "candidate",
        "risk_decision": "approved", "risk_notes": "synthetic", "run_id": "RUN_ONE",
        "journaled_at": "2026-01-01T10:00:00+00:00", "review_status": "open",
        "user_action": "", "outcome": "", "notes": ""}
    return result | changes


def write(path, rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_bytes(output.getvalue().encode())


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "trade_journal.csv"
    write(path, [row(run_id="", journaled_at="legacy unknown"),
        row(journaled_at="2026-01-01T11:00:00+00:00", notes="  NA  "),
        row(notes="  NA  "), row(run_id="RUN_TWO", outcome="null")])
    return path


def apply(path, plan=None, **changes):
    plan = plan or migration.inspect_migration(path)
    arguments = dict(expected_source_sha256=plan["source_sha256"],
        expected_plan_sha256=plan["plan_sha256"], reviewed_commit=COMMIT,
        review_reference="synthetic reviewed migration", writers_stopped=True,
        checkout_verifier=lambda commit: None)
    return migration.apply_migration(path, **(arguments | changes))


def test_dry_run_is_read_only_and_unkeyed_rows_stay_unresolved(source):
    before = {p: p.read_bytes() for p in source.parent.iterdir()}
    plan = migration.inspect_migration(source)
    assert plan["source_rows"] == 4
    assert plan["operational_rows"] == 2
    assert plan["unresolved_unkeyed_rows"] == 1
    assert plan["equivalent_keyed_retry_rows"] == 1
    assert {p: p.read_bytes() for p in source.parent.iterdir()} == before


def test_lossless_archive_every_row_mapping_earliest_timestamp_and_review_fields(source):
    original = source.read_bytes()
    result = apply(source)
    archive = source.parent / "trade_journal.csv.history" / result["source_sha256"]
    assert (archive / "original.csv").read_bytes() == original
    assert source.read_bytes() == (archive / "operational.csv").read_bytes()
    rows = list(csv.DictReader(source.open()))
    assert [r["run_id"] for r in rows] == ["RUN_ONE", "RUN_TWO"]
    assert rows[0]["journaled_at"] == "2026-01-01T10:00:00+00:00"
    assert rows[0]["notes"] == "  NA  "
    assert rows[1]["outcome"] == "null"
    plan = json.loads((archive / "plan.json").read_bytes())
    assert [r["source_row"] for r in plan["row_map"]] == [1, 2, 3, 4]
    assert [r["operational_row"] for r in plan["row_map"]] == [None, 1, 1, 2]
    assert plan["row_map"][0]["disposition"] == "unresolved_missing_identity"
    assert plan["row_map"][0]["journaled_at"] == "legacy unknown"
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in archive.iterdir())
    migration.validate_journal_history(source)
    proposed = journal.build_journal_entries(pd.DataFrame([row(ticker="NEXT")]), "RUN_NEW")
    merged, additions = journal.merge_journal_entries(pd.read_csv(source, dtype=str, keep_default_na=False), proposed)
    assert len(merged) == 3 and len(additions) == 1


def test_unkeyed_observations_never_deduplicated_or_given_synthetic_identity(tmp_path):
    path = tmp_path / "trade_journal.csv"
    write(path, [row(run_id="", journaled_at=f"old-{i}") for i in range(20)] + [row()])
    result = apply(path)
    assert result["unresolved_unkeyed_rows"] == 20
    assert result["equivalent_keyed_retry_rows"] == 0
    assert result["operational_rows"] == 1


@pytest.mark.parametrize("changes", [{"notes": "changed"}, {"risk_notes": "changed"},
    {"outcome": "NA"}, {"custom": "different"}, {"ticker": " test "},
    {"run_id": " RUN_ONE "}, {"review_status": "reviewed"}, {"user_action": "manual"},
    {"adjusted_setup_score": "82.50"}])
def test_conflicting_keyed_source_or_manual_content_is_not_discarded(tmp_path, changes):
    path = tmp_path / "trade_journal.csv"
    first = row(custom="original")
    write(path, [first, first | changes])
    original = path.read_bytes()
    with pytest.raises(migration.JournalMigrationError, match="conflict"):
        migration.inspect_migration(path)
    assert path.read_bytes() == original
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("value", ["unknown", "2026-01-01T10:00:00", ""])
def test_duplicate_timestamp_must_be_orderable_without_guessing_timezone(tmp_path, value):
    path = tmp_path / "trade_journal.csv"
    write(path, [row(), row(journaled_at=value)])
    with pytest.raises(migration.JournalMigrationError, match="timezone"):
        migration.inspect_migration(path)


def test_exact_source_plan_review_commit_and_stopped_writers_required(source):
    plan = migration.inspect_migration(source)
    original = source.read_bytes()
    for changes in ({"expected_source_sha256": "f" * 64}, {"expected_plan_sha256": "f" * 64},
                    {"writers_stopped": False}, {"review_reference": ""}, {"reviewed_commit": "bad"}):
        with pytest.raises(migration.JournalMigrationError):
            apply(source, plan, **changes)
        assert source.read_bytes() == original
    def reject(commit):
        raise ValueError("unreviewed")
    with pytest.raises(ValueError, match="unreviewed"):
        apply(source, plan, checkout_verifier=reject)
    assert not (source.parent / "trade_journal.csv.history").exists()


def test_replay_after_completed_migration_preserves_new_advice(source):
    plan = migration.inspect_migration(source)
    apply(source, plan)
    with source.open("a") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row()))
        writer.writerow(row(run_id="NEW_RUN"))
    before = {p: p.read_bytes() for p in source.parent.rglob("*") if p.is_file()}
    assert apply(source, plan)["status"] == "ALREADY_APPLIED"
    assert {p: p.read_bytes() for p in source.parent.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("failure_point", ["operational.csv", "plan.json", "prepared.json", "completed.json"])
def test_crash_resumes_only_same_original_and_plan(source, monkeypatch, failure_point):
    plan = migration.inspect_migration(source)
    original = source.read_bytes()
    create = migration._create_exact
    def fail(path, raw):
        if path.name == failure_point:
            raise OSError("synthetic disk error")
        return create(path, raw)
    monkeypatch.setattr(migration, "_create_exact", fail)
    with pytest.raises(OSError):
        apply(source, plan)
    with pytest.raises(migration.JournalMigrationError):
        migration.validate_journal_history(source)
    monkeypatch.setattr(migration, "_create_exact", create)
    apply(source, plan)
    archive = source.parent / "trade_journal.csv.history" / plan["source_sha256"]
    assert (archive / "original.csv").read_bytes() == original
    migration.validate_journal_history(source)


def test_corrupt_archive_fails_before_any_source_overwrite(source):
    plan = migration.inspect_migration(source)
    apply(source, plan)
    archive = source.parent / "trade_journal.csv.history" / plan["source_sha256"]
    (archive / "original.csv").write_bytes(b"partial file")
    before = source.read_bytes()
    with pytest.raises(migration.JournalMigrationError):
        apply(source, plan)
    assert source.read_bytes() == before


def test_changed_source_during_partial_migration_never_overwritten(source, monkeypatch):
    plan = migration.inspect_migration(source)
    create = migration._create_exact
    def fail(path, raw):
        if path.name == "completed.json":
            raise OSError("interrupted")
        return create(path, raw)
    monkeypatch.setattr(migration, "_create_exact", fail)
    with pytest.raises(OSError):
        apply(source, plan)
    source.write_bytes(source.read_bytes() + b"unrelated edit\n")
    before = source.read_bytes()
    monkeypatch.setattr(migration, "_create_exact", create)
    with pytest.raises(migration.JournalMigrationError, match="never_overwrite"):
        apply(source, plan)
    assert source.read_bytes() == before


def test_shared_lock_serializes_migration_and_live_journal(source):
    plan = migration.inspect_migration(source)
    with migration.journal_write_lock(source):
        with pytest.raises(migration.JournalMigrationError, match="writer_active"):
            apply(source, plan)


def test_symlink_source_and_archive_are_rejected(source, tmp_path):
    link = tmp_path / "linked.csv"
    link.symlink_to(source)
    with pytest.raises(migration.JournalMigrationError, match="canonical"):
        migration.inspect_migration(link)
    history = source.parent / "trade_journal.csv.history"
    other = tmp_path / "other"
    other.mkdir()
    history.symlink_to(other, target_is_directory=True)
    before = source.read_bytes()
    with pytest.raises(migration.JournalMigrationError):
        apply(source)
    assert source.read_bytes() == before


def test_live_journal_refuses_partial_history_before_write(source, monkeypatch):
    (source.parent / "trade_journal.csv.history").mkdir()
    monkeypatch.setattr(journal, "JOURNAL_FILE", source)
    monkeypatch.setattr(journal, "load_final_shortlist", lambda: pd.DataFrame([row()]))
    monkeypatch.setattr(journal, "get_or_create_run_id", lambda: "NEW_RUN")
    before = source.read_bytes()
    with pytest.raises(migration.JournalMigrationError, match="incomplete"):
        journal.main()
    assert source.read_bytes() == before


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "advisory_history_v1.json").read_text())


def legacy_archive(source, *, completed):
    """Install frozen pre-repair bytes, rebinding only the temporary source path."""
    plan = json.loads(FIXTURE["plan.json"].encode())
    plan["source_path"] = str(source)
    archive = migration._history(source) / plan["source_sha256"]
    archive.parent.mkdir(mode=0o700)
    archive.mkdir(mode=0o700)
    prepared = {"schema": migration.SCHEMA, "source_path": str(source),
        "source_sha256": plan["source_sha256"], "plan_sha256": migration._hash(migration._json(plan)),
        "reviewed_commit": COMMIT, "review_reference": "synthetic reviewed migration"}
    for name in ("original.csv", "operational.csv"):
        (archive / name).write_bytes(FIXTURE[name].encode())
    (archive / "plan.json").write_bytes(migration._json(plan))
    (archive / "prepared.json").write_bytes(migration._json(prepared))
    if completed:
        (archive / "completed.json").write_bytes(migration._json({"schema": migration.SCHEMA,
            "prepared_sha256": migration._hash(migration._json(prepared)),
            "operational_sha256": plan["operational_sha256"], "status": "applied"}))
    return plan, archive


def test_batch_plan_exactly_matches_pre_repair_bytes_and_hashes():
    expected = FIXTURE["plan.json"].encode()
    source = Path(json.loads(expected)["source_path"])
    plan, operational = migration._plan(source, FIXTURE["original.csv"].encode())
    assert migration._json(plan) == expected
    assert operational == FIXTURE["operational.csv"].encode()
    assert migration._hash(expected) == "e349899bc10b315ba360a15597703476daf56bef753cef3be095252df05f7bf6"


def test_pre_repair_completed_archive_validates_without_changing_any_bytes(tmp_path):
    source = tmp_path / "trade_journal.csv"
    source.write_bytes(FIXTURE["operational.csv"].encode())
    plan, _ = legacy_archive(source, completed=True)
    # Existing replay deliberately preserves advice written since migration.
    source.write_bytes(source.read_bytes() + b"new advice not owned by migration\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    migration.validate_journal_history(source)
    assert apply(source, migration._summary(plan))["status"] == "ALREADY_APPLIED"
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("source_stage", ["original.csv", "operational.csv"])
def test_pre_repair_prepared_archive_recovers_same_plan(tmp_path, source_stage):
    source = tmp_path / "trade_journal.csv"
    source.write_bytes(FIXTURE[source_stage].encode())
    plan, archive = legacy_archive(source, completed=False)
    retained = {p: p.read_bytes() for p in archive.iterdir()}
    assert apply(source, migration._summary(plan))["status"] == "APPLIED_HISTORY_PRESERVED"
    assert source.read_bytes() == FIXTURE["operational.csv"].encode()
    assert {p: p.read_bytes() for p in retained} == retained
    migration.validate_journal_history(source)


@pytest.mark.parametrize("keyed_count", [2, 32, 1000])
def test_keyed_normalization_call_count_is_constant(tmp_path, monkeypatch, keyed_count):
    source = tmp_path / "trade_journal.csv"
    write(source, [row(run_id=f"RUN_{i}") for i in range(keyed_count)]
        + [row(run_id="", review_status="unvalidated history"), row(ticker=" ")])
    calls = []
    validate = migration.validate_trade_journal
    def counted(frame):
        calls.append(len(frame))
        return validate(frame)
    monkeypatch.setattr(migration, "validate_trade_journal", counted)
    migration.inspect_migration(source)
    # One batch for keyed identities; one retained operational validation.
    assert calls == [keyed_count, keyed_count]


def test_recurring_archive_validation_normalizes_one_batch(tmp_path, monkeypatch):
    source = tmp_path / "trade_journal.csv"
    source.write_bytes(FIXTURE["operational.csv"].encode())
    legacy_archive(source, completed=True)
    calls = []
    validate = migration.validate_trade_journal
    def counted(frame):
        calls.append(len(frame))
        return validate(frame)
    monkeypatch.setattr(migration, "validate_trade_journal", counted)
    for _ in range(3):
        calls.clear()
        migration.validate_journal_history(source)
        assert calls == [4, 2]


def test_entirely_unkeyed_history_never_enters_schema_validation(tmp_path, monkeypatch):
    source = tmp_path / "trade_journal.csv"
    write(source, [row(run_id=" ", review_status="old invalid value"),
        row(ticker="", journaled_at="unknown")])
    def unexpected(frame):
        raise AssertionError("unkeyed history must remain untouched")
    monkeypatch.setattr(migration, "validate_trade_journal", unexpected)
    plan = migration.inspect_migration(source)
    assert plan["unresolved_unkeyed_rows"] == 2
    assert plan["operational_rows"] == 0


def test_batch_normalization_still_rejects_invalid_keyed_rows(tmp_path):
    source = tmp_path / "trade_journal.csv"
    write(source, [row(run_id="GOOD"), row(run_id="BAD", review_status="invalid")])
    before = source.read_bytes()
    with pytest.raises(ValueError, match="invalid values"):
        migration.inspect_migration(source)
    assert source.read_bytes() == before
    assert list(tmp_path.iterdir()) == [source]
