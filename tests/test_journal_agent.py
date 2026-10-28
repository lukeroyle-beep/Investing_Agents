from __future__ import annotations

import pandas as pd
import pytest

from agents.journal_agent import journal_agent as journal


def shortlist(**changes) -> pd.DataFrame:
    row = {
        "ticker": "TEST",
        "name": "Synthetic instrument",
        "market_regime": "neutral",
        "adjusted_setup_score": 82.5,
        "adjusted_setup_status": "candidate",
        "risk_decision": "approved",
        "risk_notes": "Synthetic advice only",
    }
    row.update(changes)
    return pd.DataFrame([row])


@pytest.fixture
def journal_files(tmp_path, monkeypatch):
    source = tmp_path / "final_shortlist.csv"
    destination = tmp_path / "trade_journal.csv"
    events = []
    monkeypatch.setattr(journal, "FINAL_SHORTLIST_FILE", source)
    monkeypatch.setattr(journal, "JOURNAL_FILE", destination)
    monkeypatch.setattr(journal, "get_or_create_run_id", lambda: "RUN_SYNTHETIC")
    monkeypatch.setattr(journal, "append_artifact_written_event", lambda **event: events.append(event))
    shortlist().to_csv(source, index=False)
    return source, destination, events


def test_retry_preserves_original_row_review_content_and_file_bytes(journal_files):
    _, destination, events = journal_files
    journal.main()
    recorded = pd.read_csv(destination, dtype=str, keep_default_na=False)
    recorded.loc[0, "review_status"] = "reviewed"
    recorded.loc[0, "user_action"] = "  Keep under review  "
    recorded.loc[0, "outcome"] = "NA"
    recorded.loc[0, "notes"] = "  Manual notes must survive  "
    recorded["custom_review_field"] = "null"
    recorded.to_csv(destination, index=False)
    before = destination.read_bytes()

    journal.main()

    assert destination.read_bytes() == before
    assert len(pd.read_csv(destination)) == 1
    assert len(events) == 1
    assert events[0]["details"]["new_entries"] == 1


def test_new_ticker_appends_once_and_preserves_existing_review_fields(journal_files):
    source, destination, events = journal_files
    journal.main()
    recorded = pd.read_csv(destination, dtype=str, keep_default_na=False)
    recorded.loc[0, "notes"] = "  NA  "
    recorded.loc[0, "outcome"] = "null"
    recorded.to_csv(destination, index=False)
    pd.concat([shortlist(), shortlist(ticker="NEXT")]).to_csv(source, index=False)

    journal.main()
    journal.main()

    result = pd.read_csv(destination, dtype=str, keep_default_na=False)
    assert result["ticker"].tolist() == ["TEST", "NEXT"]
    assert result.loc[0, "journaled_at"] == recorded.loc[0, "journaled_at"]
    assert result.loc[0, "notes"] == "  NA  "
    assert result.loc[0, "outcome"] == "null"
    assert [event["details"]["new_entries"] for event in events] == [1, 1]


def test_same_ticker_in_new_run_is_a_new_observation(journal_files, monkeypatch):
    _, destination, _ = journal_files
    journal.main()
    monkeypatch.setattr(journal, "get_or_create_run_id", lambda: "RUN_NEXT")

    journal.main()

    result = pd.read_csv(destination)
    assert result["run_id"].tolist() == ["RUN_SYNTHETIC", "RUN_NEXT"]
    assert result["ticker"].tolist() == ["TEST", "TEST"]


@pytest.mark.parametrize("changes", [
    {"adjusted_setup_score": 12},
    {"risk_decision": "rejected"},
    {"risk_notes": "Different source advice"},
])
def test_conflicting_replay_stops_before_writing(journal_files, changes):
    source, destination, events = journal_files
    journal.main()
    before = destination.read_bytes()
    shortlist(**changes).to_csv(source, index=False)

    with pytest.raises(ValueError, match="conflicting source advice"):
        journal.main()

    assert destination.read_bytes() == before
    assert len(events) == 1


@pytest.mark.parametrize("conflicting", [False, True])
def test_existing_duplicate_keys_are_not_silently_repaired(journal_files, conflicting):
    _, destination, events = journal_files
    journal.main()
    existing = pd.read_csv(destination, dtype=str, keep_default_na=False)
    duplicate = existing.copy()
    if conflicting:
        duplicate.loc[0, "notes"] = "Another manual review"
    pd.concat([existing, duplicate]).to_csv(destination, index=False)
    before = destination.read_bytes()

    with pytest.raises(ValueError, match="existing entries have duplicate"):
        journal.main()

    assert destination.read_bytes() == before
    assert len(events) == 1


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_shortlist_keys_stop_before_first_write(journal_files, conflicting):
    source, destination, events = journal_files
    duplicate = shortlist(risk_decision="rejected") if conflicting else shortlist()
    pd.concat([shortlist(), duplicate]).to_csv(source, index=False)

    with pytest.raises(ValueError, match="proposed entries have duplicate"):
        journal.main()

    assert not destination.exists()
    assert not events


@pytest.mark.parametrize("field", ["run_id", "ticker"])
def test_empty_identity_is_rejected(field):
    proposed = journal.build_journal_entries(shortlist(), "RUN_SYNTHETIC")
    proposed.loc[0, field] = "  "

    with pytest.raises(ValueError, match="empty run_id or ticker"):
        journal.merge_journal_entries(pd.DataFrame(), proposed)


def test_missing_optional_score_can_replay(journal_files):
    source, destination, _ = journal_files
    shortlist(adjusted_setup_score=None).to_csv(source, index=False)
    journal.main()
    before = destination.read_bytes()

    journal.main()

    assert destination.read_bytes() == before


def test_normalised_identity_detects_duplicate_input():
    proposed = journal.build_journal_entries(
        pd.concat([shortlist(ticker="test"), shortlist(ticker=" TEST ")]),
        "RUN_SYNTHETIC",
    )
    with pytest.raises(ValueError, match="proposed entries have duplicate"):
        journal.merge_journal_entries(pd.DataFrame(), proposed)
