from __future__ import annotations

import json
from dataclasses import dataclass

import pandas as pd

from shared.io_utils import ensure_parent_dir
from shared.paths import (
    POSITION_ALERTS_PATH,
    RUN_HISTORY_PATH,
    RUN_RECONCILIATION_SUMMARY_PATH,
    data_path,
)
from shared.schema_registry import get_file_schema
from shared.schemas import validate_run_reconciliation_summary


EVENT_LOG_PATH = data_path("event_log.csv")
EQUITY_HISTORY_PATH = data_path("portfolio_equity_history.csv")
CASH_LEDGER_PATH = data_path("cash_ledger.csv")
PROCESSED_FILLS_PATH = data_path("processed_fills.csv")

RUN_RECONCILIATION_SCHEMA = get_file_schema("run_reconciliation_summary.csv")


@dataclass(frozen=True)
class DeltaMetrics:
    cash_delta: float = 0.0
    realised_pnl_delta: float = 0.0
    unrealised_pnl_delta: float = 0.0
    equity_delta: float = 0.0
    exposure_delta: float = 0.0
    note: str = ""


def _read_csv_if_exists(path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


def _read_run_history() -> pd.DataFrame:
    if not RUN_HISTORY_PATH.exists():
        raise FileNotFoundError(f"Missing run history file: {RUN_HISTORY_PATH}")
    return pd.read_csv(RUN_HISTORY_PATH, dtype=str, keep_default_na=False)


def _find_run_row(run_history_df: pd.DataFrame, run_id: str) -> pd.Series:
    matches = run_history_df[run_history_df["run_id"].astype(str).str.strip() == str(run_id).strip()]
    if matches.empty:
        raise ValueError(f"Run reconciliation could not find run_id={run_id} in run_history.csv")
    if len(matches) > 1:
        raise ValueError(f"Run reconciliation found duplicate run_history rows for run_id={run_id}")
    return matches.iloc[0]


def _read_event_log() -> pd.DataFrame:
    return _read_csv_if_exists(EVENT_LOG_PATH)


def _read_equity_history() -> pd.DataFrame:
    return _read_csv_if_exists(EQUITY_HISTORY_PATH)


def _read_cash_ledger() -> pd.DataFrame:
    return _read_csv_if_exists(CASH_LEDGER_PATH)


def _read_position_alerts() -> pd.DataFrame:
    return _read_csv_if_exists(POSITION_ALERTS_PATH)


def _event_rows_for_run(event_log_df: pd.DataFrame, run_id: str) -> pd.DataFrame:
    if event_log_df.empty or "run_id" not in event_log_df.columns:
        return pd.DataFrame()
    return event_log_df[event_log_df["run_id"].astype(str).str.strip() == str(run_id).strip()].copy()


def _count_event_type(event_rows: pd.DataFrame, event_type: str) -> int:
    if event_rows.empty or "event_type" not in event_rows.columns:
        return 0
    return int((event_rows["event_type"].astype(str).str.strip() == event_type).sum())


def _extract_validation_counts(event_rows: pd.DataFrame) -> tuple[int, int]:
    if event_rows.empty or "event_type" not in event_rows.columns:
        return 0, 0

    validation_rows = event_rows[
        event_rows["event_type"].astype(str).str.strip().isin({"validation_passed", "validation_failed"})
    ].copy()

    if validation_rows.empty:
        return 0, 0

    latest_row = validation_rows.iloc[-1]
    metadata_raw = str(latest_row.get("metadata_json") or "").strip()
    if not metadata_raw:
        return 0, 0

    try:
        metadata = json.loads(metadata_raw)
    except json.JSONDecodeError:
        return 0, 0

    details = metadata.get("details")
    if isinstance(details, dict):
        metadata_source = details
    else:
        metadata_source = metadata

    warning_count = int(metadata_source.get("warning_check_count", 0) or 0)
    failure_count = int(metadata_source.get("critical_issue_count", 0) or 0)
    return warning_count, failure_count


def _count_processed_fills(run_id: str) -> int:
    processed_fills_df = _read_csv_if_exists(PROCESSED_FILLS_PATH)
    if processed_fills_df.empty or "run_id" not in processed_fills_df.columns:
        return 0
    return int((processed_fills_df["run_id"].astype(str).str.strip() == str(run_id).strip()).sum())


def _count_exit_required_alerts(run_id: str) -> tuple[int, str]:
    alerts_df = _read_position_alerts()
    if alerts_df.empty:
        return 0, "positions_marked_exit_required=0 because position_alerts.csv has no rows"

    required_columns = {"run_id", "alert_type"}
    if not required_columns.issubset(set(alerts_df.columns)):
        return 0, "positions_marked_exit_required=0 because position_alerts.csv lacks run_id/alert_type"

    count = int(
        (
            (alerts_df["run_id"].astype(str).str.strip() == str(run_id).strip())
            & (alerts_df["alert_type"].astype(str).str.strip().str.lower() == "exit_required")
        ).sum()
    )
    return count, ""


def _compute_deltas_from_equity_history(run_id: str) -> DeltaMetrics:
    equity_history_df = _read_equity_history()
    if equity_history_df.empty or "run_id" not in equity_history_df.columns:
        return DeltaMetrics(note="deltas defaulted to 0 because portfolio_equity_history.csv is unavailable")

    matching_rows = equity_history_df[equity_history_df["run_id"].astype(str).str.strip() == str(run_id).strip()].copy()
    if matching_rows.empty:
        return DeltaMetrics(note="deltas defaulted to 0 because no equity snapshot exists for this run")

    current_index = matching_rows.index[-1]
    current_row = equity_history_df.loc[current_index]
    previous_rows = equity_history_df.loc[equity_history_df.index < current_index]

    if previous_rows.empty:
        previous_row = None
    else:
        previous_row = previous_rows.iloc[-1]

    def metric_delta(column: str) -> float:
        current_value = pd.to_numeric(pd.Series([current_row.get(column)]), errors="coerce").fillna(0.0).iloc[0]
        if previous_row is None:
            previous_value = 0.0
        else:
            previous_value = pd.to_numeric(pd.Series([previous_row.get(column)]), errors="coerce").fillna(0.0).iloc[0]
        return float(current_value) - float(previous_value)

    return DeltaMetrics(
        cash_delta=metric_delta("cash_balance"),
        realised_pnl_delta=metric_delta("realised_pnl_abs"),
        unrealised_pnl_delta=metric_delta("unrealised_pnl_abs"),
        equity_delta=metric_delta("total_equity"),
        exposure_delta=metric_delta("gross_exposure"),
        note="",
    )


def _compute_cash_delta_from_cash_ledger(run_id: str) -> tuple[float, str]:
    cash_ledger_df = _read_cash_ledger()
    if cash_ledger_df.empty or "run_id" not in cash_ledger_df.columns:
        return 0.0, ""

    run_rows = cash_ledger_df[cash_ledger_df["run_id"].astype(str).str.strip() == str(run_id).strip()].copy()
    if run_rows.empty:
        return 0.0, ""

    run_rows["amount"] = pd.to_numeric(run_rows["amount"], errors="coerce").fillna(0.0)
    run_rows["fees"] = pd.to_numeric(run_rows["fees"], errors="coerce").fillna(0.0)
    run_rows["event_type"] = run_rows["event_type"].astype(str).str.strip()

    cash_delta = 0.0
    for _, row in run_rows.iterrows():
        if row["event_type"] == "position_open":
            cash_delta += float(row["amount"]) - float(row["fees"])
        else:
            cash_delta += float(row["amount"])

    return cash_delta, "cash_delta derived from cash_ledger.csv because no equity snapshot exists for this run"


def _compose_notes(*notes: str) -> str:
    filtered = [note.strip() for note in notes if str(note).strip()]
    return " | ".join(filtered)


def build_run_reconciliation_row(run_id: str) -> dict[str, object]:
    run_history_df = _read_run_history()
    run_row = _find_run_row(run_history_df, run_id)
    event_log_df = _read_event_log()
    event_rows = _event_rows_for_run(event_log_df, run_id)

    warning_count, failure_count = _extract_validation_counts(event_rows)
    positions_marked_exit_required, exit_required_note = _count_exit_required_alerts(run_id)
    deltas = _compute_deltas_from_equity_history(run_id)
    cash_delta_fallback, cash_delta_note = _compute_cash_delta_from_cash_ledger(run_id)

    if deltas.note and cash_delta_fallback != 0.0:
        deltas = DeltaMetrics(
            cash_delta=cash_delta_fallback,
            realised_pnl_delta=deltas.realised_pnl_delta,
            unrealised_pnl_delta=deltas.unrealised_pnl_delta,
            equity_delta=deltas.equity_delta,
            exposure_delta=deltas.exposure_delta,
            note=_compose_notes(deltas.note, cash_delta_note),
        )

    return {
        "run_id": str(run_row.get("run_id", "")).strip(),
        "started_at": str(run_row.get("started_at", "")).strip(),
        "completed_at": str(run_row.get("completed_at", "")).strip(),
        "status": str(run_row.get("status", "")).strip(),
        "failed_agent": str(run_row.get("failed_agent", "")).strip(),
        "fills_processed": _count_processed_fills(run_id),
        "positions_opened": _count_event_type(event_rows, "position_opened"),
        "positions_closed": _count_event_type(event_rows, "position_closed"),
        "positions_marked_exit_required": positions_marked_exit_required,
        "cash_delta": deltas.cash_delta,
        "realised_pnl_delta": deltas.realised_pnl_delta,
        "unrealised_pnl_delta": deltas.unrealised_pnl_delta,
        "equity_delta": deltas.equity_delta,
        "exposure_delta": deltas.exposure_delta,
        "validation_warning_count": warning_count,
        "validation_failure_count": failure_count,
        "notes": _compose_notes(exit_required_note, deltas.note, str(run_row.get("notes", "")).strip()),
    }


def _upsert_reconciliation_row(row: dict[str, object]) -> pd.DataFrame:
    ensure_parent_dir(RUN_RECONCILIATION_SUMMARY_PATH)

    if RUN_RECONCILIATION_SUMMARY_PATH.exists():
        existing_df = pd.read_csv(RUN_RECONCILIATION_SUMMARY_PATH)
    else:
        existing_df = pd.DataFrame(columns=RUN_RECONCILIATION_SCHEMA.canonical_column_order)

    row_df = pd.DataFrame([row], columns=RUN_RECONCILIATION_SCHEMA.canonical_column_order)

    if existing_df.empty or "run_id" not in existing_df.columns:
        combined_df = row_df.copy()
    else:
        existing_df = existing_df[existing_df["run_id"].astype(str).str.strip() != str(row["run_id"]).strip()].copy()
        combined_df = pd.concat([existing_df, row_df], ignore_index=True)

    combined_df = validate_run_reconciliation_summary(combined_df, keep_extra_columns=False)
    combined_df.to_csv(RUN_RECONCILIATION_SUMMARY_PATH, index=False)
    return combined_df


def write_run_reconciliation_summary(run_id: str) -> dict[str, object]:
    row = build_run_reconciliation_row(run_id)
    _upsert_reconciliation_row(row)
    return row


def print_run_reconciliation_summary(row: dict[str, object]) -> None:
    print("\nRun Reconciliation Summary:")
    print(f"Run ID: {row['run_id']}")
    print(f"Status: {row['status']}")
    print(f"Failed agent: {row['failed_agent'] or 'None'}")
    print(
        "Activity: "
        f"fills={int(row['fills_processed'])}, "
        f"opened={int(row['positions_opened'])}, "
        f"closed={int(row['positions_closed'])}, "
        f"exit_required={int(row['positions_marked_exit_required'])}"
    )
    print(
        "Deltas: "
        f"cash={float(row['cash_delta']):.2f}, "
        f"realised={float(row['realised_pnl_delta']):.2f}, "
        f"unrealised={float(row['unrealised_pnl_delta']):.2f}, "
        f"equity={float(row['equity_delta']):.2f}, "
        f"exposure={float(row['exposure_delta']):.2f}"
    )
    print(
        "Validation: "
        f"warnings={int(row['validation_warning_count'])}, "
        f"failures={int(row['validation_failure_count'])}"
    )
    if str(row.get("notes", "")).strip():
        print(f"Notes: {row['notes']}")
