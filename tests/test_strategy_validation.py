from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest

from strategy.domain import (
    CostModel,
    ExperimentMetrics,
    ExperimentSpec,
    PromotionCriteria,
    StrategyValidationError,
    evaluate_promotion,
)
from strategy.registry import (
    ExperimentRegistry,
    ExperimentRegistryError,
    write_experiment_artifact,
)
from strategy.validation import (
    PointInTimeUniverse,
    assert_features_available_at_observation,
    chronological_split,
    rolling_walk_forward_splits,
)


DATES = pd.date_range("2020-01-01", periods=120, freq="B")


def _costs(stress: str = "2") -> CostModel:
    return CostModel(
        spread_bps=Decimal("5"),
        slippage_bps=Decimal("5"),
        fee_bps=Decimal("2"),
        tax_bps=Decimal("1"),
        stress_multiplier=Decimal(stress),
    )


def _spec(**overrides) -> ExperimentSpec:
    values = {
        "strategy_version": "quality_v2",
        "data_snapshot_id": "sha256:data",
        "point_in_time_universe_checksum": "sha256:universe",
        "code_revision": "abc123",
        "environment": "offline",
        "parameters": {"window": 20},
        "splits": (chronological_split(DATES),),
        "cost_model": _costs(),
        "tuned_on_partition": "validation",
        "created_at": datetime(2026, 8, 28, tzinfo=UTC),
    }
    values.update(overrides)
    return ExperimentSpec.create(**values)


def _metrics(**overrides) -> ExperimentMetrics:
    values = {
        "validation_excess_return_pct": "2",
        "test_excess_return_pct": "1",
        "test_max_drawdown_pct": "-8",
        "stressed_test_excess_return_pct": "0.5",
        "benchmark_return_pct": "5",
        "walk_forward_passed_folds": 3,
        "walk_forward_total_folds": 4,
        "deterministic_reproduction": True,
        "point_in_time_universe_passed": True,
        "survivorship_checks_passed": True,
        "exchange_calendar_passed": True,
        "cost_model_passed": True,
    }
    values.update(overrides)
    return ExperimentMetrics(**values)


def _criteria() -> PromotionCriteria:
    return PromotionCriteria(
        minimum_validation_excess_return_pct="0",
        minimum_test_excess_return_pct="0",
        minimum_stressed_test_excess_return_pct="0",
        maximum_test_drawdown_pct="15",
        minimum_walk_forward_pass_fraction="0.67",
    )


def test_chronological_and_walk_forward_splits_are_disjoint_and_deterministic():
    split = chronological_split(DATES)
    assert split.train_end < split.validation_start
    assert split.validation_end < split.test_start
    folds = rolling_walk_forward_splits(
        DATES,
        train_sessions=40,
        validation_sessions=10,
        test_sessions=10,
        step_sessions=10,
    )
    assert len(folds) == 7
    assert folds[0].test_end < folds[1].test_end
    assert folds == rolling_walk_forward_splits(
        DATES,
        train_sessions=40,
        validation_sessions=10,
        test_sessions=10,
        step_sessions=10,
    )


def test_point_in_time_universe_includes_delisted_names_before_delisting_only():
    universe = PointInTimeUniverse.from_frame(
        pd.DataFrame(
            [
                {
                    "internal_instrument_id": "id-a",
                    "symbol": "AAA",
                    "exchange": "XNYS",
                    "effective_from": "2020-01-01T00:00:00Z",
                    "effective_to": None,
                    "delisted_at": "2021-01-01T00:00:00Z",
                },
                {
                    "internal_instrument_id": "id-b",
                    "symbol": "BBB",
                    "exchange": "XNYS",
                    "effective_from": "2020-01-01T00:00:00Z",
                    "effective_to": None,
                    "delisted_at": None,
                },
            ]
        )
    )
    assert set(universe.members("2020-06-01")["symbol"]) == {"AAA", "BBB"}
    assert set(universe.members("2021-06-01")["symbol"]) == {"BBB"}
    assert universe.checksum


def test_future_available_features_are_rejected():
    frame = pd.DataFrame(
        {
            "observation": ["2026-01-01T10:00:00Z"],
            "available": ["2026-01-02T10:00:00Z"],
        }
    )
    with pytest.raises(StrategyValidationError, match="look-ahead"):
        assert_features_available_at_observation(
            frame,
            observation_column="observation",
            available_column="available",
        )


def test_cost_model_stress_and_test_tuning_guard():
    assert _costs("2").round_trip_cost("10000") == Decimal("40")
    with pytest.raises(StrategyValidationError, match="test"):
        _spec(tuned_on_partition="test")


def test_promotion_is_advisory_only_and_fails_on_any_missing_evidence():
    accepted = evaluate_promotion(_spec(), _metrics(), _criteria())
    assert accepted.eligible
    assert accepted.capability == "advisory_only"
    rejected = evaluate_promotion(
        _spec(),
        _metrics(
            deterministic_reproduction=False,
            survivorship_checks_passed=False,
            test_excess_return_pct="-1",
        ),
        _criteria(),
    )
    assert not rejected.eligible
    assert set(rejected.reasons) >= {
        "deterministic_reproduction",
        "survivorship",
        "test_benchmark",
    }


def test_experiment_artifact_and_registry_are_immutable(tmp_path):
    spec = _spec()
    source = tmp_path / "backtest_summary.csv"
    source.write_text("metric,value\nreturn,1\n", encoding="utf-8")
    artifact = write_experiment_artifact(
        tmp_path / "experiments",
        spec=spec,
        metrics=_metrics(),
        source_artifacts=(source,),
    )
    registry = ExperimentRegistry(tmp_path / "control" / "experiments.sqlite3")
    registry.record(spec, artifact)
    decision = evaluate_promotion(spec, _metrics(), _criteria())
    registry.record_promotion(
        spec=spec,
        decision=decision,
        operator_id="operator@example.test",
    )
    assert registry.latest_advisory_eligibility("quality_v2")
    with pytest.raises(ExperimentRegistryError, match="identity collision"):
        changed = _spec(experiment_id=spec.experiment_id, parameters={"window": 50})
        write_experiment_artifact(
            tmp_path / "experiments", spec=changed, metrics=_metrics()
        )
