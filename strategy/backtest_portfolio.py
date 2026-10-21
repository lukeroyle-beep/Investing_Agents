"""Research-only, cash-constrained replay of long-trade candidates.

Candidates contain an entry and an eventual exit; their future returns never
participate in allocation. Entries are processed before all same-session exits,
so proceeds from a later intraday exit cannot fund an opening trade. All marks
must be supplied explicitly for every held session. No broker or runtime state
is read or written here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose, isfinite
from typing import Iterable, Mapping

import pandas as pd


class PortfolioReplayError(ValueError):
    """Inputs or accounting are insufficient for a trustworthy replay."""


EQUITY_COLUMNS = [
    "date", "equity", "daily_pnl", "cumulative_return_pct", "cash",
    "realised_pnl", "unrealised_pnl", "open_positions", "exposure_notional",
    "exposure_pct", "turnover_notional", "turnover_pct", "entry_fees", "exit_fees",
]
DRAWDOWN_COLUMNS = ["date", "equity", "peak_equity", "drawdown", "drawdown_pct"]


@dataclass(frozen=True)
class PortfolioReplayResult:
    accepted_trades: pd.DataFrame
    rejected_trades: pd.DataFrame
    equity_curve: pd.DataFrame
    drawdown: pd.DataFrame


def _number(value: object, name: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortfolioReplayError(f"{name} must be a finite number") from exc
    if not isfinite(result) or (result <= 0 if positive else result < 0):
        sign = "positive" if positive else "non-negative"
        raise PortfolioReplayError(f"{name} must be finite and {sign}")
    return result


def _date(value: object, name: str) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise PortfolioReplayError(f"{name} must be a valid session date") from exc
    if pd.isna(result):
        raise PortfolioReplayError(f"{name} must be a valid session date")
    # These are session labels, not instants. Preserve the labelled calendar
    # date instead of shifting exchange-midnight dates through another zone.
    if result.tzinfo is not None:
        result = result.tz_localize(None)
    return result.normalize()


def _sessions(values: Iterable[object]) -> pd.DatetimeIndex:
    result = pd.DatetimeIndex([_date(value, "session") for value in values])
    if result.empty or result.has_duplicates or not result.is_monotonic_increasing:
        raise PortfolioReplayError("sessions must be nonempty, unique and increasing")
    return result


def _close_marks(history: pd.DataFrame, ticker: str) -> dict[pd.Timestamp, float]:
    if not {"Date", "Close"}.issubset(history.columns):
        raise PortfolioReplayError(f"{ticker}: price history needs Date and Close")
    marks: dict[pd.Timestamp, float] = {}
    for record in history[["Date", "Close"]].to_dict("records"):
        session = _date(record["Date"], f"{ticker} price date")
        if session in marks:
            raise PortfolioReplayError(f"{ticker}: duplicate price session {session.date()}")
        marks[session] = _number(record["Close"], f"{ticker} close", positive=True)
    return marks


def _candidate(record: dict, session_set: set[pd.Timestamp]) -> dict:
    value = dict(record)
    ticker = value.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise PortfolioReplayError("candidate ticker must be a nonempty string")
    value["ticker"] = ticker.strip()
    if value.get("side", "long") != "long":
        raise PortfolioReplayError("only long candidates are supported")
    value["side"] = "long"
    entry = _date(value.get("entry_date"), "entry_date")
    exit_ = _date(value.get("exit_date"), "exit_date")
    if entry not in session_set or exit_ not in session_set or exit_ < entry:
        raise PortfolioReplayError("entry and exit must be ordered evaluation sessions")
    if "signal_date" in value and _date(value["signal_date"], "signal_date") >= entry:
        raise PortfolioReplayError("entry must follow the signal session")
    shares = _number(value.get("shares"), "shares", positive=True)
    entry_price = _number(value.get("entry_execution_price"), "entry execution price", positive=True)
    exit_price = _number(value.get("exit_execution_price"), "exit execution price", positive=True)
    entry_value = shares * entry_price
    exit_value = shares * exit_price
    if not isfinite(entry_value) or not isfinite(exit_value):
        raise PortfolioReplayError("execution notionals must be finite")
    has_entry_fee = "entry_fee" in value and pd.notna(value["entry_fee"])
    has_exit_fee = "exit_fee" in value and pd.notna(value["exit_fee"])
    if has_entry_fee != has_exit_fee:
        raise PortfolioReplayError("explicit entry_fee and exit_fee must be supplied together")
    if has_entry_fee:
        entry_fee = _number(value["entry_fee"], "entry_fee")
        exit_fee = _number(value["exit_fee"], "exit_fee")
        if "transaction_cost" in value and not isclose(
            _number(value["transaction_cost"], "transaction_cost"),
            entry_fee + exit_fee, rel_tol=1e-8, abs_tol=1e-8,
        ):
            raise PortfolioReplayError("explicit fees do not reconcile to transaction_cost")
    else:
        # Legacy candidates contain a combined ad-valorem fee for both legs.
        # Recover that rate, then debit the appropriate fee on each execution.
        total_fee = _number(value.get("transaction_cost", 0.0), "transaction_cost")
        entry_fee = total_fee * entry_value / (entry_value + exit_value)
        exit_fee = total_fee - entry_fee
    net_pnl = exit_value - entry_value - entry_fee - exit_fee
    value.update(
        entry_date=entry.date().isoformat(), exit_date=exit_.date().isoformat(),
        shares=shares, entry_execution_price=entry_price, exit_execution_price=exit_price,
        notional=entry_value, entry_fee=entry_fee, exit_fee=exit_fee,
        transaction_cost=entry_fee + exit_fee, net_pnl=net_pnl,
        trade_return_pct=net_pnl / entry_value * 100.0,
    )
    return value


def replay_portfolio(
    trades_df: pd.DataFrame,
    history_by_ticker: Mapping[str, pd.DataFrame],
    initial_capital: float,
    sessions: Iterable[object],
) -> PortfolioReplayResult:
    """Replay fixed-size candidates without borrowing or future-return ranking.

    ``sessions`` is an explicit sequence of exchange session dates. It must
    cover every candidate entry/exit. Open positions require a positive close
    on every held session; missing marks are an error, never forward-filled.
    ``entry_fee`` and ``exit_fee`` may be supplied, otherwise a combined
    ``transaction_cost`` is split in proportion to the execution notionals.
    Input ``net_pnl`` and ``notional`` are recomputed from executions rather
    than trusted. Candidates exceeding available cash are rejected unchanged
    in size. The function requires completed candidate lifecycles and returns
    a flat, reconciled final research portfolio.
    """
    capital = _number(initial_capital, "initial_capital", positive=True)
    dates = _sessions(sessions)
    date_set = set(dates)
    candidates = [_candidate(record, date_set) for record in trades_df.to_dict("records")]
    identities: set[tuple[str, str]] = set()
    explicit_ids: set[str] = set()
    for trade in candidates:
        identity = (trade["ticker"], trade["entry_date"])
        if identity in identities:
            raise PortfolioReplayError("duplicate ticker/entry-session candidate")
        identities.add(identity)
        if "trade_id" in trade and pd.notna(trade["trade_id"]):
            identifier = str(trade["trade_id"])
            if not identifier or identifier in explicit_ids:
                raise PortfolioReplayError("duplicate or empty trade_id")
            explicit_ids.add(identifier)
    candidates.sort(key=lambda trade: (trade["entry_date"], trade["ticker"]))
    marks = {
        ticker: _close_marks(history_by_ticker[ticker], ticker)
        for ticker in sorted({trade["ticker"] for trade in candidates})
        if ticker in history_by_ticker
    }
    entries: dict[str, list[dict]] = {}
    for trade in candidates:
        entries.setdefault(trade["entry_date"], []).append(trade)

    cash = capital
    realised = 0.0
    previous_equity = capital
    peak = capital
    active: dict[str, dict] = {}
    accepted: list[dict] = []
    rejected: list[dict] = []
    equity_rows: list[dict] = []
    drawdown_rows: list[dict] = []
    for session in dates:
        label = session.date().isoformat()
        turnover = entry_fees = exit_fees = 0.0
        for trade in entries.get(label, []):
            ticker = trade["ticker"]
            required_cash = trade["notional"] + trade["entry_fee"]
            if ticker in active:
                rejected.append({**trade, "rejection_reason": "position_already_open"})
                continue
            if required_cash > cash:
                rejected.append({**trade, "rejection_reason": "insufficient_cash"})
                continue
            cash -= required_cash
            turnover += trade["notional"]
            entry_fees += trade["entry_fee"]
            active[ticker] = trade
            accepted.append({**trade, "weighted_return_pct": trade["net_pnl"] / capital * 100.0})

        for ticker, trade in list(active.items()):
            if trade["exit_date"] != label:
                continue
            exit_value = trade["shares"] * trade["exit_execution_price"]
            cash += exit_value - trade["exit_fee"]
            realised += trade["net_pnl"]
            turnover += exit_value
            exit_fees += trade["exit_fee"]
            del active[ticker]

        exposure = unrealised = 0.0
        for ticker, trade in active.items():
            mark = marks.get(ticker, {}).get(session)
            if mark is None:
                raise PortfolioReplayError(f"{ticker}: missing held-session close on {label}")
            market_value = trade["shares"] * mark
            exposure += market_value
            unrealised += market_value - trade["notional"] - trade["entry_fee"]
        equity = cash + exposure
        if not all(isfinite(number) for number in (cash, realised, unrealised, exposure, equity)):
            raise PortfolioReplayError("portfolio accounting produced a nonfinite value")
        if cash < 0 or equity <= 0:
            raise PortfolioReplayError("portfolio exhausted cash or equity")
        if not isclose(equity - capital, realised + unrealised, rel_tol=1e-9, abs_tol=1e-7):
            raise PortfolioReplayError("equity does not reconcile to realised and unrealised profit/loss")
        equity_rows.append({
            "date": label, "equity": equity, "daily_pnl": equity - previous_equity,
            "cumulative_return_pct": (equity / capital - 1.0) * 100.0,
            "cash": cash, "realised_pnl": realised, "unrealised_pnl": unrealised,
            "open_positions": len(active), "exposure_notional": exposure,
            "exposure_pct": exposure / equity * 100.0, "turnover_notional": turnover,
            "turnover_pct": turnover / capital * 100.0,
            "entry_fees": entry_fees, "exit_fees": exit_fees,
        })
        peak = max(peak, equity)
        drawdown_rows.append({
            "date": label, "equity": equity, "peak_equity": peak,
            "drawdown": equity - peak, "drawdown_pct": (equity / peak - 1.0) * 100.0,
        })
        previous_equity = equity
    if active or not isclose(cash, capital + realised, rel_tol=1e-9, abs_tol=1e-7):
        raise PortfolioReplayError("final flat portfolio did not reconcile")
    columns = list(dict.fromkeys([
        *trades_df.columns, "side", "notional", "entry_fee", "exit_fee",
        "transaction_cost", "net_pnl", "trade_return_pct", "weighted_return_pct",
    ]))
    return PortfolioReplayResult(
        accepted_trades=pd.DataFrame(accepted).reindex(columns=columns),
        rejected_trades=pd.DataFrame(rejected).reindex(columns=[*columns, "rejection_reason"]),
        equity_curve=pd.DataFrame(equity_rows, columns=EQUITY_COLUMNS),
        drawdown=pd.DataFrame(drawdown_rows, columns=DRAWDOWN_COLUMNS),
    )
