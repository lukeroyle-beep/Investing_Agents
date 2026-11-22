"""Read-only comparison of a few pre-registered strategy variants.

The point-in-time replay, its coverage gate, and its cost model are the
backtester's. GBP conversion uses the paper log's FX pairs. This module
does not import the demo order path or a broker, does not edit strategy
config, and does not send an order. A missing paper log or demo log is
reported as ``no data yet``.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from agents.backtesting_agent import backtesting_agent as backtest
from shared.io_utils import load_yaml
from shared.paper_signal_log import (
    FX_TO_GBP,
    _quote_unit,
    _to_gbp,
    evaluate_log,
    history_from_frame,
    load_config as load_paper_config,
    read_signals,
)
from shared.paths import BROKER_FILLS_PATH, project_path, runtime_path
from strategy.domain import StrategyValidationError
from strategy.validation import chronological_split


class ImprovementLoopError(ValueError):
    """The comparison cannot be reported without hiding a data problem."""


# Fixed search size. A longer config is an error, not a bigger search.
MAX_VARIANTS = 8
PARAMETER_KEYS = (
    "ma_fast_window",
    "ma_slow_window",
    "return_lookback_window",
    "stop_loss_pct",
    "take_profit_pct",
    "max_hold_days",
    "position_size_pct",
)
_INTEGER_PARAMETERS = (
    "ma_fast_window",
    "ma_slow_window",
    "return_lookback_window",
    "max_hold_days",
)
_SECRET_FIELDS = {
    "api_key",
    "user_key",
    "authorization",
    "x_api_key",
    "x_user_key",
    "credential",
    "secret",
    "password",
    "token",
    "headers",
}
_NO_DATA = "no data yet"


def loop_config_path() -> Path:
    return project_path("config", "improvement_loop.yaml")


def load_loop_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or loop_config_path()
    if not config_path.is_file():
        raise ImprovementLoopError(f"improvement loop config is missing: {config_path}")
    raw = load_yaml(config_path)
    if not isinstance(raw, dict):
        raise ImprovementLoopError("improvement loop config must be a mapping")
    return raw


def build_variants(
    loop_config: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Resolve the pre-registered list. The test window is not an input."""
    raw_variants = loop_config.get("variants")
    raw_baselines = loop_config.get("baselines")
    if not isinstance(raw_variants, list) or not raw_variants:
        raise ImprovementLoopError("variants must be a non-empty list")
    if not isinstance(raw_baselines, list) or not raw_baselines:
        raise ImprovementLoopError("baselines must include buy-and-hold and do-nothing")
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_variants:
        specs.append(_parameter_variant(item, backtest_config, seen))
    for item in raw_baselines:
        specs.append(_baseline_variant(item, loop_config, seen))
    if len(specs) > MAX_VARIANTS:
        raise ImprovementLoopError(
            f"variant cap is {MAX_VARIANTS}, including baselines; got {len(specs)}"
        )
    kinds = {item["kind"] for item in specs}
    if "buy_and_hold" not in kinds or "do_nothing" not in kinds:
        raise ImprovementLoopError("baselines must include buy-and-hold and do-nothing")
    return specs


def _identifier(item: Mapping[str, Any]) -> str:
    identifier = str(item.get("id") or "").strip()
    if not identifier or any(character.isspace() for character in identifier):
        raise ImprovementLoopError("each variant needs an id without spaces")
    return identifier


def _parameter_variant(
    item: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
    seen: set[str],
) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ImprovementLoopError("a variant must be a mapping")
    identifier = _identifier(item)
    if identifier in seen:
        raise ImprovementLoopError(f"duplicate variant id: {identifier}")
    seen.add(identifier)
    overrides = item.get("parameters") or {}
    if not isinstance(overrides, Mapping):
        raise ImprovementLoopError(f"{identifier}: parameters must be a mapping")
    unknown = sorted(set(overrides) - set(PARAMETER_KEYS))
    if unknown:
        raise ImprovementLoopError(f"{identifier}: parameters are not signal or exit settings: {unknown}")
    parameters = {key: backtest_config[key] for key in PARAMETER_KEYS}
    parameters.update(overrides)
    for key in _INTEGER_PARAMETERS:
        value = parameters[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise ImprovementLoopError(f"{identifier}: {key} must be a positive integer")
        parameters[key] = int(value)
        if parameters[key] < 1:
            raise ImprovementLoopError(f"{identifier}: {key} must be a positive integer")
    return {"id": identifier, "kind": "parameter", "parameters": parameters, "ticker": ""}


def _baseline_variant(
    item: Mapping[str, Any],
    loop_config: Mapping[str, Any],
    seen: set[str],
) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ImprovementLoopError("a baseline must be a mapping")
    identifier = _identifier(item)
    if identifier in seen:
        raise ImprovementLoopError(f"duplicate variant id: {identifier}")
    seen.add(identifier)
    kind = str(item.get("kind") or "").strip()
    if kind == "do_nothing":
        return {"id": identifier, "kind": kind, "parameters": {}, "ticker": ""}
    if kind != "buy_and_hold":
        raise ImprovementLoopError(f"{identifier}: baseline kind must be buy_and_hold or do_nothing")
    ticker = str(item.get("ticker") or "").strip()
    currencies = loop_config.get("benchmark_currencies") or {}
    if not ticker or ticker not in currencies:
        raise ImprovementLoopError(f"{identifier}: buy-and-hold ticker needs a configured currency")
    return {"id": identifier, "kind": kind, "parameters": {}, "ticker": ticker}


def _session_dates(frame: pd.DataFrame) -> list[date]:
    dates = []
    for value in pd.to_datetime(frame["Date"]):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("UTC").tz_localize(None)
        dates.append(stamp.date())
    return dates


def fx_close_map(frame: pd.DataFrame, pair: str) -> dict[date, Decimal]:
    parsed = history_from_frame(frame)
    if parsed.error or not parsed.bars:
        raise ImprovementLoopError(f"fx_missing: {pair}: {parsed.error or 'empty price history'}")
    closes: dict[date, Decimal] = {}
    for bar in parsed.bars:
        closes[bar.session_date] = bar.close
    return closes


def convert_frame_to_gbp(
    frame: pd.DataFrame,
    currency: str,
    fx_by_session: Mapping[date, Decimal] | None,
    *,
    max_gap_days: int = 4,
) -> tuple[pd.DataFrame, tuple[date, ...]]:
    """Restate one daily bar in GBP.

    The rate is the paper log's pair for that session. If that session has no
    FX close, the latest earlier close is used when it is at most
    ``max_gap_days`` calendar days before, and that session is returned so the
    report can list it. A longer gap, or no earlier close, raises. Pence use
    no FX pair. Sessions are not dropped.
    """
    import bisect

    major, _pence = _quote_unit(currency)
    if not major:
        raise ImprovementLoopError("currency_unconfirmed")
    needs_fx = major != "GBP"
    if needs_fx and major not in FX_TO_GBP:
        raise ImprovementLoopError(f"fx_missing: no GBP FX pair for {major}")
    dates = _session_dates(frame)
    ordered = sorted(fx_by_session) if needs_fx and fx_by_session else []
    factors: list[float] = []
    carried: list[date] = []
    problems: list[str] = []
    for session in dates:
        fx_close = None
        if needs_fx:
            if fx_by_session and session in fx_by_session:
                fx_close = fx_by_session[session]
            else:
                index = bisect.bisect_right(ordered, session) - 1
                previous = ordered[index] if index >= 0 else None
                gap = None if previous is None else (session - previous).days
                if previous is None or gap is None or gap > max_gap_days or fx_by_session is None:
                    earlier = previous.isoformat() if previous is not None else "none"
                    problems.append(f"{session.isoformat()} (previous {earlier})")
                    continue
                fx_close = fx_by_session[previous]
                carried.append(session)
        factors.append(float(_to_gbp(Decimal("1"), currency, fx_close)))
    if problems:
        sample = ", ".join(problems[:8])
        raise ImprovementLoopError(
            f"fx_missing: {currency} has no FX close within {max_gap_days} days "
            f"on {len(problems)} sessions ({sample})"
        )
    out = frame.copy()
    factor = pd.Series(factors, index=out.index, dtype="float64")
    for column in ("Open", "High", "Low", "Close", "Dividends"):
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="raise") * factor
    return out.reset_index(drop=True), tuple(carried)


def _resolve_path(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = project_path(*path.parts)
    return path


def _paper_lane(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": _NO_DATA, "path": str(path), "return_gbp": _NO_DATA}
    records = read_signals(path)
    if not records:
        return {"status": _NO_DATA, "path": str(path), "return_gbp": _NO_DATA}
    paper = load_paper_config()
    if path != paper.log_path:
        paper = replace(paper, log_path=path)
    _records, evaluation = evaluate_log(paper)
    summaries = []
    for summary in evaluation.summaries:
        summaries.append(asdict(summary))
    return {
        "status": "present",
        "path": str(path),
        "signals": len(records),
        "cost_bps": evaluation.cost_bps,
        "benchmark": evaluation.benchmark_ticker,
        "return_gbp": _NO_DATA,
        "detail": (
            "Scored rows are GBP excess versus the paper benchmark after "
            "cost_bps. They are not a portfolio return, so return_gbp stays "
            "no data yet."
        ),
        "summaries": summaries,
    }


def _secret_key(key: object) -> bool:
    normalised = str(key).lower().replace("-", "_")
    if normalised in _SECRET_FIELDS:
        return True
    return any(part in normalised for part in ("api_key", "secret", "password", "authorization"))


def _jsonl_lane(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        return _absent_lane(path)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return _absent_lane(path)
    records = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            raise ImprovementLoopError(f"{label} line {line_number} is blank")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImprovementLoopError(f"{label} line {line_number} is not JSON") from exc
        if not isinstance(record, dict):
            raise ImprovementLoopError(f"{label} line {line_number} is not an object")
        if any(_secret_key(key) for key in record):
            raise ImprovementLoopError(f"{label} line {line_number} has a secret-shaped field")
        records.append(record)
    outcomes: dict[str, int] = {}
    for record in records:
        outcome = str(record.get("outcome") or "")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    fills = sum(1 for record in records if str(record.get("outcome") or "") == "fill")
    return {
        "status": "present",
        "path": str(path),
        "records": len(records),
        "trade_count": fills,
        "outcomes": outcomes,
        "return_gbp": _NO_DATA,
        "excess_gbp": _NO_DATA,
        "win_rate": _NO_DATA,
        "max_drawdown": _NO_DATA,
        "detail": (
            "Local log counts only. Fills are not a closed GBP equity curve, "
            "and this loop does not call a broker."
        ),
    }


def _broker_fills_lane(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _absent_lane(path)
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return _absent_lane(path)
            rows = list(reader)
    except OSError as exc:
        raise ImprovementLoopError(f"broker fills file cannot be read: {path}") from exc
    if not rows:
        return _absent_lane(path)
    return {
        "status": "present",
        "path": str(path),
        "trade_count": len(rows),
        "return_gbp": _NO_DATA,
        "excess_gbp": _NO_DATA,
        "win_rate": _NO_DATA,
        "max_drawdown": _NO_DATA,
        "detail": (
            "Fill rows are counted. They are not marked to a GBP equity curve "
            "here, and this loop does not call a broker."
        ),
    }


def _absent_lane(path: Path) -> dict[str, Any]:
    return {
        "status": _NO_DATA,
        "path": str(path),
        "trade_count": _NO_DATA,
        "return_gbp": _NO_DATA,
        "excess_gbp": _NO_DATA,
        "win_rate": _NO_DATA,
        "max_drawdown": _NO_DATA,
    }


def _buy_and_hold_drawdown(frame: pd.DataFrame, costs: Any) -> float:
    """Peak-to-trough of the held close, after entry cost, before the exit cost."""
    friction = float(costs.slippage_bps + costs.spread_bps / 2) / 10000
    fee = float(costs.fee_bps) / 10000
    tax = float(costs.tax_bps) / 10000
    entry = float(frame.iloc[0]["Open"]) * (1 + friction) * (1 + fee + tax)
    if entry <= 0:
        raise ImprovementLoopError("buy-and-hold entry is not positive")
    if "Dividends" in frame.columns:
        dividend_values = pd.to_numeric(frame["Dividends"], errors="coerce")
        if dividend_values.isna().any() or (dividend_values < 0).any():
            raise ImprovementLoopError("buy-and-hold dividends must be finite and non-negative")
    else:
        dividend_values = pd.Series([0.0] * len(frame))
    accrued = 0.0
    marks: list[float] = []
    for index, (close, dividend) in enumerate(zip(frame["Close"], dividend_values, strict=True)):
        if index > 0:
            accrued += float(dividend)
        marks.append((float(close) + accrued) / entry)
    series = pd.Series(marks, dtype="float64")
    drawdown = (series / series.cummax() - 1.0) * 100.0
    return float(drawdown.min())


def _window_frame(history: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    dates = pd.to_datetime(history["Date"])
    frame = history.loc[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))].reset_index(drop=True)
    if frame.empty:
        raise ImprovementLoopError(f"price history does not cover {start} to {end}")
    return frame


def buy_and_hold_metrics(history: pd.DataFrame, start: str, end: str, costs: Any) -> dict[str, Any]:
    total = backtest.partition_benchmark_return(history, start, end, costs)
    return_pct = round(float(total), 4)
    drawdown = _buy_and_hold_drawdown(_window_frame(history, start, end), costs)
    win_rate = None if return_pct == 0 else (100.0 if return_pct > 0 else 0.0)
    return {
        "return_pct": return_pct,
        "win_rate_pct": win_rate,
        "max_drawdown_pct": round(drawdown, 4),
        "trade_count": 1,
    }


def metrics_from_replay(replay: Any) -> dict[str, Any]:
    equity = replay.equity_curve
    if equity is None or equity.empty:
        raise ImprovementLoopError("replay produced no equity curve")
    trades = replay.accepted_trades
    if trades is None or trades.empty:
        win_rate = None
        trade_count = 0
    else:
        trade_count = int(len(trades))
        win_rate = round(float((trades["trade_return_pct"] > 0).mean() * 100.0), 2)
    drawdown = replay.drawdown
    max_drawdown = 0.0 if drawdown is None or drawdown.empty else round(float(drawdown["drawdown_pct"].min()), 4)
    return {
        "return_pct": round(float(equity.iloc[-1]["cumulative_return_pct"]), 4),
        "win_rate_pct": win_rate,
        "max_drawdown_pct": max_drawdown,
        "trade_count": trade_count,
        "rejected_count": 0 if replay.rejected_trades is None else int(len(replay.rejected_trades)),
    }


def _with_excess(metrics: dict[str, Any], benchmark_return: float) -> dict[str, Any]:
    benchmark = round(float(benchmark_return), 4)
    row = dict(metrics)
    row["excess_pct"] = round(row["return_pct"] - benchmark, 4)
    row["benchmark_return_pct"] = benchmark
    return row


def choose_proposal(variants: list[dict[str, Any]], *, qualified: bool) -> str | None:
    """Highest in-sample excess. Out-of-sample figures are not readable here."""
    if not qualified:
        return None
    ordered = sorted(variants, key=lambda row: (-float(row["in_sample"]["excess_pct"]), row["id"]))
    return str(ordered[0]["id"])


def oos_beats_baseline(kind: str, out_of_sample_excess: float) -> bool | None:
    """True when a parameter variant's out-of-sample excess is above zero.

    Zero is not a beat. Baselines are the comparison, not candidates.
    """
    if kind != "parameter":
        return None
    return float(out_of_sample_excess) > 0.0


def coverage_fields(coverage: Any, minimum: float, below_gate: bool, gate_message: str) -> dict[str, Any]:
    return {
        "member_count": coverage.member_count,
        "covered_count": coverage.covered_count,
        "ratio": coverage.ratio,
        "coverage_pct": round(coverage.ratio * 100.0, 4),
        "minimum": minimum,
        "below_gate": below_gate,
        "gate_message": gate_message,
        "point_in_time_universe_passed": True,
        "survivorship_checks_passed": not bool(coverage.uncovered),
        "universe_mode": "point_in_time_incomplete_prices" if coverage.uncovered else "point_in_time",
        "missing": list(coverage.missing),
        "partial": list(coverage.partial),
        "unusable": list(coverage.unusable),
        "rows": list(coverage.rows),
    }


def _shared_sessions(*frames: pd.DataFrame) -> pd.DatetimeIndex:
    shared: set[pd.Timestamp] | None = None
    for frame in frames:
        sessions = set()
        for value in pd.to_datetime(frame["Date"]):
            stamp = pd.Timestamp(value)
            if stamp.tzinfo is not None:
                stamp = stamp.tz_convert("UTC").tz_localize(None)
            sessions.add(stamp.normalize())
        shared = sessions if shared is None else shared & sessions
    if not shared:
        raise ImprovementLoopError("SPY and the excess benchmark share no sessions")
    return pd.DatetimeIndex(sorted(shared))


def _confirm_currency(ticker: str, expected: str, lookup: Callable[[str], tuple[str, str | None]]) -> None:
    found, error = lookup(ticker)
    if error or not found:
        raise ImprovementLoopError(f"{ticker}: currency_unconfirmed ({error or 'no currency'})")
    if _quote_unit(found) != _quote_unit(expected):
        raise ImprovementLoopError(
            f"{ticker}: Yahoo currency {found} does not match configured {expected}"
        )


def _download_frames(
    tickers: list[str],
    benchmarks: list[str],
    fx_pairs: list[str],
    start_date: str,
    end_date: str,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, pd.DataFrame], dict[str, str]]:
    import time

    from shared.market_data import FileMarketDataCache, YFinanceMarketDataProvider

    provider = YFinanceMarketDataProvider(
        retries=2,
        cache=FileMarketDataCache(),
        sleep_func=time.sleep,
        backoff_func=lambda retry_count: float(2 ** (retry_count - 1)),
    )
    health: list[Any] = []
    unusable: dict[str, str] = {}

    def _download(ticker: str, *, allow_stale: bool) -> pd.DataFrame:
        try:
            return backtest.download_price_history(
                ticker,
                start_date,
                end_date,
                market_data_provider=provider,
                health_results=health,
                allow_stale=allow_stale,
            )
        except StrategyValidationError as exc:
            unusable[ticker] = str(exc)
            print(f"{ticker}: unusable price history: {exc}")
            return pd.DataFrame()

    benchmark_frames: dict[str, pd.DataFrame] = {}
    for ticker in benchmarks:
        print(f"Benchmark {ticker}...")
        frame = _download(ticker, allow_stale=False)
        if frame.empty:
            raise ImprovementLoopError(f"missing benchmark history: {ticker}")
        benchmark_frames[ticker] = frame
    histories: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        print(f"Loading {ticker}...")
        frame = _download(ticker, allow_stale=True)
        if not frame.empty:
            histories[ticker] = frame
    fx_frames: dict[str, pd.DataFrame] = {}
    for pair in fx_pairs:
        print(f"FX {pair}...")
        frame = _download(pair, allow_stale=True)
        if frame.empty:
            raise ImprovementLoopError(f"fx_missing: {pair} returned no prices")
        fx_frames[pair] = frame
    return histories, benchmark_frames, fx_frames, unusable


def _prepare_market(
    loop_config: Mapping[str, Any],
    backtest_config: dict[str, Any],
    *,
    histories: dict[str, pd.DataFrame] | None,
    benchmark_histories: dict[str, pd.DataFrame] | None,
    fx_histories: dict[str, pd.DataFrame] | None,
    universe: Any,
    currency_lookup: Callable[[str], tuple[str, str | None]] | None,
) -> dict[str, Any]:
    start_date = str(backtest_config.get("start_date") or "").strip()
    end_date = str(backtest_config.get("end_date") or "").strip()
    if not start_date:
        raise ImprovementLoopError("backtest start_date is missing")
    membership_end = end_date or datetime.now(timezone.utc).date().isoformat()
    universe_path = backtest.point_in_time_universe_path(backtest_config)
    if universe_path is not None:
        backtest.enforce_point_in_time_bounds(universe_path, start_date, membership_end)
    symbols = backtest.point_in_time_symbols(universe, start_date, membership_end)
    calendar_ticker = str(loop_config.get("session_calendar_ticker") or "").strip()
    excess_ticker = str(loop_config.get("excess_benchmark") or "").strip()
    currencies = dict(loop_config.get("benchmark_currencies") or {})
    member_currency = str(loop_config.get("member_quote_currency") or "").strip()
    if not calendar_ticker or not excess_ticker or not member_currency:
        raise ImprovementLoopError("calendar, excess benchmark, and member currency are required")
    if calendar_ticker not in currencies or excess_ticker not in currencies:
        raise ImprovementLoopError("calendar and excess benchmark need configured currencies")
    benchmark_names = list(dict.fromkeys([calendar_ticker, excess_ticker, *currencies]))

    confirm = currency_lookup
    if histories is None:
        if confirm is None:
            from shared.paper_signal_log import _default_currency

            confirm = _default_currency
        for ticker, expected in currencies.items():
            _confirm_currency(ticker, str(expected), confirm)
        sample = "AAPL" if "AAPL" in symbols else symbols[0]
        _confirm_currency(sample, member_currency, confirm)
        fx_pairs = sorted({
            FX_TO_GBP[major][0]
            for currency in [member_currency, *currencies.values()]
            if (major := _quote_unit(str(currency))[0]) in FX_TO_GBP
        })
        histories, benchmark_histories, fx_histories, unusable = _download_frames(
            symbols, benchmark_names, fx_pairs, start_date, end_date,
        )
        confirmed_member = sample
    else:
        if benchmark_histories is None:
            raise ImprovementLoopError("benchmark history is missing")
        fx_histories = fx_histories or {}
        unusable = {}
        confirmed_member = ""
        for ticker in benchmark_names:
            if ticker not in benchmark_histories or benchmark_histories[ticker].empty:
                raise ImprovementLoopError(f"missing benchmark history: {ticker}")

    if calendar_ticker not in benchmark_histories:
        raise ImprovementLoopError(
            f"{calendar_ticker} history is required to date the point-in-time session calendar"
        )
    calendar = backtest.history_sessions(
        {calendar_ticker: benchmark_histories[calendar_ticker]}, start_date, end_date,
    )
    price_coverage = backtest.assess_point_in_time_price_coverage(
        universe, histories, calendar, start_date, membership_end, unusable,
    )
    minimum = backtest.min_point_in_time_price_coverage(backtest_config)
    below_gate = False
    gate_message = ""
    if price_coverage.member_count == 0:
        backtest.require_point_in_time_price_coverage(price_coverage, minimum)
    try:
        backtest.require_point_in_time_price_coverage(price_coverage, minimum)
    except StrategyValidationError as exc:
        below_gate = True
        gate_message = str(exc)
    if not price_coverage.covered:
        raise ImprovementLoopError(
            "point-in-time universe has no members with usable prices\n"
            + backtest.format_price_coverage(price_coverage, minimum)
        )
    covered = set(price_coverage.covered)
    replay_universe = backtest.universe_priced_members(universe, covered)
    calendar_sessions = {backtest._naive_session(session) for session in calendar}
    priced = {
        ticker: backtest._clip_history_to_sessions(frame, calendar_sessions)
        for ticker, frame in histories.items()
        if ticker in covered
    }
    fx_maps = {pair: fx_close_map(frame, pair) for pair, frame in (fx_histories or {}).items()}
    carried_sessions: set[date] = set()

    def _fx_for(currency: str) -> dict[date, Decimal] | None:
        major, _pence = _quote_unit(currency)
        if major == "GBP":
            return None
        pair = FX_TO_GBP[major][0]
        if pair not in fx_maps:
            raise ImprovementLoopError(f"fx_missing: {pair} was not loaded for {currency}")
        return fx_maps[pair]

    gbp_histories = {}
    for ticker, frame in priced.items():
        converted, carried = convert_frame_to_gbp(frame, member_currency, _fx_for(member_currency))
        gbp_histories[ticker] = converted
        carried_sessions.update(carried)
    gbp_benchmarks = {}
    for ticker, frame in benchmark_histories.items():
        if ticker not in currencies:
            continue
        converted, carried = convert_frame_to_gbp(
            frame, str(currencies[ticker]), _fx_for(str(currencies[ticker])),
        )
        gbp_benchmarks[ticker] = converted
        carried_sessions.update(carried)
    shared = _shared_sessions(
        gbp_benchmarks[calendar_ticker],
        gbp_benchmarks[excess_ticker],
    )
    return {
        "histories": gbp_histories,
        "benchmarks": gbp_benchmarks,
        "replay_universe": replay_universe,
        "coverage": coverage_fields(price_coverage, minimum, below_gate, gate_message),
        "shared_sessions": shared,
        "calendar_sessions": len(calendar),
        "start_date": start_date,
        "membership_end": membership_end,
        "member_currency": member_currency,
        "member_currency_confirmed_for": confirmed_member,
        "fx_carried_sessions": [day.isoformat() for day in sorted(carried_sessions)],
        "calendar_ticker": calendar_ticker,
        "excess_ticker": excess_ticker,
    }


def _evaluate_windows(
    specs: list[dict[str, Any]],
    market: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], Any]:
    validation = backtest_config.get("validation") or {}
    train_fraction = float(validation.get("train_fraction", 0.6))
    validation_fraction = float(validation.get("validation_fraction", 0.2))
    split = chronological_split(
        market["shared_sessions"],
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
    )
    in_start = split.train_start.isoformat()
    in_end = split.validation_end.isoformat()
    out_start = split.test_start.isoformat()
    out_end = split.test_end.isoformat()
    if not (split.validation_end < split.test_start):
        raise ImprovementLoopError("the test window does not start after the in-sample window")
    costs = backtest.configured_costs(dict(backtest_config))
    excess_history = market["benchmarks"][market["excess_ticker"]]
    benchmark_returns = {
        "in_sample": float(backtest.partition_benchmark_return(excess_history, in_start, in_end, costs)),
        "out_of_sample": float(backtest.partition_benchmark_return(excess_history, out_start, out_end, costs)),
    }
    rows = []
    for spec in specs:
        windows = {}
        for name, start, end, history_start in (
            ("in_sample", in_start, in_end, in_start),
            ("out_of_sample", out_start, out_end, in_start),
        ):
            print(f"Evaluating {spec['id']} {name}...")
            windows[name] = _one_window(
                spec, market, backtest_config, costs, start, end, history_start, benchmark_returns[name],
            )
        rows.append({
            "id": spec["id"],
            "kind": spec["kind"],
            "ticker": spec["ticker"],
            "parameters": spec["parameters"],
            "in_sample": windows["in_sample"],
            "out_of_sample": windows["out_of_sample"],
            "oos_beats_baseline": oos_beats_baseline(spec["kind"], windows["out_of_sample"]["excess_pct"]),
        })
    split_payload = {
        "train_start": split.train_start.isoformat(),
        "train_end": split.train_end.isoformat(),
        "validation_start": split.validation_start.isoformat(),
        "validation_end": split.validation_end.isoformat(),
        "in_sample_start": in_start,
        "in_sample_end": in_end,
        "out_of_sample_start": out_start,
        "out_of_sample_end": out_end,
        "tuned_on": "in_sample",
        "train_fraction": train_fraction,
        "validation_fraction": validation_fraction,
        "note": (
            "In-sample is the train window through the validation window. "
            "Indicators on the test window may use prices from that earlier "
            "window. The test window is not used to choose a variant."
        ),
    }
    return rows, split_payload


def _one_window(
    spec: Mapping[str, Any],
    market: Mapping[str, Any],
    backtest_config: Mapping[str, Any],
    costs: Any,
    start: str,
    end: str,
    history_start: str,
    benchmark_return: float,
) -> dict[str, Any]:
    kind = spec["kind"]
    if kind == "do_nothing":
        metrics = {
            "return_pct": 0.0,
            "win_rate_pct": None,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
        }
        return _with_excess(metrics, benchmark_return)
    if kind == "buy_and_hold":
        history = market["benchmarks"].get(spec["ticker"])
        if history is None:
            raise ImprovementLoopError(f"missing benchmark history: {spec['ticker']}")
        return _with_excess(buy_and_hold_metrics(history, start, end, costs), benchmark_return)
    config = {**backtest_config, **spec["parameters"], "experiment_registry_enabled": False}
    replay = backtest.evaluate_partition(
        market["histories"],
        config,
        start,
        end,
        history_start=history_start,
        universe=market["replay_universe"],
    )
    return _with_excess(metrics_from_replay(replay), benchmark_return)


def run_loop(
    *,
    config_path: Path | None = None,
    loop_config: dict[str, Any] | None = None,
    backtest_config: dict[str, Any] | None = None,
    universe: Any = None,
    histories: dict[str, pd.DataFrame] | None = None,
    benchmark_histories: dict[str, pd.DataFrame] | None = None,
    fx_histories: dict[str, pd.DataFrame] | None = None,
    paper_log_path: Path | None = None,
    demo_trade_log_path: Path | None = None,
    broker_fills_path: Path | None = None,
    currency_lookup: Callable[[str], tuple[str, str | None]] | None = None,
) -> dict[str, Any]:
    """Compare variants. The caller writes the report; this function does not."""
    loop_config = loop_config if loop_config is not None else load_loop_config(config_path)
    if backtest_config is None:
        backtest_config = backtest.load_backtest_config()
    else:
        backtest_config = dict(backtest_config)
    if "research_universe_checksum" in backtest_config:
        raise ImprovementLoopError(
            "selected research universe is current membership, not a point-in-time universe"
        )
    specs = build_variants(loop_config, backtest_config)
    pit_file = str(loop_config.get("point_in_time_universe_file") or "").strip()
    if universe is None:
        if not pit_file:
            raise ImprovementLoopError("a point-in-time universe file is required")
        backtest_config = {**backtest_config, "point_in_time_universe_file": pit_file}
        universe = backtest.configured_point_in_time_universe(backtest_config)
        if universe is None:
            raise ImprovementLoopError("a point-in-time universe file is required")
    market = _prepare_market(
        loop_config,
        backtest_config,
        histories=histories,
        benchmark_histories=benchmark_histories,
        fx_histories=fx_histories,
        universe=universe,
        currency_lookup=currency_lookup,
    )
    rows, split_payload = _evaluate_windows(specs, market, backtest_config)
    qualified = not market["coverage"]["below_gate"]
    proposed = choose_proposal(rows, qualified=qualified)
    if not qualified:
        ordered = rows
    else:
        ordered = sorted(rows, key=lambda row: (-float(row["in_sample"]["excess_pct"]), row["id"]))
        for rank, row in enumerate(ordered, start=1):
            row["rank"] = rank
    if not qualified:
        for row in ordered:
            row["rank"] = None
    costs = backtest.configured_costs(backtest_config)
    paper_path = paper_log_path or load_paper_config().log_path
    demo_path = _resolve_path(
        demo_trade_log_path or loop_config.get("demo_trade_log") or runtime_path("logs", "demo_trades.jsonl")
    )
    fills_path = broker_fills_path or BROKER_FILLS_PATH
    coverage_status = (
        f"{market['coverage']['covered_count']}/{market['coverage']['member_count']} "
        f"({market['coverage']['coverage_pct']:.4f}%)"
        + (" below gate" if not qualified else " at or above gate")
    )
    for row in ordered:
        row["coverage_status"] = coverage_status
    return {
        "proposal_only": True,
        "orders_sent": False,
        "broker_calls": False,
        "config_written": False,
        "currency": "GBP",
        "ranking_qualified": qualified,
        "proposed_variant": proposed,
        "proposal_reason": (
            "coverage is below the gate; nothing is proposed"
            if not qualified
            else "highest in-sample GBP excess versus the benchmark after costs; the test window was not used"
        ),
        "coverage": market["coverage"],
        "coverage_status": coverage_status,
        "split": split_payload,
        "session_calendar_ticker": market["calendar_ticker"],
        "session_calendar_sessions": market["calendar_sessions"],
        "shared_boundary_sessions": len(market["shared_sessions"]),
        "excess_benchmark": market["excess_ticker"],
        "member_quote_currency": market["member_currency"],
        "member_currency_confirmed_for": market["member_currency_confirmed_for"],
        "fx_carried_sessions": market["fx_carried_sessions"],
        "gbp_conversion": (
            "Each daily bar is restated with the paper-log FX close for that "
            "session. USD uses GBPUSD=X (divide). GBp and GBX divide by 100. "
            "A session with no FX close uses the latest earlier close when it "
            "is at most 4 calendar days before, and that date is listed. A "
            "longer gap raises. Pence need no FX pair."
        ),
        "cost_model_bps": {
            "spread_bps": format(costs.spread_bps, "f"),
            "slippage_bps": format(costs.slippage_bps, "f"),
            "fee_bps": format(costs.fee_bps, "f"),
            "tax_bps": format(costs.tax_bps, "f"),
        },
        "variants": ordered,
        "paper_signal_log": _paper_lane(paper_path),
        "demo_trade_log": _jsonl_lane(demo_path, "demo trade log"),
        "broker_fills": _broker_fills_lane(fills_path),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    coverage = report["coverage"]
    lines = [
        "# Strategy improvement loop",
        "",
        "Proposes only. No strategy config was edited. No order was sent. No broker endpoint was called.",
        "",
        "## Coverage",
        "",
    ]
    if coverage["below_gate"]:
        lines.append(
            f"**PRICE COVERAGE {coverage['covered_count']}/{coverage['member_count']} "
            f"({coverage['coverage_pct']:.4f}%) IS BELOW THE {coverage['minimum']:.2%} GATE. "
            "THIS IS NOT A QUALIFIED RANKING. NOTHING IS PROPOSED.**"
        )
        lines.append("")
        lines.append(
            "Uncovered members stay listed. They are not given a zero return and they are not dropped quietly. "
            "Survivorship bias remains."
        )
    else:
        lines.append(
            f"Price coverage {coverage['covered_count']}/{coverage['member_count']} "
            f"({coverage['coverage_pct']:.4f}%) is at or above the {coverage['minimum']:.2%} gate."
        )
    lines.append("")
    lines.append(f"Universe mode: {coverage['universe_mode']}.")
    lines.append(f"point_in_time_universe_passed: {str(coverage['point_in_time_universe_passed']).lower()}.")
    lines.append(f"survivorship_checks_passed: {str(coverage['survivorship_checks_passed']).lower()}.")
    for label, names in (
        ("No price history", coverage["missing"]),
        ("Partial price history", coverage["partial"]),
        ("Unusable price history", coverage["unusable"]),
    ):
        if names:
            lines.append(f"{label} ({len(names)}): {', '.join(names)}")
    lines.append("")
    lines.append(
        f"Member quotes: {report['member_quote_currency']}"
        + (
            f", confirmed with Yahoo for {report['member_currency_confirmed_for']} only."
            if report["member_currency_confirmed_for"]
            else "."
        )
    )
    lines.append(report["gbp_conversion"])
    carried = report.get("fx_carried_sessions") or []
    if carried:
        lines.append(
            "FX close carried from the previous session on "
            f"{len(carried)} date(s): {', '.join(carried)}. "
            "Each gap is 4 calendar days or less. A longer gap is an error."
        )
    else:
        lines.append("No FX session was carried forward.")
    lines.append(
        "Costs are the backtest cost model "
        f"(fee {report['cost_model_bps']['fee_bps']} bps, "
        f"slippage {report['cost_model_bps']['slippage_bps']} bps, "
        f"spread {report['cost_model_bps']['spread_bps']} bps, "
        f"tax {report['cost_model_bps']['tax_bps']} bps), applied inside the existing replay. "
        f"Excess is versus {report['excess_benchmark']} after those costs, in GBP."
    )
    split = report["split"]
    lines.extend([
        "",
        "## Split",
        "",
        f"In-sample: {split['in_sample_start']} to {split['in_sample_end']} "
        f"(train {split['train_start']} to {split['train_end']}, "
        f"validation {split['validation_start']} to {split['validation_end']}).",
        f"Out-of-sample: {split['out_of_sample_start']} to {split['out_of_sample_end']}.",
        split["note"],
        f"Shared boundary sessions: {report['shared_boundary_sessions']} "
        f"of {report['session_calendar_sessions']} {report['session_calendar_ticker']} sessions.",
        "",
        "## Variants",
        "",
    ])
    if coverage["below_gate"]:
        lines.append("Config order. Not sorted by result, because coverage is below the gate.")
    else:
        lines.append(
            f"Sorted by in-sample excess. Proposed variant: {report['proposed_variant']}. "
            "A parameter variant whose out-of-sample excess is not above zero did not beat "
            f"{report['excess_benchmark']} after costs."
        )
    lines.append("")
    header = (
        "| variant | kind | IS return % | IS excess % | IS win rate | IS max DD % | IS trades "
        "| OOS return % | OOS excess % | OOS win rate | OOS max DD % | OOS trades "
        "| OOS beats baseline | coverage |"
    )
    lines.append(header)
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |")
    for row in report["variants"]:
        sample = row["in_sample"]
        later = row["out_of_sample"]
        flag = row["oos_beats_baseline"]
        flag_text = "baseline" if flag is None else ("yes" if flag else "no")
        lines.append(
            "| {id} | {kind} | {isr} | {ise} | {isw} | {isd} | {ist} | {osr} | {ose} | {osw} | {osd} | {ost} | {flag} | {cov} |".format(
                id=row["id"],
                kind=row["kind"],
                isr=_num(sample["return_pct"]),
                ise=_num(sample["excess_pct"]),
                isw=_num(sample["win_rate_pct"], 2),
                isd=_num(sample["max_drawdown_pct"]),
                ist=sample["trade_count"],
                osr=_num(later["return_pct"]),
                ose=_num(later["excess_pct"]),
                osw=_num(later["win_rate_pct"], 2),
                osd=_num(later["max_drawdown_pct"]),
                ost=later["trade_count"],
                flag=flag_text,
                cov=row["coverage_status"],
            )
        )
    lines.extend(["", f"Proposal: {report['proposal_reason']}.", ""])
    if report["proposed_variant"]:
        chosen = next(row for row in report["variants"] if row["id"] == report["proposed_variant"])
        if chosen["oos_beats_baseline"] is False:
            lines.append(
                f"{chosen['id']} is the in-sample leader and does not beat "
                f"{report['excess_benchmark']} out of sample after costs."
            )
            lines.append("")
    lines.extend(_lane_section("Paper signal log", report["paper_signal_log"]))
    lines.extend(_lane_section("Demo trade log", report["demo_trade_log"]))
    lines.extend(_lane_section("Broker fills", report["broker_fills"]))
    return "\n".join(lines).rstrip() + "\n"


def _num(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{digits}f}"


def _lane_section(title: str, lane: Mapping[str, Any]) -> list[str]:
    lines = [f"## {title}", ""]
    status = lane.get("status")
    if status == _NO_DATA:
        lines.append(f"{_NO_DATA}. Looked at `{lane.get('path', '')}`.")
        lines.append("")
        return lines
    lines.append(f"Status: present. Path: `{lane.get('path', '')}`.")
    if "signals" in lane:
        lines.append(
            f"Signals: {lane['signals']}. Cost assumption: {lane['cost_bps']} bps. "
            f"Benchmark: {lane['benchmark']}."
        )
        lines.append(lane["detail"])
        lines.append("")
        lines.append("| horizon | group | signals | pending | scored | hit rate | mean excess | fx_missing |")
        lines.append("| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for summary in lane.get("summaries") or []:
            lines.append(
                f"| {summary['horizon_months']} | {summary['signal_group']} | {summary['signals']} "
                f"| {summary['pending']} | {summary['scored']} "
                f"| {summary['hit_rate']} | {summary['mean_excess_return']} | {summary['fx_missing']} |"
            )
        lines.append("")
        return lines
    lines.append(
        f"Records counted: {lane.get('trade_count')}. "
        f"GBP return: {lane.get('return_gbp')}. {lane.get('detail', '')}"
    )
    lines.append("")
    return lines


def write_report(report: Mapping[str, Any], directory: Path) -> tuple[Path, Path]:
    """Write the markdown and JSON report. This is the only write the tool does."""
    directory.mkdir(parents=True, exist_ok=True)
    markdown_path = directory / "improvement_loop.md"
    json_path = directory / "improvement_loop.json"
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return markdown_path, json_path
