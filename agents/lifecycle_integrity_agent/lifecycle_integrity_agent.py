from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import pandas as pd
from pandas.errors import EmptyDataError


DATA_DIR = "data"
STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
REPORT_PATH = os.path.join(DATA_DIR, "lifecycle_integrity_report.csv")
SNAPSHOT_PATH = os.path.join(DATA_DIR, "portfolio_state_prev_snapshot.csv")

VALID_STATUSES = {"open", "exit_required", "closed"}
VALID_SIDES = {"long", "short"}

IMMUTABLE_CLOSED_FIELDS = [
    "status",
    "quantity",
    "entry_price",
    "entry_date",
    "closed_at",
    "exit_price",
    "realised_pnl_abs",
    "fees_total",
]


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


def normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    aliases = {
        "average_entry_price": "entry_price",
        "current_qty": "quantity",
    }

    for old_col, new_col in aliases.items():
        if old_col in df.columns and new_col not in df.columns:
            df[new_col] = df[old_col]

    required_columns = [
        "position_id",
        "ticker",
        "side",
        "status",
        "quantity",
        "entry_price",
        "entry_date",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "realised_pnl_abs",
        "fees_total",
        "exit_flag",
        "exit_reason",
        "run_id",
        "last_updated",
        "closed_at",
        "exit_price",
        "highest_price_since_entry",
        "lowest_price_since_entry",
    ]

    numeric_default_zero = {
        "market_value",
        "pnl_abs",
        "pnl_pct",
        "realised_pnl_abs",
        "fees_total",
    }

    for col in required_columns:
        if col not in df.columns:
            if col in numeric_default_zero:
                df[col] = 0.0
            elif col == "exit_flag":
                df[col] = False
            elif col == "exit_reason":
                df[col] = ""
            else:
                df[col] = pd.NA

    df["exit_flag"] = df["exit_flag"].fillna(False)
    df["exit_reason"] = df["exit_reason"].fillna("")

    return df


def append_issue(
    issues: List[Dict[str, Any]],
    severity: str,
    rule: str,
    position_id: str | None,
    ticker: str | None,
    detail: str,
) -> None:
    issues.append(
        {
            "checked_at": utc_now_iso(),
            "severity": severity,
            "rule": rule,
            "position_id": position_id,
            "ticker": ticker,
            "detail": detail,
        }
    )


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


def validate_state(df: pd.DataFrame) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    # 1. Duplicate position_id
    dup_position_ids = df[df["position_id"].duplicated(keep=False)]
    for _, row in dup_position_ids.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="duplicate_position_id",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="position_id appears more than once in portfolio_state.csv",
        )

    # 2. Invalid status
    invalid_status_rows = df[~df["status"].isin(VALID_STATUSES)]
    for _, row in invalid_status_rows.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="invalid_status",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail=f"Invalid status: {row.get('status')}",
        )

    # 3. Invalid side
    invalid_side_rows = df[~df["side"].isin(VALID_SIDES)]
    for _, row in invalid_side_rows.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="invalid_side",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail=f"Invalid side: {row.get('side')}",
        )

    # 4. Active positions must have valid quantity and entry_price
    active_rows = df[df["status"].isin(["open", "exit_required"])].copy()

    active_qty = pd.to_numeric(active_rows["quantity"], errors="coerce")
    invalid_qty_rows = active_rows[active_qty.isna() | (active_qty <= 0)]
    for _, row in invalid_qty_rows.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="invalid_active_quantity",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="Active position has missing or non-positive quantity",
        )

    active_entry = pd.to_numeric(active_rows["entry_price"], errors="coerce")
    invalid_entry_rows = active_rows[active_entry.isna() | (active_entry <= 0)]
    for _, row in invalid_entry_rows.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="invalid_active_entry_price",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="Active position has missing or non-positive entry_price",
        )

    # 5. exit_flag must align with status
    for _, row in df.iterrows():
        status = row.get("status")
        raw_exit_flag = row.get("exit_flag")

        if pd.isna(raw_exit_flag):
            exit_flag = ""
        elif isinstance(raw_exit_flag, bool):
            exit_flag = str(raw_exit_flag).lower()
        else:
            exit_flag = str(raw_exit_flag).strip().lower()

        if status == "open" and exit_flag == "true":
            append_issue(
                issues,
                severity="critical",
                rule="exit_flag_status_mismatch",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
                detail="status=open but exit_flag=true",
            )

        if status == "exit_required" and exit_flag != "true":
            append_issue(
                issues,
                severity="critical",
                rule="exit_flag_status_mismatch",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
                detail="status=exit_required but exit_flag is not true",
            )

        if status == "closed" and exit_flag == "true":
            append_issue(
                issues,
                severity="critical",
                rule="exit_flag_status_mismatch",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
                detail="status=closed but exit_flag=true",
            )

    # 6. Closed positions must have exit_price and closed_at
    closed_rows = df[df["status"] == "closed"].copy()

    closed_exit_price = pd.to_numeric(closed_rows["exit_price"], errors="coerce")
    invalid_closed_exit_price = closed_rows[closed_exit_price.isna() | (closed_exit_price <= 0)]
    for _, row in invalid_closed_exit_price.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="closed_missing_exit_price",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="Closed position missing valid exit_price",
        )

    invalid_closed_at = closed_rows[closed_rows["closed_at"].isna()]
    for _, row in invalid_closed_at.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="closed_missing_closed_at",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="Closed position missing closed_at timestamp",
        )

    # 7. Closed positions must have zero market value
    closed_market_value = pd.to_numeric(closed_rows["market_value"], errors="coerce").fillna(0.0)
    invalid_closed_market_value = closed_rows[closed_market_value != 0.0]
    for _, row in invalid_closed_market_value.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="closed_nonzero_market_value",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="Closed position has non-zero market_value",
        )

    # 8. Closed positions must retain valid quantity
    closed_quantity = pd.to_numeric(closed_rows["quantity"], errors="coerce")
    invalid_closed_quantity = closed_rows[closed_quantity.isna() | (closed_quantity <= 0)]
    for _, row in invalid_closed_quantity.iterrows():
        append_issue(
            issues,
            severity="critical",
            rule="closed_invalid_quantity",
            position_id=str(row.get("position_id")),
            ticker=str(row.get("ticker")),
            detail="Closed position has missing or non-positive quantity",
        )

    # 9. No duplicate active positions for same ticker and side
    active_dupes = (
        active_rows.groupby(["ticker", "side"])
        .size()
        .reset_index(name="count")
    )
    active_dupes = active_dupes[active_dupes["count"] > 1]

    for _, dup in active_dupes.iterrows():
        dup_rows = active_rows[
            (active_rows["ticker"] == dup["ticker"]) &
            (active_rows["side"] == dup["side"])
        ]
        for _, row in dup_rows.iterrows():
            append_issue(
                issues,
                severity="critical",
                rule="duplicate_active_ticker_side",
                position_id=str(row.get("position_id")),
                ticker=str(row.get("ticker")),
                detail=f"More than one active position for ticker={dup['ticker']} side={dup['side']}",
            )

    return issues


def validate_closed_position_immutability(
    current_df: pd.DataFrame,
    previous_df: pd.DataFrame,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []

    if previous_df.empty:
        return issues

    prev_closed = previous_df[previous_df["status"] == "closed"].copy()
    curr_by_id = current_df.set_index("position_id", drop=False)

    if prev_closed.empty:
        return issues

    for _, prev_row in prev_closed.iterrows():
        position_id = str(prev_row["position_id"])

        if position_id not in curr_by_id.index:
            append_issue(
                issues,
                severity="critical",
                rule="closed_position_missing_in_current_state",
                position_id=position_id,
                ticker=str(prev_row.get("ticker")),
                detail="Previously closed position is missing from current portfolio_state.csv",
            )
            continue

        curr_row = curr_by_id.loc[position_id]

        # If duplicate index somehow returns DataFrame, treat as critical corruption
        if isinstance(curr_row, pd.DataFrame):
            append_issue(
                issues,
                severity="critical",
                rule="duplicate_position_id_on_snapshot_compare",
                position_id=position_id,
                ticker=str(prev_row.get("ticker")),
                detail="Snapshot comparison found duplicate position_id in current state",
            )
            continue

        for field in IMMUTABLE_CLOSED_FIELDS:
            prev_value = prev_row.get(field)
            curr_value = curr_row.get(field)

            if not values_equal(prev_value, curr_value):
                append_issue(
                    issues,
                    severity="critical",
                    rule="closed_position_field_mutated",
                    position_id=position_id,
                    ticker=str(curr_row.get("ticker")),
                    detail=(
                        f"Closed position immutable field changed: {field}. "
                        f"previous={normalise_scalar(prev_value)} current={normalise_scalar(curr_value)}"
                    ),
                )

    return issues


def write_report(issues: List[Dict[str, Any]]) -> None:
    report_df = pd.DataFrame(issues)

    if report_df.empty:
        report_df = pd.DataFrame(
            [
                {
                    "checked_at": utc_now_iso(),
                    "severity": "info",
                    "rule": "no_issues",
                    "position_id": None,
                    "ticker": None,
                    "detail": "No lifecycle integrity issues detected",
                }
            ]
        )

    report_df.to_csv(REPORT_PATH, index=False)


def write_snapshot(df: pd.DataFrame) -> None:
    snapshot_df = df.copy()
    snapshot_df.to_csv(SNAPSHOT_PATH, index=False)


def run_lifecycle_integrity_agent() -> None:
    current_df = safe_read_csv(STATE_PATH, required=True)
    current_df = normalise_columns(current_df)

    previous_df = safe_read_csv(SNAPSHOT_PATH, required=False)
    if not previous_df.empty:
        previous_df = normalise_columns(previous_df)

    issues = []
    issues.extend(validate_state(current_df))
    issues.extend(validate_closed_position_immutability(current_df, previous_df))

    write_report(issues)

    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")

    print("Lifecycle Integrity Agent finished.")
    print(f"Saved integrity report to: {REPORT_PATH}")
    print(f"Saved prior-state snapshot to: {SNAPSHOT_PATH}")
    print(f"Critical issues found: {critical_count}")

    if critical_count > 0:
        raise RuntimeError(
            f"Lifecycle Integrity Agent hard-failed. "
            f"Critical issues found: {critical_count}. "
            f"See {REPORT_PATH}"
        )

    write_snapshot(current_df)


if __name__ == "__main__":
    run_lifecycle_integrity_agent()