from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path

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


def _text_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_recovery_requires_exact_decimal_not_half_cent_tolerance() -> None:
    fill = pd.Series(
        {
            "fill_id": "FILL_EXACT",
            "ticker": "AAPL",
            "side": "long",
            "action": "buy",
            "quantity": "1",
            "fill_price": "10",
            "fees": "0",
            "fill_timestamp": "2026-03-28T10:00:00+00:00",
        }
    )
    near = pd.Series(
        {
            "notes": "",
            "ticker": "AAPL",
            "side": "long",
            "action": "buy",
            "event_type": "position_open",
            "amount": "-10.004",
            "fees": "0",
        }
    )
    exact = near.copy()
    exact["amount"] = "-10.0"
    assert fill_agent._cash_ledger_row_matches_fill(exact, fill, "FILL_EXACT")
    assert not fill_agent._cash_ledger_row_matches_fill(near, fill, "FILL_EXACT")

    opened = pd.Series(
        {
            "ticker": "AAPL",
            "side": "long",
            "quantity": "1",
            "entry_price": "10.004",
            "entry_date": "2026-03-28T10:00:00+00:00",
            "status": "open",
            "exit_price": "",
            "closed_at": "",
        }
    )
    same = opened.copy()
    same["entry_price"] = "10.00"
    assert fill_agent._portfolio_row_matches_fill(same, fill)
    assert not fill_agent._portfolio_row_matches_fill(opened, fill)


def test_repeated_partial_fills_sum_exactly_and_cash_matches_audit(
    isolated_workspace, monkeypatch
) -> None:
    data_dir = isolated_workspace / "data"
    manual = data_dir / "manual_fills.csv"
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_EXACT_PARTIALS")

    def fill(fill_id: str, action: str, quantity: str, stamp: str) -> dict[str, str]:
        return {
            "fill_id": fill_id,
            "ticker": "AAPL",
            "side": "long",
            "action": action,
            "quantity": quantity,
            "fill_price": "0.1",
            "fees": "0.01" if action == "buy" else "0",
            "fill_timestamp": stamp,
        }

    first = [
        fill("OPEN_EXACT", "buy", "0.3", "2026-03-28T10:00:00+00:00"),
        fill("SELL_1", "sell", "0.1", "2026-03-28T11:00:00+00:00"),
        fill("SELL_2", "sell", "0.1", "2026-03-28T12:00:00+00:00"),
    ]
    pd.DataFrame(first).to_csv(manual, index=False)
    fill_agent.run_fill_agent()

    midway = _text_rows(data_dir / "portfolio_state.csv")[0]
    assert midway["status"] == "open"
    assert midway["quantity"] == "0.1"
    assert midway["entry_fees_remaining"] == "0.01"
    assert Decimal(midway["quantity"]) + Decimal("0.1") + Decimal("0.1") == Decimal("0.3")

    first.append(fill("SELL_3", "sell", "0.1", "2026-03-28T13:00:00+00:00"))
    pd.DataFrame(first).to_csv(manual, index=False)
    fill_agent.run_fill_agent()

    final = _text_rows(data_dir / "portfolio_state.csv")[0]
    assert final["status"] == "closed"
    assert final["entry_fees_remaining"] == "0"
    assert Decimal(final["realised_pnl_abs"]) == Decimal("-0.01")
    sold = sum(Decimal(item["quantity"]) for item in first if item["action"] == "sell")
    assert sold == Decimal(first[0]["quantity"])

    cash_rows = _text_rows(data_dir / "cash_state.csv")
    ledger = _text_rows(data_dir / "cash_ledger.csv")
    events = [
        json.loads(row["after_json"])
        for row in _text_rows(data_dir / "event_log.csv")
        if row["event_type"] == "cash_adjusted"
    ]
    assert [row["cash_balance_after"] for row in ledger] == [event["cash_balance"] for event in events]
    assert cash_rows[-1]["cash_balance"] == events[-1]["cash_balance"]
    assert cash_rows[-1]["cash_balance"] == "99999.99"
    assert Decimal(ledger[0]["amount"]) == Decimal("-0.03")
    assert sum((Decimal(row["amount"]) for row in ledger), Decimal(0)) == Decimal(0)


def test_full_close_does_not_treat_a_smaller_quantity_as_complete(
    isolated_workspace, monkeypatch
) -> None:
    data_dir = isolated_workspace / "data"
    manual = data_dir / "manual_fills.csv"
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_EXACT_OVERSELL")
    opened = {
        "fill_id": "OPEN_TINY",
        "ticker": "AAPL",
        "side": "long",
        "action": "buy",
        "quantity": "0.3",
        "fill_price": "0.1",
        "fees": "0",
        "fill_timestamp": "2026-03-28T10:00:00+00:00",
    }
    pd.DataFrame([opened]).to_csv(manual, index=False)
    fill_agent.run_fill_agent()
    before = (data_dir / "portfolio_state.csv").read_bytes()

    pd.DataFrame(
        [
            opened,
            {
                "fill_id": "SELL_TINY",
                "ticker": "AAPL",
                "side": "long",
                "action": "sell",
                "quantity": "0.3000000001",
                "fill_price": "0.1",
                "fees": "0",
                "fill_timestamp": "2026-03-28T11:00:00+00:00",
            },
        ]
    ).to_csv(manual, index=False)
    with pytest.raises(RuntimeError, match="exceeds the active position"):
        fill_agent.run_fill_agent()

    assert (data_dir / "portfolio_state.csv").read_bytes() == before
    position = _text_rows(data_dir / "portfolio_state.csv")[0]
    assert position["status"] == "open"
    assert position["quantity"] == "0.3"


def test_legacy_decimal_spellings_still_close_and_match_audit(
    isolated_workspace, monkeypatch
) -> None:
    data_dir = isolated_workspace / "data"
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_LEGACY_SPELLING")
    pd.DataFrame(
        [
            {
                "position_id": "POS_LEGACY",
                "ticker": "AAPL",
                "side": "long",
                "status": "open",
                "quantity": "1.0",
                "entry_price": "10.0",
                "entry_date": "2026-03-28T09:00:00+00:00",
                "current_price": "10.0",
                "market_value": "10.0",
                "pnl_abs": "0.0",
                "pnl_pct": "0.0",
                "realised_pnl_abs": "0.0",
                "fees_total": "0.0",
                "entry_fees_remaining": "0.0",
                "exit_flag": "False",
                "exit_reason": "",
                "last_updated": "2026-03-28T09:00:00+00:00",
                "run_id": "RUN_OLD",
                "closed_at": "",
                "exit_price": "",
            }
        ]
    ).to_csv(data_dir / "portfolio_state.csv", index=False)
    pd.DataFrame(
        [{"as_of": "2026-03-28T09:00:00+00:00", "cash_balance": "99990.0"}]
    ).to_csv(data_dir / "cash_state.csv", index=False)
    pd.DataFrame(
        [
            {
                "fill_id": "SELL_LEGACY",
                "ticker": "AAPL",
                "side": "long",
                "action": "sell",
                "quantity": "1",
                "fill_price": "10",
                "fees": "0",
                "fill_timestamp": "2026-03-28T12:00:00+00:00",
            }
        ]
    ).to_csv(data_dir / "manual_fills.csv", index=False)

    fill_agent.run_fill_agent()

    position = _text_rows(data_dir / "portfolio_state.csv")[0]
    cash = _text_rows(data_dir / "cash_state.csv")[-1]["cash_balance"]
    audit = json.loads(
        next(
            row["after_json"]
            for row in _text_rows(data_dir / "event_log.csv")
            if row["event_type"] == "cash_adjusted"
        )
    )["cash_balance"]
    assert position["status"] == "closed"
    assert position["entry_fees_remaining"] == "0"
    assert cash == audit == "100000"
