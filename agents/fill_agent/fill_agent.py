from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from pandas.errors import EmptyDataError

from agents.shared.event_log import (
    append_cash_adjusted_event,
    append_fill_processed_event,
    append_position_closed_event,
    append_position_opened_event,
    append_position_reduced_event,
    ensure_event_log_exists,
)
from shared.io_utils import write_csv, write_managed_csv_with_schema
from shared.paths import DATA_DIR as RUNTIME_STATE_DIR
from shared.run_context import get_or_create_run_id
from shared.schema_registry import get_file_schema
from shared.schemas import (
    CASH_LEDGER_SCHEMA as CASH_LEDGER_SCHEMA_SPEC,
    CASH_STATE_SCHEMA as CASH_STATE_SCHEMA_SPEC,
    PORTFOLIO_STATE_SCHEMA as PORTFOLIO_STATE_SCHEMA_SPEC,
    PROCESSED_FILLS_SCHEMA as PROCESSED_FILLS_SCHEMA_SPEC,
    TRADE_FILLS_SCHEMA as TRADE_FILLS_SCHEMA_SPEC,
    SchemaSpec,
    validate_cash_ledger,
    validate_cash_state,
    validate_portfolio_state,
    validate_processed_fills,
    validate_trade_fills,
)
import shared.sqlite_sidecar as sqlite_sidecar
from shared.sqlite_sidecar import append_cash_ledger_row as append_cash_ledger_row_sqlite
from shared.sqlite_sidecar import append_processed_fill_row as append_processed_fill_row_sqlite
from shared.sqlite_sidecar import append_trade_fill_row as append_trade_fill_row_sqlite
from shared.sqlite_sidecar import replace_cash_state_rows, replace_portfolio_state_rows


DATA_DIR = str(RUNTIME_STATE_DIR)

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
PROCESSED_FILLS_PATH = os.path.join(DATA_DIR, "processed_fills.csv")
TRADE_FILLS_PATH = os.path.join(DATA_DIR, "trade_fills.csv")
MANUAL_FILLS_PATH = os.path.join(DATA_DIR, "manual_fills.csv")
BROKER_FILLS_PATH = os.path.join(DATA_DIR, "broker_fills.csv")
CASH_STATE_PATH = os.path.join(DATA_DIR, "cash_state.csv")
CASH_LEDGER_PATH = os.path.join(DATA_DIR, "cash_ledger.csv")

DEFAULT_STARTING_CASH = 100000.0
AGENT_NAME = "Fill Agent"
PORTFOLIO_STATE_SCHEMA = get_file_schema("portfolio_state.csv")
CASH_STATE_SCHEMA = get_file_schema("cash_state.csv")
CASH_LEDGER_SCHEMA = get_file_schema("cash_ledger.csv")
PROCESSED_FILLS_SCHEMA = get_file_schema("processed_fills.csv")
TRADE_FILLS_SCHEMA = get_file_schema("trade_fills.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str:
    return get_or_create_run_id()


def safe_read_csv_or_default(
    path: str,
    columns: list[str],
    default_df: Optional[pd.DataFrame] = None,
    producer: str | None = None,
    schema: SchemaSpec | None = None,
) -> pd.DataFrame:
    """
    Read CSV safely.
    If file does not exist, is zero-byte, or is structurally empty, create/reset it.
    """
    if not os.path.exists(path):
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        _write_initialised_csv(df, path, producer=producer, schema=schema)
        return df

    if os.path.getsize(path) == 0:
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        _write_initialised_csv(df, path, producer=producer, schema=schema)
        return df

    try:
        df = pd.read_csv(path)
    except EmptyDataError:
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        _write_initialised_csv(df, path, producer=producer, schema=schema)
        return df

    if df.empty and len(df.columns) == 0:
        if default_df is not None:
            df = default_df.copy()
        else:
            df = pd.DataFrame(columns=columns)
        _write_initialised_csv(df, path, producer=producer, schema=schema)
        return df

    return df


def _write_initialised_csv(
    df: pd.DataFrame,
    path: str,
    *,
    producer: str | None,
    schema: SchemaSpec | None,
) -> None:
    if producer is None and schema is None:
        write_csv(df, path)
        return
    if producer is None or schema is None:
        raise ValueError("Managed initialization requires both producer and schema")
    write_managed_csv_with_schema(
        df,
        path,
        schema=schema,
        producer=producer,
        keep_extra_columns=False,
    )


def ensure_cash_files() -> tuple[pd.DataFrame, pd.DataFrame]:
    cash_state_df = safe_read_csv_or_default(
        CASH_STATE_PATH,
        columns=CASH_STATE_SCHEMA.canonical_column_order,
        default_df=pd.DataFrame(
            [{"as_of": utc_now_iso(), "cash_balance": DEFAULT_STARTING_CASH}]
        ),
        producer=AGENT_NAME,
        schema=CASH_STATE_SCHEMA_SPEC,
    )
    cash_state_df = validate_cash_state(cash_state_df, keep_extra_columns=False)

    cash_ledger_df = safe_read_csv_or_default(
        CASH_LEDGER_PATH,
        columns=CASH_LEDGER_SCHEMA.canonical_column_order,
        producer=AGENT_NAME,
        schema=CASH_LEDGER_SCHEMA_SPEC,
    )
    cash_ledger_df = validate_cash_ledger(cash_ledger_df, keep_extra_columns=False)

    return cash_state_df, cash_ledger_df


def ensure_state_file() -> pd.DataFrame:
    df = safe_read_csv_or_default(
        STATE_PATH,
        columns=PORTFOLIO_STATE_SCHEMA.canonical_column_order,
        producer=AGENT_NAME,
        schema=PORTFOLIO_STATE_SCHEMA_SPEC,
    )
    return validate_portfolio_state(df, keep_extra_columns=False)


def ensure_processed_fills_file() -> pd.DataFrame:
    df = safe_read_csv_or_default(
        PROCESSED_FILLS_PATH,
        columns=PROCESSED_FILLS_SCHEMA.canonical_column_order,
        producer=AGENT_NAME,
        schema=PROCESSED_FILLS_SCHEMA_SPEC,
    )
    return validate_processed_fills(df, keep_extra_columns=False)


def ensure_trade_fills_file() -> pd.DataFrame:
    df = safe_read_csv_or_default(
        TRADE_FILLS_PATH,
        columns=TRADE_FILLS_SCHEMA.canonical_column_order,
        producer=AGENT_NAME,
        schema=TRADE_FILLS_SCHEMA_SPEC,
    )
    return validate_trade_fills(df, keep_extra_columns=False)


def ensure_manual_fills_file() -> pd.DataFrame:
    return safe_read_csv_or_default(
        MANUAL_FILLS_PATH,
        columns=[
            "fill_id",
            "ticker",
            "side",
            "action",
            "quantity",
            "fill_price",
            "fees",
            "fill_timestamp",
        ],
    )


BROKER_IDENTITY_COLUMNS = [
    "broker",
    "environment",
    "broker_execution_id",
    "broker_order_id",
    "broker_position_id",
    "broker_reference_id",
    "broker_instrument_id",
    "broker_rate_id",
    "broker_fee",
    "broker_tax",
    "currency",
]


def ensure_broker_fills_file() -> pd.DataFrame:
    return safe_read_csv_or_default(
        BROKER_FILLS_PATH,
        columns=[
            "fill_id",
            "ticker",
            "side",
            "action",
            "quantity",
            "fill_price",
            "fees",
            "fill_timestamp",
            *BROKER_IDENTITY_COLUMNS,
        ],
    )


def read_fill_input() -> pd.DataFrame:
    manual = ensure_manual_fills_file()
    broker = ensure_broker_fills_file()

    required = [
        "fill_id",
        "ticker",
        "side",
        "action",
        "quantity",
        "fill_price",
        "fees",
        "fill_timestamp",
    ]
    missing = [col for col in required if col not in manual.columns]
    if missing:
        raise ValueError(f"Missing required fill input columns in {MANUAL_FILLS_PATH}: {missing}")
    broker_missing = [col for col in [*required, *BROKER_IDENTITY_COLUMNS] if col not in broker.columns]
    if broker_missing:
        raise ValueError(
            f"Missing broker fill columns in {BROKER_FILLS_PATH}: {broker_missing}"
        )
    for column in BROKER_IDENTITY_COLUMNS:
        if column not in manual.columns:
            manual[column] = ""
    if not broker.empty:
        if not broker["environment"].astype(str).str.lower().eq("demo").all():
            raise ValueError("Only Demo broker fills may enter economic state")
        if broker["broker_execution_id"].astype(str).str.strip().eq("").any():
            raise ValueError("Broker fills require broker_execution_id")
    return pd.concat(
        [
            manual[[*required, *BROKER_IDENTITY_COLUMNS]],
            broker[[*required, *BROKER_IDENTITY_COLUMNS]],
        ],
        ignore_index=True,
    )


def validate_fill_row(row: pd.Series) -> None:
    if pd.isna(row["fill_id"]) or str(row["fill_id"]).strip() == "":
        raise ValueError("fill_id is required")

    if pd.isna(row["ticker"]) or str(row["ticker"]).strip() == "":
        raise ValueError("ticker is required")

    if str(row["side"]).strip().lower() not in {"long", "short"}:
        raise ValueError(f"Invalid side: {row['side']}")

    if str(row["action"]).strip().lower() not in {"buy", "sell"}:
        raise ValueError(f"Invalid action: {row['action']}")

    quantity = pd.to_numeric(row["quantity"], errors="coerce")
    fill_price = pd.to_numeric(row["fill_price"], errors="coerce")
    fees = pd.to_numeric(row["fees"], errors="coerce")

    if pd.isna(quantity) or quantity <= 0:
        raise ValueError("quantity must be positive")

    if pd.isna(fill_price) or fill_price <= 0:
        raise ValueError("fill_price must be positive")

    if pd.isna(fees) or fees < 0:
        raise ValueError("fees must be zero or positive")

    if pd.isna(row["fill_timestamp"]) or str(row["fill_timestamp"]).strip() == "":
        raise ValueError("fill_timestamp is required")


def validate_fill_batch(fills_df: pd.DataFrame) -> None:
    """Validate all candidate fills before any state/cash mutation is attempted."""
    for _, row in fills_df.iterrows():
        validate_fill_row(row)

    if fills_df.empty:
        return

    fill_ids = fills_df["fill_id"].astype(str).str.strip()
    duplicate_ids = sorted(
        fill_id for fill_id in fill_ids[fill_ids.duplicated()].unique() if fill_id
    )
    if duplicate_ids:
        raise ValueError(
            "Duplicate fill_id values found in manual fills before processing: "
            f"{duplicate_ids}"
        )



def _read_existing_csv(path: str, columns: list[str]) -> pd.DataFrame:
    """Read existing CSV evidence without creating or rewriting files."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame(columns=columns)
    try:
        return pd.read_csv(path)
    except EmptyDataError:
        return pd.DataFrame(columns=columns)


def _read_sqlite_table_if_available(table_name: str) -> pd.DataFrame:
    """Read SQLite sidecar evidence only when the sidecar already exists."""
    if not os.path.exists(sqlite_sidecar.SQLITE_DB_PATH):
        return pd.DataFrame()
    try:
        return sqlite_sidecar.fetch_table_df(table_name)
    except Exception:
        return pd.DataFrame()


def _event_row_mentions_fill(row: pd.Series, fill_id: str) -> bool:
    if str(row.get("entity_id", "")).strip() == fill_id:
        return True

    metadata_raw = row.get("metadata_json", "")
    if pd.isna(metadata_raw) or str(metadata_raw).strip() == "":
        return False

    try:
        metadata = json.loads(str(metadata_raw))
    except json.JSONDecodeError:
        return fill_id in str(metadata_raw)

    def _contains(value: object) -> bool:
        if isinstance(value, dict):
            return any(_contains(v) for v in value.values())
        if isinstance(value, list):
            return any(_contains(v) for v in value)
        return str(value) == fill_id

    return _contains(metadata)


def _money_close(left: object, right: float) -> bool:
    value = pd.to_numeric(left, errors="coerce")
    return not pd.isna(value) and abs(float(value) - right) < 0.005


def _expected_cash_ledger_values(row: pd.Series) -> tuple[str, float]:
    action = str(row["action"]).strip().lower()
    quantity = float(row["quantity"])
    fill_price = float(row["fill_price"])
    fees = float(row["fees"])
    if action == "buy":
        return "position_open", -(quantity * fill_price)
    if action == "sell":
        return "position_close", (quantity * fill_price) - fees
    return "", 0.0


def _cash_ledger_row_matches_fill(row: pd.Series, fill: pd.Series, fill_id: str) -> bool:
    notes = row.get("notes", "")
    if not pd.isna(notes) and fill_id in str(notes):
        return True

    expected_event_type, expected_amount = _expected_cash_ledger_values(fill)
    if not expected_event_type:
        return False

    return (
        str(row.get("ticker", "")).strip().upper() == str(fill["ticker"]).strip().upper()
        and str(row.get("side", "")).strip().lower() == str(fill["side"]).strip().lower()
        and str(row.get("action", "")).strip().lower() == str(fill["action"]).strip().lower()
        and str(row.get("event_type", "")).strip().lower() == expected_event_type
        and _money_close(row.get("amount", None), expected_amount)
        and _money_close(row.get("fees", None), float(fill["fees"]))
    )


def _portfolio_row_matches_fill(row: pd.Series, fill: pd.Series) -> bool:
    action = str(fill["action"]).strip().lower()
    ticker_matches = str(row.get("ticker", "")).strip().upper() == str(fill["ticker"]).strip().upper()
    side_matches = str(row.get("side", "")).strip().lower() == str(fill["side"]).strip().lower()
    quantity_matches = _money_close(row.get("quantity", None), float(fill["quantity"]))
    if not (ticker_matches and side_matches and quantity_matches):
        return False

    if action == "buy":
        return (
            _money_close(row.get("entry_price", None), float(fill["fill_price"]))
            and str(row.get("entry_date", "")).strip() == str(fill["fill_timestamp"]).strip()
        )
    if action == "sell":
        return (
            str(row.get("status", "")).strip().lower() == "closed"
            and _money_close(row.get("exit_price", None), float(fill["fill_price"]))
            and str(row.get("closed_at", "")).strip() == str(fill["fill_timestamp"]).strip()
        )
    return False


def _find_interrupted_fill_evidence(row: pd.Series) -> list[str]:
    fill_id = str(row["fill_id"]).strip()
    evidence: list[str] = []

    event_log_df = _read_existing_csv(os.path.join(DATA_DIR, "event_log.csv"), get_file_schema("event_log.csv").canonical_column_order)
    if not event_log_df.empty:
        matches = event_log_df[event_log_df.apply(lambda event: _event_row_mentions_fill(event, fill_id), axis=1)]
        for event_type in sorted(matches.get("event_type", pd.Series(dtype=str)).astype(str).unique()):
            evidence.append(f"event_log.csv event_type={event_type}")

    cash_ledger_df = _read_existing_csv(CASH_LEDGER_PATH, CASH_LEDGER_SCHEMA.canonical_column_order)
    if not cash_ledger_df.empty and cash_ledger_df.apply(lambda ledger_row: _cash_ledger_row_matches_fill(ledger_row, row, fill_id), axis=1).any():
        evidence.append("cash_ledger.csv matching cash movement")

    portfolio_df = _read_existing_csv(STATE_PATH, PORTFOLIO_STATE_SCHEMA.canonical_column_order)
    if not portfolio_df.empty and portfolio_df.apply(lambda state_row: _portfolio_row_matches_fill(state_row, row), axis=1).any():
        evidence.append("portfolio_state.csv matching position mutation")

    trade_fills_df = _read_existing_csv(TRADE_FILLS_PATH, TRADE_FILLS_SCHEMA.canonical_column_order)
    if not trade_fills_df.empty and fill_id in set(
        trade_fills_df.get("fill_id", pd.Series(dtype=str)).astype(str)
    ):
        evidence.append("trade_fills.csv accepted fill identity")

    sqlite_processed_df = _read_sqlite_table_if_available("processed_fills")
    if not sqlite_processed_df.empty and fill_id in set(sqlite_processed_df.get("fill_id", pd.Series(dtype=str)).astype(str)):
        evidence.append("SQLite processed_fills row")

    sqlite_event_log_df = _read_sqlite_table_if_available("event_log")
    if not sqlite_event_log_df.empty:
        sqlite_matches = sqlite_event_log_df[sqlite_event_log_df.apply(lambda event: _event_row_mentions_fill(event, fill_id), axis=1)]
        for event_type in sorted(sqlite_matches.get("event_type", pd.Series(dtype=str)).astype(str).unique()):
            evidence.append(f"SQLite event_log event_type={event_type}")

    sqlite_cash_ledger_df = _read_sqlite_table_if_available("cash_ledger")
    if not sqlite_cash_ledger_df.empty and sqlite_cash_ledger_df.apply(lambda ledger_row: _cash_ledger_row_matches_fill(ledger_row, row, fill_id), axis=1).any():
        evidence.append("SQLite cash_ledger matching cash movement")

    sqlite_trade_fills_df = _read_sqlite_table_if_available("trade_fills")
    if not sqlite_trade_fills_df.empty and fill_id in set(
        sqlite_trade_fills_df.get("fill_id", pd.Series(dtype=str)).astype(str)
    ):
        evidence.append("SQLite trade_fills row")

    return sorted(set(evidence))


def fail_if_unmarked_fill_has_existing_mutation(
    fills_df: pd.DataFrame,
    processed_ids: set[str],
) -> None:
    """Fail closed when replay input has economic evidence but lacks canonical marker."""
    for _, row in fills_df.iterrows():
        fill_id = str(row["fill_id"]).strip()
        if fill_id in processed_ids:
            continue
        evidence = _find_interrupted_fill_evidence(row)
        if evidence:
            raise RuntimeError(
                "Interrupted Fill Agent recovery guard: fill_id "
                f"{fill_id} is absent from processed_fills.csv but existing mutation "
                f"evidence was found ({'; '.join(evidence)}). "
                "Manual recovery required: reconcile portfolio_state.csv, cash_ledger.csv, "
                "event_log.csv, processed_fills.csv, and SQLite parity; then either restore/repair "
                "the missing processed-fill marker under documented control or restore from backup. "
                "Do not auto-replay this fill."
            )


def get_cash_balance(cash_state_df: pd.DataFrame) -> float:
    if cash_state_df.empty:
        return DEFAULT_STARTING_CASH
    return float(cash_state_df.iloc[-1]["cash_balance"])


def serialise_row_state(row: pd.Series | dict) -> dict[str, object]:
    if isinstance(row, pd.Series):
        payload = row.to_dict()
    else:
        payload = dict(row)

    clean_payload: dict[str, object] = {}
    for key, value in payload.items():
        clean_payload[str(key)] = None if pd.isna(value) else value
    return clean_payload


def write_cash_balance(balance: float) -> None:
    cash_state_df = pd.DataFrame(
        [{"as_of": utc_now_iso(), "cash_balance": balance}]
    )
    cash_state_df = validate_cash_state(cash_state_df, keep_extra_columns=False)
    write_managed_csv_with_schema(
        cash_state_df,
        CASH_STATE_PATH,
        schema=CASH_STATE_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    replace_cash_state_rows(cash_state_df.to_dict(orient="records"))


def append_cash_ledger_row(
    ledger_df: pd.DataFrame,
    run_id: str,
    event_type: str,
    position_id: str,
    ticker: str,
    side: str,
    action: str,
    amount: float,
    fees: float,
    cash_balance_after: float,
    notes: str,
) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [
            {
                "ledger_id": str(uuid.uuid4()),
                "run_id": run_id,
                "timestamp": utc_now_iso(),
                "event_type": event_type,
                "position_id": position_id,
                "ticker": ticker,
                "side": side,
                "action": action,
                "amount": amount,
                "fees": fees,
                "cash_balance_after": cash_balance_after,
                "notes": notes,
            }
        ],
        columns=CASH_LEDGER_SCHEMA.canonical_column_order,
    )
    out = pd.concat([ledger_df, new_row], ignore_index=True)
    out = validate_cash_ledger(out, keep_extra_columns=False)
    write_managed_csv_with_schema(
        out,
        CASH_LEDGER_PATH,
        schema=CASH_LEDGER_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    append_cash_ledger_row_sqlite(new_row.iloc[0].to_dict())
    return out


def find_open_position(state_df: pd.DataFrame, ticker: str, side: str) -> Optional[int]:
    matches = state_df[
        (state_df["ticker"].astype(str).str.upper() == str(ticker).upper()) &
        (state_df["side"].astype(str).str.lower() == str(side).lower()) &
        (state_df["status"].astype(str).isin(["open", "exit_required"]))
    ]
    if matches.empty:
        return None
    if len(matches) > 1:
        raise RuntimeError(f"More than one active position found for ticker={ticker}, side={side}")
    return matches.index[0]


def open_long_position(
    state_df: pd.DataFrame,
    cash_balance: float,
    ledger_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> tuple[pd.DataFrame, float, pd.DataFrame]:
    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).lower()
    quantity = float(row["quantity"])
    fill_price = float(row["fill_price"])
    fees = float(row["fees"])
    gross_cost = quantity * fill_price
    total_cash_out = gross_cost + fees

    if cash_balance < total_cash_out:
        raise RuntimeError(
            f"Insufficient cash to open position in {ticker}. "
            f"Required={total_cash_out:.2f}, Available={cash_balance:.2f}"
        )

    existing_idx = find_open_position(state_df, ticker, side)
    if existing_idx is not None:
        raise RuntimeError(f"Active position already exists for {ticker} {side}")

    position_id = "POS_" + uuid.uuid4().hex[:10].upper()

    new_row = pd.DataFrame(
        [
            {
                "position_id": position_id,
                "ticker": ticker,
                "side": side,
                "status": "open",
                "quantity": quantity,
                "entry_price": fill_price,
                "entry_date": row["fill_timestamp"],
                "current_price": fill_price,
                "market_value": quantity * fill_price,
                "pnl_abs": -fees,
                "pnl_pct": (-fees / gross_cost * 100) if gross_cost > 0 else 0.0,
                "realised_pnl_abs": 0.0,
                "fees_total": fees,
                "entry_fees_remaining": fees,
                "exit_flag": False,
                "exit_reason": "",
                "last_updated": utc_now_iso(),
                "run_id": run_id,
                "closed_at": pd.NA,
                "exit_price": pd.NA,
            }
        ]
    )

    state_df = pd.concat([state_df, new_row], ignore_index=True)

    position_after = serialise_row_state(new_row.iloc[0])
    cash_balance_before = cash_balance
    cash_balance -= total_cash_out
    write_cash_balance(cash_balance)

    ledger_df = append_cash_ledger_row(
        ledger_df=ledger_df,
        run_id=run_id,
        event_type="position_open",
        position_id=position_id,
        ticker=ticker,
        side=side,
        action="buy",
        amount=-gross_cost,
        fees=fees,
        cash_balance_after=cash_balance,
        notes=f"fill_id={row['fill_id']}; Opened {quantity} shares at {fill_price}",
    )

    append_position_opened_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        position_id=position_id,
        ticker=ticker,
        message=f"Opened long position for {ticker}",
        before_state={},
        after_state=position_after,
        details={
            "fill_id": str(row["fill_id"]),
            "action": "buy",
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "fees": fees,
        },
    )
    append_cash_adjusted_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        ticker=ticker,
        position_id=position_id,
        message=f"Cash debited for opening {ticker}",
        before_state={"cash_balance": round(cash_balance_before, 2)},
        after_state={"cash_balance": round(cash_balance, 2)},
        details={
            "fill_id": str(row["fill_id"]),
            "action": "buy",
            "gross_cost": round(gross_cost, 2),
            "fees": fees,
            "net_cash_change": round(-total_cash_out, 2),
        },
    )

    return state_df, cash_balance, ledger_df


def close_long_position(
    state_df: pd.DataFrame,
    cash_balance: float,
    ledger_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> tuple[pd.DataFrame, float, pd.DataFrame]:
    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).lower()
    quantity = float(row["quantity"])
    fill_price = float(row["fill_price"])
    fees = float(row["fees"])

    idx = find_open_position(state_df, ticker, side)
    if idx is None:
        raise RuntimeError(f"No active position found to close for {ticker} {side}")

    position = state_df.loc[idx].copy()
    position_before = serialise_row_state(position)

    original_quantity = float(position["quantity"])
    if quantity > original_quantity + 1e-9:
        raise RuntimeError(
            f"Close quantity exceeds the active position for {ticker}. "
            f"Available={position['quantity']}, received={quantity}"
        )

    entry_price = float(position["entry_price"])
    total_fees_before = float(position.get("fees_total", 0.0) or 0.0)
    entry_fees_remaining_raw = pd.to_numeric(
        position.get("entry_fees_remaining", total_fees_before), errors="coerce"
    )
    entry_fees_remaining = (
        total_fees_before
        if pd.isna(entry_fees_remaining_raw)
        else float(entry_fees_remaining_raw)
    )
    realised_before_raw = pd.to_numeric(
        position.get("realised_pnl_abs", 0.0), errors="coerce"
    )
    realised_before = 0.0 if pd.isna(realised_before_raw) else float(realised_before_raw)

    gross_proceeds = quantity * fill_price
    net_proceeds = gross_proceeds - fees
    allocated_entry_fees = entry_fees_remaining * (quantity / original_quantity)
    realised_increment = net_proceeds - (quantity * entry_price) - allocated_entry_fees
    realised_pnl = realised_before + realised_increment

    if quantity < original_quantity - 1e-9:
        remaining_quantity = original_quantity - quantity
        remaining_entry_fees = entry_fees_remaining - allocated_entry_fees
        state_df.at[idx, "quantity"] = remaining_quantity
        if "capital_allocated" in state_df.columns:
            state_df.at[idx, "capital_allocated"] = remaining_quantity * entry_price
        state_df.at[idx, "current_price"] = fill_price
        state_df.at[idx, "market_value"] = remaining_quantity * fill_price
        unrealised = (
            (fill_price - entry_price) * remaining_quantity - remaining_entry_fees
        )
        state_df.at[idx, "pnl_abs"] = unrealised
        state_df.at[idx, "pnl_pct"] = (
            unrealised / (remaining_quantity * entry_price) * 100
            if remaining_quantity * entry_price > 0
            else 0.0
        )
        state_df.at[idx, "realised_pnl_abs"] = realised_pnl
        state_df.at[idx, "fees_total"] = total_fees_before + fees
        state_df.at[idx, "entry_fees_remaining"] = remaining_entry_fees
        state_df.at[idx, "exit_flag"] = "False"
        state_df.at[idx, "exit_reason"] = "partial_close"
        state_df.at[idx, "last_updated"] = utc_now_iso()
        state_df.at[idx, "run_id"] = run_id

        position_after = serialise_row_state(state_df.loc[idx])
        cash_balance_before = cash_balance
        cash_balance += net_proceeds
        write_cash_balance(cash_balance)
        ledger_df = append_cash_ledger_row(
            ledger_df=ledger_df,
            run_id=run_id,
            event_type="position_partial_close",
            position_id=str(position["position_id"]),
            ticker=ticker,
            side=side,
            action="sell",
            amount=net_proceeds,
            fees=fees,
            cash_balance_after=cash_balance,
            notes=(
                f"fill_id={row['fill_id']}; Partially closed {quantity} shares "
                f"at {fill_price}; realised_pnl_increment={realised_increment:.2f}"
            ),
        )
        append_position_reduced_event(
            run_id=run_id,
            agent_name=AGENT_NAME,
            position_id=str(position["position_id"]),
            ticker=ticker,
            message=f"Reduced long position for {ticker}",
            before_state=position_before,
            after_state=position_after,
            details={
                "fill_id": str(row["fill_id"]),
                "quantity_closed": quantity,
                "quantity_remaining": remaining_quantity,
                "fill_price": fill_price,
                "fees": fees,
                "realised_pnl_increment": round(realised_increment, 2),
            },
        )
        append_cash_adjusted_event(
            run_id=run_id,
            agent_name=AGENT_NAME,
            ticker=ticker,
            position_id=str(position["position_id"]),
            message=f"Cash credited for partial close of {ticker}",
            before_state={"cash_balance": round(cash_balance_before, 2)},
            after_state={"cash_balance": round(cash_balance, 2)},
            details={
                "fill_id": str(row["fill_id"]),
                "gross_proceeds": round(gross_proceeds, 2),
                "fees": fees,
                "net_cash_change": round(net_proceeds, 2),
            },
        )
        return state_df, cash_balance, ledger_df

    state_df.at[idx, "status"] = "closed"
    state_df.at[idx, "current_price"] = fill_price
    state_df.at[idx, "market_value"] = 0.0
    state_df.at[idx, "pnl_abs"] = 0.0
    state_df.at[idx, "pnl_pct"] = 0.0
    state_df.at[idx, "realised_pnl_abs"] = realised_pnl
    state_df.at[idx, "fees_total"] = total_fees_before + fees
    state_df.at[idx, "entry_fees_remaining"] = 0.0
    state_df.at[idx, "exit_flag"] = "False"
    state_df.at[idx, "exit_reason"] = "position_closed"
    state_df.at[idx, "last_updated"] = utc_now_iso()
    state_df.at[idx, "run_id"] = run_id
    # A persisted all-null datetime column is inferred as float by pandas on reload.
    # Restore an object-capable dtype before recording the first close timestamp.
    state_df["closed_at"] = state_df["closed_at"].astype("object")
    state_df.at[idx, "closed_at"] = row["fill_timestamp"]
    state_df.at[idx, "exit_price"] = fill_price

    position_after = serialise_row_state(state_df.loc[idx])
    cash_balance_before = cash_balance
    cash_balance += net_proceeds
    write_cash_balance(cash_balance)

    ledger_df = append_cash_ledger_row(
        ledger_df=ledger_df,
        run_id=run_id,
        event_type="position_close",
        position_id=str(position["position_id"]),
        ticker=ticker,
        side=side,
        action="sell",
        amount=net_proceeds,
        fees=fees,
        cash_balance_after=cash_balance,
        notes=f"fill_id={row['fill_id']}; Closed {quantity} shares at {fill_price}; realised_pnl={realised_pnl:.2f}",
    )

    append_position_closed_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        position_id=str(position["position_id"]),
        ticker=ticker,
        message=f"Closed long position for {ticker}",
        before_state=position_before,
        after_state=position_after,
        details={
            "fill_id": str(row["fill_id"]),
            "action": "sell",
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "fees": fees,
            "realised_pnl_abs": round(realised_pnl, 2),
        },
    )
    append_cash_adjusted_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        ticker=ticker,
        position_id=str(position["position_id"]),
        message=f"Cash credited for closing {ticker}",
        before_state={"cash_balance": round(cash_balance_before, 2)},
        after_state={"cash_balance": round(cash_balance, 2)},
        details={
            "fill_id": str(row["fill_id"]),
            "action": "sell",
            "gross_proceeds": round(gross_proceeds, 2),
            "fees": fees,
            "net_cash_change": round(net_proceeds, 2),
        },
    )

    return state_df, cash_balance, ledger_df


def append_processed_fill(processed_df: pd.DataFrame, fill_id: str, run_id: str) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [{"fill_id": fill_id, "processed_at": utc_now_iso(), "run_id": run_id}],
        columns=PROCESSED_FILLS_SCHEMA.canonical_column_order,
    )
    out = pd.concat([processed_df, new_row], ignore_index=True)
    out = validate_processed_fills(out, keep_extra_columns=False)
    write_managed_csv_with_schema(
        out,
        PROCESSED_FILLS_PATH,
        schema=PROCESSED_FILLS_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    append_processed_fill_row_sqlite(new_row.iloc[0].to_dict())
    return out


def append_trade_fill(
    trade_fills_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> pd.DataFrame:
    fill_id = str(row["fill_id"]).strip()
    if fill_id in set(trade_fills_df.get("fill_id", pd.Series(dtype=str)).astype(str)):
        raise RuntimeError(f"trade_fills.csv already contains fill_id={fill_id}")

    new_row = pd.DataFrame(
        [
            {
                "fill_id": fill_id,
                "ticker": str(row["ticker"]).strip().upper(),
                "side": str(row["side"]).strip().lower(),
                "action": str(row["action"]).strip().lower(),
                "quantity": float(row["quantity"]),
                "fill_price": float(row["fill_price"]),
                "fees": float(row["fees"]),
                "fill_timestamp": str(row["fill_timestamp"]).strip(),
                "run_id": run_id,
                **{
                    column: str(row.get(column, "")).strip()
                    for column in BROKER_IDENTITY_COLUMNS
                },
            }
        ]
    )
    output_df = validate_trade_fills(
        pd.concat([trade_fills_df, new_row], ignore_index=True),
        keep_extra_columns=False,
    )
    write_managed_csv_with_schema(
        output_df,
        TRADE_FILLS_PATH,
        schema=TRADE_FILLS_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    append_trade_fill_row_sqlite(new_row.iloc[0].to_dict())
    return output_df


def run_fill_agent() -> None:
    run_id = current_run_id()
    ensure_event_log_exists()

    state_df = ensure_state_file()
    processed_df = ensure_processed_fills_file()
    trade_fills_df = ensure_trade_fills_file()
    fills_df = read_fill_input()
    cash_state_df, ledger_df = ensure_cash_files()

    cash_balance = get_cash_balance(cash_state_df)
    processed_ids = set(processed_df["fill_id"].astype(str).tolist()) if not processed_df.empty else set()

    if fills_df.empty:
        print("Fill Agent finished.")
        print(f"No fills found in {MANUAL_FILLS_PATH}")
        print(f"Ending cash balance: {cash_balance:.2f}")
        return

    validate_fill_batch(fills_df)
    fail_if_unmarked_fill_has_existing_mutation(fills_df, processed_ids)

    for _, row in fills_df.iterrows():
        fill_id = str(row["fill_id"])
        if fill_id in processed_ids:
            continue

        action = str(row["action"]).strip().lower()
        side = str(row["side"]).strip().lower()

        print(f"Processing fill {fill_id} for {row['ticker']}")

        if side != "long":
            raise NotImplementedError("This version currently supports long positions only")

        trade_fills_df = append_trade_fill(trade_fills_df, row, run_id)

        if action == "buy":
            state_df, cash_balance, ledger_df = open_long_position(
                state_df=state_df,
                cash_balance=cash_balance,
                ledger_df=ledger_df,
                row=row,
                run_id=run_id,
            )
        elif action == "sell":
            state_df, cash_balance, ledger_df = close_long_position(
                state_df=state_df,
                cash_balance=cash_balance,
                ledger_df=ledger_df,
                row=row,
                run_id=run_id,
            )
        else:
            raise ValueError(f"Unsupported action: {action}")

        processed_df = append_processed_fill(processed_df, fill_id, run_id)
        processed_ids.add(fill_id)
        append_fill_processed_event(
            run_id=run_id,
            agent_name=AGENT_NAME,
            fill_id=fill_id,
            ticker=str(row["ticker"]).upper(),
            message=f"Processed fill {fill_id}",
            details={
                "side": side,
                "action": action,
                "quantity": float(row["quantity"]),
                "fill_price": float(row["fill_price"]),
                "fees": float(row["fees"]),
                "fill_timestamp": str(row["fill_timestamp"]),
            },
        )

    state_df = validate_portfolio_state(state_df, keep_extra_columns=False)
    write_managed_csv_with_schema(
        state_df,
        STATE_PATH,
        schema=PORTFOLIO_STATE_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    replace_portfolio_state_rows(state_df.to_dict(orient="records"))

    print("Fill Agent finished.")
    print(f"Saved state to: {STATE_PATH}")
    print(f"Saved processed fills to: {PROCESSED_FILLS_PATH}")
    print(f"Saved trade fills to: {TRADE_FILLS_PATH}")
    print(f"Saved cash state to: {CASH_STATE_PATH}")
    print(f"Saved cash ledger to: {CASH_LEDGER_PATH}")
    print(f"Ending cash balance: {cash_balance:.2f}")


if __name__ == "__main__":
    run_fill_agent()
