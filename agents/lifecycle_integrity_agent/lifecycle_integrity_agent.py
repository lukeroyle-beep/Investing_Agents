from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pandas as pd


BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"

PORTFOLIO_STATE_FILE = DATA_DIR / "portfolio_state.csv"
LIFECYCLE_INTEGRITY_REPORT_FILE = DATA_DIR / "lifecycle_integrity_report.csv"


STATE_COLUMNS = [
    "position_id",
    "ticker",
    "side",
    "status",
    "quantity",
    "entry_price",
    "entry_date",
    "capital_allocated",
    "stop_loss",
    "take_profit",
    "regime_at_entry",
    "sector",
    "signal_score",
    "highest_price_since_entry",
    "lowest_price_since_entry",
    "current_price",
    "market_value",
    "pnl_abs",
    "pnl_pct",
    "exit_flag",
    "exit_reason",
    "last_updated",
    "run_id",
]

REPORT_COLUMNS = [
    "generated_at",
    "run_id",
    "severity",
    "check_name",
    "position_id",
    "ticker",
    "message",
]

ALLOWED_SIDES = {"long", "short"}
ALLOWED_STATUSES = {"open", "closed"}
ALLOWED_EXIT_FLAGS = {"none", "review", "exit_required"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_run_id() -> str:
    return datetime.now(timezone.utc).strftime("RUN_%Y%m%dT%H%M%SZ")


def atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(temp_path, index=False)
    temp_path.replace(path)


def load_portfolio_state() -> pd.DataFrame:
    if not PORTFOLIO_STATE_FILE.exists():
        raise FileNotFoundError(f"Missing file: {PORTFOLIO_STATE_FILE}")

    df = pd.read_csv(PORTFOLIO_STATE_FILE)

    for column in STATE_COLUMNS:
        if column not in df.columns:
            if column == "status":
                df[column] = "open"
            elif column == "side":
                df[column] = "long"
            elif column == "exit_flag":
                df[column] = "none"
            elif column == "exit_reason":
                df[column] = ""
            elif column == "run_id":
                df[column] = ""
            else:
                df[column] = pd.NA

    df = df[STATE_COLUMNS].copy()

    string_columns = [
        "position_id",
        "ticker",
        "side",
        "status",
        "entry_date",
        "regime_at_entry",
        "sector",
        "exit_flag",
        "exit_reason",
        "last_updated",
        "run_id",
    ]
    for column in string_columns:
        df[column] = df[column].fillna("").astype(str).str.strip()

    df["ticker"] = df["ticker"].str.upper()
    df["side"] = df["side"].str.lower()
    df["status"] = df["status"].str.lower()
    df["exit_flag"] = df["exit_flag"].str.lower()

    numeric_columns = [
        "quantity",
        "entry_price",
        "capital_allocated",
        "stop_loss",
        "take_profit",
        "signal_score",
        "highest_price_since_entry",
        "lowest_price_since_entry",
        "current_price",
        "market_value",
        "pnl_abs",
        "pnl_pct",
    ]
    for column in numeric_columns:
        df[column] = pd.to_numeric(df[column], errors="coerce")

    return df


def build_issue(
    generated_at: str,
    run_id: str,
    severity: str,
    check_name: str,
    position_id: str,
    ticker: str,
    message: str,
) -> dict:
    return {
        "generated_at": generated_at,
        "run_id": run_id,
        "severity": severity,
        "check_name": check_name,
        "position_id": position_id,
        "ticker": ticker,
        "message": message,
    }


def run_lifecycle_integrity_agent() -> None:
    run_id = generate_run_id()
    generated_at = utc_now_iso()

    state_df = load_portfolio_state()
    issues: List[dict] = []

    if state_df.empty:
        report_df = pd.DataFrame(columns=REPORT_COLUMNS)
        atomic_write_csv(report_df, LIFECYCLE_INTEGRITY_REPORT_FILE)

        print("Lifecycle Integrity Agent finished.")
        print(f"Saved lifecycle integrity report to: {LIFECYCLE_INTEGRITY_REPORT_FILE}")
        print()
        print("Run summary:")
        print(f"Run ID: {run_id}")
        print("Total positions checked: 0")
        print("Critical issues: 0")
        print("Warnings: 0")
        return

    duplicate_ids = state_df[state_df["position_id"].duplicated(keep=False)]
    for _, row in duplicate_ids.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "duplicate_position_id",
                str(row["position_id"]),
                str(row["ticker"]),
                "Duplicate position_id detected.",
            )
        )

    blank_position_ids = state_df[state_df["position_id"] == ""]
    for _, row in blank_position_ids.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "blank_position_id",
                str(row["position_id"]),
                str(row["ticker"]),
                "Blank position_id detected.",
            )
        )

    blank_tickers = state_df[state_df["ticker"] == ""]
    for _, row in blank_tickers.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "blank_ticker",
                str(row["position_id"]),
                str(row["ticker"]),
                "Blank ticker detected.",
            )
        )

    invalid_sides = state_df[~state_df["side"].isin(ALLOWED_SIDES)]
    for _, row in invalid_sides.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "invalid_side",
                str(row["position_id"]),
                str(row["ticker"]),
                f"Invalid side value: {row['side']}",
            )
        )

    invalid_statuses = state_df[~state_df["status"].isin(ALLOWED_STATUSES)]
    for _, row in invalid_statuses.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "invalid_status",
                str(row["position_id"]),
                str(row["ticker"]),
                f"Invalid status value: {row['status']}",
            )
        )

    invalid_exit_flags = state_df[~state_df["exit_flag"].isin(ALLOWED_EXIT_FLAGS)]
    for _, row in invalid_exit_flags.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "invalid_exit_flag",
                str(row["position_id"]),
                str(row["ticker"]),
                f"Invalid exit_flag value: {row['exit_flag']}",
            )
        )

    open_positions = state_df[state_df["status"] == "open"].copy()
    grouped_open = (
        open_positions.groupby(["ticker", "side"])
        .size()
        .reset_index(name="open_count")
    )
    duplicate_open_positions = grouped_open[grouped_open["open_count"] > 1]

    for _, dup in duplicate_open_positions.iterrows():
        matching_rows = open_positions[
            (open_positions["ticker"] == dup["ticker"]) &
            (open_positions["side"] == dup["side"])
        ]
        for _, row in matching_rows.iterrows():
            issues.append(
                build_issue(
                    generated_at,
                    run_id,
                    "critical",
                    "multiple_open_positions_same_ticker_side",
                    str(row["position_id"]),
                    str(row["ticker"]),
                    f"Multiple open positions exist for ticker={row['ticker']} side={row['side']}.",
                )
            )

    invalid_open_quantity = state_df[
        (state_df["status"] == "open") &
        ((state_df["quantity"].isna()) | (state_df["quantity"] <= 0))
    ]
    for _, row in invalid_open_quantity.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "open_position_invalid_quantity",
                str(row["position_id"]),
                str(row["ticker"]),
                "Open position has missing or non-positive quantity.",
            )
        )

    invalid_open_entry_price = state_df[
        (state_df["status"] == "open") &
        ((state_df["entry_price"].isna()) | (state_df["entry_price"] <= 0))
    ]
    for _, row in invalid_open_entry_price.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "critical",
                "open_position_invalid_entry_price",
                str(row["position_id"]),
                str(row["ticker"]),
                "Open position has missing or non-positive entry_price.",
            )
        )

    missing_open_entry_date = state_df[
        (state_df["status"] == "open") &
        (state_df["entry_date"] == "")
    ]
    for _, row in missing_open_entry_date.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "open_position_missing_entry_date",
                str(row["position_id"]),
                str(row["ticker"]),
                "Open position is missing entry_date.",
            )
        )

    missing_open_last_updated = state_df[
        (state_df["status"] == "open") &
        (state_df["last_updated"] == "")
    ]
    for _, row in missing_open_last_updated.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "open_position_missing_last_updated",
                str(row["position_id"]),
                str(row["ticker"]),
                "Open position is missing last_updated.",
            )
        )

    invalid_open_exit_reason = state_df[
        (state_df["status"] == "open") &
        (state_df["exit_flag"] == "none") &
        (state_df["exit_reason"] != "")
    ]
    for _, row in invalid_open_exit_reason.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "exit_reason_present_when_exit_flag_none",
                str(row["position_id"]),
                str(row["ticker"]),
                "exit_reason is populated while exit_flag is none.",
            )
        )

    missing_flagged_exit_reason = state_df[
        (state_df["status"] == "open") &
        (state_df["exit_flag"].isin(["review", "exit_required"])) &
        (state_df["exit_reason"] == "")
    ]
    for _, row in missing_flagged_exit_reason.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "missing_exit_reason_when_flagged",
                str(row["position_id"]),
                str(row["ticker"]),
                "exit_flag is set but exit_reason is blank.",
            )
        )

    invalid_closed_quantity = state_df[
        (state_df["status"] == "closed") &
        (state_df["quantity"].fillna(0.0) != 0.0)
    ]
    for _, row in invalid_closed_quantity.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "closed_position_nonzero_quantity",
                str(row["position_id"]),
                str(row["ticker"]),
                "Closed position has non-zero quantity.",
            )
        )

    invalid_closed_market_value = state_df[
        (state_df["status"] == "closed") &
        (state_df["market_value"].fillna(0.0) != 0.0)
    ]
    for _, row in invalid_closed_market_value.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "closed_position_nonzero_market_value",
                str(row["position_id"]),
                str(row["ticker"]),
                "Closed position has non-zero market_value.",
            )
        )

    closed_positions_with_flags = state_df[
        (state_df["status"] == "closed") &
        (state_df["exit_flag"] != "none")
    ]
    for _, row in closed_positions_with_flags.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "closed_position_still_flagged",
                str(row["position_id"]),
                str(row["ticker"]),
                "Closed position still has a non-none exit_flag.",
            )
        )

    high_less_than_low = state_df[
        state_df["highest_price_since_entry"].notna() &
        state_df["lowest_price_since_entry"].notna() &
        (state_df["highest_price_since_entry"] < state_df["lowest_price_since_entry"])
    ]
    for _, row in high_less_than_low.iterrows():
        issues.append(
            build_issue(
                generated_at,
                run_id,
                "warning",
                "highest_price_below_lowest_price",
                str(row["position_id"]),
                str(row["ticker"]),
                "highest_price_since_entry is below lowest_price_since_entry.",
            )
        )

    report_df = pd.DataFrame(issues, columns=REPORT_COLUMNS)
    if report_df.empty:
        report_df = pd.DataFrame(columns=REPORT_COLUMNS)

    atomic_write_csv(report_df, LIFECYCLE_INTEGRITY_REPORT_FILE)

    critical_count = int((report_df["severity"] == "critical").sum()) if not report_df.empty else 0
    warning_count = int((report_df["severity"] == "warning").sum()) if not report_df.empty else 0

    print("Lifecycle Integrity Agent finished.")
    print(f"Saved lifecycle integrity report to: {LIFECYCLE_INTEGRITY_REPORT_FILE}")
    print()
    print("Run summary:")
    print(f"Run ID: {run_id}")
    print(f"Total positions checked: {len(state_df)}")
    print(f"Critical issues: {critical_count}")
    print(f"Warnings: {warning_count}")


if __name__ == "__main__":
    run_lifecycle_integrity_agent()