"""Strategy experiment and advisory-promotion controls."""

from strategy.domain import (
    CostModel,
    ExperimentMetrics,
    ExperimentSpec,
    PromotionCriteria,
    PromotionDecision,
    SplitWindow,
    evaluate_promotion,
)

__all__ = [
    "CostModel",
    "ExperimentMetrics",
    "ExperimentSpec",
    "PromotionCriteria",
    "PromotionDecision",
    "SplitWindow",
    "evaluate_promotion",
]
