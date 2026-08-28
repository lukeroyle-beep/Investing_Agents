from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from shared.paths import FRESHNESS_CONFIG_PATH


MODE_NORMAL = "normal"
MODE_DEGRADED = "degraded"
MODE_NO_TRADE = "no_trade"

OUTCOME_FRESH = "fresh"
OUTCOME_STALE = "stale"
OUTCOME_MISSING = "missing"
OUTCOME_MALFORMED = "malformed"
OUTCOME_FUTURE = "future"
OUTCOME_CONTRADICTORY = "contradictory"

CONTRADICTION_CLEAR = "clear"
CONTRADICTION_MATERIAL = "material"
CONTRADICTION_NOT_CHECKED = "not_checked"

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)([\"']?(?:authorization|x-api-key|x-user-key|api[_-]?key|token|secret|password)[\"']?)"
    r"\s*[:=]\s*([\"']?[^\s,;}]+)"
)


class FreshnessError(RuntimeError):
    pass


@dataclass(frozen=True)
class FreshnessPolicy:
    name: str
    kind: str
    max_age_seconds: int | None = None
    critical: bool = True


@dataclass(frozen=True)
class FreshnessConfig:
    schema_version: str
    calendar: str
    timezone: str
    future_tolerance_seconds: int
    policies: dict[str, FreshnessPolicy]
    material_relative_difference: float


@dataclass(frozen=True)
class FreshnessAssessment:
    source: str
    data_kind: str
    observation_time: str | None
    retrieval_time: str
    market_session: str | None
    calendar: str
    freshness_outcome: str
    contradiction_status: str
    mode: str
    reason: str

    @property
    def actionable(self) -> bool:
        return self.mode == MODE_NORMAL


def utc_now() -> datetime:
    return datetime.now(UTC)


def summarize_provider_error(error: object, *, limit: int = 240) -> str:
    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    text = _CREDENTIAL_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    return text[:limit]


def load_freshness_config(path: Path = FRESHNESS_CONFIG_PATH) -> FreshnessConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise FreshnessError(f"Cannot load freshness configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise FreshnessError("Freshness configuration must be a mapping")
    policies_raw = raw.get("policies")
    if not isinstance(policies_raw, dict) or not policies_raw:
        raise FreshnessError("Freshness configuration requires policies")
    policies: dict[str, FreshnessPolicy] = {}
    for name, value in policies_raw.items():
        if not isinstance(value, dict):
            raise FreshnessError(f"Freshness policy {name} must be a mapping")
        kind = str(value.get("kind", "")).strip()
        max_age = value.get("max_age_seconds")
        if kind not in {"max_age", "latest_completed_session"}:
            raise FreshnessError(f"Freshness policy {name} has unsupported kind={kind}")
        if kind == "max_age" and (not isinstance(max_age, int) or max_age <= 0):
            raise FreshnessError(f"Freshness policy {name} requires positive max_age_seconds")
        policies[str(name)] = FreshnessPolicy(
            name=str(name),
            kind=kind,
            max_age_seconds=max_age if isinstance(max_age, int) else None,
            critical=bool(value.get("critical", True)),
        )
    contradiction = raw.get("contradiction") or {}
    threshold = float(contradiction.get("material_relative_difference", 0.02))
    if not math.isfinite(threshold) or threshold <= 0:
        raise FreshnessError("material_relative_difference must be positive")
    return FreshnessConfig(
        schema_version=str(raw.get("schema_version", "")),
        calendar=str(raw.get("calendar", "XNYS")),
        timezone=str(raw.get("timezone", "America/New_York")),
        future_tolerance_seconds=int(raw.get("future_tolerance_seconds", 5)),
        policies=policies,
        material_relative_difference=threshold,
    )


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    cursor = date(year, month, 1)
    offset = (weekday - cursor.weekday()) % 7
    return cursor + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_equity_holidays(year: int) -> set[date]:
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }


@dataclass
class ExchangeCalendar:
    name: str = "XNYS"
    timezone: str = "America/New_York"
    additional_holidays: set[date] = field(default_factory=set)
    early_closes: dict[date, time] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_session(self, session: date) -> bool:
        holidays = set().union(
            *(us_equity_holidays(year) for year in (session.year - 1, session.year, session.year + 1))
        )
        return (
            session.weekday() < 5
            and session not in holidays
            and session not in self.additional_holidays
        )

    def session_close(self, session: date) -> datetime:
        close_time = self.early_closes.get(session)
        if close_time is None:
            thanksgiving = _nth_weekday(session.year, 11, 3, 4)
            standard_early_close = session in {
                thanksgiving + timedelta(days=1),
                date(session.year, 7, 3),
                date(session.year, 12, 24),
            }
            close_time = time(13, 0) if standard_early_close else time(16, 0)
        return datetime.combine(session, close_time, self.tz)

    def previous_session(self, session: date) -> date:
        cursor = session - timedelta(days=1)
        for _ in range(370):
            if self.is_session(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise FreshnessError("Could not resolve a prior trading session")

    def latest_completed_session(self, now: datetime) -> date:
        if now.tzinfo is None:
            raise FreshnessError("Clock values must be timezone-aware")
        local_now = now.astimezone(self.tz)
        today = local_now.date()
        if self.is_session(today) and local_now >= self.session_close(today):
            return today
        return self.previous_session(today + timedelta(days=1) if not self.is_session(today) else today)


def _parse_aware(value: object, field_name: str) -> datetime:
    if value is None or str(value).strip() == "":
        raise FreshnessError(f"{field_name} is missing")
    try:
        parsed = pd.Timestamp(value)
    except Exception as exc:
        raise FreshnessError(f"{field_name} is malformed") from exc
    if pd.isna(parsed) or parsed.tzinfo is None:
        raise FreshnessError(f"{field_name} must be timezone-aware")
    return parsed.to_pydatetime().astimezone(UTC)


def _mode_for_failure(policy: FreshnessPolicy) -> str:
    return MODE_NO_TRADE if policy.critical else MODE_DEGRADED


def assess_freshness(
    *,
    source: str,
    data_kind: str,
    observation_time: object,
    retrieval_time: object,
    now: datetime | None = None,
    config: FreshnessConfig | None = None,
    calendar: ExchangeCalendar | None = None,
    provider_error: object | None = None,
    observed_value: float | None = None,
    comparison_value: float | None = None,
) -> FreshnessAssessment:
    resolved_config = config or load_freshness_config()
    try:
        policy = resolved_config.policies[data_kind]
    except KeyError as exc:
        raise FreshnessError(f"No freshness policy configured for {data_kind}") from exc
    resolved_now = now or utc_now()
    if resolved_now.tzinfo is None:
        raise FreshnessError("now must be timezone-aware")
    resolved_calendar = calendar or ExchangeCalendar(
        name=resolved_config.calendar,
        timezone=resolved_config.timezone,
    )

    try:
        retrieved = _parse_aware(retrieval_time, "retrieval_time")
    except FreshnessError as exc:
        return FreshnessAssessment(
            source=source,
            data_kind=data_kind,
            observation_time=None,
            retrieval_time=str(retrieval_time or ""),
            market_session=None,
            calendar=resolved_calendar.name,
            freshness_outcome=OUTCOME_MALFORMED,
            contradiction_status=CONTRADICTION_NOT_CHECKED,
            mode=_mode_for_failure(policy),
            reason=str(exc),
        )

    future_tolerance = timedelta(seconds=resolved_config.future_tolerance_seconds)
    resolved_now_utc = resolved_now.astimezone(UTC)
    if retrieved > resolved_now_utc + future_tolerance:
        return FreshnessAssessment(
            source=source,
            data_kind=data_kind,
            observation_time=str(observation_time or "") or None,
            retrieval_time=retrieved.isoformat(),
            market_session=None,
            calendar=resolved_calendar.name,
            freshness_outcome=OUTCOME_FUTURE,
            contradiction_status=CONTRADICTION_NOT_CHECKED,
            mode=_mode_for_failure(policy),
            reason="retrieval_time is in the future",
        )

    if provider_error is not None and str(provider_error).strip():
        return FreshnessAssessment(
            source=source,
            data_kind=data_kind,
            observation_time=str(observation_time or "") or None,
            retrieval_time=retrieved.isoformat(),
            market_session=None,
            calendar=resolved_calendar.name,
            freshness_outcome=OUTCOME_MISSING,
            contradiction_status=CONTRADICTION_NOT_CHECKED,
            mode=_mode_for_failure(policy),
            reason=summarize_provider_error(provider_error),
        )

    try:
        observed = _parse_aware(observation_time, "observation_time")
    except FreshnessError as exc:
        outcome = OUTCOME_MISSING if "missing" in str(exc) else OUTCOME_MALFORMED
        return FreshnessAssessment(
            source=source,
            data_kind=data_kind,
            observation_time=None,
            retrieval_time=retrieved.isoformat(),
            market_session=None,
            calendar=resolved_calendar.name,
            freshness_outcome=outcome,
            contradiction_status=CONTRADICTION_NOT_CHECKED,
            mode=_mode_for_failure(policy),
            reason=str(exc),
        )

    if observed > resolved_now_utc + future_tolerance or observed > retrieved + future_tolerance:
        return FreshnessAssessment(
            source=source,
            data_kind=data_kind,
            observation_time=observed.isoformat(),
            retrieval_time=retrieved.isoformat(),
            market_session=None,
            calendar=resolved_calendar.name,
            freshness_outcome=OUTCOME_FUTURE,
            contradiction_status=CONTRADICTION_NOT_CHECKED,
            mode=_mode_for_failure(policy),
            reason="observation_time is in the future or later than retrieval_time",
        )

    latest_session = resolved_calendar.latest_completed_session(resolved_now)
    if policy.kind == "latest_completed_session":
        observation_session = pd.Timestamp(observation_time).date()
        market_session = observation_session.isoformat()
        if observation_session > latest_session:
            return FreshnessAssessment(
                source=source,
                data_kind=data_kind,
                observation_time=observed.isoformat(),
                retrieval_time=retrieved.isoformat(),
                market_session=market_session,
                calendar=resolved_calendar.name,
                freshness_outcome=OUTCOME_FUTURE,
                contradiction_status=CONTRADICTION_NOT_CHECKED,
                mode=_mode_for_failure(policy),
                reason=(
                    f"observation session {observation_session} is later than latest "
                    f"completed session {latest_session}"
                ),
            )
        fresh = observation_session == latest_session
        reason = (
            "observation covers the latest completed exchange session"
            if fresh
            else f"latest observation session {observation_session} precedes {latest_session}"
        )
    else:
        market_session = observed.astimezone(resolved_calendar.tz).date().isoformat()
        assert policy.max_age_seconds is not None
        age = resolved_now.astimezone(UTC) - observed
        fresh = age <= timedelta(seconds=policy.max_age_seconds)
        reason = (
            f"observation age {age.total_seconds():.3f}s is within policy"
            if fresh
            else f"observation age {age.total_seconds():.3f}s exceeds {policy.max_age_seconds}s"
        )

    contradiction_status = CONTRADICTION_NOT_CHECKED
    if observed_value is not None and comparison_value is not None:
        observed_number = float(observed_value)
        comparison_number = float(comparison_value)
        if not math.isfinite(observed_number) or not math.isfinite(comparison_number):
            return FreshnessAssessment(
                source=source,
                data_kind=data_kind,
                observation_time=observed.isoformat(),
                retrieval_time=retrieved.isoformat(),
                market_session=market_session,
                calendar=resolved_calendar.name,
                freshness_outcome=OUTCOME_MALFORMED,
                contradiction_status=CONTRADICTION_MATERIAL,
                mode=MODE_NO_TRADE,
                reason="provider comparison contains a non-finite value",
            )
        denominator = max(abs(observed_number), abs(comparison_number), 1e-12)
        relative_difference = abs(observed_number - comparison_number) / denominator
        contradiction_status = (
            CONTRADICTION_MATERIAL
            if relative_difference > resolved_config.material_relative_difference
            else CONTRADICTION_CLEAR
        )
        if contradiction_status == CONTRADICTION_MATERIAL:
            return FreshnessAssessment(
                source=source,
                data_kind=data_kind,
                observation_time=observed.isoformat(),
                retrieval_time=retrieved.isoformat(),
                market_session=market_session,
                calendar=resolved_calendar.name,
                freshness_outcome=OUTCOME_CONTRADICTORY,
                contradiction_status=contradiction_status,
                mode=MODE_NO_TRADE,
                reason="material contradiction between critical provider values",
            )

    return FreshnessAssessment(
        source=source,
        data_kind=data_kind,
        observation_time=observed.isoformat(),
        retrieval_time=retrieved.isoformat(),
        market_session=market_session,
        calendar=resolved_calendar.name,
        freshness_outcome=OUTCOME_FRESH if fresh else OUTCOME_STALE,
        contradiction_status=contradiction_status,
        mode=MODE_NORMAL if fresh else _mode_for_failure(policy),
        reason=reason,
    )


def assert_actionable_health_frame(
    frame: pd.DataFrame,
    *,
    now: datetime | None = None,
    config: FreshnessConfig | None = None,
    calendar: ExchangeCalendar | None = None,
) -> None:
    if frame.empty:
        raise FreshnessError("Critical data-source health evidence is absent")
    required = {
        "source",
        "data_kind",
        "error",
        "observation_time",
        "retrieval_time",
        "mode",
        "freshness_outcome",
        "contradiction_status",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FreshnessError(f"Data-source health is missing centralized fields: {missing}")
    modes = frame["mode"].astype(str).str.strip().str.lower()
    invalid_modes = sorted(set(modes) - {MODE_NORMAL, MODE_DEGRADED, MODE_NO_TRADE})
    if invalid_modes:
        raise FreshnessError(f"Data-source health contains invalid modes: {invalid_modes}")
    synthetic = frame[
        frame.get("source", pd.Series(index=frame.index, dtype=str))
        .astype(str)
        .str.strip()
        .str.lower()
        .eq("synthetic_bootstrap")
    ]
    if not synthetic.empty:
        raise FreshnessError(
            "Synthetic bootstrap health is structural evidence only and cannot authorize a run"
        )
    unsafe = frame[modes == MODE_NO_TRADE]
    if not unsafe.empty:
        affected = sorted(set(unsafe.get("ticker", pd.Series(dtype=str)).astype(str).str.upper()))
        raise FreshnessError(f"Critical data ambiguity requires no_trade: {affected}")

    material = frame[
        frame["contradiction_status"].astype(str).str.strip().str.lower()
        == CONTRADICTION_MATERIAL
    ]
    if not material.empty:
        raise FreshnessError("Material provider contradiction requires no_trade")

    resolved_now = now or utc_now()
    resolved_config = config or load_freshness_config()
    current_no_trade: list[str] = []
    for _, row in frame.iterrows():
        assessment = assess_freshness(
            source=str(row.get("source", "")),
            data_kind=str(row.get("data_kind", "")).strip(),
            observation_time=row.get("observation_time", ""),
            retrieval_time=row.get("retrieval_time", ""),
            now=resolved_now,
            config=resolved_config,
            calendar=calendar,
            provider_error=row.get("error", ""),
        )
        if assessment.mode == MODE_NO_TRADE:
            current_no_trade.append(str(row.get("ticker", "")).strip().upper())
    if current_no_trade:
        raise FreshnessError(
            "Critical data evidence is no longer fresh at evaluation time: "
            f"{sorted(set(current_no_trade))}"
        )


def assert_actionable_health(
    path: Path,
    *,
    now: datetime | None = None,
    config: FreshnessConfig | None = None,
    calendar: ExchangeCalendar | None = None,
) -> None:
    if not path.exists():
        raise FreshnessError("Critical data-source health artifact is missing")
    try:
        frame = pd.read_csv(path, keep_default_na=False)
    except Exception as exc:
        raise FreshnessError(f"Critical data-source health artifact is malformed: {exc}") from exc
    assert_actionable_health_frame(
        frame,
        now=now,
        config=config,
        calendar=calendar,
    )
