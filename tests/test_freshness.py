from __future__ import annotations

from datetime import UTC, date, datetime, time

import pandas as pd
import pytest

from shared.freshness import (
    MODE_DEGRADED,
    MODE_NO_TRADE,
    MODE_NORMAL,
    OUTCOME_CONTRADICTORY,
    OUTCOME_FRESH,
    OUTCOME_FUTURE,
    OUTCOME_MALFORMED,
    OUTCOME_MISSING,
    OUTCOME_STALE,
    ExchangeCalendar,
    FreshnessConfig,
    FreshnessError,
    FreshnessPolicy,
    assess_freshness,
    assert_actionable_health_frame,
    load_freshness_config,
    summarize_provider_error,
)


def _daily_assessment(observation: str, now: datetime, **kwargs):
    return assess_freshness(
        source="test",
        data_kind="daily_research_price",
        observation_time=observation,
        retrieval_time=now.isoformat(),
        now=now,
        **kwargs,
    )


def test_exchange_calendar_handles_weekend_holiday_early_close_and_dst() -> None:
    saturday = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    assert _daily_assessment("2026-08-28T00:00:00+00:00", saturday).mode == MODE_NORMAL

    before_july_six_close = datetime(2026, 7, 6, 18, 0, tzinfo=UTC)
    holiday = _daily_assessment("2026-07-02T00:00:00+00:00", before_july_six_close)
    assert holiday.freshness_outcome == OUTCOME_FRESH

    after_early_close = datetime(2026, 11, 27, 18, 1, tzinfo=UTC)
    early = _daily_assessment("2026-11-27T00:00:00+00:00", after_early_close)
    assert early.freshness_outcome == OUTCOME_FRESH

    calendar = ExchangeCalendar()
    assert calendar.session_close(date(2026, 1, 5)).astimezone(UTC).hour == 21
    assert calendar.session_close(date(2026, 3, 9)).astimezone(UTC).hour == 20


def test_injected_calendar_supports_deterministic_holiday_and_early_close() -> None:
    calendar = ExchangeCalendar(
        additional_holidays={date(2026, 5, 4)},
        early_closes={date(2026, 5, 5): time(12, 0)},
    )
    now = datetime(2026, 5, 5, 16, 1, tzinfo=UTC)
    assessment = _daily_assessment(
        "2026-05-05T00:00:00+00:00",
        now,
        calendar=calendar,
    )
    assert assessment.mode == MODE_NORMAL


@pytest.mark.parametrize(
    ("observation", "retrieval", "outcome"),
    [
        ("", "2026-08-28T12:00:00+00:00", OUTCOME_MISSING),
        ("not-a-time", "2026-08-28T12:00:00+00:00", OUTCOME_MALFORMED),
        ("2026-08-28T12:00:00", "2026-08-28T12:00:00+00:00", OUTCOME_MALFORMED),
        ("2026-08-28T12:00:00+00:00", "not-a-time", OUTCOME_MALFORMED),
        ("2026-08-28T12:01:00+00:00", "2026-08-28T12:00:00+00:00", OUTCOME_FUTURE),
    ],
)
def test_critical_timestamp_ambiguity_is_no_trade(
    observation: str,
    retrieval: str,
    outcome: str,
) -> None:
    assessment = assess_freshness(
        source="broker",
        data_kind="broker_quote",
        observation_time=observation,
        retrieval_time=retrieval,
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )
    assert assessment.freshness_outcome == outcome
    assert assessment.mode == MODE_NO_TRADE


def test_stale_and_noncritical_degraded_policies_are_distinct() -> None:
    config = load_freshness_config()
    stale = assess_freshness(
        source="broker",
        data_kind="broker_quote",
        observation_time="2026-08-28T11:59:00+00:00",
        retrieval_time="2026-08-28T12:00:00+00:00",
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        config=config,
    )
    assert stale.freshness_outcome == OUTCOME_STALE
    assert stale.mode == MODE_NO_TRADE

    degraded_config = FreshnessConfig(
        schema_version="1.0",
        calendar="XNYS",
        timezone="America/New_York",
        future_tolerance_seconds=5,
        policies={"optional": FreshnessPolicy("optional", "max_age", 10, False)},
        material_relative_difference=0.02,
    )
    degraded = assess_freshness(
        source="research",
        data_kind="optional",
        observation_time="2026-08-28T11:59:00+00:00",
        retrieval_time="2026-08-28T12:00:00+00:00",
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        config=degraded_config,
    )
    assert degraded.mode == MODE_DEGRADED


def test_provider_errors_and_material_contradictions_fail_closed_and_redact() -> None:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    errored = assess_freshness(
        source="broker",
        data_kind="broker_quote",
        observation_time="",
        retrieval_time=now.isoformat(),
        now=now,
        provider_error='401 {"x-api-key":"super-secret"}',
    )
    assert errored.mode == MODE_NO_TRADE
    assert errored.freshness_outcome == OUTCOME_MISSING
    assert "super-secret" not in errored.reason
    assert "[REDACTED]" in errored.reason

    contradictory = assess_freshness(
        source="broker",
        data_kind="broker_quote",
        observation_time=now.isoformat(),
        retrieval_time=now.isoformat(),
        now=now,
        observed_value=100.0,
        comparison_value=110.0,
    )
    assert contradictory.mode == MODE_NO_TRADE
    assert contradictory.freshness_outcome == OUTCOME_CONTRADICTORY
    assert contradictory.contradiction_status == "material"


def test_health_gate_blocks_absent_and_no_trade_evidence() -> None:
    with pytest.raises(FreshnessError, match="absent"):
        assert_actionable_health_frame(pd.DataFrame())
    with pytest.raises(FreshnessError, match="no_trade"):
        assert_actionable_health_frame(
            pd.DataFrame(
                [
                    {
                        "ticker": "AAPL",
                        "source": "broker",
                        "data_kind": "broker_quote",
                        "error": "",
                        "observation_time": "2026-08-28T12:00:00+00:00",
                        "retrieval_time": "2026-08-28T12:00:00+00:00",
                        "mode": "no_trade",
                        "freshness_outcome": "stale",
                        "contradiction_status": "not_checked",
                    }
                ]
            )
        )

    with pytest.raises(FreshnessError, match="no longer fresh"):
        assert_actionable_health_frame(
            pd.DataFrame(
                [
                    {
                        "ticker": "AAPL",
                        "source": "broker",
                        "data_kind": "broker_quote",
                        "error": "",
                        "observation_time": "2026-08-28T11:58:00+00:00",
                        "retrieval_time": "2026-08-28T11:58:00+00:00",
                        "mode": "normal",
                        "freshness_outcome": "fresh",
                        "contradiction_status": "not_checked",
                    }
                ]
            ),
            now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        )


def test_config_pins_operational_freshness_defaults() -> None:
    config = load_freshness_config()
    assert config.policies["broker_quote"].max_age_seconds == 30
    assert config.policies["account_snapshot"].max_age_seconds == 15
    assert config.policies["order_snapshot"].max_age_seconds == 15
    assert config.policies["approval"].max_age_seconds == 300
    assert config.material_relative_difference == 0.02
    assert "credential" not in summarize_provider_error("token=credential")
