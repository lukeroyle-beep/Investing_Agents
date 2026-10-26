"""Broker-neutral immutable source facts consumed by the pure risk evaluator.

Only the execution-side evidence bridge projects validated broker receipts into
this data contract. Risk imports no broker transport or adapter capabilities.
"""
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from execution.domain import _aware_utc, payload_hash, decimal_value


def epoch(value):
    delta = _aware_utc(value, "source_time") - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1000000)


@dataclass(frozen=True, slots=True)
class SourcePosition:
    broker_position_id: str
    broker_instrument_id: int
    current_exposure: Decimal


@dataclass(frozen=True, slots=True)
class RiskAccountSource:
    model_version: str
    source_evidence_sha256: str
    time_evidence_hash: str
    demo_customer_id: str
    currency: str
    equity: Decimal
    available_cash: Decimal
    invested_amount: Decimal
    broker_margin: Decimal
    current_pnl: Decimal
    positions: tuple[SourcePosition, ...]
    earliest_component_at: datetime
    latest_component_at: datetime
    assessed_at: datetime
    deadline_epoch: Decimal
    original_blockers: tuple[str, ...]
    age_limit: int = 15
    projection_hash: str = ""

    def __post_init__(self):
        for name in ("source_evidence_sha256", "time_evidence_hash"):
            value = getattr(self, name)
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("source_digest_invalid")
        if (self.model_version != "etoro-component-time-v1" or self.currency != "USD"
                or not self.demo_customer_id or type(self.age_limit) is not int
                or not 0 < self.age_limit <= 15):
            raise ValueError("source_contract_invalid")
        for name in ("earliest_component_at", "latest_component_at", "assessed_at"):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        if not self.earliest_component_at <= self.latest_component_at <= self.assessed_at:
            raise ValueError("source_component_clock_order_invalid")
        for name in ("equity", "available_cash", "invested_amount", "broker_margin", "current_pnl", "deadline_epoch"):
            object.__setattr__(self, name, decimal_value(getattr(self, name), name))
        if self.deadline_epoch > epoch(self.earliest_component_at) + self.age_limit:
            raise ValueError("source_deadline_exceeds_structure_expiry")
        if not all(isinstance(x, SourcePosition) for x in self.positions):
            raise ValueError("typed_source_positions_required")
        digest = payload_hash({x.name: getattr(self, x.name) for x in fields(self) if x.name != "projection_hash"})
        if self.projection_hash and self.projection_hash != digest:
            raise ValueError("source_projection_digest_mismatch")
        object.__setattr__(self, "projection_hash", digest)

    def validate_at(self, now, age_limit=15):
        blockers = list(self.original_blockers)
        if self.model_version != "etoro-component-time-v1" or age_limit > self.age_limit:
            blockers.append("source_time_contract_mismatch")
        moment = _aware_utc(now, "now")
        tightened_deadline = self.deadline_epoch - Decimal(max(0, self.age_limit - age_limit))
        if moment < self.assessed_at or epoch(moment) > tightened_deadline:
            blockers.append("source_component_evidence_stale_or_future")
        return tuple(blockers)

    def freshness_deadline(self, age_limit=15):
        # datetime supports only microseconds; round down, never extend a source
        # nanosecond deadline to the next representable instant.
        seconds = self.deadline_epoch - Decimal(max(0, self.age_limit - age_limit))
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=int(seconds * 1000000))
