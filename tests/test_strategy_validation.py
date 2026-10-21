from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json

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


def _run_artifact(root, spec, *, metrics=None, history="date,close\n2024-01-02,100\n",
                  folds="fold,return\n1,2\n", summary="run_id,return\nfirst,2\n"):
    source_dir = root / "sources"
    source_dir.mkdir(parents=True)
    files = []
    for name, contents in (
        ("historical_prices.csv", history),
        ("walk_forward.csv", folds),
        ("backtest_summary.csv", summary),
    ):
        path = source_dir / name
        path.write_text(contents, encoding="utf-8")
        files.append(path)
    return write_experiment_artifact(
        root / "experiments", spec=spec, metrics=metrics or _metrics(),
        source_artifacts=tuple(files),
    )


def test_identical_experiment_across_runs_reuses_identity_and_accepts_run_metadata(tmp_path):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    first = _spec()
    assert registry.resolve_identity(first) == first
    first_artifact = _run_artifact(tmp_path / "first", first)
    registry.record(first, first_artifact)

    candidate = _spec(created_at=datetime(2026, 10, 21, tzinfo=UTC))
    assert candidate.experiment_id != first.experiment_id
    assert candidate.experiment_key == first.experiment_key
    replay = registry.resolve_identity(candidate)
    assert replay == first
    artifact = _run_artifact(
        tmp_path / "second", replay, summary="run_id,return\nsecond,2\n"
    )
    registry.record(replay, artifact)
    with registry._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
        assert connection.execute("SELECT artifact_path FROM experiments").fetchone()[0] == str(first_artifact)


def test_replayed_experiment_rejects_changed_metrics(tmp_path):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    spec = _spec()
    first = _run_artifact(tmp_path / "first", spec)
    registry.record(spec, first)
    replay = registry.resolve_identity(_spec())
    changed = _run_artifact(
        tmp_path / "second", replay, metrics=_metrics(test_max_drawdown_pct="-9")
    )
    with pytest.raises(ExperimentRegistryError, match="metrics changed"):
        registry.record(replay, changed)


@pytest.mark.parametrize("change", ["history", "folds"])
def test_replayed_experiment_rejects_changed_stable_evidence(tmp_path, change):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    spec = _spec()
    registry.record(spec, _run_artifact(tmp_path / "first", spec))
    replay = registry.resolve_identity(_spec())
    changed = _run_artifact(tmp_path / "second", replay, **{change: "changed evidence\n"})
    with pytest.raises(ExperimentRegistryError, match="replay evidence changed"):
        registry.record(replay, changed)


def test_replayed_experiment_cannot_drop_stable_evidence(tmp_path):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    spec = _spec()
    registry.record(spec, _run_artifact(tmp_path / "first", spec))
    replay = registry.resolve_identity(_spec())
    empty = write_experiment_artifact(tmp_path / "second", spec=replay, metrics=_metrics())
    with pytest.raises(ExperimentRegistryError, match="replay evidence changed"):
        registry.record(replay, empty)


@pytest.mark.parametrize("filename", ["historical_prices.csv", "walk_forward.csv", "backtest_summary.csv"])
def test_registry_rechecks_stored_evidence_before_accepting_replay(tmp_path, filename):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    spec = _spec()
    first = _run_artifact(tmp_path / "first", spec)
    registry.record(spec, first)
    (first.parent / filename).write_text("tampered\n", encoding="utf-8")
    second = _run_artifact(tmp_path / "second", registry.resolve_identity(_spec()))
    with pytest.raises(ExperimentRegistryError, match="evidence integrity failed"):
        registry.record(spec, second)


def test_registry_rejects_missing_or_changed_stored_manifest(tmp_path):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    spec = _spec()
    first = _run_artifact(tmp_path / "first", spec)
    registry.record(spec, first)
    second = _run_artifact(tmp_path / "second", registry.resolve_identity(_spec()))
    first.write_text(first.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ExperimentRegistryError, match="artifact integrity failed"):
        registry.record(spec, second)
    first.unlink()
    with pytest.raises(ExperimentRegistryError, match="artifact integrity failed"):
        registry.record(spec, second)


def test_resolve_identity_rejects_inconsistent_stored_spec(tmp_path):
    registry = ExperimentRegistry(tmp_path / "experiments.sqlite3")
    spec = _spec()
    registry.record(spec, _run_artifact(tmp_path / "first", spec))
    with registry._connect() as connection:
        payload = json.loads(connection.execute("SELECT payload_json FROM experiments").fetchone()[0])
        payload["strategy_version"] = "tampered"
        connection.execute("UPDATE experiments SET payload_json = ?", (json.dumps(payload),))
    with pytest.raises(ExperimentRegistryError, match="immutable experiment changed"):
        registry.resolve_identity(_spec())


def test_same_artifact_directory_rejects_metric_and_source_changes(tmp_path):
    spec = _spec()
    artifact = _run_artifact(tmp_path / "first", spec)
    root = artifact.parent.parent
    assert write_experiment_artifact(root, spec=spec, metrics=_metrics()) == artifact
    with pytest.raises(ExperimentRegistryError, match="metrics changed"):
        write_experiment_artifact(root, spec=spec, metrics=_metrics(test_excess_return_pct="99"))
    sources = tuple(sorted((tmp_path / "first" / "sources").glob("*.csv")))
    assert write_experiment_artifact(root, spec=spec, metrics=_metrics(), source_artifacts=sources) == artifact
    (tmp_path / "first" / "sources" / "historical_prices.csv").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ExperimentRegistryError, match="source changed"):
        write_experiment_artifact(root, spec=spec, metrics=_metrics(), source_artifacts=sources)


def test_same_artifact_directory_rechecks_immutable_copy_hashes(tmp_path):
    spec = _spec()
    artifact = _run_artifact(tmp_path / "first", spec)
    (artifact.parent / "historical_prices.csv").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ExperimentRegistryError, match="evidence integrity failed"):
        write_experiment_artifact(artifact.parent.parent, spec=spec, metrics=_metrics())
