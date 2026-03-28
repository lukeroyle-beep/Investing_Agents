from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd
from pandas.errors import EmptyDataError

from agents.shared.event_log import append_validation_event
from shared.invariants import (
    InvariantFailure,
    InvariantResult,
    build_invariant_context,
    evaluate_all_invariants,
)
from shared.paths import DATA_DIR
from shared.run_context import get_or_create_run_id


STATE_PATH = str(DATA_DIR / "portfolio_state.csv")
REPORT_PATH = str(DATA_DIR / "lifecycle_integrity_report.csv")
SNAPSHOT_PATH = str(DATA_DIR / "portfolio_state_prev_snapshot.csv")
CASH_STATE_PATH = str(DATA_DIR / "cash_state.csv")
EQUITY_HISTORY_PATH = str(DATA_DIR / "portfolio_equity_history.csv")
PROCESSED_FILLS_PATH = str(DATA_DIR / "processed_fills.csv")
CASH_LEDGER_PATH = str(DATA_DIR / "cash_ledger.csv")
RUN_HISTORY_PATH = str(DATA_DIR / "run_history.csv")
AGENT_NAME = "Lifecycle Integrity Agent"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_read_csv(path: str, required: bool = True) -> pd.DataFrame:
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(f"Required file not found: {path}")
        return pd.DataFrame()

    if os.path.getsize(path) == 0:
        if required:
            raise ValueError(f"Required CSV is zero-byte empty: {path}")
        return pd.DataFrame()

    try:
        return pd.read_csv(path)
    except EmptyDataError:
        if required:
            raise ValueError(f"Required CSV has no parseable columns: {path}")
        return pd.DataFrame()


def append_issue(
    issues: List[Dict[str, Any]],
    record_type: str,
    severity: str,
    invariant_name: str | None,
    rule: str,
    position_id: str | None,
    ticker: str | None,
    detail: str,
    total_checks: int | None = None,
    passed_checks: int | None = None,
    warning_count: int | None = None,
    failure_count: int | None = None,
) -> None:
    issues.append(
        {
            "checked_at": utc_now_iso(),
            "record_type": record_type,
            "severity": severity,
            "invariant_name": invariant_name,
            "rule": rule,
            "position_id": position_id,
            "ticker": ticker,
            "detail": detail,
            "total_checks": total_checks,
            "passed_checks": passed_checks,
            "warning_count": warning_count,
            "failure_count": failure_count,
        }
    )


def write_report(issues: List[Dict[str, Any]]) -> None:
    report_df = pd.DataFrame(issues)

    if report_df.empty:
        report_df = pd.DataFrame(
            [
                {
                    "checked_at": utc_now_iso(),
                    "record_type": "summary",
                    "severity": "info",
                    "invariant_name": None,
                    "rule": "no_issues",
                    "position_id": None,
                    "ticker": None,
                    "detail": "No lifecycle integrity issues detected",
                    "total_checks": 0,
                    "passed_checks": 0,
                    "warning_count": 0,
                    "failure_count": 0,
                }
            ]
        )

    report_df.to_csv(REPORT_PATH, index=False)


def write_snapshot(df: pd.DataFrame) -> None:
    snapshot_df = df.copy()
    snapshot_df.to_csv(SNAPSHOT_PATH, index=False)


def emit_validation_summary_event(
    run_id: str,
    issues: List[Dict[str, Any]],
    critical_count: int,
    warning_count: int,
    passed_count: int,
    passed: bool,
) -> None:
    """
    Append one summary validation event for this Lifecycle Integrity run.
    """
    critical_issues = [issue for issue in issues if issue.get("severity") == "critical"]
    affected_position_ids = sorted(
        {
            str(issue.get("position_id")).strip()
            for issue in critical_issues
            if str(issue.get("position_id") or "").strip()
        }
    )

    append_validation_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        passed=passed,
        message=(
            "Lifecycle Integrity validation passed"
            if passed
            else f"Lifecycle Integrity validation failed: {critical_count} critical issues detected"
        ),
        metadata={
            "validation_stage": "final_summary",
            "total_issue_count": len(issues),
            "passed_check_count": passed_count,
            "warning_check_count": warning_count,
            "critical_issue_count": critical_count,
            "report_path": REPORT_PATH,
            "affected_position_ids": affected_position_ids,
        },
    )


def _failures_to_issue_rows(failures: list[InvariantFailure]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    for failure in failures:
        append_issue(
            issues=issues,
            record_type="detail",
            severity=failure.severity,
            invariant_name=failure.invariant_name,
            rule=failure.invariant_name,
            position_id=failure.position_id,
            ticker=failure.ticker,
            detail=failure.message,
        )

    return issues


def _build_report_rows(results: list[InvariantResult]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    total_checks = len(results)
    passed_checks = sum(1 for result in results if result.passed)
    warning_count = sum(1 for result in results if result.has_warning and not result.has_hard_failure)
    failure_count = sum(1 for result in results if result.has_hard_failure)

    append_issue(
        issues=issues,
        record_type="summary",
        severity="info" if failure_count == 0 else "error",
        invariant_name=None,
        rule="summary",
        position_id=None,
        ticker=None,
        detail=(
            "Lifecycle Integrity summary: "
            f"passed_checks={passed_checks}, warnings={warning_count}, failures={failure_count}"
        ),
        total_checks=total_checks,
        passed_checks=passed_checks,
        warning_count=warning_count,
        failure_count=failure_count,
    )

    for result in results:
        if result.passed:
            append_issue(
                issues=issues,
                record_type="check",
                severity="info",
                invariant_name=result.invariant_name,
                rule=result.invariant_name,
                position_id=None,
                ticker=None,
                detail="Invariant passed.",
            )
            continue

        if result.has_warning and not result.has_hard_failure:
            append_issue(
                issues=issues,
                record_type="check",
                severity="warning",
                invariant_name=result.invariant_name,
                rule=result.invariant_name,
                position_id=None,
                ticker=None,
                detail="Invariant produced warnings.",
            )
        else:
            append_issue(
                issues=issues,
                record_type="check",
                severity="critical",
                invariant_name=result.invariant_name,
                rule=result.invariant_name,
                position_id=None,
                ticker=None,
                detail="Invariant failed.",
            )

    issues.extend(_failures_to_issue_rows([failure for result in results for failure in result.failures]))
    return issues


def run_lifecycle_integrity_agent() -> None:
    run_id = get_or_create_run_id()
    current_df = safe_read_csv(STATE_PATH, required=True)
    previous_df = safe_read_csv(SNAPSHOT_PATH, required=False)
    cash_state_df = safe_read_csv(CASH_STATE_PATH, required=False)
    equity_history_df = safe_read_csv(EQUITY_HISTORY_PATH, required=False)
    processed_fills_df = safe_read_csv(PROCESSED_FILLS_PATH, required=False)
    cash_ledger_df = safe_read_csv(CASH_LEDGER_PATH, required=False)
    run_history_df = safe_read_csv(RUN_HISTORY_PATH, required=False)

    invariant_context = build_invariant_context(
        current_state=current_df,
        previous_state=previous_df,
        cash_state=cash_state_df,
        equity_history=equity_history_df,
        processed_fills=processed_fills_df,
        cash_ledger=cash_ledger_df,
        run_history=run_history_df,
    )
    current_df = invariant_context.current_state
    results = evaluate_all_invariants(invariant_context)
    issues = _build_report_rows(results)

    write_report(issues)

    critical_count = sum(1 for result in results if result.has_hard_failure)
    warning_count = sum(1 for result in results if result.has_warning and not result.has_hard_failure)
    passed_count = sum(1 for result in results if result.passed)

    print("Lifecycle Integrity Agent finished.")
    print(f"Saved integrity report to: {REPORT_PATH}")
    print(f"Saved prior-state snapshot to: {SNAPSHOT_PATH}")
    print(f"Passed checks: {passed_count}")
    print(f"Warnings found: {warning_count}")
    print(f"Critical issues found: {critical_count}")

    if critical_count > 0:
        emit_validation_summary_event(
            run_id=run_id,
            issues=issues,
            critical_count=critical_count,
            warning_count=warning_count,
            passed_count=passed_count,
            passed=False,
        )
        raise RuntimeError(
            f"Lifecycle Integrity Agent hard-failed. "
            f"Critical issues found: {critical_count}. "
            f"See {REPORT_PATH}"
        )

    emit_validation_summary_event(
        run_id=run_id,
        issues=issues,
        critical_count=critical_count,
        warning_count=warning_count,
        passed_count=passed_count,
        passed=True,
    )
    write_snapshot(current_df)


if __name__ == "__main__":
    run_lifecycle_integrity_agent()
