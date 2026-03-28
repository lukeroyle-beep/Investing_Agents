from __future__ import annotations

from shared.invariants import (
    build_invariant_context,
    invariant_closed_positions_cannot_mutate_economic_fields,
    invariant_each_processed_fill_appears_exactly_once,
    invariant_each_run_has_a_single_terminal_status,
    invariant_lifecycle_transitions_are_valid_only,
    invariant_total_equity_equals_cash_plus_open_market_value,
)
from tests.helpers import (
    cash_state_frame,
    closed_position_row,
    equity_history_frame,
    open_position_row,
    portfolio_state_frame,
    processed_fills_frame,
    run_history_frame,
)


def test_invalid_lifecycle_transition_rejected() -> None:
    previous_state = portfolio_state_frame([closed_position_row(status="closed")])
    current_state = portfolio_state_frame([open_position_row(position_id="POS001", status="open")])
    context = build_invariant_context(current_state=current_state, previous_state=previous_state)

    failures = invariant_lifecycle_transitions_are_valid_only(context)

    assert any("closed -> open" in failure.message for failure in failures)


def test_closed_position_immutability_enforced() -> None:
    previous_state = portfolio_state_frame([closed_position_row(exit_price=110.0)])
    current_state = portfolio_state_frame([closed_position_row(exit_price=111.5)])
    context = build_invariant_context(current_state=current_state, previous_state=previous_state)

    failures = invariant_closed_positions_cannot_mutate_economic_fields(context)

    assert any("immutable field changed: exit_price" in failure.message for failure in failures)


def test_total_equity_identity_check() -> None:
    current_state = portfolio_state_frame([open_position_row()])
    cash_state = cash_state_frame(balance=900.0)
    equity_history = equity_history_frame(
        [
            {
                "timestamp": "2026-03-28T10:00:00+00:00",
                "run_id": "RUN_EQ_1",
                "cash_balance": 900.0,
                "open_market_value": 1050.0,
                "gross_exposure": 1050.0,
                "net_exposure": 1050.0,
                "unrealised_pnl_abs": 50.0,
                "realised_pnl_abs": 0.0,
                "total_equity": 1900.0,
                "open_positions": 1,
                "closed_positions": 0,
                "peak_equity": 1900.0,
                "drawdown_abs": 0.0,
                "drawdown_pct": 0.0,
            }
        ]
    )
    context = build_invariant_context(
        current_state=current_state,
        cash_state=cash_state,
        equity_history=equity_history,
    )

    failures = invariant_total_equity_equals_cash_plus_open_market_value(context)

    assert any("does not equal cash plus open-position market value" in failure.message for failure in failures)


def test_duplicate_fill_detection() -> None:
    current_state = portfolio_state_frame([open_position_row()])
    processed_fills = processed_fills_frame(
        [
            {"fill_id": "FILL001", "processed_at": "2026-03-28T10:00:00+00:00", "run_id": "RUN_DUP"},
            {"fill_id": "FILL001", "processed_at": "2026-03-28T10:01:00+00:00", "run_id": "RUN_DUP"},
        ]
    )
    context = build_invariant_context(current_state=current_state, processed_fills=processed_fills)

    failures = invariant_each_processed_fill_appears_exactly_once(context)

    assert any("duplicate fill_id=FILL001" in failure.message for failure in failures)


def test_run_history_single_terminal_status_rule() -> None:
    current_state = portfolio_state_frame([open_position_row()])
    run_history = run_history_frame(
        [
            {
                "run_id": "RUN_TERMINAL",
                "started_at": "2026-03-28T10:00:00+00:00",
                "completed_at": "",
                "status": "success",
                "failed_agent": "",
                "error_message": "",
                "notes": "",
            },
            {
                "run_id": "RUN_TERMINAL",
                "started_at": "2026-03-28T10:00:00+00:00",
                "completed_at": "2026-03-28T10:05:00+00:00",
                "status": "failed",
                "failed_agent": "Lifecycle Integrity Agent",
                "error_message": "duplicate row",
                "notes": "",
            },
        ]
    )
    context = build_invariant_context(current_state=current_state, run_history=run_history)

    failures = invariant_each_run_has_a_single_terminal_status(context)

    messages = [failure.message for failure in failures]
    assert any("Duplicate run history row found for run_id=RUN_TERMINAL" in message for message in messages)
    assert any("Terminal run status without completed_at for run_id=RUN_TERMINAL" in message for message in messages)
