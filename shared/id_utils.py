# shared/id_utils.py

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

FILL_ID_PATTERN = re.compile(r"^(MANUAL|IBKR|T212)_[0-9]{8}T[0-9]{6}Z_[A-Z0-9]{6}$")


def generate_fill_id(source: str = "MANUAL") -> str:
    """
    Generate a unique fill_id in a standardised format.

    Example:
    MANUAL_20260316T223015Z_A1B2C3
    """
    source_clean = source.strip().upper()
    if source_clean not in {"MANUAL", "IBKR", "T212"}:
        raise ValueError(f"Unsupported fill id source: {source}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(3).upper()  # 6 hex chars
    return f"{source_clean}_{timestamp}_{suffix}"


def validate_fill_id(fill_id: str) -> bool:
    """
    Return True if fill_id matches the required system format.
    """
    if not isinstance(fill_id, str):
        return False
    return bool(FILL_ID_PATTERN.fullmatch(fill_id.strip()))


def assert_valid_fill_id(fill_id: str) -> None:
    """
    Raise ValueError if fill_id is invalid.
    """
    if not validate_fill_id(fill_id):
        raise ValueError(
            f"Invalid fill_id '{fill_id}'. Expected format like "
            f"MANUAL_20260316T223015Z_A1B2C3"
        )