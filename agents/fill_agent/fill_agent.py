from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from typing import Optional

import pandas as pd
from pandas.errors import EmptyDataError

from agents.fill_agent.live_broker_admission import (
    assert_legacy_fill_inputs_admissible,
    assert_legacy_fill_row_admissible,
)
from agents.shared.event_log import (
    append_cash_adjusted_event,
    append_fill_processed_event,
    append_position_closed_event,
    append_position_opened_event,
    append_position_reduced_event,
    ensure_event_log_exists,
)
from execution.accounting import _sum as exact_sum
from execution.domain import DomainValidationError, canonical_decimal, decimal_text, decimal_value
from execution.pilot_policy import multiply
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

DEFAULT_STARTING_CASH = Decimal("100000")
AGENT_NAME = "Fill Agent"
# Cash, quantity, price, and fee columns stay canonical decimal text. The
# legacy CSV schema and SQLite REAL mirror are unchanged; REAL remains a
# compatibility copy and is not the exact record.
EXACT_CASH_COLUMNS = frozenset({"cash_balance"})
EXACT_LEDGER_COLUMNS = frozenset({"amount", "fees", "cash_balance_after"})
EXACT_TRADE_FILL_COLUMNS = frozenset({"quantity", "fill_price", "fees"})
EXACT_PORTFOLIO_COLUMNS = frozenset({
    "quantity",
    "entry_price",
    "capital_allocated",
    "current_price",
    "market_value",
    "pnl_abs",
    "pnl_pct",
    "realised_pnl_abs",
    "fees_total",
    "entry_fees_remaining",
    "exit_price",
})
# Optional on legacy rows. open_long_position always writes all three.
CLOSE_ECONOMIC_FIELDS = ("fees_total", "entry_fees_remaining", "realised_pnl_abs")
PERCENT_INCREMENT = Decimal("0.0000000001")
PORTFOLIO_STATE_SCHEMA = get_file_schema("portfolio_state.csv")
CASH_STATE_SCHEMA = get_file_schema("cash_state.csv")
CASH_LEDGER_SCHEMA = get_file_schema("cash_ledger.csv")
PROCESSED_FILLS_SCHEMA = get_file_schema("processed_fills.csv")
TRADE_FILLS_SCHEMA = get_file_schema("trade_fills.csv")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_increment(value: Decimal) -> Decimal:
    """Increment implied by a canonical demo decimal's own exponent.

    Demo execution values do not carry a separate venue tick. ``canonical_decimal``
    is their spelling, and the exponent that spelling retains is the increment
    quantity, price, and fees are already quantized to.
    """
    exponent = value.as_tuple().exponent
    if exponent >= 0:
        return Decimal(1)
    return Decimal((0, (1,), exponent))


def _quantize(value: Decimal, increment: Decimal, rounding: str) -> Decimal:
    if increment <= 0:
        raise ValueError("price/quantity increment must be positive")
    units = Fraction(canonical_decimal(value)) / Fraction(increment)
    with localcontext() as ctx:
        ctx.prec = max(80, len(str(units.numerator)) + len(str(units.denominator)) + 5)
        ctx.rounding = rounding
        count = (Decimal(units.numerator) / Decimal(units.denominator)).to_integral_value(rounding=rounding)
    return canonical_decimal(count * increment)


def _money(value: object, field: str = "amount") -> Decimal:
    try:
        return canonical_decimal(decimal_value(value, field))
    except DomainValidationError as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc


def _on_own_increment(value: object, field: str) -> Decimal:
    parsed = _money(value, field)
    return _quantize(parsed, _decimal_increment(parsed), ROUND_HALF_EVEN)


def _text(value: Decimal) -> str:
    return decimal_text(value)


def _percent(part: Decimal, whole: Decimal) -> Decimal:
    """Display ratio only. Cash and quantity are not passed through this scale."""
    if whole == 0:
        return Decimal(0)
    ratio = (Fraction(part) / Fraction(whole)) * 100
    with localcontext() as ctx:
        ctx.prec = 80
        ctx.rounding = ROUND_HALF_EVEN
        value = Decimal(ratio.numerator) / Decimal(ratio.denominator)
    return _quantize(value, PERCENT_INCREMENT, ROUND_HALF_EVEN)


def _allocate_entry_fee(remaining: Decimal, quantity: Decimal, original: Decimal) -> Decimal:
    """Floor a pro-rata fee to the fee increment. The closing slice takes the rest.

    Flooring keeps every intermediate allocation on the fee increment. The
    slice that takes the final quantity receives the unallocated remainder, so
    repeated partial closes sum back to the original fee exactly.
    """
    if quantity == original:
        return remaining
    if remaining == 0 or quantity == 0:
        return Decimal(0)
    increment = _decimal_increment(remaining)
    share = Fraction(remaining) * Fraction(quantity) / Fraction(original)
    units = share / Fraction(increment)
    count = units.numerator // units.denominator
    allocated = canonical_decimal(Decimal(count) * increment)
    if allocated > remaining:
        return remaining
    return allocated


def _same_decimal(left: object, right: object) -> bool:
    try:
        return _money(left) == _money(right)
    except ValueError:
        return False


def current_run_id() -> str:
    return get_or_create_run_id()


def safe_read_csv_or_default(
    path: str,
    columns: list[str],
    default_df: Optional[pd.DataFrame] = None,
    producer: str | None = None,
    schema: SchemaSpec | None = None,
    preserve_decimal_text: bool = False,
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
        if preserve_decimal_text:
            df = pd.read_csv(path, dtype=str, keep_default_na=False)
        else:
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
            [{"as_of": utc_now_iso(), "cash_balance": decimal_text(DEFAULT_STARTING_CASH)}]
        ),
        producer=AGENT_NAME,
        schema=CASH_STATE_SCHEMA_SPEC,
        preserve_decimal_text=True,
    )
    cash_state_df = validate_cash_state(
        cash_state_df, keep_extra_columns=False, exact_decimal_columns=EXACT_CASH_COLUMNS
    )

    cash_ledger_df = safe_read_csv_or_default(
        CASH_LEDGER_PATH,
        columns=CASH_LEDGER_SCHEMA.canonical_column_order,
        producer=AGENT_NAME,
        schema=CASH_LEDGER_SCHEMA_SPEC,
        preserve_decimal_text=True,
    )
    cash_ledger_df = validate_cash_ledger(
        cash_ledger_df, keep_extra_columns=False, exact_decimal_columns=EXACT_LEDGER_COLUMNS
    )

    return cash_state_df, cash_ledger_df


def ensure_state_file() -> pd.DataFrame:
    df = safe_read_csv_or_default(
        STATE_PATH,
        columns=PORTFOLIO_STATE_SCHEMA.canonical_column_order,
        producer=AGENT_NAME,
        schema=PORTFOLIO_STATE_SCHEMA_SPEC,
        preserve_decimal_text=True,
    )
    # Schema coercion maps '', 'n/a', and other non-decimals to NA. Close
    # must still see those tokens; NA is only for a column that was absent.
    raw_close_money = {
        column: df[column].copy()
        for column in CLOSE_ECONOMIC_FIELDS
        if column in df.columns
    }
    validated = validate_portfolio_state(
        df, keep_extra_columns=False, exact_decimal_columns=EXACT_PORTFOLIO_COLUMNS
    )
    for column, raw in raw_close_money.items():
        validated[column] = raw.reindex(validated.index)
    return validated


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
        preserve_decimal_text=True,
    )
    return validate_trade_fills(
        df, keep_extra_columns=False, exact_decimal_columns=EXACT_TRADE_FILL_COLUMNS
    )


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
        preserve_decimal_text=True,
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
        preserve_decimal_text=True,
    )


def read_fill_input() -> pd.DataFrame:
    assert_legacy_fill_inputs_admissible(
        manual_fills_path=MANUAL_FILLS_PATH, broker_fills_path=BROKER_FILLS_PATH)
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

    try:
        quantity = _on_own_increment(row["quantity"], "quantity")
    except ValueError:
        quantity = Decimal(0)
    try:
        fill_price = _on_own_increment(row["fill_price"], "fill_price")
    except ValueError:
        fill_price = Decimal(0)
    try:
        fees = _on_own_increment(row["fees"], "fees")
    except ValueError:
        fees = Decimal(-1)

    if quantity <= 0:
        raise ValueError("quantity must be positive")

    if fill_price <= 0:
        raise ValueError("fill_price must be positive")

    if fees < 0:
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
        return pd.read_csv(path, dtype=str, keep_default_na=False)
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


def _money_close(left: object, right: Decimal) -> bool:
    return _same_decimal(left, right)


def _expected_cash_ledger_values(row: pd.Series) -> tuple[str, Decimal]:
    action = str(row["action"]).strip().lower()
    try:
        quantity = _on_own_increment(row["quantity"], "quantity")
        fill_price = _on_own_increment(row["fill_price"], "fill_price")
        fees = _on_own_increment(row["fees"], "fees")
    except ValueError:
        return "", Decimal(0)
    gross = canonical_decimal(multiply(quantity, fill_price))
    if action == "buy":
        return "position_open", canonical_decimal(-gross)
    if action == "sell":
        return "position_close", exact_sum((gross, fees.copy_negate()))
    return "", Decimal(0)


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
        and _money_close(row.get("fees", None), _on_own_increment(fill["fees"], "fees"))
    )


def _portfolio_row_matches_fill(row: pd.Series, fill: pd.Series) -> bool:
    action = str(fill["action"]).strip().lower()
    ticker_matches = str(row.get("ticker", "")).strip().upper() == str(fill["ticker"]).strip().upper()
    side_matches = str(row.get("side", "")).strip().lower() == str(fill["side"]).strip().lower()
    quantity_matches = _money_close(row.get("quantity", None), _on_own_increment(fill["quantity"], "quantity"))
    if not (ticker_matches and side_matches and quantity_matches):
        return False

    if action == "buy":
        return (
            _money_close(row.get("entry_price", None), _on_own_increment(fill["fill_price"], "fill_price"))
            and str(row.get("entry_date", "")).strip() == str(fill["fill_timestamp"]).strip()
        )
    if action == "sell":
        return (
            str(row.get("status", "")).strip().lower() == "closed"
            and _money_close(row.get("exit_price", None), _on_own_increment(fill["fill_price"], "fill_price"))
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


def get_cash_balance(cash_state_df: pd.DataFrame) -> Decimal:
    if cash_state_df.empty:
        return DEFAULT_STARTING_CASH
    return _money(cash_state_df.iloc[-1]["cash_balance"])


def serialise_row_state(row: pd.Series | dict) -> dict[str, object]:
    if isinstance(row, pd.Series):
        payload = row.to_dict()
    else:
        payload = dict(row)

    clean_payload: dict[str, object] = {}
    for key, value in payload.items():
        clean_payload[str(key)] = None if pd.isna(value) else value
    return clean_payload


def write_cash_balance(balance: Decimal) -> None:
    cash_state_df = pd.DataFrame(
        [{"as_of": utc_now_iso(), "cash_balance": _text(_money(balance))}]
    )
    cash_state_df = validate_cash_state(
        cash_state_df, keep_extra_columns=False, exact_decimal_columns=EXACT_CASH_COLUMNS
    )
    write_managed_csv_with_schema(
        cash_state_df,
        CASH_STATE_PATH,
        schema=CASH_STATE_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
        exact_decimal_columns=EXACT_CASH_COLUMNS,
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
    amount: Decimal,
    fees: Decimal,
    cash_balance_after: Decimal,
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
                "amount": _text(_money(amount)),
                "fees": _text(_money(fees)),
                "cash_balance_after": _text(_money(cash_balance_after)),
                "notes": notes,
            }
        ],
        columns=CASH_LEDGER_SCHEMA.canonical_column_order,
    )
    out = pd.concat([ledger_df, new_row], ignore_index=True)
    out = validate_cash_ledger(
        out, keep_extra_columns=False, exact_decimal_columns=EXACT_LEDGER_COLUMNS
    )
    write_managed_csv_with_schema(
        out,
        CASH_LEDGER_PATH,
        schema=CASH_LEDGER_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
        exact_decimal_columns=EXACT_LEDGER_COLUMNS,
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
    cash_balance: Decimal | float | str,
    ledger_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> tuple[pd.DataFrame, Decimal, pd.DataFrame]:
    assert_legacy_fill_row_admissible(row)
    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).lower()
    quantity = _on_own_increment(row["quantity"], "quantity")
    fill_price = _on_own_increment(row["fill_price"], "fill_price")
    fees = _on_own_increment(row["fees"], "fees")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if fill_price <= 0:
        raise ValueError("fill_price must be positive")
    if fees < 0:
        raise ValueError("fees must be zero or positive")
    gross_cost = canonical_decimal(multiply(quantity, fill_price))
    total_cash_out = exact_sum((gross_cost, fees))
    cash_balance = _money(cash_balance)
    quantity_text = _text(quantity)
    price_text = _text(fill_price)
    fees_text = _text(fees)

    if cash_balance < total_cash_out:
        raise RuntimeError(
            f"Insufficient cash to open position in {ticker}. "
            f"Required={_text(total_cash_out)}, Available={_text(cash_balance)}"
        )

    existing_idx = find_open_position(state_df, ticker, side)
    if existing_idx is not None:
        raise RuntimeError(f"Active position already exists for {ticker} {side}")

    position_id = "POS_" + uuid.uuid4().hex[:10].upper()
    pnl_abs = canonical_decimal(-fees)

    new_row = pd.DataFrame(
        [
            {
                "position_id": position_id,
                "ticker": ticker,
                "side": side,
                "status": "open",
                "quantity": quantity_text,
                "entry_price": price_text,
                "entry_date": row["fill_timestamp"],
                "current_price": price_text,
                "market_value": _text(gross_cost),
                "pnl_abs": _text(pnl_abs),
                "pnl_pct": _text(_percent(pnl_abs, gross_cost)),
                "realised_pnl_abs": "0",
                "fees_total": fees_text,
                "entry_fees_remaining": fees_text,
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
    cash_before_text = _text(cash_balance)
    cash_balance = exact_sum((cash_balance, total_cash_out.copy_negate()))
    cash_text = _text(cash_balance)
    write_cash_balance(cash_balance)

    ledger_df = append_cash_ledger_row(
        ledger_df=ledger_df,
        run_id=run_id,
        event_type="position_open",
        position_id=position_id,
        ticker=ticker,
        side=side,
        action="buy",
        amount=canonical_decimal(-gross_cost),
        fees=fees,
        cash_balance_after=cash_balance,
        notes=f"fill_id={row['fill_id']}; Opened {quantity_text} shares at {price_text}",
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
            "quantity": quantity_text,
            "fill_price": price_text,
            "fees": fees_text,
        },
    )
    append_cash_adjusted_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        ticker=ticker,
        position_id=position_id,
        message=f"Cash debited for opening {ticker}",
        before_state={"cash_balance": cash_before_text},
        after_state={"cash_balance": cash_text},
        details={
            "fill_id": str(row["fill_id"]),
            "action": "buy",
            "gross_cost": _text(gross_cost),
            "fees": fees_text,
            "net_cash_change": _text(canonical_decimal(-total_cash_out)),
        },
    )

    return state_df, cash_balance, ledger_df


def _money_token_missing(value: object) -> bool:
    """True when the cell was never stored.

    An empty string is present and is not a decimal. None and pandas NA are
    the optional-column absence legacy rows are allowed to have.
    """
    if isinstance(value, str):
        return False
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except TypeError:
        return False


def _position_opened_by_fill_agent(position_id: object, ledger_df: pd.DataFrame) -> bool:
    """True when open_long_position created this portfolio row.

    open_long_position is the only writer of cash-ledger event_type
    ``position_open``, and it stores realised_pnl_abs, fees_total, and
    entry_fees_remaining in that same call. A row with no matching
    position_open event was not opened by Fill Agent, so those optional
    columns may be absent. A later partial close does not write this marker.
    """
    position = "" if position_id is None else str(position_id).strip()
    if position == "" or ledger_df.empty:
        return False
    if "event_type" not in ledger_df.columns or "position_id" not in ledger_df.columns:
        return False
    opened = ledger_df["event_type"].astype(str).str.strip().eq("position_open")
    same = ledger_df["position_id"].astype(str).str.strip().eq(position)
    return bool((opened & same).any())


def _stored_money(value: object, fallback: Decimal, *, field: str, required: bool) -> Decimal:
    if _money_token_missing(value):
        if required:
            raise ValueError(
                f"{field} is missing on a position opened by Fill Agent; refusing to close"
            )
        return fallback
    try:
        return _money(value, field)
    except ValueError as exc:
        raise ValueError(
            f"{field} is present but not an exact decimal ({value!r}); refusing to close"
        ) from exc


def close_long_position(
    state_df: pd.DataFrame,
    cash_balance: Decimal | float | str,
    ledger_df: pd.DataFrame,
    row: pd.Series,
    run_id: str,
) -> tuple[pd.DataFrame, Decimal, pd.DataFrame]:
    assert_legacy_fill_row_admissible(row)
    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).lower()
    quantity = _on_own_increment(row["quantity"], "quantity")
    fill_price = _on_own_increment(row["fill_price"], "fill_price")
    fees = _on_own_increment(row["fees"], "fees")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if fill_price <= 0:
        raise ValueError("fill_price must be positive")
    if fees < 0:
        raise ValueError("fees must be zero or positive")
    cash_balance = _money(cash_balance)

    idx = find_open_position(state_df, ticker, side)
    if idx is None:
        raise RuntimeError(f"No active position found to close for {ticker} {side}")

    position = state_df.loc[idx].copy()
    position_before = serialise_row_state(position)

    original_quantity = _money(position["quantity"], "quantity")
    if quantity > original_quantity:
        raise RuntimeError(
            f"Close quantity exceeds the active position for {ticker}. "
            f"Available={_text(original_quantity)}, received={_text(quantity)}"
        )

    entry_price = _money(position["entry_price"], "entry_price")
    opened_by_fill_agent = _position_opened_by_fill_agent(position.get("position_id"), ledger_df)
    total_fees_before = _stored_money(
        position.get("fees_total", None),
        Decimal(0),
        field="fees_total",
        required=opened_by_fill_agent,
    )
    entry_fees_remaining = _stored_money(
        position.get("entry_fees_remaining", None),
        total_fees_before,
        field="entry_fees_remaining",
        required=opened_by_fill_agent,
    )
    realised_before = _stored_money(
        position.get("realised_pnl_abs", None),
        Decimal(0),
        field="realised_pnl_abs",
        required=opened_by_fill_agent,
    )

    gross_proceeds = canonical_decimal(multiply(quantity, fill_price))
    net_proceeds = exact_sum((gross_proceeds, fees.copy_negate()))
    allocated_entry_fees = _allocate_entry_fee(entry_fees_remaining, quantity, original_quantity)
    entry_cost = canonical_decimal(multiply(quantity, entry_price))
    realised_increment = exact_sum((
        net_proceeds, entry_cost.copy_negate(), allocated_entry_fees.copy_negate()
    ))
    realised_pnl = exact_sum((realised_before, realised_increment))
    quantity_text = _text(quantity)
    price_text = _text(fill_price)
    fees_text = _text(fees)
    realised_text = _text(realised_pnl)
    increment_text = _text(realised_increment)

    if quantity < original_quantity:
        remaining_quantity = exact_sum((original_quantity, quantity.copy_negate()))
        remaining_entry_fees = exact_sum((entry_fees_remaining, allocated_entry_fees.copy_negate()))
        remaining_basis = canonical_decimal(multiply(remaining_quantity, entry_price))
        unrealised = exact_sum((
            canonical_decimal(multiply(exact_sum((fill_price, entry_price.copy_negate())), remaining_quantity)),
            remaining_entry_fees.copy_negate(),
        ))
        state_df.at[idx, "quantity"] = _text(remaining_quantity)
        if "capital_allocated" in state_df.columns:
            state_df.at[idx, "capital_allocated"] = _text(remaining_basis)
        state_df.at[idx, "current_price"] = price_text
        state_df.at[idx, "market_value"] = _text(canonical_decimal(multiply(remaining_quantity, fill_price)))
        state_df.at[idx, "pnl_abs"] = _text(unrealised)
        state_df.at[idx, "pnl_pct"] = _text(_percent(unrealised, remaining_basis))
        state_df.at[idx, "realised_pnl_abs"] = realised_text
        state_df.at[idx, "fees_total"] = _text(exact_sum((total_fees_before, fees)))
        state_df.at[idx, "entry_fees_remaining"] = _text(remaining_entry_fees)
        state_df.at[idx, "exit_flag"] = "False"
        state_df.at[idx, "exit_reason"] = "partial_close"
        state_df.at[idx, "last_updated"] = utc_now_iso()
        state_df.at[idx, "run_id"] = run_id

        position_after = serialise_row_state(state_df.loc[idx])
        cash_before_text = _text(cash_balance)
        cash_balance = exact_sum((cash_balance, net_proceeds))
        cash_text = _text(cash_balance)
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
                f"fill_id={row['fill_id']}; Partially closed {quantity_text} shares "
                f"at {price_text}; realised_pnl_increment={increment_text}"
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
                "quantity_closed": quantity_text,
                "quantity_remaining": _text(remaining_quantity),
                "fill_price": price_text,
                "fees": fees_text,
                "realised_pnl_increment": increment_text,
            },
        )
        append_cash_adjusted_event(
            run_id=run_id,
            agent_name=AGENT_NAME,
            ticker=ticker,
            position_id=str(position["position_id"]),
            message=f"Cash credited for partial close of {ticker}",
            before_state={"cash_balance": cash_before_text},
            after_state={"cash_balance": cash_text},
            details={
                "fill_id": str(row["fill_id"]),
                "gross_proceeds": _text(gross_proceeds),
                "fees": fees_text,
                "net_cash_change": _text(net_proceeds),
            },
        )
        return state_df, cash_balance, ledger_df

    state_df.at[idx, "status"] = "closed"
    state_df.at[idx, "current_price"] = price_text
    state_df.at[idx, "market_value"] = "0"
    state_df.at[idx, "pnl_abs"] = "0"
    state_df.at[idx, "pnl_pct"] = "0"
    state_df.at[idx, "realised_pnl_abs"] = realised_text
    state_df.at[idx, "fees_total"] = _text(exact_sum((total_fees_before, fees)))
    state_df.at[idx, "entry_fees_remaining"] = "0"
    state_df.at[idx, "exit_flag"] = "False"
    state_df.at[idx, "exit_reason"] = "position_closed"
    state_df.at[idx, "last_updated"] = utc_now_iso()
    state_df.at[idx, "run_id"] = run_id
    # A persisted all-null datetime column is inferred as float by pandas on reload.
    # Restore an object-capable dtype before recording the first close timestamp.
    state_df["closed_at"] = state_df["closed_at"].astype("object")
    state_df.at[idx, "closed_at"] = row["fill_timestamp"]
    state_df.at[idx, "exit_price"] = price_text

    position_after = serialise_row_state(state_df.loc[idx])
    cash_before_text = _text(cash_balance)
    cash_balance = exact_sum((cash_balance, net_proceeds))
    cash_text = _text(cash_balance)
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
        notes=f"fill_id={row['fill_id']}; Closed {quantity_text} shares at {price_text}; realised_pnl={realised_text}",
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
            "quantity": quantity_text,
            "fill_price": price_text,
            "fees": fees_text,
            "realised_pnl_abs": realised_text,
        },
    )
    append_cash_adjusted_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        ticker=ticker,
        position_id=str(position["position_id"]),
        message=f"Cash credited for closing {ticker}",
        before_state={"cash_balance": cash_before_text},
        after_state={"cash_balance": cash_text},
        details={
            "fill_id": str(row["fill_id"]),
            "action": "sell",
            "gross_proceeds": _text(gross_proceeds),
            "fees": fees_text,
            "net_cash_change": _text(net_proceeds),
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
    assert_legacy_fill_row_admissible(row)
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
                "quantity": _text(_on_own_increment(row["quantity"], "quantity")),
                "fill_price": _text(_on_own_increment(row["fill_price"], "fill_price")),
                "fees": _text(_on_own_increment(row["fees"], "fees")),
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
        exact_decimal_columns=EXACT_TRADE_FILL_COLUMNS,
    )
    write_managed_csv_with_schema(
        output_df,
        TRADE_FILLS_PATH,
        schema=TRADE_FILLS_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
        exact_decimal_columns=EXACT_TRADE_FILL_COLUMNS,
    )
    append_trade_fill_row_sqlite(new_row.iloc[0].to_dict())
    return output_df


def run_fill_agent() -> None:
    # This must precede run/event/runtime initialization: a staged broker fill
    # cannot silently bootstrap a synthetic empty USD100,000 account.
    assert_legacy_fill_inputs_admissible(
        manual_fills_path=MANUAL_FILLS_PATH, broker_fills_path=BROKER_FILLS_PATH)
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
        print(f"Ending cash balance: {_text(cash_balance)}")
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
                "quantity": _text(_on_own_increment(row["quantity"], "quantity")),
                "fill_price": _text(_on_own_increment(row["fill_price"], "fill_price")),
                "fees": _text(_on_own_increment(row["fees"], "fees")),
                "fill_timestamp": str(row["fill_timestamp"]),
            },
        )

    state_df = validate_portfolio_state(
        state_df, keep_extra_columns=False, exact_decimal_columns=EXACT_PORTFOLIO_COLUMNS
    )
    write_managed_csv_with_schema(
        state_df,
        STATE_PATH,
        schema=PORTFOLIO_STATE_SCHEMA_SPEC,
        producer=AGENT_NAME,
        keep_extra_columns=False,
        exact_decimal_columns=EXACT_PORTFOLIO_COLUMNS,
    )
    replace_portfolio_state_rows(state_df.to_dict(orient="records"))

    print("Fill Agent finished.")
    print(f"Saved state to: {STATE_PATH}")
    print(f"Saved processed fills to: {PROCESSED_FILLS_PATH}")
    print(f"Saved trade fills to: {TRADE_FILLS_PATH}")
    print(f"Saved cash state to: {CASH_STATE_PATH}")
    print(f"Saved cash ledger to: {CASH_LEDGER_PATH}")
    print(f"Ending cash balance: {_text(cash_balance)}")


if __name__ == "__main__":
    run_fill_agent()
