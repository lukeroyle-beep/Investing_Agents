from __future__ import annotations

import pytest

from shared.portfolio_state_helpers import (
    is_active_position_status,
    is_closed_position_status,
    normalise_exit_flag,
    normalise_position_status,
    parse_boolean_flag,
)


def test_parse_boolean_flag_accepts_existing_exit_flag_shapes() -> None:
    assert parse_boolean_flag(True) is True
    assert parse_boolean_flag(False) is False
    assert parse_boolean_flag("true") is True
    assert parse_boolean_flag("False") is False
    assert parse_boolean_flag("none") is False
    assert parse_boolean_flag("") is False
    assert parse_boolean_flag(1) is True
    assert parse_boolean_flag(0) is False


def test_parse_boolean_flag_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="Unrecognised boolean value"):
        parse_boolean_flag("maybe")


def test_lifecycle_status_helpers_share_canonical_interpretation() -> None:
    assert normalise_position_status(" Exit_Required ") == "exit_required"
    assert is_active_position_status("open") is True
    assert is_active_position_status("exit_required") is True
    assert is_closed_position_status("closed") is True
    assert normalise_exit_flag("true") == "true"
    assert normalise_exit_flag("False") == ""
