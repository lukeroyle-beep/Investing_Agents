from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import DATA_DIR
from shared.artifact_manifest import REQUIRED_ARTIFACTS
from shared.run_finalizer import validate_finalization_record
from shared.schema_registry import get_file_schema


@dataclass(frozen=True)
class CheckIssue:
    severity: str
    message: str


SCHEMA_CRITICAL_FILES = list(REQUIRED_ARTIFACTS)

CUSTOM_REQUIRED_COLUMNS = {
    "advisory_trades.csv": {"ticker", "action", "direction", "advice_status", "run_id"},
    "exit_advice.csv": {"position_id", "ticker", "exit_action", "status", "run_id"},
    "final_shortlist.csv": {"ticker", "risk_decision", "adjusted_setup_score"},
    "portfolio_orders.csv": {
        "run_id",
        "internal_instrument_id",
        "ticker",
        "exchange",
        "currency",
        "direction",
        "asset_type",
        "execution_environment",
        "order_type",
        "sizing_method",
        "sizing_value",
        "entry_price",
        "position_size_pct",
        "capital_allocated",
    },
    "signal_setups.csv": {"ticker"},
    "signal_top_setups.csv": {"ticker"},
    "news_flags.csv": {"ticker", "has_news"},
    "macro_regime.csv": {"market_regime"},
}

RUN_SCOPED_FILES = [
    "advisory_trades.csv",
    "exit_advice.csv",
    "run_reconciliation_summary.csv",
    "portfolio_orders.csv",
]

CLOSED_IMMUTABLE_FIELDS = [
    "status",
    "quantity",
    "entry_price",
    "entry_date",
    "closed_at",
    "exit_price",
    "realised_pnl_abs",
    "fees_total",
]


def read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def add_issue(issues: list[CheckIssue], severity: str, message: str) -> None:
    issues.append(CheckIssue(severity=severity, message=message))


def required_columns_for(file_name: str) -> set[str]:
    if file_name in CUSTOM_REQUIRED_COLUMNS:
        return set(CUSTOM_REQUIRED_COLUMNS[file_name])
    return set(get_file_schema(file_name).required_columns)


def check_required_artifacts(issues: list[CheckIssue], files: Iterable[str]) -> None:
    for file_name in files:
        path = DATA_DIR / file_name
        if not path.exists():
            add_issue(issues, "critical", f"Missing artifact: {file_name}")
            continue

        try:
            df = read_csv(path)
        except Exception as exc:
            add_issue(issues, "critical", f"Cannot read {file_name}: {exc}")
            continue

        required = required_columns_for(file_name)
        missing = sorted(required - set(df.columns))
        if missing:
            add_issue(issues, "critical", f"{file_name}: missing required columns {missing}")


def latest_run_id(issues: list[CheckIssue]) -> str | None:
    path = DATA_DIR / "run_history.csv"
    if not path.exists():
        add_issue(issues, "critical", "Missing artifact: run_history.csv")
        return None

    try:
        df = read_csv(path)
    except Exception as exc:
        add_issue(issues, "critical", f"Cannot read run_history.csv: {exc}")
        return None

    if df.empty or "run_id" not in df.columns:
        add_issue(issues, "critical", "run_history.csv has no run_id rows")
        return None

    if "started_at" in df.columns:
        df = df.sort_values(by=["started_at", "run_id"])

    run_id = str(df.iloc[-1].get("run_id", "")).strip()
    if not run_id:
        add_issue(issues, "critical", "Latest run_history.csv row has blank run_id")
        return None

    return run_id


def check_run_scope_consistency(issues: list[CheckIssue], run_id: str | None) -> None:
    if not run_id:
        return

    for file_name in RUN_SCOPED_FILES:
        path = DATA_DIR / file_name
        if not path.exists():
            continue

        df = read_csv(path)
        if df.empty:
            continue

        if "run_id" not in df.columns:
            add_issue(issues, "critical", f"{file_name}: missing run_id for current-run consistency check")
            continue

        matching = df[df["run_id"].astype(str).str.strip() == run_id]
        if matching.empty:
            add_issue(issues, "critical", f"{file_name}: no rows for latest run_id {run_id}")


def check_latest_run_finalization(issues: list[CheckIssue], run_id: str | None) -> None:
    if not run_id:
        return
    history_path = DATA_DIR / "run_history.csv"
    history = read_csv(history_path)
    matching = history[history["run_id"].astype(str).str.strip() == run_id]
    if len(matching) != 1:
        add_issue(issues, "critical", f"run_history.csv has ambiguous latest run_id {run_id}")
        return
    status = str(matching.iloc[0].get("status", "")).strip().lower()
    status = {"success": "succeeded", "running": "started"}.get(status, status)
    if status != "succeeded":
        add_issue(issues, "critical", f"Latest run {run_id} is not succeeded: status={status}")
        return
    record_path = DATA_DIR.parent / "runs" / run_id / "run_finalization.json"
    try:
        validate_finalization_record(record_path, state_dir=DATA_DIR)
    except Exception as exc:
        add_issue(
            issues,
            "critical",
            f"Latest run {run_id} lacks valid finalization proof: {exc}",
        )


def check_duplicate_processed_fills(issues: list[CheckIssue]) -> None:
    path = DATA_DIR / "processed_fills.csv"
    if not path.exists():
        return

    df = read_csv(path)
    if df.empty or "fill_id" not in df.columns:
        return

    duplicate_ids = sorted(
        fill_id
        for fill_id in df.loc[df["fill_id"].astype(str).str.strip().duplicated(keep=False), "fill_id"].unique()
        if str(fill_id).strip()
    )
    if duplicate_ids:
        add_issue(issues, "critical", f"processed_fills.csv has duplicate fill_id values: {duplicate_ids}")


def check_closed_position_immutability(issues: list[CheckIssue]) -> None:
    current_path = DATA_DIR / "portfolio_state.csv"
    previous_path = DATA_DIR / "portfolio_state_prev_snapshot.csv"
    if not current_path.exists() or not previous_path.exists():
        return

    current = read_csv(current_path)
    previous = read_csv(previous_path)
    required = {"position_id", "status"}
    if not required.issubset(current.columns) or not required.issubset(previous.columns):
        return

    current_closed = current[current["status"].astype(str).str.lower().str.strip() == "closed"].copy()
    previous_closed = previous[previous["status"].astype(str).str.lower().str.strip() == "closed"].copy()
    if current_closed.empty or previous_closed.empty:
        return

    previous_by_id = previous_closed.set_index("position_id", drop=False)
    for _, current_row in current_closed.iterrows():
        position_id = str(current_row.get("position_id", "")).strip()
        if not position_id or position_id not in previous_by_id.index:
            continue

        previous_row = previous_by_id.loc[position_id]
        if isinstance(previous_row, pd.DataFrame):
            add_issue(issues, "critical", f"portfolio_state_prev_snapshot.csv has duplicate closed position_id {position_id}")
            continue

        for field in CLOSED_IMMUTABLE_FIELDS:
            if field not in current.columns or field not in previous.columns:
                continue
            current_value = str(current_row.get(field, "")).strip()
            previous_value = str(previous_row.get(field, "")).strip()
            if current_value != previous_value:
                add_issue(
                    issues,
                    "critical",
                    f"Closed position {position_id} mutated immutable field {field}: {previous_value!r} -> {current_value!r}",
                )


def check_data_source_health(issues: list[CheckIssue]) -> None:
    path = DATA_DIR / "data_source_health.csv"
    if not path.exists():
        return

    df = read_csv(path)
    if df.empty:
        return

    if "error" in df.columns:
        error_rows = df[df["error"].astype(str).str.strip() != ""]
        if not error_rows.empty:
            add_issue(issues, "critical", f"data_source_health.csv reports {len(error_rows)} source error rows")

    if "stale" in df.columns:
        stale_rows = df[df["stale"].astype(str).str.lower().isin({"true", "1", "yes"})]
        if not stale_rows.empty:
            add_issue(issues, "critical", f"data_source_health.csv reports {len(stale_rows)} stale rows")

    if "mode" in df.columns:
        no_trade_rows = df[df["mode"].astype(str).str.lower().eq("no_trade")]
        degraded_rows = df[df["mode"].astype(str).str.lower().eq("degraded")]
        if not no_trade_rows.empty:
            add_issue(
                issues,
                "critical",
                f"data_source_health.csv requires no_trade for {len(no_trade_rows)} rows",
            )
        elif not degraded_rows.empty:
            add_issue(
                issues,
                "warning",
                f"data_source_health.csv is degraded for {len(degraded_rows)} rows",
            )


def run_checks() -> list[CheckIssue]:
    issues: list[CheckIssue] = []
    check_required_artifacts(issues, SCHEMA_CRITICAL_FILES)
    check_required_artifacts(issues, CUSTOM_REQUIRED_COLUMNS.keys())
    run_id = latest_run_id(issues)
    check_run_scope_consistency(issues, run_id)
    check_latest_run_finalization(issues, run_id)
    check_duplicate_processed_fills(issues)
    check_closed_position_immutability(issues)
    check_data_source_health(issues)
    return issues


def main() -> int:
    issues = run_checks()
    critical = [issue for issue in issues if issue.severity == "critical"]
    warnings = [issue for issue in issues if issue.severity != "critical"]

    if critical:
        print("NIGHTLY CHECKLIST: FAIL")
    elif warnings:
        print("NIGHTLY CHECKLIST: PASS WITH WARNINGS")
    else:
        print("NIGHTLY CHECKLIST: PASS")

    for issue in critical + warnings:
        print(f" - {issue.severity.upper()}: {issue.message}")

    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
