from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import pandas as pd

from strategy.domain import SplitWindow, StrategyValidationError, content_hash


def chronological_split(
    dates: Iterable[object],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> SplitWindow:
    ordered = pd.DatetimeIndex(pd.to_datetime(list(dates))).sort_values().unique()
    if len(ordered) < 5:
        raise StrategyValidationError("chronological split needs at least five sessions")
    if not 0 < train_fraction < 1 or not 0 < validation_fraction < 1:
        raise StrategyValidationError("split fractions must be within 0..1")
    train_end = max(1, int(len(ordered) * train_fraction))
    validation_end = max(train_end + 1, int(len(ordered) * (train_fraction + validation_fraction)))
    if validation_end >= len(ordered):
        raise StrategyValidationError("split fractions leave no test observations")
    return SplitWindow(
        fold=1,
        train_start=ordered[0].date(),
        train_end=ordered[train_end - 1].date(),
        validation_start=ordered[train_end].date(),
        validation_end=ordered[validation_end - 1].date(),
        test_start=ordered[validation_end].date(),
        test_end=ordered[-1].date(),
    )


def rolling_walk_forward_splits(
    dates: Iterable[object],
    *,
    train_sessions: int,
    validation_sessions: int,
    test_sessions: int,
    step_sessions: int | None = None,
) -> tuple[SplitWindow, ...]:
    ordered = pd.DatetimeIndex(pd.to_datetime(list(dates))).sort_values().unique()
    values = [item.date() for item in ordered]
    step = step_sessions or test_sessions
    if min(train_sessions, validation_sessions, test_sessions, step) <= 0:
        raise StrategyValidationError("walk-forward window sizes must be positive")
    total = train_sessions + validation_sessions + test_sessions
    folds: list[SplitWindow] = []
    start = 0
    while start + total <= len(values):
        train_end = start + train_sessions - 1
        validation_start = train_end + 1
        validation_end = validation_start + validation_sessions - 1
        test_start = validation_end + 1
        test_end = test_start + test_sessions - 1
        folds.append(
            SplitWindow(
                fold=len(folds) + 1,
                train_start=values[start],
                train_end=values[train_end],
                validation_start=values[validation_start],
                validation_end=values[validation_end],
                test_start=values[test_start],
                test_end=values[test_end],
            )
        )
        start += step
    if not folds:
        raise StrategyValidationError("insufficient sessions for one walk-forward fold")
    return tuple(folds)


@dataclass(frozen=True)
class PointInTimeUniverse:
    frame: pd.DataFrame
    checksum: str

    @classmethod
    def from_frame(cls, frame: pd.DataFrame) -> "PointInTimeUniverse":
        required = {
            "internal_instrument_id",
            "symbol",
            "exchange",
            "effective_from",
            "effective_to",
            "delisted_at",
        }
        missing = sorted(required - set(frame.columns))
        if missing:
            raise StrategyValidationError(f"point-in-time universe missing: {missing}")
        value = frame.copy()
        for column in ("effective_from", "effective_to", "delisted_at"):
            value[column] = pd.to_datetime(value[column], utc=True, errors="coerce")
        if value["effective_from"].isna().any():
            raise StrategyValidationError("effective_from must be known")
        if (
            value["effective_to"].notna()
            & (value["effective_to"] <= value["effective_from"])
        ).any():
            raise StrategyValidationError("universe effective interval is invalid")
        if value.duplicated(
            ["internal_instrument_id", "effective_from"], keep=False
        ).any():
            raise StrategyValidationError("point-in-time universe has duplicate identity")
        records = value.fillna("").astype(str).to_dict(orient="records")
        return cls(frame=value, checksum=content_hash(records))

    def members(self, as_of: date | str | pd.Timestamp) -> pd.DataFrame:
        moment = pd.Timestamp(as_of)
        moment = moment.tz_localize("UTC") if moment.tzinfo is None else moment.tz_convert("UTC")
        active = self.frame["effective_from"] <= moment
        active &= self.frame["effective_to"].isna() | (self.frame["effective_to"] > moment)
        active &= self.frame["delisted_at"].isna() | (self.frame["delisted_at"] > moment)
        return self.frame.loc[active].copy().reset_index(drop=True)


def assert_features_available_at_observation(
    frame: pd.DataFrame,
    *,
    observation_column: str,
    available_column: str,
) -> None:
    observation = pd.to_datetime(frame[observation_column], utc=True, errors="coerce")
    available = pd.to_datetime(frame[available_column], utc=True, errors="coerce")
    if observation.isna().any() or available.isna().any():
        raise StrategyValidationError("feature timestamps are missing or malformed")
    if (available > observation).any():
        raise StrategyValidationError("look-ahead feature availability detected")
