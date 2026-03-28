from __future__ import annotations

import pandas as pd
from pandas.testing import assert_frame_equal

import shared.event_log as shared_event_log
from shared.paths import data_path
from agents.fill_agent import fill_agent


def test_fill_outputs_append_only_across_runs(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    pd.DataFrame(
        [
            {
                "fill_id": "FILL001",
                "ticker": "AAPL",
                "side": "long",
                "action": "buy",
                "quantity": 2,
                "fill_price": 100.0,
                "fees": 1.0,
                "fill_timestamp": "2026-03-28T10:00:00+00:00",
            }
        ]
    ).to_csv(manual_fills_path, index=False)

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_APPEND_1")
    fill_agent.run_fill_agent()

    processed_after_first = pd.read_csv(data_dir / "processed_fills.csv")
    ledger_after_first = pd.read_csv(data_dir / "cash_ledger.csv")
    event_log_after_first = pd.read_csv(data_dir / "event_log.csv")

    pd.DataFrame(
        [
            {
                "fill_id": "FILL001",
                "ticker": "AAPL",
                "side": "long",
                "action": "buy",
                "quantity": 2,
                "fill_price": 100.0,
                "fees": 1.0,
                "fill_timestamp": "2026-03-28T10:00:00+00:00",
            },
            {
                "fill_id": "FILL002",
                "ticker": "MSFT",
                "side": "long",
                "action": "buy",
                "quantity": 1,
                "fill_price": 200.0,
                "fees": 1.5,
                "fill_timestamp": "2026-03-28T10:10:00+00:00",
            },
        ]
    ).to_csv(manual_fills_path, index=False)

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_APPEND_2")
    fill_agent.run_fill_agent()

    processed_after_second = pd.read_csv(data_dir / "processed_fills.csv")
    ledger_after_second = pd.read_csv(data_dir / "cash_ledger.csv")
    event_log_after_second = pd.read_csv(data_dir / "event_log.csv")

    assert len(processed_after_first) == 1
    assert len(processed_after_second) == 2
    assert_frame_equal(
        processed_after_first.reset_index(drop=True),
        processed_after_second.head(len(processed_after_first)).reset_index(drop=True),
        check_dtype=False,
    )

    assert len(ledger_after_first) == 1
    assert len(ledger_after_second) == 2
    assert_frame_equal(
        ledger_after_first.reset_index(drop=True),
        ledger_after_second.head(len(ledger_after_first)).reset_index(drop=True),
        check_dtype=False,
    )

    assert len(event_log_after_first) == 3
    assert len(event_log_after_second) == 6
    assert_frame_equal(
        event_log_after_first.reset_index(drop=True),
        event_log_after_second.head(len(event_log_after_first)).reset_index(drop=True),
        check_dtype=False,
    )


def test_event_log_appends_without_rewriting_existing_rows(isolated_workspace) -> None:
    shared_event_log.append_run_lifecycle_event(
        run_id="RUN_EVENT_APPEND_1",
        event_type="run_started",
        message="First event",
        details={"started_at": "2026-03-28T10:00:00+00:00"},
    )

    first_df = pd.read_csv(isolated_workspace / "data" / "event_log.csv")

    shared_event_log.append_run_lifecycle_event(
        run_id="RUN_EVENT_APPEND_2",
        event_type="run_completed",
        message="Second event",
        details={"completed_at": "2026-03-28T10:05:00+00:00"},
    )

    second_df = pd.read_csv(isolated_workspace / "data" / "event_log.csv")

    assert len(first_df) == 1
    assert len(second_df) == 2
    assert_frame_equal(
        first_df.reset_index(drop=True),
        second_df.head(1).reset_index(drop=True),
        check_dtype=False,
    )


def test_event_log_default_path_matches_shared_data_dir() -> None:
    assert shared_event_log.EVENT_LOG_PATH == data_path("event_log.csv")
