from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class SchedulerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    enabled: bool
    environment: str
    broker_submission_enabled: bool
    per_order_human_approval_required: bool
    approval_ttl_seconds: int
    heartbeat_interval_seconds: int
    minimum_trading_sessions: int
    minimum_approved_mutation_cycles: int
    required_fault_drills: tuple[str, ...]

    @classmethod
    def load(cls, path: Path | str) -> "SchedulerConfig":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            qualification = raw["qualification"]
            result = cls(
                enabled=raw["enabled"] is True,
                environment=str(raw["environment"]).strip().lower(),
                broker_submission_enabled=raw["broker_submission_enabled"] is True,
                per_order_human_approval_required=(
                    raw["per_order_human_approval_required"] is True
                ),
                approval_ttl_seconds=int(raw["approval_ttl_seconds"]),
                heartbeat_interval_seconds=int(raw["heartbeat_interval_seconds"]),
                minimum_trading_sessions=int(
                    qualification["minimum_trading_sessions"]
                ),
                minimum_approved_mutation_cycles=int(
                    qualification["minimum_approved_mutation_cycles"]
                ),
                required_fault_drills=tuple(
                    str(item).strip() for item in qualification["required_fault_drills"]
                ),
            )
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
            raise SchedulerConfigurationError(
                "scheduler configuration is missing or invalid"
            ) from exc
        if result.environment != "demo":
            raise SchedulerConfigurationError("scheduler supports Demo only")
        if result.broker_submission_enabled:
            raise SchedulerConfigurationError(
                "scheduler may never receive broker submission capability"
            )
        if not result.per_order_human_approval_required:
            raise SchedulerConfigurationError("every scheduled order requires approval")
        if result.approval_ttl_seconds <= 0 or result.approval_ttl_seconds > 300:
            raise SchedulerConfigurationError("approval TTL exceeds the five-minute cap")
        if result.heartbeat_interval_seconds <= 0:
            raise SchedulerConfigurationError("heartbeat interval must be positive")
        if result.minimum_trading_sessions < 30:
            raise SchedulerConfigurationError("Gate C requires at least 30 sessions")
        if result.minimum_approved_mutation_cycles < 20:
            raise SchedulerConfigurationError("Gate C requires at least 20 cycles")
        if not result.required_fault_drills or any(
            not item for item in result.required_fault_drills
        ):
            raise SchedulerConfigurationError("required fault drills are incomplete")
        return result
