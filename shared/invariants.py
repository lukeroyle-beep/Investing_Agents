from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd

from shared.portfolio_state_helpers import (
    ACTIVE_POSITION_STATUSES,
    CLOSED_POSITION_STATUS,
    VALID_POSITION_SIDES,
    VALID_POSITION_STATUSES,
    normalise_exit_flag,
    normalise_position_status,
)
from shared.schemas import (
    validate_cash_ledger,
    validate_cash_state,
    validate_portfolio_equity_history,
    validate_portfolio_state,
    validate_processed_fills,
)

VALID_LIFECYCLE_TRANSITIONS = {
    "open": {"open", "exit_required", "closed"},
    "exit_required": {"exit_required", "closed"},
    "closed": {"closed"},
}
RUN_TERMINAL_STATUSES = {"success", "failed"}
RUN_ALLOWED_STATUSES = RUN_TERMINAL_STATUSES | {"running"}
OPEN_VALUATION_FIELDS = ["current_price", "market_value", "pnl_abs", "pnl_pct"]
CLOSED_POSITION_REQUIRED_FIELDS = ["closed_at", "exit_price", "realised_pnl_abs", "fees_total"]
CLOSED_POSITION_IMMUTABLE_FIELDS = [
    "status",
    "quantity",
    "entry_price",
    "entry_date",
    "closed_at",
    "exit_price",
    "realised_pnl_abs",
    "fees_total",
]


@dataclass(frozen=True)
class InvariantFailure:
    invariant_name: str
    message: str
    severity: str = "critical"
    position_id: str | None = None
    ticker: str | None = None


@dataclass(frozen=True)
class InvariantDefinition:
    name: str
    validator: Callable[["InvariantContext"], list[InvariantFailure]]
    severity: str = "critical"


@dataclass(frozen=True)
class InvariantContext:
    current_state: pd.DataFrame
    previous_state: pd.DataFrame
    cash_state: pd.DataFrame
    equity_history: pd.DataFrame
    processed_fills: pd.DataFrame
    cash_ledger: pd.DataFrame
    run_history: pd.DataFrame


@dataclass(frozen=True)
class InvariantResult:
    invariant_name: str
    severity: str
    failures: list[InvariantFailure]

    @property
    def passed(self) -> bool:
        return len(self.failures) == 0

    @property
    def has_hard_failure(self) -> bool:
        return any(failure.severity in {"critical", "error"} for failure in self.failures)

    @property
    def has_warning(self) -> bool:
        return any(failure.severity == "warning" for failure in self.failures)


def normalise_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return round(float(value), 10)

    text = str(value).strip()
    if text == "":
        return None

    return text


def values_equal(a: Any, b: Any) -> bool:
    return normalise_scalar(a) == normalise_scalar(b)


def _empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def build_invariant_context(
    *,
    current_state: pd.DataFrame,
    previous_state: pd.DataFrame | None = None,
    cash_state: pd.DataFrame | None = None,
    equity_history: pd.DataFrame | None = None,
    processed_fills: pd.DataFrame | None = None,
    cash_ledger: pd.DataFrame | None = None,
    run_history: pd.DataFrame | None = None,
) -> InvariantContext:
    return InvariantContext(
        current_state=validate_portfolio_state(current_state, keep_extra_columns=True),
        previous_state=(
            validate_portfolio_state(previous_state, keep_extra_columns=True)
            if previous_state is not None and not previous_state.empty
            else _empty_frame(validate_portfolio_state(pd.DataFrame()).columns.tolist())
        ),
        cash_state=(
            validate_cash_state(cash_state, keep_extra_columns=True)
            if cash_state is not None and not cash_state.empty
            else _empty_frame(validate_cash_state(pd.DataFrame()).columns.tolist())
        ),
        equity_history=(
            validate_portfolio_equity_history(equity_history, keep_extra_columns=True)
            if equity_history is not None and not equity_history.empty
            else _empty_frame(validate_portfolio_equity_history(pd.DataFrame()).columns.tolist())
        ),
        processed_fills=(
            validate_processed_fills(processed_fills, keep_extra_columns=True)
            if processed_fills is not None and not processed_fills.empty
            else _empty_frame(validate_processed_fills(pd.DataFrame()).columns.tolist())
        ),
        cash_ledger=(
            validate_cash_ledger(cash_ledger, keep_extra_columns=True)
            if cash_ledger is not None and not cash_ledger.empty
            else _empty_frame(validate_cash_ledger(pd.DataFrame()).columns.tolist())
        ),
        run_history=(
            run_history.copy()
            if run_history is not None and not run_history.empty
            else _empty_frame(["run_id", "started_at", "completed_at", "status", "failed_agent", "error_message", "notes"])
        ),
    )


def _failure(
    invariant_name: str,
    message: str,
    *,
    severity: str = "critical",
    position_id: str | None = None,
    ticker: str | None = None,
) -> InvariantFailure:
    return InvariantFailure(
        invariant_name=invariant_name,
        message=message,
        severity=severity,
        position_id=position_id,
        ticker=ticker,
    )


def invariant_total_equity_equals_cash_plus_open_market_value(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    if context.equity_history.empty:
        return failures

    latest_equity_row = context.equity_history.iloc[-1]
    cash_balance = pd.to_numeric(pd.Series([latest_equity_row.get("cash_balance")]), errors="coerce").iloc[0]
    open_market_value = pd.to_numeric(pd.Series([latest_equity_row.get("open_market_value")]), errors="coerce").iloc[0]
    actual_total_equity = pd.to_numeric(pd.Series([latest_equity_row.get("total_equity")]), errors="coerce").iloc[0]
    expected_total_equity = float(cash_balance) + float(open_market_value)

    if pd.isna(actual_total_equity) or pd.isna(cash_balance) or pd.isna(open_market_value):
        failures.append(
            _failure(
                "total_equity_equals_cash_plus_open_market_value",
                "Latest portfolio equity history row has invalid cash_balance, open_market_value, or total_equity.",
            )
        )
        return failures

    if abs(float(actual_total_equity) - expected_total_equity) > 1e-6:
        failures.append(
            _failure(
                "total_equity_equals_cash_plus_open_market_value",
                (
                    "Latest total equity does not equal cash plus open-position market value. "
                    f"expected={expected_total_equity:.10f} actual={float(actual_total_equity):.10f}"
                ),
            )
        )

    return failures


def invariant_closed_positions_cannot_mutate_economic_fields(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    if context.previous_state.empty:
        return failures

    previous_closed = context.previous_state[
        context.previous_state["status"] == CLOSED_POSITION_STATUS
    ].copy()
    current_by_id = context.current_state.set_index("position_id", drop=False)

    for _, previous_row in previous_closed.iterrows():
        position_id = str(previous_row["position_id"])

        if position_id not in current_by_id.index:
            failures.append(
                _failure(
                    "closed_positions_cannot_mutate_economic_fields",
                    "Previously closed position is missing from current portfolio_state.csv.",
                    position_id=position_id,
                    ticker=str(previous_row.get("ticker")),
                )
            )
            continue

        current_row = current_by_id.loc[position_id]
        if isinstance(current_row, pd.DataFrame):
            failures.append(
                _failure(
                    "closed_positions_cannot_mutate_economic_fields",
                    "Current portfolio_state.csv has duplicate position_id values during closed-position comparison.",
                    position_id=position_id,
                    ticker=str(previous_row.get("ticker")),
                )
            )
            continue

        for field in CLOSED_POSITION_IMMUTABLE_FIELDS:
            if not values_equal(previous_row.get(field), current_row.get(field)):
                failures.append(
                    _failure(
                        "closed_positions_cannot_mutate_economic_fields",
                        (
                            f"Closed position immutable field changed: {field}. "
                            f"previous={normalise_scalar(previous_row.get(field))} "
                            f"current={normalise_scalar(current_row.get(field))}"
                        ),
                        position_id=position_id,
                        ticker=str(current_row.get("ticker")),
                    )
                )

    return failures


def invariant_realised_pnl_changes_only_through_fills_or_explicit_cash_events(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    if context.previous_state.empty:
        return failures

    previous_by_id = context.previous_state.set_index("position_id", drop=False)
    current_closed = context.current_state[
        context.current_state["status"] == CLOSED_POSITION_STATUS
    ].copy()

    close_event_run_ids = set()
    if not context.cash_ledger.empty and "event_type" in context.cash_ledger.columns:
        close_event_rows = context.cash_ledger[
            context.cash_ledger["event_type"].astype(str).str.strip().isin({"position_close", "cash_adjusted"})
        ].copy()
        close_event_run_ids = set(close_event_rows["run_id"].astype(str).str.strip())

    processed_fill_run_ids = set()
    if not context.processed_fills.empty and "run_id" in context.processed_fills.columns:
        processed_fill_run_ids = set(context.processed_fills["run_id"].astype(str).str.strip())

    for _, current_row in current_closed.iterrows():
        position_id = str(current_row["position_id"])
        current_realised = pd.to_numeric(pd.Series([current_row.get("realised_pnl_abs")]), errors="coerce").iloc[0]

        if position_id not in previous_by_id.index:
            if pd.notna(current_realised) and abs(float(current_realised)) > 1e-6:
                current_run_id = str(current_row.get("run_id") or "").strip()
                if current_run_id not in close_event_run_ids and current_run_id not in processed_fill_run_ids:
                    failures.append(
                        _failure(
                            "realised_pnl_changes_only_through_fills_or_explicit_cash_events",
                            "New closed position has realised_pnl_abs without a matching processed fill or cash-ledger event.",
                            position_id=position_id,
                            ticker=str(current_row.get("ticker")),
                        )
                    )
            continue

        previous_row = previous_by_id.loc[position_id]
        if isinstance(previous_row, pd.DataFrame):
            continue

        previous_realised = pd.to_numeric(pd.Series([previous_row.get("realised_pnl_abs")]), errors="coerce").iloc[0]
        if values_equal(previous_realised, current_realised):
            continue

        current_run_id = str(current_row.get("run_id") or "").strip()
        if current_run_id not in close_event_run_ids and current_run_id not in processed_fill_run_ids:
            failures.append(
                _failure(
                    "realised_pnl_changes_only_through_fills_or_explicit_cash_events",
                    (
                        "realised_pnl_abs changed without a matching processed fill or cash-ledger event. "
                        f"previous={normalise_scalar(previous_realised)} current={normalise_scalar(current_realised)}"
                    ),
                    position_id=position_id,
                    ticker=str(current_row.get("ticker")),
                )
            )

    return failures


def invariant_each_processed_fill_appears_exactly_once(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    if context.processed_fills.empty:
        return failures

    fill_ids = context.processed_fills["fill_id"].fillna("").astype(str).str.strip()
    blank_rows = context.processed_fills[fill_ids == ""]
    for _, _row in blank_rows.iterrows():
        failures.append(
            _failure(
                "each_processed_fill_appears_exactly_once",
                "processed_fills.csv contains a blank fill_id.",
            )
        )

    duplicate_rows = context.processed_fills[fill_ids.duplicated(keep=False)]
    for _, row in duplicate_rows.iterrows():
        failures.append(
            _failure(
                "each_processed_fill_appears_exactly_once",
                f"processed_fills.csv contains duplicate fill_id={str(row.get('fill_id')).strip()}",
            )
        )

    return failures


def invariant_lifecycle_transitions_are_valid_only(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    duplicate_position_rows = context.current_state[
        context.current_state["position_id"].duplicated(keep=False)
    ]
    for _, row in duplicate_position_rows.iterrows():
        failures.append(
            _failure(
                "lifecycle_transitions_are_valid_only",
                "position_id appears more than once in portfolio_state.csv.",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
            )
        )

    invalid_status_rows = context.current_state[
        ~context.current_state["status"].isin(VALID_POSITION_STATUSES)
    ]
    for _, row in invalid_status_rows.iterrows():
        failures.append(
            _failure(
                "lifecycle_transitions_are_valid_only",
                f"Invalid lifecycle status: {row.get('status')}",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
            )
        )

    invalid_side_rows = context.current_state[~context.current_state["side"].isin(VALID_POSITION_SIDES)]
    for _, row in invalid_side_rows.iterrows():
        failures.append(
            _failure(
                "lifecycle_transitions_are_valid_only",
                f"Invalid position side: {row.get('side')}",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
            )
        )

    active_rows = context.current_state[
        context.current_state["status"].isin(ACTIVE_POSITION_STATUSES)
    ].copy()

    active_qty = pd.to_numeric(active_rows["quantity"], errors="coerce")
    invalid_qty_rows = active_rows[active_qty.isna() | (active_qty <= 0)]
    for _, row in invalid_qty_rows.iterrows():
        failures.append(
            _failure(
                "lifecycle_transitions_are_valid_only",
                "Active position has missing or non-positive quantity.",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
            )
        )

    active_entry = pd.to_numeric(active_rows["entry_price"], errors="coerce")
    invalid_entry_rows = active_rows[active_entry.isna() | (active_entry <= 0)]
    for _, row in invalid_entry_rows.iterrows():
        failures.append(
            _failure(
                "lifecycle_transitions_are_valid_only",
                "Active position has missing or non-positive entry_price.",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
            )
        )

    active_dupes = (
        active_rows.groupby(["ticker", "side"]).size().reset_index(name="count")
    )
    active_dupes = active_dupes[active_dupes["count"] > 1]
    for _, dup in active_dupes.iterrows():
        dup_rows = active_rows[
            (active_rows["ticker"] == dup["ticker"]) & (active_rows["side"] == dup["side"])
        ]
        for _, row in dup_rows.iterrows():
            failures.append(
                _failure(
                    "lifecycle_transitions_are_valid_only",
                    f"More than one active position for ticker={dup['ticker']} side={dup['side']}",
                    position_id=str(row.get("position_id")),
                    ticker=str(row.get("ticker")),
                )
            )

    for _, row in context.current_state.iterrows():
        status = normalise_position_status(row.get("status"))
        exit_flag = normalise_exit_flag(row.get("exit_flag"))

        if status == "open" and exit_flag == "true":
            failures.append(
                _failure(
                    "lifecycle_transitions_are_valid_only",
                    "status=open but exit_flag=true",
                    position_id=str(row.get("position_id")),
                    ticker=str(row.get("ticker")),
                )
            )
        if status == "exit_required" and exit_flag != "true":
            failures.append(
                _failure(
                    "lifecycle_transitions_are_valid_only",
                    "status=exit_required but exit_flag is not true",
                    position_id=str(row.get("position_id")),
                    ticker=str(row.get("ticker")),
                )
            )
        if status == "closed" and exit_flag == "true":
            failures.append(
                _failure(
                    "lifecycle_transitions_are_valid_only",
                    "status=closed but exit_flag=true",
                    position_id=str(row.get("position_id")),
                    ticker=str(row.get("ticker")),
                )
            )

    if context.previous_state.empty:
        return failures

    previous_by_id = context.previous_state.set_index("position_id", drop=False)
    for _, current_row in context.current_state.iterrows():
        position_id = str(current_row["position_id"])
        if position_id not in previous_by_id.index:
            continue

        previous_row = previous_by_id.loc[position_id]
        if isinstance(previous_row, pd.DataFrame):
            continue

        previous_status = normalise_position_status(previous_row.get("status"))
        current_status = normalise_position_status(current_row.get("status"))
        allowed_next = VALID_LIFECYCLE_TRANSITIONS.get(previous_status)

        if allowed_next is None or current_status not in allowed_next:
            failures.append(
                _failure(
                    "lifecycle_transitions_are_valid_only",
                    f"Invalid lifecycle transition: {previous_status} -> {current_status}",
                    position_id=position_id,
                    ticker=str(current_row.get("ticker")),
                )
            )

    return failures


def invariant_each_run_has_a_single_terminal_status(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    if context.run_history.empty:
        return failures

    run_history = context.run_history.copy()
    if "run_id" not in run_history.columns or "status" not in run_history.columns:
        failures.append(
            _failure(
                "each_run_has_a_single_terminal_status",
                "run_history.csv is missing run_id or status columns.",
            )
        )
        return failures

    run_history["run_id"] = run_history["run_id"].fillna("").astype(str).str.strip()
    run_history["status"] = run_history["status"].fillna("").astype(str).str.strip().str.lower()

    invalid_status_rows = run_history[~run_history["status"].isin(RUN_ALLOWED_STATUSES)]
    for _, row in invalid_status_rows.iterrows():
        failures.append(
            _failure(
                "each_run_has_a_single_terminal_status",
                f"Invalid run status for run_id={row.get('run_id')}: {row.get('status')}",
            )
        )

    duplicate_run_ids = run_history[run_history["run_id"].duplicated(keep=False)]
    for _, row in duplicate_run_ids.iterrows():
        failures.append(
            _failure(
                "each_run_has_a_single_terminal_status",
                f"Duplicate run history row found for run_id={row.get('run_id')}",
            )
        )

    for _, row in run_history.iterrows():
        status = str(row.get("status")).strip().lower()
        completed_at = str(row.get("completed_at") or "").strip()

        if status in RUN_TERMINAL_STATUSES and completed_at == "":
            failures.append(
                _failure(
                    "each_run_has_a_single_terminal_status",
                    f"Terminal run status without completed_at for run_id={row.get('run_id')}",
                )
            )

    return failures


def invariant_open_positions_have_live_valuation_fields_populated(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    open_rows = context.current_state[
        context.current_state["status"].isin(ACTIVE_POSITION_STATUSES)
    ].copy()

    for field in OPEN_VALUATION_FIELDS:
        numeric_values = pd.to_numeric(open_rows[field], errors="coerce")
        invalid_rows = open_rows[numeric_values.isna()]
        for _, row in invalid_rows.iterrows():
            failures.append(
                _failure(
                    "open_positions_have_live_valuation_fields_populated",
                    f"Open position is missing live valuation field: {field}",
                    position_id=str(row.get("position_id")),
                    ticker=str(row.get("ticker")),
                )
            )

    return failures


def invariant_closed_positions_have_closure_fields_populated(
    context: InvariantContext,
) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    closed_rows = context.current_state[
        context.current_state["status"] == CLOSED_POSITION_STATUS
    ].copy()

    for field in CLOSED_POSITION_REQUIRED_FIELDS:
        if field == "closed_at":
            invalid_rows = closed_rows[closed_rows[field].isna() | (closed_rows[field].astype(str).str.strip() == "")]
        else:
            numeric_values = pd.to_numeric(closed_rows[field], errors="coerce")
            invalid_rows = closed_rows[numeric_values.isna()]

        for _, row in invalid_rows.iterrows():
            failures.append(
                _failure(
                    "closed_positions_have_closure_fields_populated",
                    f"Closed position is missing closure field: {field}",
                    position_id=str(row.get("position_id")),
                    ticker=str(row.get("ticker")),
                )
            )

    closed_market_value = pd.to_numeric(closed_rows["market_value"], errors="coerce").fillna(0.0)
    invalid_market_value_rows = closed_rows[closed_market_value != 0.0]
    for _, row in invalid_market_value_rows.iterrows():
        failures.append(
            _failure(
                "closed_positions_have_closure_fields_populated",
                "Closed position has non-zero market_value.",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
            )
        )

    return failures


INVARIANTS: list[InvariantDefinition] = [
    InvariantDefinition(
        name="total_equity_equals_cash_plus_open_market_value",
        validator=invariant_total_equity_equals_cash_plus_open_market_value,
    ),
    InvariantDefinition(
        name="closed_positions_cannot_mutate_economic_fields",
        validator=invariant_closed_positions_cannot_mutate_economic_fields,
    ),
    InvariantDefinition(
        name="realised_pnl_changes_only_through_fills_or_explicit_cash_events",
        validator=invariant_realised_pnl_changes_only_through_fills_or_explicit_cash_events,
    ),
    InvariantDefinition(
        name="each_processed_fill_appears_exactly_once",
        validator=invariant_each_processed_fill_appears_exactly_once,
    ),
    InvariantDefinition(
        name="lifecycle_transitions_are_valid_only",
        validator=invariant_lifecycle_transitions_are_valid_only,
    ),
    InvariantDefinition(
        name="each_run_has_a_single_terminal_status",
        validator=invariant_each_run_has_a_single_terminal_status,
    ),
    InvariantDefinition(
        name="open_positions_have_live_valuation_fields_populated",
        validator=invariant_open_positions_have_live_valuation_fields_populated,
    ),
    InvariantDefinition(
        name="closed_positions_have_closure_fields_populated",
        validator=invariant_closed_positions_have_closure_fields_populated,
    ),
]


def evaluate_all_invariants(context: InvariantContext) -> list[InvariantResult]:
    results: list[InvariantResult] = []

    for invariant in INVARIANTS:
        failures = invariant.validator(context)
        results.append(
            InvariantResult(
                invariant_name=invariant.name,
                severity=invariant.severity,
                failures=failures,
            )
        )

    return results


def validate_all_invariants(context: InvariantContext) -> list[InvariantFailure]:
    failures: list[InvariantFailure] = []

    for result in evaluate_all_invariants(context):
        failures.extend(result.failures)

    return failures
