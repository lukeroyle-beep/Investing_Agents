from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from shared.mission_control_data_health import build_data_source_health_card


def _health_row(**overrides):
    row = {
        "ticker": "AAPL",
        "source": "fake",
        "data_kind": "daily_research_price",
        "error": "",
        "retry_count": 0,
        "observation_time": "2026-05-01T00:00:00+00:00",
        "retrieval_time": "2026-05-01T12:00:00+00:00",
        "market_session": "2026-05-01",
        "calendar": "XNYS",
        "freshness_outcome": "fresh",
        "contradiction_status": "not_checked",
        "mode": "normal",
        "reason": "fresh",
        "stale": False,
        "fetched_at": "2026-05-01T12:00:00+00:00",
        "as_of": "2026-05-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_data_source_health_card_reports_missing_artifact(tmp_path) -> None:
    card = build_data_source_health_card(tmp_path / "missing.csv")

    assert card["status"] == "Missing"
    assert card["total_checks"] == 0
    assert "No data_source_health.csv" in card["message"]


def test_data_source_health_card_reports_ok_when_all_checks_clean(tmp_path) -> None:
    path = tmp_path / "data_source_health.csv"
    pd.DataFrame(
        [
            _health_row()
        ]
    ).to_csv(path, index=False)

    card = build_data_source_health_card(
        path,
        now=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
    )

    assert card == {
        "status": "OK",
        "mode": "normal",
        "total_checks": 1,
        "error_checks": 0,
        "stale_checks": 0,
        "affected_tickers": [],
        "message": "Market data healthy across 1 provider checks.",
    }


def test_data_source_health_card_reports_hold_for_errors_and_stale_rows(tmp_path) -> None:
    path = tmp_path / "data_source_health.csv"
    pd.DataFrame(
        [
            _health_row(
                error="provider down",
                retry_count=2,
                freshness_outcome="missing",
                mode="no_trade",
                observation_time="",
                as_of="",
            ),
            _health_row(
                ticker="MSFT",
                stale=True,
                freshness_outcome="stale",
                mode="no_trade",
                observation_time="2026-04-01T00:00:00+00:00",
                as_of="2026-04-01T00:00:00+00:00",
            ),
        ]
    ).to_csv(path, index=False)

    card = build_data_source_health_card(
        path,
        now=datetime(2026, 5, 1, 21, 0, tzinfo=UTC),
    )

    assert card["status"] == "No Trade"
    assert card["mode"] == "no_trade"
    assert card["total_checks"] == 2
    assert card["error_checks"] == 1
    assert card["stale_checks"] == 1
    assert card["affected_tickers"] == ["AAPL", "MSFT"]
