from __future__ import annotations

import pytest

import shared.event_log as shared_event_log
import shared.sqlite_sidecar as shared_sqlite_sidecar


@pytest.fixture
def isolated_workspace(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shared_event_log, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_event_log, "EVENT_LOG_PATH", data_dir / "event_log.csv")
    monkeypatch.setattr(shared_sqlite_sidecar, "SQLITE_DB_PATH", data_dir / "trading_system.sqlite3")

    return tmp_path
