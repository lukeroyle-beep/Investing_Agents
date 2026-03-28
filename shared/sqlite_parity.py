from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from shared.paths import data_path
from shared.sqlite_sidecar import fetch_all_rows

EVENT_LOG_PATH = data_path("event_log.csv")
RUN_HISTORY_PATH = data_path("run_history.csv")
RUN_RECONCILIATION_SUMMARY_PATH = data_path("run_reconciliation_summary.csv")
CASH_LEDGER_PATH = data_path("cash_ledger.csv")
PROCESSED_FILLS_PATH = data_path("processed_fills.csv")
PORTFOLIO_EQUITY_HISTORY_PATH = data_path("portfolio_equity_history.csv")


@dataclass(frozen=True)
class ParityIssue:
    table_name: str
    issue_type: str
    message: str


@dataclass(frozen=True)
class TableParityConfig:
    table_name: str
    csv_path: Path
    key_columns: list[str]
    compare_columns: list[str]
    run_scoped: bool = True


@dataclass(frozen=True)
class ParityReport:
    run_id: str | None
    issues: list[ParityIssue]

    @property
    def passed(self) -> bool:
        return len(self.issues) == 0


def _parity_tables() -> list[TableParityConfig]:
    return [
        TableParityConfig(
            table_name="event_log",
            csv_path=EVENT_LOG_PATH,
            key_columns=["event_id"],
            compare_columns=[
                "event_id",
                "run_id",
                "event_time",
                "agent_name",
                "event_type",
                "entity_type",
                "entity_id",
                "ticker",
                "position_id",
                "order_id",
                "severity",
                "message",
                "before_json",
                "after_json",
                "metadata_json",
            ],
        ),
        TableParityConfig(
            table_name="run_history",
            csv_path=RUN_HISTORY_PATH,
            key_columns=["run_id"],
            compare_columns=[
                "run_id",
                "started_at",
                "completed_at",
                "status",
                "failed_agent",
                "error_message",
                "notes",
            ],
        ),
        TableParityConfig(
            table_name="run_reconciliation_summary",
            csv_path=RUN_RECONCILIATION_SUMMARY_PATH,
            key_columns=["run_id"],
            compare_columns=[
                "run_id",
                "started_at",
                "completed_at",
                "status",
                "failed_agent",
                "fills_processed",
                "positions_opened",
                "positions_closed",
                "positions_marked_exit_required",
                "cash_delta",
                "realised_pnl_delta",
                "unrealised_pnl_delta",
                "equity_delta",
                "exposure_delta",
                "validation_warning_count",
                "validation_failure_count",
                "notes",
            ],
        ),
        TableParityConfig(
            table_name="cash_ledger",
            csv_path=CASH_LEDGER_PATH,
            key_columns=["ledger_id"],
            compare_columns=[
                "ledger_id",
                "run_id",
                "timestamp",
                "event_type",
                "position_id",
                "ticker",
                "side",
                "action",
                "amount",
                "fees",
                "cash_balance_after",
                "notes",
            ],
        ),
        TableParityConfig(
            table_name="processed_fills",
            csv_path=PROCESSED_FILLS_PATH,
            key_columns=["fill_id"],
            compare_columns=[
                "fill_id",
                "processed_at",
                "run_id",
            ],
        ),
        TableParityConfig(
            table_name="portfolio_equity_history",
            csv_path=PORTFOLIO_EQUITY_HISTORY_PATH,
            key_columns=["run_id", "timestamp"],
            compare_columns=[
                "timestamp",
                "run_id",
                "cash_balance",
                "open_market_value",
                "gross_exposure",
                "net_exposure",
                "unrealised_pnl_abs",
                "realised_pnl_abs",
                "total_equity",
                "open_positions",
                "closed_positions",
                "peak_equity",
                "drawdown_abs",
                "drawdown_pct",
            ],
        ),
    ]


def _normalise_scalar(value: Any) -> Any:
    if pd.isna(value):
        return None

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return round(float(value), 10)

    text = str(value).strip()
    return None if text == "" else text


def _prepare_df(df: pd.DataFrame, config: TableParityConfig, run_id: str | None) -> pd.DataFrame:
    if df.empty:
        columns = list(dict.fromkeys(config.compare_columns))
        return pd.DataFrame(columns=columns)

    output_df = df.copy()
    for column in config.compare_columns:
        if column not in output_df.columns:
            output_df[column] = pd.NA

    output_df = output_df[config.compare_columns].copy()

    if run_id is not None and config.run_scoped and "run_id" in output_df.columns:
        output_df = output_df[output_df["run_id"].astype(str).str.strip() == str(run_id).strip()].copy()

    sort_columns = [column for column in config.key_columns if column in output_df.columns]
    if sort_columns:
        output_df = output_df.sort_values(by=sort_columns, kind="stable").reset_index(drop=True)

    for column in output_df.columns:
        output_df[column] = output_df[column].map(_normalise_scalar)

    return output_df.reset_index(drop=True)


def _load_csv_df(config: TableParityConfig, run_id: str | None) -> pd.DataFrame:
    if not config.csv_path.exists():
        return pd.DataFrame(columns=config.compare_columns)
    return _prepare_df(pd.read_csv(config.csv_path), config, run_id)


def _load_sqlite_df(config: TableParityConfig, run_id: str | None) -> pd.DataFrame:
    rows = fetch_all_rows(config.table_name)
    return _prepare_df(pd.DataFrame(rows), config, run_id)


def validate_table_parity(config: TableParityConfig, run_id: str | None = None) -> list[ParityIssue]:
    csv_df = _load_csv_df(config, run_id)
    sqlite_df = _load_sqlite_df(config, run_id)
    issues: list[ParityIssue] = []

    if len(csv_df) != len(sqlite_df):
        issues.append(
            ParityIssue(
                table_name=config.table_name,
                issue_type="row_count_mismatch",
                message=(
                    f"{config.table_name}: CSV rows={len(csv_df)} "
                    f"SQLite rows={len(sqlite_df)}"
                ),
            )
        )

    csv_keys = {tuple(csv_df.iloc[idx][key] for key in config.key_columns) for idx in range(len(csv_df))}
    sqlite_keys = {tuple(sqlite_df.iloc[idx][key] for key in config.key_columns) for idx in range(len(sqlite_df))}

    missing_in_sqlite = sorted(csv_keys - sqlite_keys)
    if missing_in_sqlite:
        issues.append(
            ParityIssue(
                table_name=config.table_name,
                issue_type="missing_rows_in_sqlite",
                message=f"{config.table_name}: keys present in CSV but missing in SQLite: {missing_in_sqlite[:5]}",
            )
        )

    missing_in_csv = sorted(sqlite_keys - csv_keys)
    if missing_in_csv:
        issues.append(
            ParityIssue(
                table_name=config.table_name,
                issue_type="missing_rows_in_csv",
                message=f"{config.table_name}: keys present in SQLite but missing in CSV: {missing_in_csv[:5]}",
            )
        )

    if issues:
        return issues

    csv_by_key = {
        tuple(csv_df.iloc[idx][key] for key in config.key_columns): csv_df.iloc[idx].to_dict()
        for idx in range(len(csv_df))
    }
    sqlite_by_key = {
        tuple(sqlite_df.iloc[idx][key] for key in config.key_columns): sqlite_df.iloc[idx].to_dict()
        for idx in range(len(sqlite_df))
    }

    for key in sorted(csv_keys):
        csv_row = csv_by_key[key]
        sqlite_row = sqlite_by_key[key]
        differing_columns = [
            column
            for column in config.compare_columns
            if _normalise_scalar(csv_row.get(column)) != _normalise_scalar(sqlite_row.get(column))
        ]
        if differing_columns:
            issues.append(
                ParityIssue(
                    table_name=config.table_name,
                    issue_type="value_mismatch",
                    message=(
                        f"{config.table_name}: key={key} differs in columns {differing_columns}. "
                        f"csv={ {column: _normalise_scalar(csv_row.get(column)) for column in differing_columns} } "
                        f"sqlite={ {column: _normalise_scalar(sqlite_row.get(column)) for column in differing_columns} }"
                    ),
                )
            )
            break

    return issues


def validate_sqlite_dual_write_parity(run_id: str | None = None) -> ParityReport:
    issues: list[ParityIssue] = []
    for config in _parity_tables():
        issues.extend(validate_table_parity(config, run_id=run_id))
    return ParityReport(run_id=run_id, issues=issues)


def format_parity_report(report: ParityReport) -> str:
    header = (
        f"SQLite parity check passed for run_id={report.run_id}."
        if report.passed
        else f"SQLite parity check failed for run_id={report.run_id}."
    )
    if report.passed:
        return header

    detail_lines = "\n".join(f"- [{issue.table_name}] {issue.issue_type}: {issue.message}" for issue in report.issues)
    return f"{header}\n{detail_lines}"
