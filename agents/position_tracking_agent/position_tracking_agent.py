from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pandas as pd

from agents.shared.event_log import append_artifact_written_event
from shared import market_data
from shared.freshness import (
    OUTCOME_FRESH,
    OUTCOME_FUTURE,
    OUTCOME_STALE,
    assess_freshness,
    summarize_provider_error,
)
from shared.io_utils import read_csv_with_schema, write_managed_csv_with_schema
from shared.market_data import MarketDataProvider, _to_utc_iso
from shared.paths import DATA_DIR as RUNTIME_STATE_DIR
from shared.paths import project_path
from shared.portfolio_state_helpers import (
    ACTIVE_POSITION_STATUSES,
    CLOSED_POSITION_STATUS,
    normalise_position_status,
)
from shared.run_context import get_or_create_run_id
from shared.schemas import (
    PORTFOLIO_MONITOR_SCHEMA,
    PORTFOLIO_STATE_SCHEMA,
    POSITION_ALERTS_SCHEMA,
    validate_portfolio_monitor,
    validate_position_alerts,
)


DATA_DIR = str(RUNTIME_STATE_DIR)

STATE_PATH = os.path.join(DATA_DIR, "portfolio_state.csv")
MONITOR_PATH = os.path.join(DATA_DIR, "portfolio_monitor.csv")
ALERTS_PATH = os.path.join(DATA_DIR, "position_alerts.csv")
AGENT_NAME = "Position Tracking Agent"
UNIVERSE_PATH = project_path("data_sources", "stock_universe.csv")
_PRICE_QUANTUM = Decimal("0.0001")
_MONEY_QUANTUM = Decimal("0.01")
_PERCENT_QUANTUM = Decimal("0.0001")
_HISTORY_LOOKBACK_DAYS = 21
_MARK_DATA_KIND = "daily_research_price"
_UNUSABLE_MARK_STATUSES = {"stale", "error"}


@dataclass(frozen=True)
class PositionMark:
    status: str
    price: float | None
    price_as_of: str
    source: str
    reason: str
    listing_currency: str
    provider_symbol: str


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_run_id() -> str:
    return get_or_create_run_id()


def read_portfolio_state() -> pd.DataFrame:
    return read_csv_with_schema(
        STATE_PATH,
        schema=PORTFOLIO_STATE_SCHEMA,
        empty_ok=False,
    )


def read_portfolio_monitor() -> pd.DataFrame:
    if not os.path.exists(MONITOR_PATH):
        return validate_portfolio_monitor(pd.DataFrame(), keep_extra_columns=False)
    return read_csv_with_schema(
        MONITOR_PATH,
        schema=PORTFOLIO_MONITOR_SCHEMA,
        empty_ok=True,
    )


def write_portfolio_monitor(df: pd.DataFrame) -> None:
    write_managed_csv_with_schema(
        df,
        MONITOR_PATH,
        schema=PORTFOLIO_MONITOR_SCHEMA,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )


def ensure_alerts_file() -> pd.DataFrame:
    if os.path.exists(ALERTS_PATH):
        return validate_position_alerts(pd.read_csv(ALERTS_PATH), keep_extra_columns=False)

    df = pd.DataFrame(
        columns=POSITION_ALERTS_SCHEMA.column_order
    )
    df = validate_position_alerts(df, keep_extra_columns=False)
    write_managed_csv_with_schema(
        df,
        ALERTS_PATH,
        schema=POSITION_ALERTS_SCHEMA,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    return df


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    return parsed


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_EVEN)


def round_mark_price(value: object) -> float:
    """Round a mark to the advisory price scale with half-even money rounding."""
    return float(_quantize(_decimal(value, "price"), _PRICE_QUANTUM))


def _normalise_currency(value: object) -> str:
    """Map feed and listing codes onto one unit. GBp is pence, the same unit as GBX.

    Uppercasing first would turn GBp into GBP and treat a London pence quote as pounds.
    """
    text = str(value or "").strip()
    if text in {"GBp", "GBX", "GBx"} or text.lower() == "gbx":
        return "GBX"
    if text.lower() == "gbp":
        return "GBP"
    return text.upper()


def load_listing_map(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, str]]:
    source = Path(path) if path is not None else UNIVERSE_PATH
    frame = pd.read_csv(source, dtype=str, keep_default_na=False)
    missing = {"ticker", "currency"} - set(frame.columns)
    if missing:
        raise ValueError(f"stock universe is missing columns: {sorted(missing)}")
    listings: dict[str, dict[str, str]] = {}
    for row in frame.to_dict(orient="records"):
        symbol = str(row["ticker"]).strip()
        key = symbol.upper()
        if not key:
            continue
        if key in listings:
            raise ValueError(f"stock universe has duplicate ticker {key}")
        listings[key] = {
            "ticker": symbol,
            "currency": _normalise_currency(row.get("currency", "")),
        }
    return listings


def _history_window(now: datetime) -> tuple[str, str]:
    day = now.astimezone(timezone.utc).date()
    start = (day - timedelta(days=_HISTORY_LOOKBACK_DAYS)).isoformat()
    # Yahoo's end is exclusive. The next UTC date includes the run's session
    # and does not request a later one.
    end = (day + timedelta(days=1)).isoformat()
    return start, end


def _close_series(data: pd.DataFrame) -> pd.Series:
    if data is None or data.empty:
        return pd.Series(dtype="float64")
    close: object
    if "Close" in data.columns:
        close = data["Close"]
    elif isinstance(data.columns, pd.MultiIndex) and "Close" in data.columns.get_level_values(0):
        close = data["Close"]
    else:
        return pd.Series(dtype="float64")
    if isinstance(close, pd.DataFrame):
        if close.shape[1] == 0:
            return pd.Series(dtype="float64")
        close = close.iloc[:, 0]
    series = pd.to_numeric(close, errors="coerce").dropna()
    if not isinstance(series.index, pd.DatetimeIndex):
        series.index = pd.to_datetime(series.index)
    return series.sort_index()


def _read_quote_currency(provider: object, symbol: str) -> tuple[str | None, str | None]:
    factory = getattr(provider, "ticker_factory", None)
    if not callable(factory):
        return None, None
    try:
        instrument = factory(symbol)
        fast_info = getattr(instrument, "fast_info", None)
        raw = None
        if fast_info is not None:
            getter = getattr(fast_info, "get", None)
            if callable(getter):
                raw = getter("currency")
            else:
                raw = fast_info["currency"]
        if raw is None or str(raw).strip() == "":
            info = getattr(instrument, "info", None)
            if isinstance(info, dict):
                raw = info.get("currency")
        if raw is None or str(raw).strip() == "":
            return None, None
        return str(raw).strip(), None
    except Exception as exc:
        return None, summarize_provider_error(exc)


def _currency_note(
    listing_currency: str,
    raw_quote: str | None,
    quote_error: str | None,
) -> tuple[str, str]:
    """Return ('ok', note) or ('error', reason). Never rescales pence into pounds."""
    if quote_error:
        return (
            "ok",
            f"quote currency unavailable ({quote_error}); listing currency "
            f"{listing_currency} used with no unit conversion",
        )
    if raw_quote is None:
        return (
            "ok",
            f"quote currency unavailable; listing currency {listing_currency} "
            "used with no unit conversion",
        )
    quote = _normalise_currency(raw_quote)
    if quote != listing_currency:
        return (
            "error",
            f"quote currency {raw_quote} does not match listing currency "
            f"{listing_currency}; price was not converted",
        )
    if raw_quote != listing_currency:
        return (
            "ok",
            f"quote currency {raw_quote} matches listing currency {listing_currency}; "
            "no unit conversion",
        )
    return "ok", f"quote currency {listing_currency}; no unit conversion"


def _select_usable_bar(
    data: pd.DataFrame,
    *,
    now: datetime,
    source: str,
    retrieved_at: str,
) -> tuple[Decimal, str, object] | None:
    chosen: tuple[Decimal, str, object] | None = None
    for timestamp, raw_price in _close_series(data).items():
        as_of = _to_utc_iso(timestamp)
        if not as_of:
            continue
        try:
            observed = _decimal(raw_price, "price")
        except ValueError:
            continue
        if observed <= 0:
            continue
        assessment = assess_freshness(
            source=source,
            data_kind=_MARK_DATA_KIND,
            observation_time=as_of,
            retrieval_time=retrieved_at,
            now=now,
        )
        if assessment.freshness_outcome == OUTCOME_FUTURE:
            continue
        chosen = (observed, as_of, assessment)
    return chosen


def resolve_position_mark(
    ticker: str,
    *,
    provider: MarketDataProvider | None = None,
    universe_path: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    listings: dict[str, dict[str, str]] | None = None,
) -> PositionMark:
    """Latest daily close from the shared read-only market-data provider.

    The print has to be at or before ``now``. Freshness uses the configured
    daily-price policy and the exchange calendar. A stale or failed print is
    returned with an explicit status and is not rewritten into a fresh price.
    """
    run_now = now or datetime.now(timezone.utc)
    if run_now.tzinfo is None:
        raise ValueError("run time must be timezone-aware")
    run_now = run_now.astimezone(timezone.utc)
    symbol = str(ticker).strip().upper()
    table = listings if listings is not None else load_listing_map(universe_path)
    listing = table.get(symbol)
    if listing is None:
        return PositionMark(
            status="error",
            price=None,
            price_as_of="",
            source="",
            reason=f"unknown symbol: {symbol} is not in stock_universe.csv",
            listing_currency="",
            provider_symbol="",
        )

    listing_currency = listing["currency"]
    provider_symbol = listing["ticker"]
    if not listing_currency:
        return PositionMark(
            status="error",
            price=None,
            price_as_of="",
            source="",
            reason=f"listing currency is missing for {provider_symbol}",
            listing_currency="",
            provider_symbol=provider_symbol,
        )

    resolved_provider = provider if provider is not None else market_data._default_provider
    start, end = _history_window(run_now)
    result = market_data.fetch_price_history(
        provider_symbol,
        period="1mo",
        interval="1d",
        auto_adjust=True,
        provider=resolved_provider,
        start=start,
        end=end,
    )
    source = result.metadata.source or ""
    base = dict(
        listing_currency=listing_currency,
        provider_symbol=provider_symbol,
        source=source,
    )
    if result.metadata.error:
        return PositionMark(
            status="error",
            price=None,
            price_as_of="",
            reason=summarize_provider_error(result.metadata.error),
            **base,
        )

    selected = _select_usable_bar(
        result.data,
        now=run_now,
        source=source or "market_data",
        retrieved_at=result.metadata.fetched_at or run_now.isoformat(),
    )
    if selected is None:
        return PositionMark(
            status="error",
            price=None,
            price_as_of="",
            reason=f"{provider_symbol} returned no price at or before the run time",
            **base,
        )

    observed, as_of, assessment = selected
    if assessment.freshness_outcome == OUTCOME_FRESH and assessment.actionable:
        status = "fresh"
    elif assessment.freshness_outcome == OUTCOME_STALE:
        status = "stale"
    else:
        return PositionMark(
            status="error",
            price=None,
            price_as_of="",
            reason=assessment.reason or "mark failed the freshness check",
            **base,
        )

    raw_quote, quote_error = _read_quote_currency(resolved_provider, provider_symbol)
    currency_status, currency_note = _currency_note(listing_currency, raw_quote, quote_error)
    if currency_status == "error":
        return PositionMark(
            status="error",
            price=None,
            price_as_of="",
            reason=currency_note,
            **base,
        )

    reason = assessment.reason or ""
    if currency_note:
        reason = f"{reason}; {currency_note}" if reason else currency_note
    return PositionMark(
        status=status,
        price=float(_quantize(observed, _PRICE_QUANTUM)),
        price_as_of=as_of,
        reason=reason,
        **base,
    )


def get_latest_price(ticker: str, fallback_price: Optional[float] = None) -> float:
    """Return a fresh mark, or raise. A missing feed is not filled with ``fallback_price``."""
    del fallback_price
    mark = resolve_position_mark(ticker)
    if mark.status != "fresh" or mark.price is None:
        raise ValueError(f"No fresh mark for {ticker}: {mark.status}: {mark.reason}")
    return float(mark.price)


def calculate_long_pnl(
    quantity: float,
    entry_price: float,
    current_price: float,
) -> tuple[float, float, float]:
    quantity_d = _decimal(quantity, "quantity")
    entry_d = _decimal(entry_price, "entry_price")
    price_d = _quantize(_decimal(current_price, "current_price"), _PRICE_QUANTUM)
    market_value = _quantize(quantity_d * price_d, _MONEY_QUANTUM)
    pnl_abs = _quantize((price_d - entry_d) * quantity_d, _MONEY_QUANTUM)
    cost_basis = quantity_d * entry_d
    if cost_basis <= 0:
        pnl_pct = Decimal(0)
    else:
        pnl_pct = _quantize((pnl_abs / cost_basis) * Decimal(100), _PERCENT_QUANTUM)
    return float(market_value), float(pnl_abs), float(pnl_pct)


def calculate_short_pnl(quantity: float, entry_price: float, current_price: float) -> tuple[float, float, float]:
    """
    For shorts:
    - market_value here is stored as current notional exposure
    - pnl_abs rises as price falls
    """
    quantity_d = _decimal(quantity, "quantity")
    entry_d = _decimal(entry_price, "entry_price")
    price_d = _quantize(_decimal(current_price, "current_price"), _PRICE_QUANTUM)
    market_value = _quantize(quantity_d * price_d, _MONEY_QUANTUM)
    pnl_abs = _quantize((entry_d - price_d) * quantity_d, _MONEY_QUANTUM)
    initial_notional = quantity_d * entry_d
    if initial_notional <= 0:
        pnl_pct = Decimal(0)
    else:
        pnl_pct = _quantize((pnl_abs / initial_notional) * Decimal(100), _PERCENT_QUANTUM)
    return float(market_value), float(pnl_abs), float(pnl_pct)


def append_alert(
    alerts_df: pd.DataFrame,
    run_id: str,
    position_id: str,
    ticker: str,
    status: str,
    alert_type: str,
    message: str,
) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [
            {
                "alert_id": f"ALT_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}",
                "run_id": run_id,
                "timestamp": utc_now_iso(),
                "position_id": position_id,
                "ticker": ticker,
                "status": status,
                "alert_type": alert_type,
                "message": message,
            }
        ]
    )
    out = pd.concat([alerts_df, new_row], ignore_index=True)
    out = validate_position_alerts(out, keep_extra_columns=False)
    write_managed_csv_with_schema(
        out,
        ALERTS_PATH,
        schema=POSITION_ALERTS_SCHEMA,
        producer=AGENT_NAME,
        keep_extra_columns=False,
    )
    return out


def validate_tracking_inputs(row: pd.Series) -> None:
    quantity = pd.to_numeric(row.get("quantity"), errors="coerce")
    entry_price = pd.to_numeric(row.get("entry_price"), errors="coerce")

    if pd.isna(quantity) or quantity <= 0:
        raise ValueError(
            f"Invalid active quantity for position_id={row.get('position_id')} ticker={row.get('ticker')}"
        )

    if pd.isna(entry_price) or entry_price <= 0:
        raise ValueError(
            f"Invalid active entry_price for position_id={row.get('position_id')} ticker={row.get('ticker')}"
        )

    side = str(row.get("side")).strip().lower()
    if side not in {"long", "short"}:
        raise ValueError(
            f"Invalid side for position_id={row.get('position_id')} ticker={row.get('ticker')}: {row.get('side')}"
        )


def _numeric_value(*values: object) -> float | None:
    for value in values:
        numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
        if not pd.isna(numeric):
            return float(numeric)
    return None


def _text_field(row: pd.Series, name: str) -> str:
    if name not in row.index:
        return ""
    value = row.get(name)
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _same_price(left: object, right: object) -> bool:
    return _quantize(_decimal(left, "price"), _PRICE_QUANTUM) == _quantize(
        _decimal(right, "price"),
        _PRICE_QUANTUM,
    )


def process_active_position(
    row: pd.Series,
    run_id: str,
    previous_monitor_row: pd.Series | None = None,
    *,
    provider: MarketDataProvider | None = None,
    universe_path: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
    listings: dict[str, dict[str, str]] | None = None,
) -> dict[str, object]:
    validate_tracking_inputs(row)

    ticker = str(row["ticker"]).upper()
    side = str(row["side"]).strip().lower()
    quantity = float(row["quantity"])
    entry_price = float(row["entry_price"])

    previous_monitor_row = (
        previous_monitor_row
        if previous_monitor_row is not None
        else pd.Series(dtype=object)
    )
    stored_price = _numeric_value(
        previous_monitor_row.get("current_price"),
        row.get("current_price"),
        entry_price,
    )
    mark = resolve_position_mark(
        ticker,
        provider=provider,
        universe_path=universe_path,
        now=now,
        listings=listings,
    )
    price_as_of = mark.price_as_of
    mark_source = mark.source
    mark_status = mark.status
    mark_reason = mark.reason
    listing_currency = mark.listing_currency

    if mark.status == "fresh" and mark.price is not None:
        latest_price = round_mark_price(mark.price)
    elif mark.status == "stale" and mark.price is not None:
        latest_price = round_mark_price(mark.price)
    else:
        if stored_price is None:
            raise ValueError(
                f"No price available for {ticker}: {mark.reason or mark.status}"
            )
        latest_price = round_mark_price(stored_price)
        mark_status = "error"
        mark_reason = (
            f"{mark.reason}; last stored price retained and flagged"
            if mark.reason
            else "last stored price retained and flagged"
        )
        previous_current = _numeric_value(previous_monitor_row.get("current_price"))
        if previous_current is not None and _same_price(latest_price, previous_current):
            price_as_of = _text_field(previous_monitor_row, "price_as_of")
            mark_source = _text_field(previous_monitor_row, "mark_source")
        else:
            price_as_of = ""
            mark_source = ""
        if not listing_currency:
            listing_currency = _text_field(previous_monitor_row, "listing_currency")

    if side == "long":
        market_value, pnl_abs, pnl_pct = calculate_long_pnl(quantity, entry_price, latest_price)
    else:
        market_value, pnl_abs, pnl_pct = calculate_short_pnl(quantity, entry_price, latest_price)

    previous_high = _numeric_value(
        previous_monitor_row.get("highest_price_since_entry"),
        row.get("highest_price_since_entry"),
    )
    previous_low = _numeric_value(
        previous_monitor_row.get("lowest_price_since_entry"),
        row.get("lowest_price_since_entry"),
    )
    if mark_status == "fresh":
        highest = latest_price if previous_high is None else max(previous_high, latest_price)
        lowest = latest_price if previous_low is None else min(previous_low, latest_price)
    else:
        highest = previous_high if previous_high is not None else latest_price
        lowest = previous_low if previous_low is not None else latest_price

    return {
        "position_id": str(row["position_id"]),
        "ticker": ticker,
        "side": side,
        "status": normalise_position_status(row["status"]),
        "quantity": quantity,
        "entry_price": entry_price,
        "current_price": latest_price,
        "market_value": market_value,
        "pnl_abs": pnl_abs,
        "pnl_pct": pnl_pct,
        "highest_price_since_entry": highest,
        "lowest_price_since_entry": lowest,
        "marked_at": utc_now_iso(),
        "price_as_of": price_as_of,
        "mark_source": mark_source,
        "mark_status": mark_status,
        "mark_reason": mark_reason,
        "listing_currency": listing_currency,
        "run_id": run_id,
    }


def run_position_tracking_agent(
    provider: MarketDataProvider | None = None,
    universe_path: str | os.PathLike[str] | None = None,
    now: datetime | None = None,
) -> None:
    run_id = current_run_id()
    run_now = now or datetime.now(timezone.utc)

    state_df = read_portfolio_state()
    previous_monitor_df = read_portfolio_monitor()
    alerts_df = ensure_alerts_file()

    if state_df.empty:
        write_portfolio_monitor(
            validate_portfolio_monitor(pd.DataFrame(), keep_extra_columns=False)
        )
        append_artifact_written_event(
            run_id=run_id,
            agent_name=AGENT_NAME,
            entity_type="portfolio",
            entity_id="portfolio_monitor",
            message="Position tracking completed with no positions to update.",
            details={
                "portfolio_monitor_path": MONITOR_PATH,
                "position_alerts_path": ALERTS_PATH,
                "total_positions": 0,
                "active_positions_updated": 0,
                "exit_required_positions": 0,
                "alerts_added": 0,
            },
        )
        print("Position Tracking Agent finished.")
        print(f"Saved portfolio monitor to: {MONITOR_PATH}")
        print(f"Saved position alerts to: {ALERTS_PATH}")
        print("")
        print("Run summary:")
        print("Total positions: 0")
        print("Open positions: 0")
        print("Exit required: 0")
        print(f"Updated at: {utc_now_iso()}")
        return

    output_rows: list[dict[str, object]] = []
    previous_by_position_id: dict[str, pd.Series] = {}
    if not previous_monitor_df.empty:
        if previous_monitor_df["position_id"].duplicated().any():
            duplicate_ids = sorted(
                previous_monitor_df.loc[
                    previous_monitor_df["position_id"].duplicated(keep=False),
                    "position_id",
                ].astype(str).unique()
            )
            raise ValueError(
                f"portfolio_monitor.csv has duplicate position_id values: {duplicate_ids}"
            )
        previous_by_position_id = {
            str(row["position_id"]): row
            for _, row in previous_monitor_df.iterrows()
        }

    total_positions = len(state_df)
    active_count = 0
    exit_required_count = 0
    alert_count = 0
    listings = (
        load_listing_map(universe_path)
        if state_df["status"].map(normalise_position_status).isin(ACTIVE_POSITION_STATUSES).any()
        else {}
    )

    for _, row in state_df.iterrows():
        status = normalise_position_status(row.get("status"))

        if status in ACTIVE_POSITION_STATUSES:
            active_count += 1
            original_status = status

            updated_row = process_active_position(
                row,
                run_id,
                previous_monitor_row=previous_by_position_id.get(
                    str(row["position_id"])
                ),
                provider=provider,
                universe_path=universe_path,
                now=run_now,
                listings=listings,
            )

            output_rows.append(updated_row)

            if str(updated_row["mark_status"]) in _UNUSABLE_MARK_STATUSES:
                alerts_df = append_alert(
                    alerts_df=alerts_df,
                    run_id=run_id,
                    position_id=str(updated_row["position_id"]),
                    ticker=str(updated_row["ticker"]),
                    status=str(updated_row["status"]),
                    alert_type=str(updated_row["mark_status"]),
                    message=str(updated_row["mark_reason"]),
                )
                alert_count += 1

            if original_status == "exit_required":
                exit_required_count += 1
                alerts_df = append_alert(
                    alerts_df=alerts_df,
                    run_id=run_id,
                    position_id=str(updated_row["position_id"]),
                    ticker=str(updated_row["ticker"]),
                    status=str(updated_row["status"]),
                    alert_type="exit_required",
                    message="Position remains in exit_required state and was mark-to-market updated only.",
                )
                alert_count += 1

        elif status == CLOSED_POSITION_STATUS:
            continue

        else:
            raise ValueError(
                f"Invalid status encountered in Position Tracking Agent for "
                f"position_id={row.get('position_id')} ticker={row.get('ticker')}: {row.get('status')}"
            )

    out_df = validate_portfolio_monitor(
        pd.DataFrame(output_rows),
        keep_extra_columns=False,
    )
    write_portfolio_monitor(out_df)
    append_artifact_written_event(
        run_id=run_id,
        agent_name=AGENT_NAME,
        entity_type="portfolio",
        entity_id="portfolio_monitor",
        message="Position tracking mark-to-market update completed.",
        details={
            "portfolio_monitor_path": MONITOR_PATH,
            "position_alerts_path": ALERTS_PATH,
            "total_positions": total_positions,
            "active_positions_updated": active_count,
            "exit_required_positions": exit_required_count,
            "alerts_added": alert_count,
        },
    )

    print("Position Tracking Agent finished.")
    print(f"Saved portfolio monitor to: {MONITOR_PATH}")
    print(f"Saved position alerts to: {ALERTS_PATH}")
    print("")
    print("Run summary:")
    print(f"Total positions: {total_positions}")
    print(f"Active positions updated: {active_count}")
    print(f"Exit required positions: {exit_required_count}")
    print(f"Alerts added: {alert_count}")
    print(f"Updated at: {utc_now_iso()}")


if __name__ == "__main__":
    run_position_tracking_agent()
