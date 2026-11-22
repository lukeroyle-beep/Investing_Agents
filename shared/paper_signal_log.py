"""Append-only paper signal log and read-only horizon scoring.

This module records buy, sell, and hold views the agents already produced and
later compares them with daily closes. It does not place, cancel, or amend
orders, and it does not call a broker.

Scoring rules (long only)
-------------------------
Returns are simple price returns, ``(later - earlier) / earlier``, calculated
with ``Decimal``. The configured ``cost_bps`` is an assumption, not a measured
broker fee. It is a round trip (entry and exit of the evaluation window) and
is subtracted only from the side the signal chooses. It is printed on every
summary. Outcomes are not stored back into the log.

* ``buy_versus_benchmark`` (advisory ready long): choose the ticker. Excess =
  ticker return after cost, minus the benchmark return. Hit when excess > 0.
  A tie is a miss.
* ``sell_long_versus_benchmark`` (exit take-profit or stop on a long): choose
  the benchmark. Excess = benchmark return after cost, minus the ticker
  return. Hit when excess > 0, which is the case where the benchmark beats the
  ticker after the cost of switching. A tie is a miss.
* ``keep_long_versus_benchmark`` (exit hold on a long): keep the ticker and do
  not trade, so the cost is zero. Excess = ticker return minus benchmark
  return. Hit when excess >= 0, including a tie.
* ``do_not_enter_versus_benchmark`` (advisory hold for review): do not buy.
  Cost is zero. Excess = benchmark return minus ticker return. Hit when
  excess >= 0, including a tie: waiting did at least as well as buying.

Short views are recorded and then left unscored. A blocked advisory row, an
exit review, a raise-stop, and an already-closed stub are not buy/sell/hold
views and are not recorded.

Prices and the calendar
-----------------------
The signal price is the price the agent stored. It is never replaced with a
later fetch. The benchmark price at the signal is the last daily close whose
session date is strictly before the signal's UTC date, so a same-day close is
not treated as already known. A daily bar is treated as known only when its
session date is strictly before the evaluation UTC date.

The outcome price is the first daily close with session date on or after the
horizon date, and only if that session is already known. The horizon date is
the signal's UTC date plus the number of months, clamped to the last calendar
day of the target month. A horizon whose date has not been reached, or whose
close is not yet known, is pending. Pending rows have no estimated price.

Outcome closes use ``fetch_price_history(..., auto_adjust=False, actions=True)``,
the same adjustment choice as the backtester: a later dividend does not
rewrite earlier bars. Yahoo still split-adjusts that Close. If a split is
visible on or after the signal date, the row is flagged and not scored,
because the logged price is not restated. Advisory signal prices themselves
come from the signal agent's adjusted close, so they can differ from this
unadjusted outcome series by past dividends. That difference is stated here
and is not repaired by rewriting the logged price.

Currency
--------
The investor's results are in GBP. Excess and the hit use simple price
returns after both the ticker and the benchmark have been turned into GBP.
The listing's own return is still reported and is not the scored return.

``GBP`` prices are already pounds. ``GBp`` and ``GBX`` are pence and are
divided by 100. A logged pound price against a pence feed is
``quote_currency_conflict`` and is not rescaled. USD uses the Yahoo close
``GBPUSD=X`` (USD per 1 GBP; divide). EUR uses ``EURGBP=X`` (GBP per 1 EUR;
multiply). JPY uses ``GBPJPY=X`` (JPY per 1 GBP; divide). Those closes come
from the same daily history path as prices.

The signal FX close is the last session strictly before the signal UTC date.
The outcome FX close is the first session on or after the horizon and before
the evaluation UTC date. If a required close is missing, the row is
``fx_missing`` and is not scored in the local currency instead. A missing
currency is ``currency_unconfirmed`` and is not scored. A failed price read
is ``data_feed_error`` or ``outcome_missing`` and is not omitted.
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd
import yaml

from shared.freshness import summarize_provider_error
from shared.market_data import fetch_price_history
from shared.paths import (
    PAPER_SIGNAL_CONFIG_PATH,
    PAPER_SIGNAL_LOG_PATH,
    PROJECT_ROOT,
    project_path,
)


SCHEMA_VERSION = "1"
SIGNALS = frozenset({"buy", "sell", "hold"})
SCORE_RULES = frozenset(
    {
        "buy_versus_benchmark",
        "sell_long_versus_benchmark",
        "keep_long_versus_benchmark",
        "do_not_enter_versus_benchmark",
    }
)
RETURN_QUANTUM = Decimal("0.00000001")
PRICE_QUANTUM = Decimal("0.00000001")
PERCENT_QUANTUM = Decimal("0.0001")
IDENTITY_FIELDS = (
    "run_id",
    "agent",
    "ticker",
    "signal",
    "source_view",
    "position_id",
)
PAYLOAD_FIELDS = (
    "price",
    "price_currency",
    "price_source",
    "price_field",
    "rationale",
    "score_rule",
    "direction",
    "position_side",
)
REQUIRED_FIELDS = IDENTITY_FIELDS + (
    "schema_version",
    "signal_id",
    "signalled_at",
    "signal_date_utc",
    "price",
    "price_source",
    "price_currency",
    "rationale",
    "score_rule",
)

ADVISORY_PRICE_SOURCE = "yfinance"
ADVISORY_PRICE_FIELD = "adjusted_daily_close"
ADVISORY_PRICE_VIA = (
    "portfolio_orders.entry_price copied from the signal agent's latest "
    "adjusted daily close"
)
EXIT_PRICE_SOURCE = "portfolio_monitor"
EXIT_PRICE_FIELD = "current_price"
EXIT_PRICE_VIA = "exit decision current_price taken from portfolio_monitor.csv"
OUTCOME_PRICE_BASIS = "yfinance daily Close with auto_adjust false"
REPORTING_CURRENCY = "GBP"
# Yahoo "=X" close is quote currency per 1 unit of the base. GBPUSD=X is USD
# per GBP (divide). EURGBP=X is GBP per EUR (multiply). GBPJPY=X is JPY per GBP
# (divide). Pence are not an FX pair; GBp and GBX divide by 100.
FX_TO_GBP: dict[str, tuple[str, str]] = {
    "USD": ("GBPUSD=X", "divide"),
    "EUR": ("EURGBP=X", "multiply"),
    "JPY": ("GBPJPY=X", "divide"),
}
PENCE_PER_POUND = Decimal(100)

DEFINITIONS = """\
Paper signal scoring (read only; no order is sent).
Signal price: the price stored when the view was logged. It is not replaced.
Benchmark price at the signal: last daily close with session date strictly
before the signal's UTC date.
Outcome price: first daily close on or after the signal UTC date plus the
horizon, and only once that session date is before the evaluation UTC date.
A horizon that has not reached that point is pending and is not estimated.
Buy: excess = ticker return after the cost assumption, minus benchmark return.
Hit when excess > 0. A tie is a miss.
Sell (exit a long): excess = benchmark return after the cost assumption, minus
ticker return. Hit when excess > 0, i.e. the benchmark beats the ticker after
the cost of switching. A tie is a miss.
Hold (keep a long): no cost. Excess = ticker return minus benchmark return.
Hit when excess >= 0, including a tie.
Hold (advisory review, do not enter): no cost. Excess = benchmark return minus
ticker return. Hit when excess >= 0, including a tie.
Cost: the configured round-trip basis points, printed above. It is an
assumption, not a measured fee. Hold rules apply 0.
Currency: excess is a GBP price return. GBP is pounds. GBp and GBX are pence
and are divided by 100. A logged pound price against a pence feed is
quote_currency_conflict and is not rescaled. USD uses Yahoo GBPUSD=X (USD per
GBP; divide). EUR uses Yahoo EURGBP=X (GBP per EUR; multiply). JPY uses Yahoo
GBPJPY=X (JPY per GBP; divide). Signal FX is the last close strictly before
the signal UTC date. Outcome FX is the first close on or after the horizon
and before the evaluation UTC date. Missing FX is fx_missing and is not filled
with the local return. Local returns stay in the local columns. A missing
currency is currency_unconfirmed and is not scored. A failed price read is an
error row, not a skipped row.
Outcome series: Yahoo Close with auto_adjust off, so a later dividend does not
rewrite the bar. A split on or after the signal date flags the row instead of
scoring it. Advisory prices were adjusted closes at signal time and are not
rewritten onto that series.
"""


class PaperSignalLogError(RuntimeError):
    """The paper log or its configuration cannot be used as written."""


class PaperSignalLogConflict(PaperSignalLogError):
    """The same run already recorded a different view for this identity."""


@dataclass(frozen=True, slots=True)
class PaperSignalConfig:
    benchmark_ticker: str
    benchmark_name: str
    cost_bps: Decimal
    horizons_months: tuple[int, ...]
    log_path: Path
    pending_session_lag_days: int = 7


@dataclass(frozen=True, slots=True)
class LogResult:
    recorded: bool
    reason: str
    signal_id: str = ""


@dataclass(frozen=True, slots=True)
class CloseBar:
    session_date: date
    close: Decimal


@dataclass(frozen=True, slots=True)
class PriceHistory:
    bars: tuple[CloseBar, ...]
    split_dates: tuple[date, ...] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class HorizonOutcome:
    signal_id: str
    signalled_at: str
    ticker: str
    signal: str
    score_rule: str
    horizon_months: int
    horizon_date: str
    status: str
    detail: str
    signal_price: str
    outcome_price: str
    outcome_session: str
    benchmark_ticker: str
    benchmark_entry_price: str
    benchmark_outcome_price: str
    benchmark_outcome_session: str
    ticker_return: str
    benchmark_return: str
    ticker_return_local: str
    benchmark_return_local: str
    cost_applied_bps: str
    excess_return: str
    hit: str
    currency_basis: str
    ticker_currency: str
    benchmark_currency: str


@dataclass(frozen=True, slots=True)
class HorizonSummary:
    horizon_months: int
    signal_group: str
    signals: int
    pending: int
    flagged: int
    scored: int
    hits: int
    hit_rate: str
    mean_excess_return: str
    cost_bps: str
    gbp: int
    fx_missing: int


@dataclass(frozen=True, slots=True)
class Evaluation:
    as_of: str
    benchmark_ticker: str
    benchmark_name: str
    cost_bps: str
    outcome_price_basis: str
    outcomes: tuple[HorizonOutcome, ...]
    summaries: tuple[HorizonSummary, ...]


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PaperSignalLogError(f"{label} is missing")
    try:
        if not isinstance(value, str) and pd.isna(value):
            raise PaperSignalLogError(f"{label} is missing")
    except TypeError:
        pass
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise PaperSignalLogError(f"{label} is not a decimal") from exc
    if not number.is_finite():
        raise PaperSignalLogError(f"{label} is not a finite decimal")
    return number


def decimal_text(number: Decimal) -> str:
    """Stable, non-scientific text. 100.00 and 100 are the same record."""
    return format(number.normalize(), "f")


def _percent_text(number: Decimal) -> str:
    percent = (number * Decimal(100)).quantize(PERCENT_QUANTUM, rounding=ROUND_HALF_EVEN)
    return f"{format(percent, 'f')}%"


def _require_utc(value: str, label: str) -> datetime:
    text = str(value).strip()
    if not text:
        raise PaperSignalLogError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperSignalLogError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise PaperSignalLogError(f"{label} must include an explicit UTC offset")
    return parsed.astimezone(UTC)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def add_months(day: date, months: int) -> date:
    """Calendar months, clamped to the last real day of the target month."""
    if months < 0:
        raise PaperSignalLogError("horizon months must be positive")
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = _last_day(year, month)
    return date(year, month, min(day.day, last_day))


def _last_day(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def session_date_of(value: object) -> date:
    """Session date as the bar index labels it, without shifting timezones."""
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise PaperSignalLogError("price bar has no timestamp")
    return timestamp.date()


def advisory_signal(advice_status: object, direction: object) -> str | None:
    status = str(advice_status or "").strip().lower()
    side = str(direction or "").strip().lower()
    if status == "hold_for_review":
        return "hold"
    if status == "ready_for_manual_review" and side == "long":
        return "buy"
    if status == "ready_for_manual_review" and side == "short":
        return "sell"
    return None


def advisory_score_rule(advice_status: object, direction: object) -> str:
    status = str(advice_status or "").strip().lower()
    side = str(direction or "").strip().lower()
    if status == "hold_for_review":
        return "do_not_enter_versus_benchmark"
    if status == "ready_for_manual_review" and side == "long":
        return "buy_versus_benchmark"
    if status == "ready_for_manual_review" and side == "short":
        return "sell_long_versus_benchmark"
    raise PaperSignalLogError("advisory row is not a scored buy, sell, or hold view")


def exit_signal(exit_action: object, status: object, exit_reason: object) -> str | None:
    if str(status or "").strip().lower() == "no_action":
        return None
    if str(exit_reason or "").strip().lower() == "already_closed":
        return None
    action = str(exit_action or "").strip().lower()
    if action == "hold":
        return "hold"
    if action in {"take_profit", "close"}:
        return "sell"
    return None


def exit_score_rule(exit_action: object, position_side: object) -> str:
    action = str(exit_action or "").strip().lower()
    side = str(position_side or "").strip().lower() or "long"
    if side != "long":
        raise PaperSignalLogError("short exit views are recorded but not scored")
    if action == "hold":
        return "keep_long_versus_benchmark"
    if action in {"take_profit", "close"}:
        return "sell_long_versus_benchmark"
    raise PaperSignalLogError("exit row is not a scored sell or hold view")


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    try:
        if not isinstance(value, str) and pd.isna(value):
            return ""
    except TypeError:
        pass
    return " ".join(str(value).replace("\r", " ").replace("\n", " ").split())


def _redact(value: str) -> str:
    return summarize_provider_error(value, limit=1000)


def _currency_text(value: object) -> str:
    """Currency code, keeping the pence marker in ``GBp`` and ``GBX``."""
    text = _clean_text(value)
    if text.upper() in {"NAN", "NONE", "NAT", "<NA>"}:
        return ""
    if text == "GBp" or text.upper() == "GBX":
        return "GBp" if text == "GBp" else "GBX"
    return text.upper()


def _quote_unit(value: object) -> tuple[str, bool]:
    """Major currency and whether the quote is pence.

    ``GBp`` is pence. ``GBP`` is pounds. Upper-casing alone would hide that.
    """
    text = _currency_text(value)
    if not text:
        return "", False
    if text in {"GBp", "GBX"}:
        return "GBP", True
    return text, False


_universe_currencies: dict[str, str] | None = None


def universe_currency(ticker: str) -> str:
    """Currency declared for a ticker in the checked-in universe file."""
    global _universe_currencies
    if _universe_currencies is None:
        mapping: dict[str, str] = {}
        path = project_path("data_sources", "stock_universe.csv")
        if path.exists():
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    symbol = _clean_text(row.get("ticker")).upper()
                    currency = _currency_text(row.get("currency"))
                    if symbol and currency and symbol not in mapping:
                        mapping[symbol] = currency
        _universe_currencies = mapping
    return _universe_currencies.get(ticker.strip().upper(), "")


def _resolve_currency(ticker: str, explicit: object) -> str:
    declared = _currency_text(explicit)
    if declared:
        return declared
    return universe_currency(ticker)


def _positive_price(value: object) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) and pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if not text:
        return None
    try:
        number = _decimal(text, "price")
    except PaperSignalLogError:
        return None
    if number <= 0:
        return None
    return decimal_text(number)


def load_config(path: Path | None = None) -> PaperSignalConfig:
    config_path = Path(path) if path is not None else PAPER_SIGNAL_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PaperSignalLogError(
            f"Cannot load paper signal configuration: {summarize_provider_error(exc)}"
        ) from exc
    if not isinstance(raw, dict):
        raise PaperSignalLogError("paper signal configuration must be a mapping")
    if str(raw.get("schema_version", "")).strip() != SCHEMA_VERSION:
        raise PaperSignalLogError("unsupported paper signal configuration schema")

    benchmark = os.environ.get("PAPER_SIGNAL_BENCHMARK", "").strip() or str(
        raw.get("benchmark_ticker", "")
    ).strip()
    if not benchmark:
        raise PaperSignalLogError("benchmark_ticker is required")
    name = str(raw.get("benchmark_name", benchmark)).strip() or benchmark

    cost_source = os.environ.get("PAPER_SIGNAL_COST_BPS", "").strip() or raw.get("cost_bps")
    if isinstance(cost_source, bool) or not isinstance(cost_source, (str, int)):
        raise PaperSignalLogError("cost_bps must be an integer or a decimal string")
    cost = _decimal(cost_source, "cost_bps")
    if cost < 0:
        raise PaperSignalLogError("cost_bps cannot be negative")

    horizons_raw = raw.get("horizons_months")
    if not isinstance(horizons_raw, list) or not horizons_raw:
        raise PaperSignalLogError("horizons_months must be a non-empty list")
    horizons: list[int] = []
    for item in horizons_raw:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise PaperSignalLogError("each horizon must be a positive number of months")
        horizons.append(item)
    if len(set(horizons)) != len(horizons):
        raise PaperSignalLogError("horizons_months contains a duplicate")

    lag = raw.get("pending_session_lag_days", 7)
    if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
        raise PaperSignalLogError("pending_session_lag_days must be a non-negative integer")

    log_override = os.environ.get("PAPER_SIGNAL_LOG_PATH", "").strip()
    if log_override:
        log_path = Path(log_override).expanduser()
    else:
        configured = str(raw.get("log_path", "")).strip()
        log_path = Path(configured) if configured else PAPER_SIGNAL_LOG_PATH
    if not log_path.is_absolute():
        log_path = PROJECT_ROOT / log_path
    return PaperSignalConfig(
        benchmark_ticker=benchmark,
        benchmark_name=name,
        cost_bps=cost,
        horizons_months=tuple(horizons),
        log_path=log_path,
        pending_session_lag_days=lag,
    )


def _record_from_line(line: str, line_number: int) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise PaperSignalLogError(f"paper signal log line {line_number} is not JSON") from exc
    if not isinstance(payload, dict):
        raise PaperSignalLogError(f"paper signal log line {line_number} is not an object")
    missing = [field for field in REQUIRED_FIELDS if field not in payload]
    if missing:
        raise PaperSignalLogError(
            f"paper signal log line {line_number} is missing {', '.join(missing)}"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise PaperSignalLogError(f"paper signal log line {line_number} has an unknown schema")
    if payload.get("signal") not in SIGNALS:
        raise PaperSignalLogError(f"paper signal log line {line_number} has an unknown signal")
    if payload.get("read_only") is not True:
        raise PaperSignalLogError(f"paper signal log line {line_number} is not marked read-only")
    return payload


def read_signals(path: Path | None = None) -> list[dict[str, Any]]:
    log_path = Path(path) if path is not None else load_config().log_path
    if not log_path.exists():
        return []
    with log_path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            raw = handle.read()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if raw == "":
        return []
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise PaperSignalLogError(f"paper signal log line {line_number} is blank")
        record = _record_from_line(line, line_number)
        signal_id = str(record["signal_id"])
        if signal_id in seen:
            raise PaperSignalLogError(f"paper signal log repeats signal_id {signal_id}")
        seen.add(signal_id)
        records.append(record)
    return records


def append_signal(record: Mapping[str, Any], path: Path | None = None) -> LogResult:
    """Append one view. The same identity with the same payload is a no-op."""
    log_path = Path(path) if path is not None else load_config().log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(record)
    body["schema_version"] = SCHEMA_VERSION
    body["read_only"] = True
    with log_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            existing_raw = handle.read()
            existing: list[dict[str, Any]] = []
            if existing_raw:
                for line_number, line in enumerate(existing_raw.splitlines(), start=1):
                    if not line.strip():
                        raise PaperSignalLogError(
                            f"paper signal log line {line_number} is blank"
                        )
                    existing.append(_record_from_line(line, line_number))
            identity = {field: body[field] for field in IDENTITY_FIELDS}
            for prior in existing:
                if {field: prior.get(field) for field in IDENTITY_FIELDS} != identity:
                    continue
                if all(prior.get(field) == body.get(field) for field in PAYLOAD_FIELDS):
                    return LogResult(False, "already_recorded", str(prior["signal_id"]))
                raise PaperSignalLogConflict(
                    "paper signal log already has a different view for "
                    f"{body['agent']} {body['ticker']} run {body['run_id']}"
                )
            line = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return LogResult(True, "recorded", str(body["signal_id"]))


def _build_record(
    *,
    ticker: str,
    signal: str,
    score_rule: str,
    price: str,
    price_source: str,
    price_field: str,
    price_via: str,
    price_currency: str,
    rationale: str,
    agent: str,
    source_view: str,
    direction: str,
    position_side: str,
    position_id: str,
    signalled_at: str,
    run_id: str,
) -> dict[str, Any]:
    moment = _require_utc(signalled_at, "signalled_at")
    run = _clean_text(run_id)
    symbol = _clean_text(ticker).upper()
    if not run or not symbol:
        raise PaperSignalLogError("a paper signal needs a run id and a ticker")
    if signal not in SIGNALS:
        raise PaperSignalLogError("signal must be buy, sell, or hold")
    if score_rule not in SCORE_RULES:
        raise PaperSignalLogError("unknown score rule")
    now = datetime.now(UTC)
    return {
        "schema_version": SCHEMA_VERSION,
        "signal_id": uuid.uuid4().hex,
        "signalled_at": _utc_text(moment),
        "signal_date_utc": moment.date().isoformat(),
        "ticker": symbol,
        "signal": signal,
        "score_rule": score_rule,
        "price": price,
        "price_source": price_source,
        "price_field": price_field,
        "price_via": price_via,
        "price_currency": price_currency,
        "rationale": _redact(_clean_text(rationale)),
        "agent": agent,
        "source_view": source_view,
        "direction": _clean_text(direction).lower(),
        "position_side": _clean_text(position_side).lower(),
        "position_id": _clean_text(position_id),
        "run_id": run,
        "logged_at": _utc_text(now),
        "read_only": True,
    }


def log_advisory_view(
    *,
    ticker: str,
    advice_status: object,
    direction: object,
    price: object,
    currency: object,
    rationale: object,
    signalled_at: str,
    run_id: str,
    path: Path | None = None,
) -> LogResult:
    signal = advisory_signal(advice_status, direction)
    if signal is None:
        return LogResult(False, "not_a_buy_sell_hold_view")
    price_text = _positive_price(price)
    if price_text is None:
        print(
            "paper signal log: not recorded: "
            f"Advisory Agent {ticker} {advice_status} has no price"
        )
        return LogResult(False, "missing_price")
    side = str(direction or "").strip().lower()
    rule = advisory_score_rule(advice_status, direction)
    if side == "short":
        rule = "sell_long_versus_benchmark"
    record = _build_record(
        ticker=str(ticker),
        signal=signal,
        score_rule=rule,
        price=price_text,
        price_source=ADVISORY_PRICE_SOURCE,
        price_field=ADVISORY_PRICE_FIELD,
        price_via=ADVISORY_PRICE_VIA,
        price_currency=_resolve_currency(str(ticker), currency),
        rationale=str(rationale or ""),
        agent="Advisory Agent",
        source_view=str(advice_status).strip().lower(),
        direction=side,
        position_side=side,
        position_id="",
        signalled_at=signalled_at,
        run_id=run_id,
    )
    if side == "short":
        record["score_rule"] = "sell_long_versus_benchmark"
        record["unscored_reason"] = "short_not_scored"
    return append_signal(record, path=path)


def log_exit_view(
    *,
    ticker: str,
    exit_action: object,
    status: object,
    exit_reason: object,
    position_side: object,
    price: object,
    rationale: object,
    signalled_at: str,
    run_id: str,
    position_id: str,
    path: Path | None = None,
) -> LogResult:
    signal = exit_signal(exit_action, status, exit_reason)
    if signal is None:
        return LogResult(False, "not_a_buy_sell_hold_view")
    price_text = _positive_price(price)
    if price_text is None:
        print(
            "paper signal log: not recorded: "
            f"Exit Agent {ticker} {exit_action} has no price"
        )
        return LogResult(False, "missing_price")
    side = str(position_side or "").strip().lower() or "long"
    short = side != "long"
    rule = "keep_long_versus_benchmark" if signal == "hold" else "sell_long_versus_benchmark"
    record = _build_record(
        ticker=str(ticker),
        signal=signal,
        score_rule=rule,
        price=price_text,
        price_source=EXIT_PRICE_SOURCE,
        price_field=EXIT_PRICE_FIELD,
        price_via=EXIT_PRICE_VIA,
        price_currency=_resolve_currency(str(ticker), ""),
        rationale=str(rationale or ""),
        agent="Exit Agent",
        source_view=str(exit_action).strip().lower(),
        direction=side,
        position_side=side,
        position_id=str(position_id or ""),
        signalled_at=signalled_at,
        run_id=run_id,
    )
    if short:
        record["unscored_reason"] = "short_not_scored"
    return append_signal(record, path=path)


def _column_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if isinstance(frame.columns, pd.MultiIndex):
        flattened = frame.copy()
        flattened.columns = flattened.columns.get_level_values(0)
        return flattened
    return frame


def _series_at(frame: pd.DataFrame, name: str) -> pd.Series | None:
    if name not in frame.columns:
        return None
    column = frame[name]
    if isinstance(column, pd.DataFrame):
        column = column.iloc[:, 0]
    return column


def history_from_frame(frame: pd.DataFrame) -> PriceHistory:
    table = _column_frame(frame)
    if not isinstance(table.index, pd.DatetimeIndex):
        date_column = "Date" if "Date" in table.columns else "date" if "date" in table.columns else ""
        if not date_column:
            return PriceHistory((), None, "price history has no session dates")
        table = table.copy()
        table.index = pd.to_datetime(table[date_column])
    close = _series_at(table, "Close")
    if close is None:
        return PriceHistory((), None, "price history has no Close column")
    splits = _series_at(table, "Stock Splits")
    bars: list[CloseBar] = []
    split_dates: list[date] | None = None if splits is None else []
    for index, raw_close in close.items():
        try:
            if pd.isna(raw_close):
                continue
        except TypeError:
            pass
        try:
            price = _decimal(raw_close, "close")
        except PaperSignalLogError:
            parsed_splits = None if split_dates is None else tuple(split_dates)
            return PriceHistory((), parsed_splits, "close is not a decimal")
        if price <= 0:
            continue
        # Yahoo delivers binary floats. Keep eight decimal places, half-even.
        price = price.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)
        session = session_date_of(index)
        bars.append(CloseBar(session, price))
        if splits is not None:
            raw_split = splits.loc[index]
            try:
                missing_split = pd.isna(raw_split)
            except TypeError:
                missing_split = False
            if not missing_split:
                try:
                    split = _decimal(raw_split, "split")
                except PaperSignalLogError:
                    return PriceHistory((), None, "stock split is not a decimal")
                if split not in {Decimal(0), Decimal(1)}:
                    split_dates.append(session)
    bars.sort(key=lambda bar: bar.session_date)
    deduped: list[CloseBar] = []
    for bar in bars:
        if deduped and deduped[-1].session_date == bar.session_date:
            deduped[-1] = bar
        else:
            deduped.append(bar)
    return PriceHistory(
        tuple(deduped),
        None if split_dates is None else tuple(sorted(set(split_dates))),
        None,
    )


def _last_close_before(bars: tuple[CloseBar, ...], signal_day: date) -> CloseBar | None:
    prior = [bar for bar in bars if bar.session_date < signal_day]
    return prior[-1] if prior else None


def _first_known_close(
    bars: tuple[CloseBar, ...],
    horizon: date,
    evaluation_day: date,
) -> CloseBar | None:
    eligible = [
        bar for bar in bars if horizon <= bar.session_date < evaluation_day
    ]
    return eligible[0] if eligible else None


def _quantize_return(value: Decimal) -> Decimal:
    return value.quantize(RETURN_QUANTUM, rounding=ROUND_HALF_EVEN)


def _simple_return(start: Decimal, end: Decimal) -> Decimal:
    return (end - start) / start


def _score_excess(
    rule: str,
    ticker_gross: Decimal,
    benchmark_gross: Decimal,
    cost_rate: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    if rule == "buy_versus_benchmark":
        excess = _quantize_return((ticker_gross - cost_rate) - benchmark_gross)
        return excess, cost_rate, excess > 0
    if rule == "sell_long_versus_benchmark":
        excess = _quantize_return((benchmark_gross - cost_rate) - ticker_gross)
        return excess, cost_rate, excess > 0
    if rule == "keep_long_versus_benchmark":
        excess = _quantize_return(ticker_gross - benchmark_gross)
        return excess, Decimal(0), excess >= 0
    if rule == "do_not_enter_versus_benchmark":
        excess = _quantize_return(benchmark_gross - ticker_gross)
        return excess, Decimal(0), excess >= 0
    raise PaperSignalLogError(f"unknown score rule {rule}")


def _blank_outcome(record: Mapping[str, Any], horizon: int, horizon_day: date, **fields: str) -> HorizonOutcome:
    base = {
        "signal_id": str(record["signal_id"]),
        "signalled_at": str(record["signalled_at"]),
        "ticker": str(record["ticker"]),
        "signal": str(record["signal"]),
        "score_rule": str(record.get("score_rule", "")),
        "horizon_months": horizon,
        "horizon_date": horizon_day.isoformat(),
        "status": "",
        "detail": "",
        "signal_price": str(record.get("price", "")),
        "outcome_price": "",
        "outcome_session": "",
        "benchmark_ticker": "",
        "benchmark_entry_price": "",
        "benchmark_outcome_price": "",
        "benchmark_outcome_session": "",
        "ticker_return": "",
        "benchmark_return": "",
        "ticker_return_local": "",
        "benchmark_return_local": "",
        "cost_applied_bps": "",
        "excess_return": "",
        "hit": "",
        "currency_basis": "",
        "ticker_currency": str(record.get("price_currency", "")),
        "benchmark_currency": "",
    }
    base.update(fields)
    return HorizonOutcome(**base)


def _fetch_history(
    ticker: str,
    start: date,
    end: date,
    provider: Any | None,
) -> PriceHistory:
    try:
        result = fetch_price_history(
            ticker,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=True,
            provider=provider,
        )
    except Exception as exc:
        return PriceHistory((), None, summarize_provider_error(exc) or "price fetch failed")
    if result.metadata.error:
        return PriceHistory((), None, summarize_provider_error(result.metadata.error) or "price fetch failed")
    parsed = history_from_frame(result.data)
    if parsed.error:
        return parsed
    if not parsed.bars:
        return PriceHistory((), parsed.split_dates, "empty price history")
    return parsed


def _default_currency(ticker: str) -> tuple[str, str | None]:
    try:
        import yfinance as yf

        fast = yf.Ticker(ticker).fast_info
        raw = fast.get("currency") if hasattr(fast, "get") else getattr(fast, "currency", None)
        text = _currency_text(raw)
        return text, None
    except Exception as exc:
        return "", summarize_provider_error(exc) or "currency lookup failed"


def evaluate_signals(
    records: list[Mapping[str, Any]],
    config: PaperSignalConfig,
    *,
    as_of: datetime,
    provider: Any | None = None,
    currency_lookup: Callable[[str], tuple[str, str | None]] | None = None,
) -> Evaluation:
    moment = _require_utc(as_of.isoformat(), "as_of")
    evaluation_day = moment.date()
    lookup = currency_lookup or _default_currency
    histories: dict[str, PriceHistory] = {}
    fetched_from: dict[str, date] = {}
    currencies: dict[str, tuple[str, str | None]] = {}

    def history_for(ticker: str, start: date) -> PriceHistory:
        previous = fetched_from.get(ticker)
        if previous is not None and previous <= start:
            return histories[ticker]
        loaded = _fetch_history(ticker, start, evaluation_day, provider)
        histories[ticker] = loaded
        fetched_from[ticker] = start
        return loaded

    def currency_for(ticker: str) -> tuple[str, str | None]:
        cached = currencies.get(ticker)
        if cached is None:
            cached = lookup(ticker)
            currencies[ticker] = cached
        return cached

    outcomes: list[HorizonOutcome] = []
    earliest_start: date | None = None
    needs_fetch = False
    prepared: list[tuple[Mapping[str, Any], date, list[tuple[int, date, str]]]] = []
    for record in records:
        signal_day = date.fromisoformat(str(record["signal_date_utc"]))
        horizons: list[tuple[int, date, str]] = []
        for months in config.horizons_months:
            horizon_day = add_months(signal_day, months)
            if evaluation_day < horizon_day:
                horizons.append((months, horizon_day, "pending"))
            else:
                horizons.append((months, horizon_day, "due"))
                if record.get("unscored_reason") != "short_not_scored":
                    needs_fetch = True
        if any(state == "due" for _, _, state in horizons) and record.get("unscored_reason") != "short_not_scored":
            start = signal_day - timedelta(days=40)
            earliest_start = start if earliest_start is None else min(earliest_start, start)
        prepared.append((record, signal_day, horizons))

    if needs_fetch and earliest_start is not None:
        history_for(config.benchmark_ticker, earliest_start)

    benchmark_currency, benchmark_currency_error = ("", None)
    if needs_fetch:
        benchmark_currency, benchmark_currency_error = currency_for(config.benchmark_ticker)

    fx_start = earliest_start

    def fx_history(pair: str) -> PriceHistory:
        if fx_start is None:
            return PriceHistory((), None, "FX window was not opened")
        return history_for(pair, fx_start)

    for record, signal_day, horizons in prepared:
        if record.get("unscored_reason") == "short_not_scored":
            for months, horizon_day, state in horizons:
                status = "pending" if state == "pending" else "short_not_scored"
                detail = (
                    "horizon_not_elapsed"
                    if state == "pending"
                    else "short views are logged and not given a long-only score"
                )
                outcomes.append(
                    _blank_outcome(
                        record,
                        months,
                        horizon_day,
                        status=status,
                        detail=detail,
                        benchmark_ticker=config.benchmark_ticker,
                    )
                )
            continue
        due = any(state == "due" for _, _, state in horizons)
        ticker_history = (
            history_for(str(record["ticker"]), signal_day - timedelta(days=40)) if due else None
        )
        logged_currency = str(record.get("price_currency", ""))
        feed_currency, feed_currency_error = currency_for(str(record["ticker"])) if due else ("", None)
        if (
            logged_currency
            and feed_currency
            and _quote_unit(logged_currency) != _quote_unit(feed_currency)
        ):
            feed_currency_error = (
                f"quote_currency_conflict: logged {logged_currency} but the outcome feed "
                f"reports {feed_currency}; row not scored and no rescaling was applied"
            )
        ticker_currency = logged_currency or feed_currency
        for months, horizon_day, state in horizons:
            if state == "pending":
                outcomes.append(
                    _blank_outcome(
                        record,
                        months,
                        horizon_day,
                        status="pending",
                        detail="horizon_not_elapsed",
                        benchmark_ticker=config.benchmark_ticker,
                        benchmark_currency=benchmark_currency,
                        ticker_currency=ticker_currency,
                    )
                )
                continue
            outcome = _score_due_horizon(
                record=record,
                config=config,
                signal_day=signal_day,
                months=months,
                horizon_day=horizon_day,
                evaluation_day=evaluation_day,
                ticker_history=ticker_history,
                benchmark_history=histories.get(config.benchmark_ticker),
                ticker_currency=ticker_currency,
                benchmark_currency=benchmark_currency,
                currency_note=feed_currency_error or benchmark_currency_error or "",
                fx_history=fx_history,
            )
            outcomes.append(outcome)

    summaries = _summaries(outcomes, config)
    return Evaluation(
        as_of=_utc_text(moment),
        benchmark_ticker=config.benchmark_ticker,
        benchmark_name=config.benchmark_name,
        cost_bps=decimal_text(config.cost_bps),
        outcome_price_basis=OUTCOME_PRICE_BASIS,
        outcomes=tuple(outcomes),
        summaries=tuple(summaries),
    )


def _session_still_pending(
    history: PriceHistory,
    horizon_day: date,
    evaluation_day: date,
    lag_days: int,
) -> bool:
    """True when a close on or after the horizon may still arrive."""
    lag = timedelta(days=lag_days)
    latest = history.bars[-1].session_date if history.bars else None
    within_lag = evaluation_day - horizon_day <= lag
    feed_is_current = latest is not None and evaluation_day - latest <= lag
    # No known session on or after the horizon yet. A bar dated the
    # evaluation day is not known. While the horizon is recent and the
    # feed's latest session is recent, keep waiting instead of estimating.
    return within_lag and (
        latest is None or latest < horizon_day or latest >= evaluation_day
    ) and (feed_is_current or latest is None or latest >= evaluation_day)


def _to_gbp(local: Decimal, currency: str, fx_close: Decimal | None) -> Decimal:
    """Listing quote to GBP. Pence divide by 100. FX close is the Yahoo rate."""
    _major, pence = _quote_unit(currency)
    amount = local / PENCE_PER_POUND if pence else local
    if fx_close is not None:
        _pair, direction = FX_TO_GBP[_quote_unit(currency)[0]]
        amount = amount / fx_close if direction == "divide" else amount * fx_close
    return amount.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _fx_clause(
    currency: str,
    signal_bar: CloseBar | None,
    outcome_bar: CloseBar | None,
    start_gbp: Decimal,
    end_gbp: Decimal,
) -> str:
    major, pence = _quote_unit(currency)
    parts: list[str] = []
    if pence:
        parts.append("GBp/100")
    spec = FX_TO_GBP.get(major)
    if spec is not None and signal_bar is not None and outcome_bar is not None:
        pair, _direction = spec
        parts.append(
            f"{pair} {decimal_text(signal_bar.close)}@{signal_bar.session_date.isoformat()}"
            f" -> {decimal_text(outcome_bar.close)}@{outcome_bar.session_date.isoformat()}"
        )
    parts.append(f"GBP {decimal_text(start_gbp)} -> {decimal_text(end_gbp)}")
    return " ".join(parts)


def _resolve_fx(
    currency: str,
    *,
    label: str,
    signal_day: date,
    horizon_day: date,
    evaluation_day: date,
    lag_days: int,
    fx_history: Callable[[str], PriceHistory],
) -> tuple[str, str, CloseBar | None, CloseBar | None]:
    """Return status, detail, signal close, outcome close.

    Status is ``ok`` for GBP and for a pair with both closes. GBP has no bars.
    """
    major, _pence = _quote_unit(currency)
    if major == "GBP":
        return "ok", "", None, None
    spec = FX_TO_GBP.get(major)
    if spec is None:
        return "fx_missing", f"no GBP FX pair for {major or label}", None, None
    pair, _direction = spec
    history = fx_history(pair)
    if history.error:
        return "fx_missing", f"{pair}: {history.error}", None, None
    signal_bar = _last_close_before(history.bars, signal_day)
    if signal_bar is None:
        return (
            "fx_missing",
            f"no {pair} close with session date before the signal UTC date",
            None,
            None,
        )
    outcome_bar = _first_known_close(history.bars, horizon_day, evaluation_day)
    if outcome_bar is None:
        if _session_still_pending(history, horizon_day, evaluation_day, lag_days):
            return "pending", f"awaiting_fx_close: {pair}", signal_bar, None
        return "fx_missing", f"no {pair} close on or after the horizon", signal_bar, None
    if signal_bar.close <= 0 or outcome_bar.close <= 0:
        return "fx_missing", f"{pair} close is not positive", signal_bar, outcome_bar
    return "ok", "", signal_bar, outcome_bar


def _local_return_fields(
    signal_price: Decimal,
    ticker_outcome: CloseBar,
    benchmark_entry: CloseBar,
    benchmark_outcome: CloseBar,
) -> dict[str, str]:
    return {
        "outcome_price": decimal_text(ticker_outcome.close),
        "outcome_session": ticker_outcome.session_date.isoformat(),
        "benchmark_entry_price": decimal_text(benchmark_entry.close),
        "benchmark_outcome_price": decimal_text(benchmark_outcome.close),
        "benchmark_outcome_session": benchmark_outcome.session_date.isoformat(),
        "ticker_return_local": decimal_text(
            _quantize_return(_simple_return(signal_price, ticker_outcome.close))
        ),
        "benchmark_return_local": decimal_text(
            _quantize_return(_simple_return(benchmark_entry.close, benchmark_outcome.close))
        ),
    }


def _score_due_horizon(
    *,
    record: Mapping[str, Any],
    config: PaperSignalConfig,
    signal_day: date,
    months: int,
    horizon_day: date,
    evaluation_day: date,
    ticker_history: PriceHistory | None,
    benchmark_history: PriceHistory | None,
    ticker_currency: str,
    benchmark_currency: str,
    currency_note: str,
    fx_history: Callable[[str], PriceHistory],
) -> HorizonOutcome:
    common = {
        "benchmark_ticker": config.benchmark_ticker,
        "ticker_currency": ticker_currency,
        "benchmark_currency": benchmark_currency,
    }
    if record.get("unscored_reason") == "short_not_scored":
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="short_not_scored",
            detail="short views are logged and not given a long-only score",
            **common,
        )
    if currency_note.startswith("quote_currency_conflict:"):
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="quote_currency_conflict",
            detail=currency_note,
            **common,
        )
    rule = str(record.get("score_rule", ""))
    if rule not in SCORE_RULES:
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="invalid_signal",
            detail="score rule is missing",
            **common,
        )
    try:
        signal_price = _decimal(record.get("price"), "signal price")
    except PaperSignalLogError:
        signal_price = Decimal(0)
    if signal_price <= 0:
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="invalid_signal_price",
            detail="logged signal price is missing or not positive",
            **common,
        )
    if ticker_history is None or ticker_history.error:
        detail = ticker_history.error if ticker_history is not None else "price history was not fetched"
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="data_feed_error",
            detail=f"{record['ticker']}: {detail}",
            **common,
        )
    if benchmark_history is None or benchmark_history.error:
        detail = (
            benchmark_history.error
            if benchmark_history is not None
            else "benchmark history was not fetched"
        )
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="data_feed_error",
            detail=f"{config.benchmark_ticker}: {detail}",
            **common,
        )
    benchmark_entry = _last_close_before(benchmark_history.bars, signal_day)
    if benchmark_entry is None:
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="benchmark_entry_missing",
            detail="no benchmark close with session date before the signal UTC date",
            **common,
        )
    ticker_outcome = _first_known_close(ticker_history.bars, horizon_day, evaluation_day)
    benchmark_outcome = _first_known_close(benchmark_history.bars, horizon_day, evaluation_day)
    if ticker_outcome is None or benchmark_outcome is None:
        return _missing_outcome(
            record,
            config,
            months,
            horizon_day,
            evaluation_day,
            ticker_history,
            benchmark_history,
            ticker_outcome,
            benchmark_outcome,
            common,
        )
    split_dates = tuple(ticker_history.split_dates or ()) + tuple(benchmark_history.split_dates or ())
    if ticker_history.split_dates is None or benchmark_history.split_dates is None:
        split_note = "split_check_unavailable"
    else:
        split_note = ""
    if any(split_day >= signal_day for split_day in split_dates):
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="split_basis_mismatch",
            detail="a split on or after the signal date restates Yahoo Close; row not scored",
            benchmark_entry_price=decimal_text(benchmark_entry.close),
            **common,
        )
    local_fields = _local_return_fields(
        signal_price, ticker_outcome, benchmark_entry, benchmark_outcome
    )
    if not ticker_currency or not benchmark_currency:
        detail = "currency is missing; no FX rate was assumed"
        if currency_note:
            detail = f"{detail}; {currency_note}"
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="currency_unconfirmed",
            detail=detail,
            currency_basis="currency_unconfirmed",
            **local_fields,
            **common,
        )
    ticker_fx = _resolve_fx(
        ticker_currency,
        label=str(record["ticker"]),
        signal_day=signal_day,
        horizon_day=horizon_day,
        evaluation_day=evaluation_day,
        lag_days=config.pending_session_lag_days,
        fx_history=fx_history,
    )
    benchmark_fx = _resolve_fx(
        benchmark_currency,
        label=config.benchmark_ticker,
        signal_day=signal_day,
        horizon_day=horizon_day,
        evaluation_day=evaluation_day,
        lag_days=config.pending_session_lag_days,
        fx_history=fx_history,
    )
    for status, detail, _signal_bar, _outcome_bar in (ticker_fx, benchmark_fx):
        if status == "ok":
            continue
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status=status,
            detail=detail,
            currency_basis="fx_missing" if status == "fx_missing" else "",
            **local_fields,
            **common,
        )
    ticker_signal_fx = ticker_fx[2].close if ticker_fx[2] is not None else None
    ticker_outcome_fx = ticker_fx[3].close if ticker_fx[3] is not None else None
    benchmark_signal_fx = benchmark_fx[2].close if benchmark_fx[2] is not None else None
    benchmark_outcome_fx = benchmark_fx[3].close if benchmark_fx[3] is not None else None
    signal_gbp = _to_gbp(signal_price, ticker_currency, ticker_signal_fx)
    outcome_gbp = _to_gbp(ticker_outcome.close, ticker_currency, ticker_outcome_fx)
    benchmark_entry_gbp = _to_gbp(benchmark_entry.close, benchmark_currency, benchmark_signal_fx)
    benchmark_outcome_gbp = _to_gbp(
        benchmark_outcome.close, benchmark_currency, benchmark_outcome_fx
    )
    if signal_gbp <= 0 or benchmark_entry_gbp <= 0:
        return _blank_outcome(
            record,
            months,
            horizon_day,
            status="fx_missing",
            detail="GBP price is not positive",
            currency_basis="fx_missing",
            **local_fields,
            **common,
        )
    ticker_gross = _simple_return(signal_gbp, outcome_gbp)
    benchmark_gross = _simple_return(benchmark_entry_gbp, benchmark_outcome_gbp)
    cost_rate = config.cost_bps / Decimal(10000)
    excess, cost_applied, hit = _score_excess(rule, ticker_gross, benchmark_gross, cost_rate)
    detail_parts = [
        "gbp",
        "ticker " + _fx_clause(ticker_currency, ticker_fx[2], ticker_fx[3], signal_gbp, outcome_gbp),
        "benchmark "
        + _fx_clause(
            benchmark_currency,
            benchmark_fx[2],
            benchmark_fx[3],
            benchmark_entry_gbp,
            benchmark_outcome_gbp,
        ),
    ]
    if split_note:
        detail_parts.append(split_note)
    if currency_note:
        detail_parts.append(currency_note)
    return _blank_outcome(
        record,
        months,
        horizon_day,
        status="scored",
        detail="; ".join(detail_parts),
        ticker_return=decimal_text(_quantize_return(ticker_gross)),
        benchmark_return=decimal_text(_quantize_return(benchmark_gross)),
        cost_applied_bps=decimal_text((cost_applied * Decimal(10000)).normalize()),
        excess_return=decimal_text(excess),
        hit="hit" if hit else "miss",
        currency_basis="gbp",
        **local_fields,
        **common,
    )


def _missing_outcome(
    record: Mapping[str, Any],
    config: PaperSignalConfig,
    months: int,
    horizon_day: date,
    evaluation_day: date,
    ticker_history: PriceHistory,
    benchmark_history: PriceHistory,
    ticker_outcome: CloseBar | None,
    benchmark_outcome: CloseBar | None,
    common: dict[str, str],
) -> HorizonOutcome:
    missing_names = []
    pending = True
    for label, history, found in (
        (str(record["ticker"]), ticker_history, ticker_outcome),
        (config.benchmark_ticker, benchmark_history, benchmark_outcome),
    ):
        if found is not None:
            continue
        missing_names.append(label)
        if not _session_still_pending(
            history, horizon_day, evaluation_day, config.pending_session_lag_days
        ):
            pending = False
    status = "pending" if pending else "outcome_missing"
    detail = (
        "awaiting_session_close: " + ", ".join(missing_names)
        if pending
        else "no close on or after the horizon: " + ", ".join(missing_names)
    )
    return _blank_outcome(record, months, horizon_day, status=status, detail=detail, **common)


def _summaries(outcomes: list[HorizonOutcome], config: PaperSignalConfig) -> list[HorizonSummary]:
    groups = ["all", "buy", "sell", "hold"]
    rows: list[HorizonSummary] = []
    for months in config.horizons_months:
        for group in groups:
            selected = [
                row
                for row in outcomes
                if row.horizon_months == months and (group == "all" or row.signal == group)
            ]
            if group != "all" and not selected:
                continue
            rows.append(_summarize(months, group, selected, config.cost_bps))
    return rows


def _summarize(
    months: int,
    group: str,
    rows: list[HorizonOutcome],
    cost_bps: Decimal,
) -> HorizonSummary:
    pending = sum(1 for row in rows if row.status == "pending")
    scored_rows = [row for row in rows if row.status == "scored"]
    flagged = len(rows) - pending - len(scored_rows)
    hits = sum(1 for row in scored_rows if row.hit == "hit")
    if scored_rows:
        hit_rate = _percent_text(_quantize_return(Decimal(hits) / Decimal(len(scored_rows))))
        excess_values = [_decimal(row.excess_return, "excess") for row in scored_rows]
        mean_excess = _percent_text(_quantize_return(sum(excess_values, Decimal(0)) / Decimal(len(excess_values))))
    else:
        hit_rate = "n/a"
        mean_excess = "n/a"
    return HorizonSummary(
        horizon_months=months,
        signal_group=group,
        signals=len(rows),
        pending=pending,
        flagged=flagged,
        scored=len(scored_rows),
        hits=hits,
        hit_rate=hit_rate,
        mean_excess_return=mean_excess,
        cost_bps=decimal_text(cost_bps),
        gbp=sum(1 for row in scored_rows if row.currency_basis == "gbp"),
        fx_missing=sum(1 for row in rows if row.status == "fx_missing"),
    )


def format_report(records: list[Mapping[str, Any]], evaluation: Evaluation) -> str:
    lines: list[str] = []
    lines.append("Paper signal log (read only; no order was sent)")
    lines.append(
        f"Evaluation UTC: {evaluation.as_of} | benchmark: {evaluation.benchmark_ticker} "
        f"({evaluation.benchmark_name})"
    )
    lines.append(
        f"Cost assumption: {evaluation.cost_bps} bps round trip on buy and sell; "
        "0 bps on hold. Not a measured broker fee."
    )
    lines.append(f"Outcome price basis: {evaluation.outcome_price_basis}")
    lines.append(
        f"Reporting currency: {REPORTING_CURRENCY}. Excess and hit use GBP price returns. "
        "ticker_return_local and benchmark_return_local stay in the listing quote."
    )
    lines.append("")
    lines.append("Log")
    if not records:
        lines.append("(no signals recorded)")
    else:
        lines.append(
            "signalled_at | ticker | signal | price | currency | source | rationale"
        )
        for record in records:
            rationale = str(record.get("rationale", ""))
            if len(rationale) > 80:
                rationale = rationale[:77] + "..."
            lines.append(
                " | ".join(
                    [
                        str(record.get("signalled_at", "")),
                        str(record.get("ticker", "")),
                        str(record.get("signal", "")),
                        str(record.get("price", "")),
                        str(record.get("price_currency", "")) or "(currency not on the view)",
                        str(record.get("price_source", "")),
                        rationale or "(no rationale on the view)",
                    ]
                )
            )
    lines.append("")
    lines.append("Outcomes")
    lines.append(
        "ticker | signal | horizon | status | hit | excess | ticker_return_gbp | "
        "benchmark_return_gbp | ticker_return_local | benchmark_return_local | "
        "currency_basis | detail"
    )
    if not evaluation.outcomes:
        lines.append("(no outcomes)")
    for row in evaluation.outcomes:
        lines.append(
            " | ".join(
                [
                    row.ticker,
                    row.signal,
                    f"{row.horizon_months}m",
                    row.status,
                    row.hit or "-",
                    row.excess_return or "-",
                    row.ticker_return or "-",
                    row.benchmark_return or "-",
                    row.ticker_return_local or "-",
                    row.benchmark_return_local or "-",
                    row.currency_basis or "-",
                    row.detail,
                ]
            )
        )
    lines.append("")
    lines.append("Summary by horizon")
    lines.append(
        "horizon | group | signals | pending | flagged | scored | hits | "
        "hit_rate | mean_excess_gbp | gbp | fx_missing | cost_bps"
    )
    for row in evaluation.summaries:
        lines.append(
            " | ".join(
                [
                    f"{row.horizon_months}m",
                    row.signal_group,
                    str(row.signals),
                    str(row.pending),
                    str(row.flagged),
                    str(row.scored),
                    str(row.hits),
                    row.hit_rate,
                    row.mean_excess_return,
                    str(row.gbp),
                    str(row.fx_missing),
                    row.cost_bps,
                ]
            )
        )
    lines.append("")
    lines.append(DEFINITIONS.rstrip())
    lines.append("")
    return "\n".join(lines)


def format_markdown(records: list[Mapping[str, Any]], evaluation: Evaluation) -> str:
    text = format_report(records, evaluation)
    sections = text.split("\n\n")
    chunks = ["# Paper signal log", ""]
    for section in sections:
        rows = section.splitlines()
        if not rows:
            continue
        title = rows[0]
        body = rows[1:]
        if title in {"Log", "Outcomes", "Summary by horizon"} and body:
            chunks.append(f"## {title}")
            chunks.append("")
            header = body[0]
            chunks.append("| " + header.replace(" | ", " | ") + " |")
            chunks.append("| " + " | ".join("---" for _ in header.split(" | ")) + " |")
            for line in body[1:]:
                if line.startswith("("):
                    chunks.append("")
                    chunks.append(line)
                    continue
                chunks.append("| " + line + " |")
            chunks.append("")
        else:
            chunks.append(title if title.startswith("Paper") or title.startswith("Evaluation") or title.startswith("Cost") or title.startswith("Outcome price") else f"## {title}")
            chunks.append("")
            if body:
                chunks.extend(body)
                chunks.append("")
    return "\n".join(chunks).rstrip() + "\n"


def evaluate_log(
    config: PaperSignalConfig | None = None,
    *,
    as_of: datetime | None = None,
    provider: Any | None = None,
    currency_lookup: Callable[[str], tuple[str, str | None]] | None = None,
) -> tuple[list[dict[str, Any]], Evaluation]:
    resolved = config or load_config()
    records = read_signals(resolved.log_path)
    evaluation = evaluate_signals(
        records,
        resolved,
        as_of=as_of or datetime.now(UTC),
        provider=provider,
        currency_lookup=currency_lookup,
    )
    return records, evaluation
