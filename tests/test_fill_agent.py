from __future__ import annotations

import pytest
import pandas as pd
from pandas.testing import assert_frame_equal

from agents.fill_agent import fill_agent


def _manual_fill(fill_id: str = "FILL001") -> dict[str, object]:
    return {
        "fill_id": fill_id,
        "ticker": "AAPL",
        "side": "long",
        "action": "buy",
        "quantity": 2,
        "fill_price": 100.0,
        "fees": 1.0,
        "fill_timestamp": "2026-03-28T10:00:00+00:00",
    }


def test_fill_agent_is_idempotent(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_FILL_TEST")

    pd.DataFrame([_manual_fill()]).to_csv(manual_fills_path, index=False)

    fill_agent.run_fill_agent()

    first_state = pd.read_csv(data_dir / "portfolio_state.csv")
    first_processed = pd.read_csv(data_dir / "processed_fills.csv")
    first_cash = pd.read_csv(data_dir / "cash_state.csv")
    first_ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    first_event_log = pd.read_csv(data_dir / "event_log.csv")

    fill_agent.run_fill_agent()

    second_state = pd.read_csv(data_dir / "portfolio_state.csv")
    second_processed = pd.read_csv(data_dir / "processed_fills.csv")
    second_cash = pd.read_csv(data_dir / "cash_state.csv")
    second_ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    second_event_log = pd.read_csv(data_dir / "event_log.csv")

    assert len(first_processed) == 1
    assert len(first_ledger) == 1
    assert len(first_event_log) == 3

    assert_frame_equal(first_state, second_state, check_dtype=False)
    assert_frame_equal(first_processed, second_processed, check_dtype=False)
    assert_frame_equal(first_cash, second_cash, check_dtype=False)
    assert_frame_equal(first_ledger, second_ledger, check_dtype=False)
    assert_frame_equal(first_event_log, second_event_log, check_dtype=False)


def test_fill_agent_fails_closed_when_marker_missing_after_mutation(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_INTERRUPTED")
    pd.DataFrame([_manual_fill("FILL_INTERRUPTED")]).to_csv(manual_fills_path, index=False)

    fill_agent.run_fill_agent()
    first_ledger = pd.read_csv(data_dir / "cash_ledger.csv")

    pd.DataFrame(columns=["fill_id", "processed_at", "run_id"]).to_csv(
        data_dir / "processed_fills.csv",
        index=False,
    )

    with pytest.raises(RuntimeError) as excinfo:
        fill_agent.run_fill_agent()

    message = str(excinfo.value)
    assert "FILL_INTERRUPTED" in message
    assert "absent from processed_fills.csv" in message
    assert "Manual recovery required" in message
    assert "restore/repair the missing processed-fill marker" in message
    assert "Do not auto-replay" in message

    second_ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    assert_frame_equal(first_ledger, second_ledger, check_dtype=False)


def test_fill_agent_rejects_duplicate_fill_ids_before_mutation(isolated_workspace) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    pd.DataFrame([_manual_fill("DUP_FILL"), _manual_fill("DUP_FILL")]).to_csv(
        manual_fills_path,
        index=False,
    )

    with pytest.raises(ValueError) as excinfo:
        fill_agent.run_fill_agent()

    assert "Duplicate fill_id values" in str(excinfo.value)
    ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    assert ledger.empty


def test_fill_agent_replay_skips_fill_already_in_processed_fills(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_ALREADY_DONE")
    pd.DataFrame([_manual_fill("FILL_DONE")]).to_csv(manual_fills_path, index=False)
    pd.DataFrame(
        [{"fill_id": "FILL_DONE", "processed_at": "2026-03-28T10:05:00+00:00", "run_id": "RUN_PRIOR"}]
    ).to_csv(data_dir / "processed_fills.csv", index=False)

    fill_agent.run_fill_agent()

    processed = pd.read_csv(data_dir / "processed_fills.csv")
    ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    event_log = pd.read_csv(data_dir / "event_log.csv")

    assert processed["fill_id"].tolist() == ["FILL_DONE"]
    assert ledger.empty
    assert event_log.empty


def test_fill_agent_processes_first_time_fill(isolated_workspace, monkeypatch) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"

    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_FIRST_TIME")
    pd.DataFrame([_manual_fill("FILL_FIRST")]).to_csv(manual_fills_path, index=False)

    fill_agent.run_fill_agent()

    processed = pd.read_csv(data_dir / "processed_fills.csv")
    ledger = pd.read_csv(data_dir / "cash_ledger.csv")
    event_log = pd.read_csv(data_dir / "event_log.csv")

    assert processed["fill_id"].tolist() == ["FILL_FIRST"]
    assert len(ledger) == 1
    assert "FILL_FIRST" in ledger.iloc[0]["notes"]
    assert len(event_log) == 3


def test_fill_agent_applies_partial_close_then_full_close_exactly_once(
    isolated_workspace, monkeypatch
) -> None:
    data_dir = isolated_workspace / "data"
    manual_fills_path = data_dir / "manual_fills.csv"
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_PARTIAL_CLOSE")

    fills = [
        {**_manual_fill("OPEN_TWO"), "fees": 2.0},
        {
            **_manual_fill("CLOSE_ONE"),
            "action": "sell",
            "quantity": 1,
            "fill_price": 110.0,
            "fees": 1.0,
            "fill_timestamp": "2026-03-28T11:00:00+00:00",
        },
    ]
    pd.DataFrame(fills).to_csv(manual_fills_path, index=False)
    fill_agent.run_fill_agent()

    partial_state = pd.read_csv(data_dir / "portfolio_state.csv").iloc[0]
    assert partial_state["status"] == "open"
    assert partial_state["quantity"] == pytest.approx(1.0)
    assert partial_state["realised_pnl_abs"] == pytest.approx(8.0)
    assert partial_state["entry_fees_remaining"] == pytest.approx(1.0)
    assert partial_state["fees_total"] == pytest.approx(3.0)

    fills.append(
        {
            **_manual_fill("CLOSE_LAST"),
            "action": "sell",
            "quantity": 1,
            "fill_price": 120.0,
            "fees": 1.0,
            "fill_timestamp": "2026-03-28T12:00:00+00:00",
        }
    )
    pd.DataFrame(fills).to_csv(manual_fills_path, index=False)
    fill_agent.run_fill_agent()

    final_state = pd.read_csv(data_dir / "portfolio_state.csv").iloc[0]
    cash = pd.read_csv(data_dir / "cash_state.csv").iloc[-1]
    events = pd.read_csv(data_dir / "event_log.csv")
    assert final_state["status"] == "closed"
    assert final_state["quantity"] == pytest.approx(1.0)
    assert final_state["realised_pnl_abs"] == pytest.approx(26.0)
    assert final_state["entry_fees_remaining"] == pytest.approx(0.0)
    assert final_state["fees_total"] == pytest.approx(4.0)
    assert cash["cash_balance"] == pytest.approx(100026.0)
    assert (events["event_type"] == "position_reduced").sum() == 1
    assert (events["event_type"] == "position_closed").sum() == 1
