from __future__ import annotations

from dataclasses import dataclass, field


NUMERIC_TYPE_NAMES = {"int", "integer", "float", "number", "numeric"}
TEXT_TYPE_NAMES = {"str", "string", "text", "json"}
DATETIME_TYPE_NAMES = {"datetime", "timestamp", "date"}


@dataclass(frozen=True)
class FileSchemaRegistryEntry:
    file_name: str
    schema_version: str
    owner_agent: str
    required_columns: list[str]
    optional_columns: list[str] = field(default_factory=list)
    column_aliases: dict[str, list[str]] = field(default_factory=dict)
    expected_types: dict[str, str] = field(default_factory=dict)
    nullability: dict[str, bool] = field(default_factory=dict)
    canonical_column_order: list[str] = field(default_factory=list)
    write_mode: str = "full_rewrite"
    append_only: bool = False
    mutability_rules: dict[str, str] = field(default_factory=dict)

    @property
    def all_columns(self) -> list[str]:
        ordered = list(self.canonical_column_order)

        if ordered:
            return ordered

        return list(dict.fromkeys(self.required_columns + self.optional_columns))

    def columns_with_types(self, *type_names: str) -> list[str]:
        wanted = {name.strip().lower() for name in type_names}
        return [
            column
            for column in self.all_columns
            if self.expected_types.get(column, "").strip().lower() in wanted
        ]

    def nullable_columns(self) -> list[str]:
        return [column for column in self.all_columns if self.nullability.get(column, True)]

    def non_nullable_columns(self) -> list[str]:
        return [column for column in self.all_columns if not self.nullability.get(column, True)]


def _build_registry() -> dict[str, FileSchemaRegistryEntry]:
    registry: dict[str, FileSchemaRegistryEntry] = {}

    def register(entry: FileSchemaRegistryEntry) -> None:
        registry[entry.file_name] = entry

    register(
        FileSchemaRegistryEntry(
            file_name="portfolio_state.csv",
            schema_version="1.0",
            owner_agent="Fill Agent",
            required_columns=[
                "position_id",
                "ticker",
                "side",
                "status",
                "entry_date",
                "entry_price",
                "quantity",
                "capital_allocated",
                "stop_loss",
                "take_profit",
                "current_price",
                "market_value",
                "pnl_abs",
                "pnl_pct",
                "regime_at_entry",
                "sector",
                "signal_score",
                "highest_price_since_entry",
                "lowest_price_since_entry",
                "exit_reason",
                "last_updated",
                "run_id",
            ],
            optional_columns=[
                "exit_flag",
                "realised_pnl_abs",
                "fees_total",
                "closed_at",
                "exit_price",
            ],
            column_aliases={
                "entry_price": ["average_entry_price"],
                "quantity": ["current_qty"],
                "pnl_abs": ["unrealised_pnl_abs"],
                "pnl_pct": ["unrealised_pnl_pct"],
                "last_updated": ["last_updated_at"],
            },
            expected_types={
                "position_id": "string",
                "ticker": "string",
                "side": "string",
                "status": "string",
                "entry_date": "datetime",
                "entry_price": "float",
                "quantity": "float",
                "capital_allocated": "float",
                "stop_loss": "float",
                "take_profit": "float",
                "current_price": "float",
                "market_value": "float",
                "pnl_abs": "float",
                "pnl_pct": "float",
                "regime_at_entry": "string",
                "sector": "string",
                "signal_score": "float",
                "highest_price_since_entry": "float",
                "lowest_price_since_entry": "float",
                "exit_flag": "string",
                "exit_reason": "string",
                "last_updated": "datetime",
                "run_id": "string",
                "realised_pnl_abs": "float",
                "fees_total": "float",
                "closed_at": "datetime",
                "exit_price": "float",
            },
            nullability={
                "position_id": False,
                "ticker": False,
                "side": False,
                "status": False,
                "entry_date": False,
                "entry_price": False,
                "quantity": False,
                "capital_allocated": True,
                "stop_loss": True,
                "take_profit": True,
                "current_price": True,
                "market_value": True,
                "pnl_abs": True,
                "pnl_pct": True,
                "regime_at_entry": True,
                "sector": True,
                "signal_score": True,
                "highest_price_since_entry": True,
                "lowest_price_since_entry": True,
                "exit_flag": True,
                "exit_reason": True,
                "last_updated": True,
                "run_id": True,
                "realised_pnl_abs": True,
                "fees_total": True,
                "closed_at": True,
                "exit_price": True,
            },
            canonical_column_order=[
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
                "realised_pnl_abs",
                "fees_total",
                "closed_at",
                "exit_price",
            ],
            write_mode="full_rewrite_with_row_level_state_mutations",
            append_only=False,
            mutability_rules={
                "economic_boundary": "Fill processing is the economic mutation boundary.",
                "closed_positions": "Closed positions remain economically immutable after closure.",
                "non_mutating_agents": "Exit Agent and Portfolio Equity Agent must not mutate portfolio_state.csv.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="cash_state.csv",
            schema_version="1.0",
            owner_agent="Fill Agent",
            required_columns=["as_of", "cash_balance"],
            expected_types={
                "as_of": "datetime",
                "cash_balance": "float",
            },
            nullability={
                "as_of": False,
                "cash_balance": False,
            },
            canonical_column_order=["as_of", "cash_balance"],
            write_mode="full_rewrite_single_row_snapshot",
            append_only=False,
            mutability_rules={
                "ownership": "Only Fill Agent updates the cash balance snapshot.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="cash_ledger.csv",
            schema_version="1.0",
            owner_agent="Fill Agent",
            required_columns=[
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
            expected_types={
                "ledger_id": "string",
                "run_id": "string",
                "timestamp": "datetime",
                "event_type": "string",
                "position_id": "string",
                "ticker": "string",
                "side": "string",
                "action": "string",
                "amount": "float",
                "fees": "float",
                "cash_balance_after": "float",
                "notes": "string",
            },
            nullability={
                "ledger_id": False,
                "run_id": False,
                "timestamp": False,
                "event_type": False,
                "position_id": False,
                "ticker": False,
                "side": False,
                "action": False,
                "amount": False,
                "fees": False,
                "cash_balance_after": False,
                "notes": True,
            },
            canonical_column_order=[
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
            write_mode="append_rows",
            append_only=True,
            mutability_rules={
                "append_only": "Cash ledger rows are immutable after append.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="processed_fills.csv",
            schema_version="1.0",
            owner_agent="Fill Agent",
            required_columns=["fill_id", "processed_at", "run_id"],
            expected_types={
                "fill_id": "string",
                "processed_at": "datetime",
                "run_id": "string",
            },
            nullability={
                "fill_id": False,
                "processed_at": False,
                "run_id": True,
            },
            canonical_column_order=["fill_id", "processed_at", "run_id"],
            write_mode="append_rows",
            append_only=True,
            mutability_rules={
                "append_only": "Processed fills are recorded once per fill_id and then treated as immutable.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="portfolio_equity_history.csv",
            schema_version="1.0",
            owner_agent="Portfolio Equity Agent",
            required_columns=[
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
            expected_types={
                "timestamp": "datetime",
                "run_id": "string",
                "cash_balance": "float",
                "open_market_value": "float",
                "gross_exposure": "float",
                "net_exposure": "float",
                "unrealised_pnl_abs": "float",
                "realised_pnl_abs": "float",
                "total_equity": "float",
                "open_positions": "float",
                "closed_positions": "float",
                "peak_equity": "float",
                "drawdown_abs": "float",
                "drawdown_pct": "float",
            },
            nullability={
                "timestamp": False,
                "run_id": False,
                "cash_balance": False,
                "open_market_value": False,
                "gross_exposure": False,
                "net_exposure": False,
                "unrealised_pnl_abs": False,
                "realised_pnl_abs": False,
                "total_equity": False,
                "open_positions": False,
                "closed_positions": False,
                "peak_equity": False,
                "drawdown_abs": False,
                "drawdown_pct": False,
            },
            canonical_column_order=[
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
            write_mode="append_rows_then_recompute_derived_columns",
            append_only=False,
            mutability_rules={
                "history": "New snapshots append; drawdown-derived fields may be recomputed across the full file.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="performance_summary.csv",
            schema_version="1.0",
            owner_agent="Portfolio Equity Agent",
            required_columns=[
                "latest_timestamp",
                "latest_run_id",
                "current_total_equity",
                "peak_equity",
                "peak_equity_timestamp",
                "current_drawdown_abs",
                "current_drawdown_pct",
                "max_drawdown_abs",
                "max_drawdown_pct",
                "max_drawdown_timestamp",
                "observation_count",
            ],
            expected_types={
                "latest_timestamp": "datetime",
                "latest_run_id": "string",
                "current_total_equity": "float",
                "peak_equity": "float",
                "peak_equity_timestamp": "datetime",
                "current_drawdown_abs": "float",
                "current_drawdown_pct": "float",
                "max_drawdown_abs": "float",
                "max_drawdown_pct": "float",
                "max_drawdown_timestamp": "datetime",
                "observation_count": "float",
            },
            nullability={
                "latest_timestamp": False,
                "latest_run_id": False,
                "current_total_equity": False,
                "peak_equity": False,
                "peak_equity_timestamp": False,
                "current_drawdown_abs": False,
                "current_drawdown_pct": False,
                "max_drawdown_abs": False,
                "max_drawdown_pct": False,
                "max_drawdown_timestamp": False,
                "observation_count": False,
            },
            canonical_column_order=[
                "latest_timestamp",
                "latest_run_id",
                "current_total_equity",
                "peak_equity",
                "peak_equity_timestamp",
                "current_drawdown_abs",
                "current_drawdown_pct",
                "max_drawdown_abs",
                "max_drawdown_pct",
                "max_drawdown_timestamp",
                "observation_count",
            ],
            write_mode="full_rewrite_single_row_summary",
            append_only=False,
            mutability_rules={
                "summary": "Performance summary is regenerated from portfolio_equity_history.csv.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="run_reconciliation_summary.csv",
            schema_version="1.0",
            owner_agent="Pipeline Orchestrator",
            required_columns=[
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
            expected_types={
                "run_id": "string",
                "started_at": "datetime",
                "completed_at": "datetime",
                "status": "string",
                "failed_agent": "string",
                "fills_processed": "float",
                "positions_opened": "float",
                "positions_closed": "float",
                "positions_marked_exit_required": "float",
                "cash_delta": "float",
                "realised_pnl_delta": "float",
                "unrealised_pnl_delta": "float",
                "equity_delta": "float",
                "exposure_delta": "float",
                "validation_warning_count": "float",
                "validation_failure_count": "float",
                "notes": "string",
            },
            nullability={
                "run_id": False,
                "started_at": False,
                "completed_at": True,
                "status": False,
                "failed_agent": True,
                "fills_processed": False,
                "positions_opened": False,
                "positions_closed": False,
                "positions_marked_exit_required": False,
                "cash_delta": False,
                "realised_pnl_delta": False,
                "unrealised_pnl_delta": False,
                "equity_delta": False,
                "exposure_delta": False,
                "validation_warning_count": False,
                "validation_failure_count": False,
                "notes": True,
            },
            canonical_column_order=[
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
            write_mode="append_then_targeted_row_update",
            append_only=False,
            mutability_rules={
                "lifecycle": "One reconciliation row per run_id, updated deterministically from authoritative logs and state.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="run_history.csv",
            schema_version="1.0",
            owner_agent="Pipeline Orchestrator",
            required_columns=[
                "run_id",
                "started_at",
                "completed_at",
                "status",
                "failed_agent",
                "error_message",
                "notes",
            ],
            expected_types={
                "run_id": "string",
                "started_at": "datetime",
                "completed_at": "datetime",
                "status": "string",
                "failed_agent": "string",
                "error_message": "string",
                "notes": "string",
            },
            nullability={
                "run_id": False,
                "started_at": False,
                "completed_at": True,
                "status": False,
                "failed_agent": True,
                "error_message": True,
                "notes": True,
            },
            canonical_column_order=[
                "run_id",
                "started_at",
                "completed_at",
                "status",
                "failed_agent",
                "error_message",
                "notes",
            ],
            write_mode="append_then_targeted_row_update",
            append_only=False,
            mutability_rules={
                "lifecycle": "A run row is appended once, then status/completion fields may be updated for that same run_id.",
            },
        )
    )

    register(
        FileSchemaRegistryEntry(
            file_name="event_log.csv",
            schema_version="1.0",
            owner_agent="Shared Event Log",
            required_columns=[
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
            expected_types={
                "event_id": "string",
                "run_id": "string",
                "event_time": "datetime",
                "agent_name": "string",
                "event_type": "string",
                "entity_type": "string",
                "entity_id": "string",
                "ticker": "string",
                "position_id": "string",
                "order_id": "string",
                "severity": "string",
                "message": "string",
                "before_json": "json",
                "after_json": "json",
                "metadata_json": "json",
            },
            nullability={
                "event_id": False,
                "run_id": False,
                "event_time": False,
                "agent_name": False,
                "event_type": False,
                "entity_type": False,
                "entity_id": False,
                "ticker": True,
                "position_id": True,
                "order_id": True,
                "severity": False,
                "message": True,
                "before_json": True,
                "after_json": True,
                "metadata_json": True,
            },
            canonical_column_order=[
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
            write_mode="append_rows",
            append_only=True,
            mutability_rules={
                "append_only": "Event log rows are immutable after append.",
            },
        )
    )

    return registry


_REGISTRY = _build_registry()


def get_file_schema(file_name: str) -> FileSchemaRegistryEntry:
    try:
        return _REGISTRY[file_name]
    except KeyError as exc:
        raise KeyError(f"No registered schema found for {file_name}") from exc


def list_registered_schemas() -> list[FileSchemaRegistryEntry]:
    return list(_REGISTRY.values())


def registry_contains(file_name: str) -> bool:
    return file_name in _REGISTRY
