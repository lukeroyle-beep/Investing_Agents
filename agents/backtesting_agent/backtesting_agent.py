from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite, isnan
import os
from typing import Any

import pandas as pd

from shared.market_data import (
    MarketDataProvider,
    MarketDataResult,
    append_market_data_health_artifact,
    fetch_price_history,
    market_data_is_actionable,
)
from shared.io_utils import load_yaml, write_csv_with_run_id
from shared.paths import EXPERIMENT_REGISTRY_PATH, config_path, data_path, run_path
from shared.run_context import get_or_create_run_id
from shared.research_universe import selected_research_universe
from strategy.domain import CostModel, ExperimentMetrics, ExperimentSpec, StrategyValidationError, content_hash
from strategy.backtest_portfolio import replay_portfolio
from strategy.registry import ExperimentRegistry, write_experiment_artifact
from strategy.validation import rolling_walk_forward_splits


BACKTEST_CONFIG_FILE = config_path("backtesting.yaml")
BACKTEST_TRADES_FILE = data_path("backtest_trades.csv")
BACKTEST_SUMMARY_FILE = data_path("backtest_summary.csv")
BACKTEST_EQUITY_CURVE_FILE = data_path("backtest_equity_curve.csv")
BACKTEST_DRAWDOWN_FILE = data_path("backtest_drawdown.csv")
BACKTEST_BENCHMARK_FILE = data_path("backtest_benchmark_comparison.csv")
BACKTEST_REJECTED_FILE = data_path("backtest_rejected_trades.csv")


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "tickers": ["AAPL", "MSFT", "NVDA", "AMZN"],
    "benchmark_tickers": ["SPY", "QQQ"],
    "start_date": "2022-01-01",
    "end_date": "",
    "initial_capital": 100000,
    "position_size_pct": 10.0,
    "ma_fast_window": 20,
    "ma_slow_window": 50,
    "return_lookback_window": 60,
    "stop_loss_pct": 8.0,
    "take_profit_pct": 15.0,
    "max_hold_days": 30,
    "min_history_days": 120,
    "allow_short": False,
    "transaction_cost_bps": 5.0,
    "slippage_bps": 5.0,
}


TRADE_COLUMNS = [
    "ticker",
    "side",
    "entry_date",
    "exit_date",
    "signal_date",
    "entry_price",
    "exit_price",
    "entry_execution_price",
    "exit_execution_price",
    "shares",
    "gross_pnl",
    "transaction_cost",
    "entry_fee",
    "exit_fee",
    "slippage_cost",
    "net_pnl",
    "notional",
    "stop_price",
    "take_profit_price",
    "hold_days",
    "exit_reason",
    "gross_trade_return_pct",
    "trade_return_pct",
    "weighted_return_pct",
]


EQUITY_COLUMNS = [
    "date",
    "equity",
    "daily_pnl",
    "cumulative_return_pct",
    "open_positions",
    "exposure_notional",
    "exposure_pct",
    "turnover_notional",
    "turnover_pct",
]


DRAWDOWN_COLUMNS = ["date", "equity", "peak_equity", "drawdown", "drawdown_pct"]
BENCHMARK_COLUMNS = ["benchmark_ticker", "start_date", "end_date", "benchmark_return_pct", "strategy_return_pct", "excess_return_pct"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        if isinstance(value, float) and isnan(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def load_backtest_config() -> dict[str, Any]:
    config = load_yaml(BACKTEST_CONFIG_FILE, empty_ok=True)
    merged = DEFAULT_CONFIG.copy()
    merged.update(config or {})
    selection = selected_research_universe()
    if selection is not None:
        merged["tickers"] = list(selection.tickers)
        merged["research_universe_version"] = selection.version
        merged["research_universe_checksum"] = selection.checksum
    return merged


def _normalise_history_df(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    if "Date" not in df.columns and "index" in df.columns:
        df = df.rename(columns={"index": "Date"})

    required_cols = {"Date", "Open", "High", "Low", "Close"}
    if not required_cols.issubset(df.columns):
        return pd.DataFrame()

    if "Volume" not in df.columns:
        df["Volume"] = 0

    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date")
    if df["Date"].duplicated().any():
        raise StrategyValidationError("duplicate historical observations")
    df["ticker"] = ticker

    return df.reset_index(drop=True)


def download_price_history(
    ticker: str,
    start_date: str,
    end_date: str,
    market_data_provider: MarketDataProvider | None = None,
    health_results: list[MarketDataResult] | None = None,
) -> pd.DataFrame:
    market_data = fetch_price_history(
        ticker,
        start=start_date,
        end=end_date or None,
        auto_adjust=True,
        provider=market_data_provider,
    )
    if health_results is not None:
        health_results.append(market_data)

    if not market_data_is_actionable(market_data):
        print(
            f"Skipping {ticker}: data is not actionable: "
            f"{market_data.metadata.reason or market_data.metadata.error}"
        )
        return pd.DataFrame()

    return _normalise_history_df(market_data.data, ticker)


def prepare_indicators(
    df: pd.DataFrame,
    ma_fast_window: int,
    ma_slow_window: int,
    return_lookback_window: int,
) -> pd.DataFrame:
    out = df.copy()

    out["ma_fast"] = out["Close"].rolling(ma_fast_window).mean()
    out["ma_slow"] = out["Close"].rolling(ma_slow_window).mean()
    out["return_lb_pct"] = (out["Close"] / out["Close"].shift(return_lookback_window) - 1.0) * 100.0

    return out


def _long_trade_accounting(
    entry_price: float,
    raw_exit_price: float,
    initial_capital: float,
    position_size_pct: float,
    transaction_cost_bps: float,
    slippage_bps: float,
    spread_bps: float = 0.0,
    tax_bps: float = 0.0,
) -> dict[str, float]:
    notional = initial_capital * position_size_pct / 100.0
    slippage_rate = (slippage_bps + spread_bps / 2.0) / 10000.0
    cost_rate = transaction_cost_bps / 10000.0

    entry_execution_price = entry_price * (1.0 + slippage_rate)
    exit_execution_price = raw_exit_price * (1.0 - slippage_rate)
    shares = notional / entry_execution_price if entry_execution_price > 0 else 0.0

    entry_value = shares * entry_execution_price
    exit_value = shares * exit_execution_price
    entry_fee = entry_value * (cost_rate + tax_bps / 10000.0)
    exit_fee = exit_value * cost_rate
    transaction_cost = entry_fee + exit_fee
    gross_pnl = (raw_exit_price - entry_price) * shares
    net_pnl = exit_value - entry_value - transaction_cost
    slippage_cost = ((entry_price * shares) - entry_value) + (exit_value - (raw_exit_price * shares))

    gross_return_pct = (gross_pnl / notional * 100.0) if notional > 0 else 0.0
    net_return_pct = (net_pnl / notional * 100.0) if notional > 0 else 0.0

    return {
        "entry_execution_price": entry_execution_price,
        "exit_execution_price": exit_execution_price,
        "shares": shares,
        "gross_pnl": gross_pnl,
        "transaction_cost": transaction_cost,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "slippage_cost": abs(slippage_cost),
        "net_pnl": net_pnl,
        "notional": notional,
        "gross_trade_return_pct": gross_return_pct,
        "trade_return_pct": net_return_pct,
        "weighted_return_pct": net_pnl / initial_capital * 100.0 if initial_capital > 0 else 0.0,
    }


def simulate_long_trades_for_ticker(
    df: pd.DataFrame,
    ticker: str,
    position_size_pct: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    max_hold_days: int,
    ma_fast_window: int,
    ma_slow_window: int,
    return_lookback_window: int,
    initial_capital: float = 100000.0,
    transaction_cost_bps: float = 0.0,
    slippage_bps: float = 0.0,
    spread_bps: float = 0.0,
    tax_bps: float = 0.0,
    entry_start: str | None = None,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []

    if df.empty:
        return trades

    numeric = (initial_capital, position_size_pct, stop_loss_pct,
               take_profit_pct, transaction_cost_bps, slippage_bps, spread_bps, tax_bps)
    if not all(isfinite(float(value)) for value in numeric):
        raise StrategyValidationError("backtest parameters must be finite")
    if initial_capital <= 0 or not 0 < position_size_pct <= 100:
        raise StrategyValidationError("capital and position size must be positive and unleveraged")
    if not 0 < stop_loss_pct < 100 or take_profit_pct <= 0:
        raise StrategyValidationError("invalid stop or take-profit percentage")
    if min(transaction_cost_bps, slippage_bps, spread_bps, tax_bps) < 0:
        raise StrategyValidationError("costs must not be negative")
    if slippage_bps + spread_bps / 2 >= 10000:
        raise StrategyValidationError("execution friction must be less than the price")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 1
           for value in (max_hold_days, ma_fast_window, ma_slow_window, return_lookback_window)):
        raise StrategyValidationError("lookback and holding windows must be positive integers")
    dates = pd.to_datetime(df["Date"], errors="raise")
    if dates.isna().any() or dates.duplicated().any() or not dates.is_monotonic_increasing:
        raise StrategyValidationError("history dates must be unique and chronological")
    prices = df[["Open", "High", "Low", "Close"]].astype(float)
    if not all(isfinite(value) and value > 0 for value in prices.to_numpy().flat):
        raise StrategyValidationError("history prices must be finite and positive")
    if ((prices["High"] < prices[["Open", "Close", "Low"]].max(axis=1)) |
        (prices["Low"] > prices[["Open", "Close", "High"]].min(axis=1))).any():
        raise StrategyValidationError("history price ranges are inconsistent")

    df = prepare_indicators(df, ma_fast_window, ma_slow_window, return_lookback_window).copy()
    df = df.reset_index(drop=True)

    warmup = max(ma_fast_window, ma_slow_window, return_lookback_window)

    i = warmup
    while i < len(df) - 1:
        row = df.iloc[i]

        signal_ok = (
            pd.notna(row["ma_fast"])
            and pd.notna(row["ma_slow"])
            and pd.notna(row["return_lb_pct"])
            and row["Close"] > row["ma_fast"]
            and row["ma_fast"] > row["ma_slow"]
            and row["return_lb_pct"] > 0
        )

        if not signal_ok:
            i += 1
            continue

        entry_row = df.iloc[i + 1]
        signal_date = pd.to_datetime(row["Date"])
        entry_date = pd.to_datetime(entry_row["Date"])
        if entry_start is not None and entry_date < pd.Timestamp(entry_start):
            i += 1
            continue
        entry_price = safe_float(entry_row["Open"], 0.0)

        if entry_price <= 0:
            i += 1
            continue

        stop_price = entry_price * (1.0 - stop_loss_pct / 100.0)
        take_profit_price = entry_price * (1.0 + take_profit_pct / 100.0)

        exit_date = None
        exit_price = None
        exit_reason = None
        hold_days = 0

        j = i + 1
        while j < len(df):
            day = df.iloc[j]
            hold_days = j - (i + 1) + 1

            day_low = safe_float(day["Low"], 0.0)
            day_high = safe_float(day["High"], 0.0)
            day_close = safe_float(day["Close"], 0.0)
            day_open = float(day["Open"])

            # An overnight gap cannot fill at a stop price the market skipped.
            # On ambiguous daily bars the stop wins over the profit target.
            if day_open <= stop_price:
                exit_date = pd.to_datetime(day["Date"])
                exit_price = day_open
                exit_reason = "stop_gap"
                break

            if day_open >= take_profit_price:
                exit_date = pd.to_datetime(day["Date"])
                exit_price = take_profit_price
                exit_reason = "take_profit"
                break

            if day_low <= stop_price:
                exit_date = pd.to_datetime(day["Date"])
                exit_price = stop_price
                exit_reason = "stop_loss"
                break

            if day_high >= take_profit_price:
                exit_date = pd.to_datetime(day["Date"])
                exit_price = take_profit_price
                exit_reason = "take_profit"
                break

            if hold_days >= max_hold_days:
                exit_date = pd.to_datetime(day["Date"])
                exit_price = day_close
                exit_reason = "max_hold"
                break

            j += 1

        if exit_date is None:
            final_row = df.iloc[-1]
            exit_date = pd.to_datetime(final_row["Date"])
            exit_price = safe_float(final_row["Close"], entry_price)
            exit_reason = "end_of_data"
            hold_days = len(df) - (i + 1)

        accounting = _long_trade_accounting(
            entry_price=entry_price,
            raw_exit_price=float(exit_price),
            initial_capital=initial_capital,
            position_size_pct=position_size_pct,
            transaction_cost_bps=transaction_cost_bps,
            slippage_bps=slippage_bps,
            spread_bps=spread_bps,
            tax_bps=tax_bps,
        )

        trades.append(
            {
                "ticker": ticker,
                "side": "long",
                "entry_date": entry_date.date().isoformat(),
                "exit_date": exit_date.date().isoformat(),
                "signal_date": signal_date.date().isoformat(),
                "entry_price": entry_price,
                "exit_price": float(exit_price),
                **accounting,
                "stop_price": round(stop_price, 4),
                "take_profit_price": round(take_profit_price, 4),
                "hold_days": int(hold_days),
                "exit_reason": exit_reason,
                "gross_trade_return_pct": round(accounting["gross_trade_return_pct"], 4),
                "trade_return_pct": round(accounting["trade_return_pct"], 4),
                "weighted_return_pct": round(accounting["weighted_return_pct"], 4),
            }
        )

        # Proposed trades are not holdings. A rejected candidate must not
        # suppress later signals until its hypothetical future exit.
        # Portfolio replay alone decides which positions are actually open.
        i += 1

    return trades


def build_equity_curve(
    trades_df: pd.DataFrame,
    initial_capital: float,
    start_date: str,
    end_date: str,
    *,
    histories: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Require historical marks; realised-only curves are not risk evidence."""
    sessions = history_sessions(histories, start_date, end_date)
    result = replay_portfolio(trades_df, histories, initial_capital, sessions)
    if not result.rejected_trades.empty:
        raise StrategyValidationError("equity input contains unfunded trades; use replay_portfolio")
    return result.equity_curve, result.drawdown


def history_sessions(histories, start_date="", end_date="") -> pd.DatetimeIndex:
    dates = [date for history in histories.values() for date in history["Date"]]
    sessions = pd.DatetimeIndex(pd.to_datetime(dates)).sort_values().unique()
    if start_date:
        sessions = sessions[sessions >= pd.Timestamp(start_date)]
    if end_date:
        sessions = sessions[sessions <= pd.Timestamp(end_date)]
    if sessions.empty:
        raise StrategyValidationError("no historical sessions available for evaluation")
    return sessions


def configured_costs(config: dict[str, Any]) -> CostModel:
    raw = config.get("cost_model", {})
    return CostModel(
        spread_bps=raw.get("spread_bps", 0),
        slippage_bps=raw.get("slippage_bps", config.get("slippage_bps", 0)),
        fee_bps=raw.get("fee_bps", config.get("transaction_cost_bps", 0)),
        tax_bps=raw.get("tax_bps", 0),
        stress_multiplier=raw.get("stress_multiplier", 1),
    )


def evaluate_partition(histories, config, start, end, *, stress=False, history_start=None):
    """Fixed parameters, cash reset, past-only warmup, liquidation at window end."""
    costs = configured_costs(config)
    multiplier = float(costs.stress_multiplier) if stress else 1.0
    sliced = {}
    candidates = []
    expected_sessions = history_sessions(histories, str(start), str(end))
    if expected_sessions[0] != pd.Timestamp(start) or expected_sessions[-1] != pd.Timestamp(end):
        raise StrategyValidationError("price history does not cover the evaluation boundaries")
    for ticker, history in sorted(histories.items()):
        dates = pd.to_datetime(history["Date"])
        selected = dates <= pd.Timestamp(end)
        if history_start is not None:
            selected &= dates >= pd.Timestamp(history_start)
        frame = history.loc[selected].copy().reset_index(drop=True)
        observed = pd.DatetimeIndex(pd.to_datetime(frame["Date"]))
        in_window = observed[(observed >= pd.Timestamp(start)) & (observed <= pd.Timestamp(end))]
        if not in_window.equals(expected_sessions):
            raise StrategyValidationError(f"{ticker}: incomplete evaluation-session coverage")
        sliced[ticker] = frame
        candidates.extend(simulate_long_trades_for_ticker(
            frame, ticker,
            initial_capital=float(config["initial_capital"]),
            position_size_pct=float(config["position_size_pct"]),
            stop_loss_pct=float(config["stop_loss_pct"]),
            take_profit_pct=float(config["take_profit_pct"]),
            max_hold_days=int(config["max_hold_days"]),
            ma_fast_window=int(config["ma_fast_window"]),
            ma_slow_window=int(config["ma_slow_window"]),
            return_lookback_window=int(config["return_lookback_window"]),
            transaction_cost_bps=float(costs.fee_bps) * multiplier,
            slippage_bps=float(costs.slippage_bps) * multiplier,
            spread_bps=float(costs.spread_bps) * multiplier,
            tax_bps=float(costs.tax_bps) * multiplier,
            entry_start=str(start),
        ))
    sessions = history_sessions(sliced, str(start), str(end))
    return replay_portfolio(pd.DataFrame(candidates, columns=TRADE_COLUMNS), sliced,
                            float(config["initial_capital"]), sessions)


def partition_benchmark_return(history, start, end, costs):
    dates = pd.to_datetime(history["Date"])
    frame = history.loc[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end))]
    if frame.empty or pd.Timestamp(frame.iloc[0]["Date"]) != pd.Timestamp(start) or pd.Timestamp(frame.iloc[-1]["Date"]) != pd.Timestamp(end):
        raise StrategyValidationError("benchmark does not cover the evaluation window")
    friction = float(costs.slippage_bps + costs.spread_bps / 2) / 10000
    fee = float(costs.fee_bps) / 10000
    tax = float(costs.tax_bps) / 10000
    entry = float(frame.iloc[0]["Open"]) * (1 + friction) * (1 + fee + tax)
    exit_value = float(frame.iloc[-1]["Close"]) * (1 - friction) * (1 - fee)
    if not isfinite(entry) or not isfinite(exit_value) or min(entry, exit_value) <= 0:
        raise StrategyValidationError("invalid benchmark prices or costs")
    return (exit_value / entry - 1) * 100


def evaluate_walk_forward(histories, benchmark_history, config):
    validation = config.get("validation", {})
    windows = validation.get("walk_forward", {})
    if int(windows.get("step_sessions", windows.get("test_sessions", 63))) < int(windows.get("test_sessions", 63)):
        raise StrategyValidationError("walk-forward test windows must not overlap")
    splits = rolling_walk_forward_splits(
        history_sessions(histories),
        train_sessions=int(windows.get("train_sessions", 252)),
        validation_sessions=int(windows.get("validation_sessions", 63)),
        test_sessions=int(windows.get("test_sessions", 63)),
        step_sessions=int(windows.get("step_sessions", windows.get("test_sessions", 63))),
    )
    if len(splits) < int(validation.get("minimum_walk_forward_folds", 3)):
        raise StrategyValidationError("insufficient walk-forward folds for configured minimum")
    if not benchmark_history:
        raise StrategyValidationError("a benchmark is required for experiment evidence")
    benchmark_name = str(config.get("benchmark_tickers", list(benchmark_history))[0])
    if benchmark_name not in benchmark_history:
        raise StrategyValidationError("configured primary benchmark is missing")
    benchmark = benchmark_history[benchmark_name]
    costs = configured_costs(config)
    rows = []
    for split in splits:
        validation_result = evaluate_partition(histories, config, split.validation_start,
                                               split.validation_end, history_start=split.train_start)
        test = evaluate_partition(histories, config, split.test_start, split.test_end,
                                  history_start=split.train_start)
        stressed = evaluate_partition(histories, config, split.test_start, split.test_end,
                                      stress=True, history_start=split.train_start)
        validation_benchmark = partition_benchmark_return(
            benchmark, split.validation_start, split.validation_end, costs)
        test_benchmark = partition_benchmark_return(benchmark, split.test_start, split.test_end, costs)
        rows.append({
            "fold": split.fold,
            "validation_start": str(split.validation_start), "validation_end": str(split.validation_end),
            "test_start": str(split.test_start), "test_end": str(split.test_end),
            "validation_return_pct": float(validation_result.equity_curve.iloc[-1]["cumulative_return_pct"]),
            "validation_benchmark_return_pct": validation_benchmark,
            "test_return_pct": float(test.equity_curve.iloc[-1]["cumulative_return_pct"]),
            "benchmark_return_pct": test_benchmark,
            "test_max_drawdown_pct": float(test.drawdown["drawdown_pct"].min()),
            "stressed_test_return_pct": float(stressed.equity_curve.iloc[-1]["cumulative_return_pct"]),
            "test_trades": len(test.accepted_trades),
            "test_rejected_trades": len(test.rejected_trades),
        })
    evidence = pd.DataFrame(rows)
    evidence["validation_excess_return_pct"] = evidence["validation_return_pct"] - evidence["validation_benchmark_return_pct"]
    evidence["test_excess_return_pct"] = evidence["test_return_pct"] - evidence["benchmark_return_pct"]
    evidence["stressed_test_excess_return_pct"] = evidence["stressed_test_return_pct"] - evidence["benchmark_return_pct"]
    return splits, evidence


def calculate_benchmark_return(history_df: pd.DataFrame) -> float:
    if history_df.empty or len(history_df) < 2:
        return 0.0
    first_close = safe_float(history_df.iloc[0]["Close"], 0.0)
    last_close = safe_float(history_df.iloc[-1]["Close"], 0.0)
    if first_close <= 0:
        return 0.0
    return (last_close / first_close - 1.0) * 100.0


def build_benchmark_comparison(
    benchmark_history: dict[str, pd.DataFrame],
    strategy_return_pct: float,
    start_date: str,
    end_date: str,
    costs: CostModel | None = None,
) -> pd.DataFrame:
    rows = []
    for ticker, history_df in benchmark_history.items():
        if history_df.empty:
            raise StrategyValidationError(f"missing benchmark history: {ticker}")
        dates = pd.to_datetime(history_df["Date"])
        history_df = history_df.loc[(dates >= pd.Timestamp(start_date)) & (dates <= pd.Timestamp(end_date))]
        benchmark_return_pct = (partition_benchmark_return(history_df, start_date, end_date, costs)
                                if costs is not None else calculate_benchmark_return(history_df))
        benchmark_start = start_date
        benchmark_end = end_date
        if not history_df.empty:
            benchmark_start = pd.to_datetime(history_df.iloc[0]["Date"]).date().isoformat()
            benchmark_end = pd.to_datetime(history_df.iloc[-1]["Date"]).date().isoformat()
        rows.append(
            {
                "benchmark_ticker": ticker,
                "start_date": benchmark_start,
                "end_date": benchmark_end,
                "benchmark_return_pct": round(benchmark_return_pct, 4),
                "strategy_return_pct": round(strategy_return_pct, 4),
                "excess_return_pct": round(strategy_return_pct - benchmark_return_pct, 4),
            }
        )
    return pd.DataFrame(rows, columns=BENCHMARK_COLUMNS)


def calculate_summary(
    trades_df: pd.DataFrame,
    config: dict[str, Any],
    run_id: str,
    equity_df: pd.DataFrame | None = None,
    drawdown_df: pd.DataFrame | None = None,
    benchmark_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    initial_capital = safe_float(config.get("initial_capital"), 100000.0)
    strategy_return_pct = 0.0
    max_drawdown_pct = 0.0
    avg_exposure_pct = 0.0
    max_exposure_pct = 0.0
    turnover_pct = 0.0

    if equity_df is not None and not equity_df.empty:
        strategy_return_pct = safe_float(equity_df.iloc[-1]["cumulative_return_pct"], 0.0)
        avg_exposure_pct = safe_float(equity_df["exposure_pct"].mean(), 0.0)
        max_exposure_pct = safe_float(equity_df["exposure_pct"].max(), 0.0)
        turnover_pct = safe_float(equity_df["turnover_pct"].sum(), 0.0)
    if drawdown_df is not None and not drawdown_df.empty:
        max_drawdown_pct = safe_float(drawdown_df["drawdown_pct"].min(), 0.0)

    first_benchmark_return = 0.0
    first_excess_return = strategy_return_pct
    if benchmark_df is not None and not benchmark_df.empty:
        first_benchmark_return = safe_float(benchmark_df.iloc[0]["benchmark_return_pct"], 0.0)
        first_excess_return = safe_float(benchmark_df.iloc[0]["excess_return_pct"], strategy_return_pct)

    base = {
        "run_id": run_id,
        "generated_at": utc_now_iso(),
        "tickers_requested": len(config.get("tickers", [])),
        "tickers_tested": 0 if trades_df.empty else trades_df["ticker"].nunique(),
        "total_trades": len(trades_df),
        "winners": 0,
        "losers": 0,
        "win_rate_pct": 0.0,
        "avg_trade_return_pct": 0.0,
        "median_trade_return_pct": 0.0,
        "avg_weighted_return_pct": 0.0,
        "profit_factor": 0.0,
        "avg_hold_days": 0.0,
        "strategy_return_pct": round(strategy_return_pct, 4),
        "benchmark_return_pct": round(first_benchmark_return, 4),
        "excess_return_pct": round(first_excess_return, 4),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "avg_exposure_pct": round(avg_exposure_pct, 4),
        "max_exposure_pct": round(max_exposure_pct, 4),
        "turnover_pct": round(turnover_pct, 4),
        "start_date": config.get("start_date", ""),
        "end_date": config.get("end_date", "") or "",
        "initial_capital": initial_capital,
        "position_size_pct": safe_float(config.get("position_size_pct"), 10.0),
        "transaction_cost_bps": safe_float(config.get("transaction_cost_bps"), 0.0),
        "slippage_bps": safe_float(config.get("slippage_bps"), 0.0),
        "stop_loss_pct": safe_float(config.get("stop_loss_pct"), 8.0),
        "take_profit_pct": safe_float(config.get("take_profit_pct"), 15.0),
        "max_hold_days": safe_int(config.get("max_hold_days"), 30),
    }

    if trades_df.empty:
        return pd.DataFrame([base])

    winners_df = trades_df[trades_df["trade_return_pct"] > 0].copy()
    losers_df = trades_df[trades_df["trade_return_pct"] < 0].copy()

    gross_profit = winners_df["net_pnl"].sum()
    gross_loss = abs(losers_df["net_pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

    base.update(
        {
            "winners": int((trades_df["trade_return_pct"] > 0).sum()),
            "losers": int((trades_df["trade_return_pct"] < 0).sum()),
            "win_rate_pct": round((trades_df["trade_return_pct"] > 0).mean() * 100.0, 2),
            "avg_trade_return_pct": round(trades_df["trade_return_pct"].mean(), 4),
            "median_trade_return_pct": round(trades_df["trade_return_pct"].median(), 4),
            "avg_weighted_return_pct": round(trades_df["weighted_return_pct"].mean(), 4),
            "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
            "avg_hold_days": round(trades_df["hold_days"].mean(), 2),
        }
    )

    return pd.DataFrame([base])


def _empty_trades_df() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_COLUMNS)


def record_immutable_experiment(
    *,
    run_id: str,
    config: dict[str, Any],
    trades_df: pd.DataFrame,
    histories: dict[str, pd.DataFrame],
    benchmark_history: dict[str, pd.DataFrame],
) -> str | None:
    """Save reproducible research evidence; this grants no execution authority."""
    if config.get("experiment_registry_enabled") is not True:
        return None
    splits, evidence = evaluate_walk_forward(histories, benchmark_history, config)
    repeated_splits, repeated_evidence = evaluate_walk_forward(histories, benchmark_history, config)
    reproducible = splits == repeated_splits and evidence.equals(repeated_evidence)
    if not reproducible:
        raise StrategyValidationError("replaying the same snapshot changed evaluation results")
    snapshot_rows: list[dict[str, object]] = []
    for role, frames in (("strategy", histories), ("benchmark", benchmark_history)):
        for ticker, history in sorted(frames.items()):
            for row in history.to_dict(orient="records"):
                snapshot_rows.append({**row, "ticker": ticker, "role": role})
    data_snapshot_id = content_hash(snapshot_rows)
    universe_identity = {
        "kind": "static_current_universe_unqualified",
        "tickers": sorted(histories),
    }
    if "research_universe_checksum" in config:
        universe_identity["selected_manifest_checksum"] = config["research_universe_checksum"]
    universe_checksum = content_hash(universe_identity)
    costs = configured_costs(config)
    parameters = {
        key: config.get(key)
        for key in (
            "ma_fast_window",
            "ma_slow_window",
            "return_lookback_window",
            "stop_loss_pct",
            "take_profit_pct",
            "max_hold_days",
            "position_size_pct",
            "initial_capital",
            "benchmark_tickers",
            "validation",
        )
    }
    parameters["objective"] = "virtual_functionality_no_return_target"
    parameters["portfolio_accounting_version"] = "cash_mark_to_market_v1"
    parameters["window_policy"] = "fixed_parameters_flat_start_end_liquidation"
    if "research_universe_checksum" in config:
        parameters["research_universe_version"] = config["research_universe_version"]
        parameters["research_universe_checksum"] = config["research_universe_checksum"]
        parameters["selected_tickers"] = config["tickers"]
    spec = ExperimentSpec.create(
        strategy_version=str(config.get("strategy_version", "unversioned")),
        data_snapshot_id=data_snapshot_id,
        point_in_time_universe_checksum=universe_checksum,
        code_revision=os.environ.get(
            "INVESTING_CODE_REVISION", "working-tree-unverified"
        ),
        environment="offline",
        parameters=parameters,
        splits=splits,
        cost_model=costs,
        tuned_on_partition=str(config.get("parameter_tuning_partition", "validation")),
    )
    # Use the weakest window for gate metrics. These are observations, not a
    # return target, and are not compounded into a fictitious trading history.
    metrics = ExperimentMetrics(
        validation_excess_return_pct=float(evidence["validation_excess_return_pct"].min()),
        test_excess_return_pct=float(evidence["test_excess_return_pct"].min()),
        test_max_drawdown_pct=float(evidence["test_max_drawdown_pct"].min()),
        stressed_test_excess_return_pct=float(evidence["stressed_test_excess_return_pct"].min()),
        benchmark_return_pct=float(evidence["benchmark_return_pct"].min()),
        walk_forward_passed_folds=int((evidence["test_excess_return_pct"] >= 0).sum()),
        walk_forward_total_folds=len(splits),
        deterministic_reproduction=reproducible,
        point_in_time_universe_passed=False,
        survivorship_checks_passed=False,
        exchange_calendar_passed=False,
        cost_model_passed=False,
    )
    evidence_dir = run_path(run_id, "backtest_evidence")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    prices_path = evidence_dir / "historical_prices.csv"
    folds_path = evidence_dir / "walk_forward.csv"
    # Save actual inputs and every fold, not merely a hash of discarded inputs.
    pd.DataFrame(snapshot_rows).to_csv(prices_path, index=False)
    evidence.to_csv(folds_path, index=False)
    sources = tuple(
        path
        for path in (
            BACKTEST_TRADES_FILE,
            BACKTEST_SUMMARY_FILE,
            BACKTEST_EQUITY_CURVE_FILE,
            BACKTEST_DRAWDOWN_FILE,
            BACKTEST_BENCHMARK_FILE,
            BACKTEST_REJECTED_FILE,
            prices_path,
            folds_path,
        )
        if path.is_file()
    )
    registry = ExperimentRegistry(EXPERIMENT_REGISTRY_PATH)
    spec = registry.resolve_identity(spec)
    artifact = write_experiment_artifact(
        run_path(run_id, "experiments"),
        spec=spec,
        metrics=metrics,
        source_artifacts=sources,
    )
    registry.record(spec, artifact)
    return str(spec.experiment_id)


def run() -> None:
    run_id = get_or_create_run_id()
    print(f"Run ID: {run_id}")

    config = load_backtest_config()

    if not bool(config.get("enabled", True)):
        print("Backtesting disabled in config.")
        return

    tickers = [str(t).strip().upper() for t in config.get("tickers", []) if str(t).strip()]
    benchmark_tickers = [str(t).strip().upper() for t in config.get("benchmark_tickers", ["SPY", "QQQ"]) if str(t).strip()]
    start_date = str(config.get("start_date", "2022-01-01")).strip()
    end_date = str(config.get("end_date", "")).strip()

    initial_capital = safe_float(config.get("initial_capital"), 100000.0)
    min_history_days = safe_int(config.get("min_history_days"), 120)
    allow_short = bool(config.get("allow_short", False))

    if allow_short:
        raise StrategyValidationError("short selling is not supported by this research engine")

    strategy_history: dict[str, pd.DataFrame] = {}
    health_results: list[MarketDataResult] = []
    tickers_tested = 0

    for ticker in tickers:
        print(f"Backtesting {ticker}...")
        history_df = download_price_history(
            ticker,
            start_date,
            end_date,
            health_results=health_results,
        )

        if history_df.empty or len(history_df) < min_history_days:
            print(f"Skipping {ticker}: insufficient history.")
            continue

        strategy_history[ticker] = history_df.copy()

        tickers_tested += 1

    sessions = history_sessions(strategy_history, start_date, end_date)
    replay = evaluate_partition(strategy_history, config, str(sessions[0].date()),
                                str(sessions[-1].date()))
    trades_df = replay.accepted_trades
    equity_df, drawdown_df = replay.equity_curve, replay.drawdown
    strategy_return_pct = safe_float(equity_df.iloc[-1]["cumulative_return_pct"], 0.0) if not equity_df.empty else 0.0

    benchmark_history: dict[str, pd.DataFrame] = {}
    for benchmark_ticker in benchmark_tickers:
        print(f"Benchmarking against {benchmark_ticker}...")
        benchmark_history[benchmark_ticker] = download_price_history(
            benchmark_ticker,
            start_date,
            end_date,
            health_results=health_results,
        )
    benchmark_df = build_benchmark_comparison(
        benchmark_history, strategy_return_pct, str(sessions[0].date()),
        str(sessions[-1].date()), configured_costs(config))

    summary_df = calculate_summary(
        trades_df,
        config,
        run_id=run_id,
        equity_df=equity_df,
        drawdown_df=drawdown_df,
        benchmark_df=benchmark_df,
    )
    summary_df["tickers_tested"] = tickers_tested
    summary_df["rejected_trades"] = len(replay.rejected_trades)
    summary_df["objective"] = "virtual_functionality_no_return_target"
    summary_df["return_target_pct"] = None
    summary_df["accounting"] = "cash_plus_marked_positions"
    summary_df["data_qualification"] = "research_only_unqualified"
    summary_df["start_date"] = str(sessions[0].date())
    summary_df["end_date"] = str(sessions[-1].date())

    append_market_data_health_artifact(health_results)

    write_csv_with_run_id(trades_df, BACKTEST_TRADES_FILE, run_id=run_id)
    write_csv_with_run_id(summary_df, BACKTEST_SUMMARY_FILE, run_id=run_id)
    write_csv_with_run_id(equity_df, BACKTEST_EQUITY_CURVE_FILE, run_id=run_id)
    write_csv_with_run_id(drawdown_df, BACKTEST_DRAWDOWN_FILE, run_id=run_id)
    write_csv_with_run_id(benchmark_df, BACKTEST_BENCHMARK_FILE, run_id=run_id)
    write_csv_with_run_id(replay.rejected_trades, BACKTEST_REJECTED_FILE, run_id=run_id)

    experiment_id = record_immutable_experiment(
        run_id=run_id,
        config=config,
        trades_df=trades_df,
        histories=strategy_history,
        benchmark_history=benchmark_history,
    )

    total_trades = len(trades_df)
    win_rate = 0.0 if total_trades == 0 else round((trades_df["trade_return_pct"] > 0).mean() * 100.0, 2)

    print("\nBacktesting Agent finished.")
    print(f"Saved backtest trades to: {BACKTEST_TRADES_FILE}")
    print(f"Saved backtest summary to: {BACKTEST_SUMMARY_FILE}")
    print(f"Saved backtest equity curve to: {BACKTEST_EQUITY_CURVE_FILE}")
    print(f"Saved backtest drawdown to: {BACKTEST_DRAWDOWN_FILE}")
    print(f"Saved backtest benchmark comparison to: {BACKTEST_BENCHMARK_FILE}")
    print("\nRun summary:")
    print(f"Run ID: {run_id}")
    print(f"Tickers requested: {len(tickers)}")
    print(f"Tickers tested: {tickers_tested}")
    print(f"Total trades: {total_trades}")
    print(f"Win rate: {win_rate}%")
    if experiment_id:
        print(f"Immutable experiment ID: {experiment_id}")
        print("Promotion status: not evaluated; advisory-only evidence gate applies")


if __name__ == "__main__":
    run()
