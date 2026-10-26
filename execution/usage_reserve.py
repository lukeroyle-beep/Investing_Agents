"""Fresh account-wide quota admission; no quota reset or credit bypass.

A broker send needs the user's 20% floor plus five percentage points reserved
for lookup and reconciliation. These percentages are allowances, not a promise
about the token cost of recovery. The caller must refresh from the usage tool.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


MINIMUM_FLOOR_PERCENT = Decimal("20")
MINIMUM_RECOVERY_RESERVE_PERCENT = Decimal("5")
MAX_USAGE_AGE_SECONDS = 60


class UsageEvidenceError(ValueError):
    """Malformed quota data cannot authorize another broker send."""


def _moment(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise UsageEvidenceError("timezone_aware_usage_time_required")
    return value.astimezone(timezone.utc)


def _percent(value):
    if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 100:
        raise UsageEvidenceError("finite_decimal_usage_percentage_required")
    return value


@dataclass(frozen=True, slots=True)
class UsageWindow:
    window_id: str
    remaining_percent: Decimal | None
    primary: bool = False

    def __post_init__(self):
        if not isinstance(self.window_id, str) or not self.window_id.strip():
            raise UsageEvidenceError("usage_window_identity_required")
        if type(self.primary) is not bool:
            raise UsageEvidenceError("typed_primary_window_flag_required")
        if self.remaining_percent is not None:
            _percent(self.remaining_percent)


@dataclass(frozen=True, slots=True)
class UsageAdmission:
    """Read-only quota snapshot checked again immediately before submission.

    The source's complete available-window identity list must match the supplied
    windows. At least one primary window must exist. An absent/null secondary
    does not invent another quota window; an available but unknown primary or
    secondary is a blocker. All available buckets participate in the minimum.
    """

    observed_at: datetime
    windows: tuple[UsageWindow, ...]
    available_window_ids: tuple[str, ...]
    source_reference: str
    source_sha256: str
    floor_percent: Decimal = MINIMUM_FLOOR_PERCENT
    recovery_reserve_percent: Decimal = MINIMUM_RECOVERY_RESERVE_PERCENT

    def __post_init__(self):
        object.__setattr__(self, "observed_at", _moment(self.observed_at))
        if not isinstance(self.windows, tuple) or not all(isinstance(item, UsageWindow) for item in self.windows):
            raise UsageEvidenceError("typed_usage_windows_required")
        ids = [item.window_id for item in self.windows]
        if len(set(ids)) != len(ids):
            raise UsageEvidenceError("duplicate_usage_window")
        if (not isinstance(self.available_window_ids, tuple)
                or any(not isinstance(value, str) or not value.strip() for value in self.available_window_ids)
                or len(set(self.available_window_ids)) != len(self.available_window_ids)):
            raise UsageEvidenceError("explicit_available_usage_windows_required")
        if (not isinstance(self.source_reference, str) or not self.source_reference.strip()
                or not isinstance(self.source_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", self.source_sha256) is None):
            raise UsageEvidenceError("usage_source_provenance_required")
        _percent(self.floor_percent)
        _percent(self.recovery_reserve_percent)
        if self.floor_percent < MINIMUM_FLOOR_PERCENT or self.recovery_reserve_percent < MINIMUM_RECOVERY_RESERVE_PERCENT:
            raise UsageEvidenceError("usage_floor_or_recovery_reserve_cannot_be_relaxed")
        if self.floor_percent + self.recovery_reserve_percent > 100:
            raise UsageEvidenceError("usage_floor_plus_reserve_exceeds_allowance")

    @property
    def minimum_remaining_percent(self) -> Decimal | None:
        if (not any(item.primary for item in self.windows)
                or set(self.available_window_ids) != {item.window_id for item in self.windows}
                or any(item.remaining_percent is None for item in self.windows)):
            return None
        return min(item.remaining_percent for item in self.windows)

    @property
    def required_submission_percent(self) -> Decimal:
        return self.floor_percent + self.recovery_reserve_percent

    def _evidence_blockers(self, now: datetime) -> list[str]:
        now = _moment(now)
        blockers = []
        age = (now - self.observed_at).total_seconds()
        if age < 0 or age > MAX_USAGE_AGE_SECONDS:
            blockers.append("usage_snapshot_stale_or_future")
        if not any(item.primary for item in self.windows):
            blockers.append("primary_usage_window_missing")
        if set(self.available_window_ids) != {item.window_id for item in self.windows}:
            blockers.append("available_usage_window_coverage_mismatch")
        if any(item.remaining_percent is None for item in self.windows):
            blockers.append("usage_window_allowance_unknown")
        return blockers

    def validate_for_submission(self, now: datetime) -> tuple[str, ...]:
        blockers = self._evidence_blockers(now)
        remaining = self.minimum_remaining_percent
        if remaining is not None and remaining < self.required_submission_percent:
            blockers.append("usage_recovery_reserve_insufficient")
        return tuple(blockers)

    def validate_for_work(self, now: datetime) -> tuple[str, ...]:
        blockers = self._evidence_blockers(now)
        remaining = self.minimum_remaining_percent
        if remaining is not None and remaining <= self.floor_percent:
            blockers.append("usage_remaining_floor_reached")
        return tuple(blockers)
