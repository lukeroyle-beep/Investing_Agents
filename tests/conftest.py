from __future__ import annotations

import pytest

import shared.event_log as shared_event_log


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shared_event_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_event_log, "EVENT_LOG_PATH", data_dir / "event_log.csv")

    return tmp_path
