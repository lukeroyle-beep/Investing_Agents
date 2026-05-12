from __future__ import annotations

import pandas as pd

from shared.mission_control_data_health import build_data_source_health_card


def test_data_source_health_card_reports_missing_artifact(tmp_path) -> None:
    card = build_data_source_health_card(tmp_path / "missing.csv")

    assert card["status"] == "Missing"
    assert card["total_checks"] == 0
    assert "No data_source_health.csv" in card["message"]


def test_data_source_health_card_reports_ok_when_all_checks_clean(tmp_path) -> None:
    path = tmp_path / "data_source_health.csv"
    pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "source": "fake",
                "error": "",
                "stale": False,
                "retry_count": 0,
                "fetched_at": "2026-05-01T12:00:00+00:00",
                "as_of": "2026-05-01T00:00:00+00:00",
            }
        ]
    ).to_csv(path, index=False)

    card = build_data_source_health_card(path)

    assert card == {
        "status": "OK",
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
            {
                "ticker": "AAPL",
                "source": "fake",
                "error": "provider down",
                "stale": False,
                "retry_count": 2,
                "fetched_at": "2026-05-01T12:00:00+00:00",
                "as_of": "",
            },
            {
                "ticker": "MSFT",
                "source": "fake",
                "error": "",
                "stale": True,
                "retry_count": 0,
                "fetched_at": "2026-05-01T12:01:00+00:00",
                "as_of": "2026-04-01T00:00:00+00:00",
            },
        ]
    ).to_csv(path, index=False)

    card = build_data_source_health_card(path)

    assert card["status"] == "Hold"
    assert card["total_checks"] == 2
    assert card["error_checks"] == 1
    assert card["stale_checks"] == 1
    assert card["affected_tickers"] == ["AAPL", "MSFT"]
