from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from uuid import UUID, uuid4


EXPERIMENT_SCHEMA_VERSION = "1.0"


class StrategyValidationError(ValueError):
    pass


def _date(value: date | str, name: str) -> date:
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise StrategyValidationError(f"{name} must be an ISO date") from exc


def _decimal(value: Decimal | str | int | float, name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StrategyValidationError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise StrategyValidationError(f"{name} must be finite")
    return result


def _primitive(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_primitive(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in sorted(value.items())}
    if hasattr(value, "__dataclass_fields__"):
        return _primitive(asdict(value))
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _primitive(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SplitWindow:
    fold: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date

    def __post_init__(self) -> None:
        if self.fold < 1:
            raise StrategyValidationError("fold must be positive")
        for name in (
            "train_start",
            "train_end",
            "validation_start",
            "validation_end",
            "test_start",
            "test_end",
        ):
            object.__setattr__(self, name, _date(getattr(self, name), name))
        if not (
            self.train_start
            <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.test_start
            <= self.test_end
        ):
            raise StrategyValidationError(
                "train, validation, and test windows must be chronological and disjoint"
            )


@dataclass(frozen=True, slots=True)
class CostModel:
    spread_bps: Decimal
    slippage_bps: Decimal
    fee_bps: Decimal
    tax_bps: Decimal
    stress_multiplier: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        for name in (
            "spread_bps",
            "slippage_bps",
            "fee_bps",
            "tax_bps",
            "stress_multiplier",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if min(self.spread_bps, self.slippage_bps, self.fee_bps, self.tax_bps) < 0:
            raise StrategyValidationError("cost assumptions must not be negative")
        if self.stress_multiplier < 1:
            raise StrategyValidationError("cost stress multiplier must be at least 1")

    def round_trip_cost(self, notional: Decimal | str | int | float) -> Decimal:
        amount = _decimal(notional, "notional")
        if amount < 0:
            raise StrategyValidationError("notional must not be negative")
        bps = (
            self.spread_bps
            + (self.slippage_bps * Decimal("2"))
            + (self.fee_bps * Decimal("2"))
            + self.tax_bps
        ) * self.stress_multiplier
        return amount * bps / Decimal("10000")


@dataclass(frozen=True, slots=True)
class ExperimentSpec:
    experiment_id: UUID
    experiment_key: str
    strategy_version: str
    data_snapshot_id: str
    point_in_time_universe_checksum: str
    code_revision: str
    environment: str
    parameters: tuple[tuple[str, str], ...]
    splits: tuple[SplitWindow, ...]
    cost_model: CostModel
    tuned_on_partition: str
    created_at: datetime
    schema_version: str = EXPERIMENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiment_id", UUID(str(self.experiment_id)))
        moment = self.created_at
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise StrategyValidationError("created_at must be timezone-aware")
        object.__setattr__(self, "created_at", moment.astimezone(UTC))
        object.__setattr__(
            self,
            "parameters",
            tuple(sorted((str(key), str(value)) for key, value in self.parameters)),
        )
        object.__setattr__(self, "splits", tuple(self.splits))
        for name in (
            "strategy_version",
            "data_snapshot_id",
            "point_in_time_universe_checksum",
            "code_revision",
            "environment",
        ):
            if not str(getattr(self, name)).strip():
                raise StrategyValidationError(f"{name} must not be blank")
        if self.tuned_on_partition not in {"train", "validation"}:
            raise StrategyValidationError("parameters may not be tuned on test data")
        if not self.splits:
            raise StrategyValidationError("at least one chronological split is required")
        if self.experiment_key != content_hash(self.key_payload()):
            raise StrategyValidationError("experiment_key does not match immutable evidence")

    @classmethod
    def create(
        cls,
        *,
        strategy_version: str,
        data_snapshot_id: str,
        point_in_time_universe_checksum: str,
        code_revision: str,
        environment: str,
        parameters: Mapping[str, object],
        splits: tuple[SplitWindow, ...],
        cost_model: CostModel,
        tuned_on_partition: str = "validation",
        created_at: datetime | None = None,
        experiment_id: UUID | str | None = None,
    ) -> "ExperimentSpec":
        values = {
            "strategy_version": str(strategy_version).strip(),
            "data_snapshot_id": str(data_snapshot_id).strip(),
            "point_in_time_universe_checksum": str(
                point_in_time_universe_checksum
            ).strip(),
            "code_revision": str(code_revision).strip(),
            "environment": str(environment).strip().lower(),
            "parameters": tuple(
                sorted((str(key), str(value)) for key, value in parameters.items())
            ),
            "splits": tuple(splits),
            "cost_model": cost_model,
            "tuned_on_partition": str(tuned_on_partition).strip().lower(),
            "schema_version": EXPERIMENT_SCHEMA_VERSION,
        }
        key = content_hash(values)
        return cls(
            experiment_id=UUID(str(experiment_id)) if experiment_id else uuid4(),
            experiment_key=key,
            created_at=(created_at or datetime.now(UTC)),
            **values,
        )

    def key_payload(self) -> dict[str, Any]:
        return {
            "strategy_version": self.strategy_version,
            "data_snapshot_id": self.data_snapshot_id,
            "point_in_time_universe_checksum": self.point_in_time_universe_checksum,
            "code_revision": self.code_revision,
            "environment": self.environment,
            "parameters": self.parameters,
            "splits": self.splits,
            "cost_model": self.cost_model,
            "tuned_on_partition": self.tuned_on_partition,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExperimentMetrics:
    validation_excess_return_pct: Decimal
    test_excess_return_pct: Decimal
    test_max_drawdown_pct: Decimal
    stressed_test_excess_return_pct: Decimal
    benchmark_return_pct: Decimal
    walk_forward_passed_folds: int
    walk_forward_total_folds: int
    deterministic_reproduction: bool
    point_in_time_universe_passed: bool
    survivorship_checks_passed: bool
    exchange_calendar_passed: bool
    cost_model_passed: bool

    def __post_init__(self) -> None:
        for name in (
            "validation_excess_return_pct",
            "test_excess_return_pct",
            "test_max_drawdown_pct",
            "stressed_test_excess_return_pct",
            "benchmark_return_pct",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if self.walk_forward_total_folds < 1:
            raise StrategyValidationError("walk-forward evidence must contain a fold")
        if not 0 <= self.walk_forward_passed_folds <= self.walk_forward_total_folds:
            raise StrategyValidationError("walk-forward pass count is invalid")


@dataclass(frozen=True, slots=True)
class PromotionCriteria:
    minimum_validation_excess_return_pct: Decimal
    minimum_test_excess_return_pct: Decimal
    minimum_stressed_test_excess_return_pct: Decimal
    maximum_test_drawdown_pct: Decimal
    minimum_walk_forward_pass_fraction: Decimal

    def __post_init__(self) -> None:
        for name in (
            "minimum_validation_excess_return_pct",
            "minimum_test_excess_return_pct",
            "minimum_stressed_test_excess_return_pct",
            "maximum_test_drawdown_pct",
            "minimum_walk_forward_pass_fraction",
        ):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        if not Decimal("0") <= self.minimum_walk_forward_pass_fraction <= Decimal("1"):
            raise StrategyValidationError("walk-forward pass fraction must be within 0..1")


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    experiment_id: UUID
    eligible: bool
    capability: str
    reasons: tuple[str, ...]
    decided_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiment_id", UUID(str(self.experiment_id)))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.capability != "advisory_only":
            raise StrategyValidationError("strategy promotion can grant advisory_only only")


def evaluate_promotion(
    spec: ExperimentSpec,
    metrics: ExperimentMetrics,
    criteria: PromotionCriteria,
    *,
    now: datetime | None = None,
) -> PromotionDecision:
    reasons: list[str] = []
    checks = {
        "validation_benchmark": (
            metrics.validation_excess_return_pct
            >= criteria.minimum_validation_excess_return_pct
        ),
        "test_benchmark": (
            metrics.test_excess_return_pct >= criteria.minimum_test_excess_return_pct
        ),
        "stressed_costs": (
            metrics.stressed_test_excess_return_pct
            >= criteria.minimum_stressed_test_excess_return_pct
        ),
        "test_drawdown": (
            abs(metrics.test_max_drawdown_pct)
            <= abs(criteria.maximum_test_drawdown_pct)
        ),
        "walk_forward": (
            Decimal(metrics.walk_forward_passed_folds)
            / Decimal(metrics.walk_forward_total_folds)
            >= criteria.minimum_walk_forward_pass_fraction
        ),
        "deterministic_reproduction": metrics.deterministic_reproduction,
        "point_in_time_universe": metrics.point_in_time_universe_passed,
        "survivorship": metrics.survivorship_checks_passed,
        "exchange_calendar": metrics.exchange_calendar_passed,
        "cost_model": metrics.cost_model_passed,
        "test_not_tuned": spec.tuned_on_partition != "test",
    }
    reasons.extend(name for name, passed in checks.items() if not passed)
    return PromotionDecision(
        experiment_id=spec.experiment_id,
        eligible=not reasons,
        capability="advisory_only",
        reasons=tuple(reasons),
        decided_at=(now or datetime.now(UTC)).astimezone(UTC),
    )
