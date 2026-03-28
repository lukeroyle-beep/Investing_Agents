from __future__ import annotations

from typing import Any

import pandas as pd


ACTIVE_POSITION_STATUSES = {"open", "exit_required"}
CLOSED_POSITION_STATUS = "closed"
VALID_POSITION_STATUSES = ACTIVE_POSITION_STATUSES | {CLOSED_POSITION_STATUS}
VALID_POSITION_SIDES = {"long", "short"}


def normalise_position_status(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().lower()


def is_active_position_status(value: Any) -> bool:
    return normalise_position_status(value) in ACTIVE_POSITION_STATUSES


def is_closed_position_status(value: Any) -> bool:
    return normalise_position_status(value) == CLOSED_POSITION_STATUS


def parse_boolean_flag(value: Any) -> bool:
    if pd.isna(value):
        return False

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False

    text = str(value).strip().lower()

    if text in {"true", "1", "yes", "y"}:
        return True

    if text in {"false", "0", "no", "n", "", "none", "null", "nan"}:
        return False

    raise ValueError(f"Unrecognised boolean value: {value}")


def normalise_exit_flag(value: Any) -> str:
    return "true" if parse_boolean_flag(value) else ""
