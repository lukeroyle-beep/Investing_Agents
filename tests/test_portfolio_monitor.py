from __future__ import annotations

import pytest

from shared.portfolio_monitor import merge_authoritative_monitor
from tests.helpers import (
    open_position_row,
    portfolio_monitor_frame,
    portfolio_monitor_row,
    portfolio_state_frame,
)


def test_monitor_marks_override_legacy_fill_snapshot_values() -> None:
    position = open_position_row(current_price=105.0, market_value=1050.0, pnl_abs=50.0)
    state = portfolio_state_frame([position])
    monitor = portfolio_monitor_frame(
        [
            portfolio_monitor_row(
                position,
                current_price=111.0,
                market_value=1110.0,
                pnl_abs=110.0,
                pnl_pct=11.0,
            )
        ]
    )

    merged = merge_authoritative_monitor(state, monitor)

    assert merged.iloc[0]["current_price"] == 111.0
    assert merged.iloc[0]["market_value"] == 1110.0
    assert merged.iloc[0]["pnl_abs"] == 110.0
    assert merged.iloc[0]["pnl_pct"] == 11.0


def test_monitor_missing_active_position_fails_closed() -> None:
    state = portfolio_state_frame([open_position_row()])
    empty_monitor = portfolio_monitor_frame([])

    with pytest.raises(ValueError, match=r"missing=\['POS001'\]"):
        merge_authoritative_monitor(state, empty_monitor)


def test_monitor_contradicting_economic_context_fails_closed() -> None:
    position = open_position_row(quantity=10.0)
    state = portfolio_state_frame([position])
    monitor = portfolio_monitor_frame(
        [portfolio_monitor_row(position, quantity=11.0)]
    )

    with pytest.raises(ValueError, match="field=quantity"):
        merge_authoritative_monitor(state, monitor)


def test_duplicate_monitor_position_identity_fails_closed() -> None:
    position = open_position_row()
    state = portfolio_state_frame([position])
    row = portfolio_monitor_row(position)
    monitor = portfolio_monitor_frame([row, row])

    with pytest.raises(ValueError, match="duplicate position_id"):
        merge_authoritative_monitor(state, monitor)
