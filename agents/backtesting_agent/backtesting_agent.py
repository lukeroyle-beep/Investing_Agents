from __future__ import annotations

from datetime import datetime, timezone
from math import isnan
from typing import Any

import pandas as pd

from shared.market_data import (
    MarketDataProvider,
    MarketDataResult,
    append_market_data_health_artifact,
    fetch_price_history,
)
from shared.io_utils import load_yaml, write_csv_with_run_id
from shared.paths import config_path, data_path
from shared.run_context import get_or_create_run_id


BACKTEST_CONFIG_FILE = config_path("backtesting.yaml")
BACKTEST_TRADES_FILE = data_path("backtest_trades.csv")
BACKTEST_SUMMARY_FILE = data_path("backtest_summary.csv")
BACKTEST_EQUITY_CURVE_FILE = data_path("backtest_equity_curve.csv")
BACKTEST_DRAWDOWN_FILE = data_path("backtest_drawdown.csv")
BACKTEST_BENCHMARK_FILE = data_path("backtest_benchmark_comparison.csv")


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
    df = df.sort_values("Date").drop_duplicates("Date", keep="last")
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

    if market_data.metadata.error:
        print(f"Skipping {ticker}: market data error: {market_data.metadata.error}")
        return pd.DataFrame()

    if market_data.metadata.stale:
        print(f"Warning: {ticker} market data is stale as of {market_data.metadata.as_of}.")

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
) -> dict[str, float]:
    notional = initial_capital * position_size_pct / 100.0
    slippage_rate = slippage_bps / 10000.0
    cost_rate = transaction_cost_bps / 10000.0

    entry_execution_price = entry_price * (1.0 + slippage_rate)
    exit_execution_price = raw_exit_price * (1.0 - slippage_rate)
    shares = notional / entry_execution_price if entry_execution_price > 0 else 0.0

    entry_value = shares * entry_execution_price
    exit_value = shares * exit_execution_price
    transaction_cost = (entry_value + exit_value) * cost_rate
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
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []

    if df.empty:
        return trades

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
        )

        trades.append(
            {
                "ticker": ticker,
                "side": "long",
                "entry_date": entry_date.date().isoformat(),
                "exit_date": exit_date.date().isoformat(),
                "signal_date": signal_date.date().isoformat(),
                "entry_price": round(entry_price, 4),
                "exit_price": round(float(exit_price), 4),
                "entry_execution_price": round(accounting["entry_execution_price"], 4),
                "exit_execution_price": round(accounting["exit_execution_price"], 4),
                "shares": round(accounting["shares"], 6),
                "gross_pnl": round(accounting["gross_pnl"], 4),
                "transaction_cost": round(accounting["transaction_cost"], 4),
                "slippage_cost": round(accounting["slippage_cost"], 4),
                "net_pnl": round(accounting["net_pnl"], 4),
                "notional": round(accounting["notional"], 4),
                "stop_price": round(stop_price, 4),
                "take_profit_price": round(take_profit_price, 4),
                "hold_days": int(hold_days),
                "exit_reason": exit_reason,
                "gross_trade_return_pct": round(accounting["gross_trade_return_pct"], 4),
                "trade_return_pct": round(accounting["trade_return_pct"], 4),
                "weighted_return_pct": round(accounting["weighted_return_pct"], 4),
            }
        )

        # move past the exit day to avoid overlapping same-ticker positions
        exit_idx = df.index[df["Date"] == pd.Timestamp(exit_date)]
        if len(exit_idx) > 0:
            i = int(exit_idx[0]) + 1
        else:
            i = j + 1

    return trades


def _date_range_from_inputs(start_date: str, end_date: str, trades_df: pd.DataFrame) -> pd.DatetimeIndex:
    if trades_df.empty and not start_date:
        return pd.DatetimeIndex([])

    start = pd.to_datetime(start_date) if start_date else pd.to_datetime(trades_df["entry_date"].min())
    if end_date:
        end = pd.to_datetime(end_date)
    elif trades_df.empty:
        end = start
    else:
        end = pd.to_datetime(trades_df["exit_date"].max())

    if pd.isna(start) or pd.isna(end) or end < start:
        return pd.DatetimeIndex([])

    return pd.date_range(start=start, end=end, freq="D")


def build_equity_curve(
    trades_df: pd.DataFrame,
    initial_capital: float,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    dates = _date_range_from_inputs(start_date, end_date, trades_df)
    if len(dates) == 0:
        equity_df = pd.DataFrame(columns=EQUITY_COLUMNS)
        drawdown_df = pd.DataFrame(columns=DRAWDOWN_COLUMNS)
        return equity_df, drawdown_df

    rows: list[dict[str, Any]] = []
    equity = initial_capital
    trade_rows = trades_df.to_dict("records") if not trades_df.empty else []

    for date in dates:
        date_text = date.date().isoformat()
        exited_today = [trade for trade in trade_rows if trade.get("exit_date") == date_text]
        entered_today = [trade for trade in trade_rows if trade.get("entry_date") == date_text]
        open_today = [
            trade
            for trade in trade_rows
            if str(trade.get("entry_date", "")) <= date_text <= str(trade.get("exit_date", ""))
        ]

        daily_pnl = sum(safe_float(trade.get("net_pnl"), 0.0) for trade in exited_today)
        turnover_notional = sum(safe_float(trade.get("notional"), 0.0) for trade in entered_today + exited_today)
        exposure_notional = sum(safe_float(trade.get("notional"), 0.0) for trade in open_today)
        equity += daily_pnl

        rows.append(
            {
                "date": date_text,
                "equity": round(equity, 4),
                "daily_pnl": round(daily_pnl, 4),
                "cumulative_return_pct": round((equity / initial_capital - 1.0) * 100.0, 4) if initial_capital > 0 else 0.0,
                "open_positions": len(open_today),
                "exposure_notional": round(exposure_notional, 4),
                "exposure_pct": round(exposure_notional / equity * 100.0, 4) if equity > 0 else 0.0,
                "turnover_notional": round(turnover_notional, 4),
                "turnover_pct": round(turnover_notional / initial_capital * 100.0, 4) if initial_capital > 0 else 0.0,
            }
        )

    equity_df = pd.DataFrame(rows, columns=EQUITY_COLUMNS)
    running_peak = equity_df["equity"].cummax()
    drawdown = equity_df["equity"] - running_peak
    drawdown_df = pd.DataFrame(
        {
            "date": equity_df["date"],
            "equity": equity_df["equity"],
            "peak_equity": running_peak.round(4),
            "drawdown": drawdown.round(4),
            "drawdown_pct": ((drawdown / running_peak) * 100.0).round(4),
        },
        columns=DRAWDOWN_COLUMNS,
    )

    return equity_df, drawdown_df


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
) -> pd.DataFrame:
    rows = []
    for ticker, history_df in benchmark_history.items():
        benchmark_return_pct = calculate_benchmark_return(history_df)
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

    gross_profit = winners_df["trade_return_pct"].sum()
    gross_loss = abs(losers_df["trade_return_pct"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    base.update(
        {
            "winners": int((trades_df["trade_return_pct"] > 0).sum()),
            "losers": int((trades_df["trade_return_pct"] < 0).sum()),
            "win_rate_pct": round((trades_df["trade_return_pct"] > 0).mean() * 100.0, 2),
            "avg_trade_return_pct": round(trades_df["trade_return_pct"].mean(), 4),
            "median_trade_return_pct": round(trades_df["trade_return_pct"].median(), 4),
            "avg_weighted_return_pct": round(trades_df["weighted_return_pct"].mean(), 4),
            "profit_factor": round(profit_factor, 4),
            "avg_hold_days": round(trades_df["hold_days"].mean(), 2),
        }
    )

    return pd.DataFrame([base])


def _empty_trades_df() -> pd.DataFrame:
    return pd.DataFrame(columns=TRADE_COLUMNS)


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
    position_size_pct = safe_float(config.get("position_size_pct"), 10.0)
    transaction_cost_bps = safe_float(config.get("transaction_cost_bps"), 0.0)
    slippage_bps = safe_float(config.get("slippage_bps"), 0.0)
    ma_fast_window = safe_int(config.get("ma_fast_window"), 20)
    ma_slow_window = safe_int(config.get("ma_slow_window"), 50)
    return_lookback_window = safe_int(config.get("return_lookback_window"), 60)
    stop_loss_pct = safe_float(config.get("stop_loss_pct"), 8.0)
    take_profit_pct = safe_float(config.get("take_profit_pct"), 15.0)
    max_hold_days = safe_int(config.get("max_hold_days"), 30)
    min_history_days = safe_int(config.get("min_history_days"), 120)
    allow_short = bool(config.get("allow_short", False))

    if allow_short:
        print("Note: allow_short=true is not implemented in this first version. Running long-only.")

    all_trades: list[dict[str, Any]] = []
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

        ticker_trades = simulate_long_trades_for_ticker(
            df=history_df,
            ticker=ticker,
            position_size_pct=position_size_pct,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            max_hold_days=max_hold_days,
            ma_fast_window=ma_fast_window,
            ma_slow_window=ma_slow_window,
            return_lookback_window=return_lookback_window,
            initial_capital=initial_capital,
            transaction_cost_bps=transaction_cost_bps,
            slippage_bps=slippage_bps,
        )

        if ticker_trades:
            all_trades.extend(ticker_trades)

        tickers_tested += 1

    trades_df = pd.DataFrame(all_trades)
    if trades_df.empty:
        trades_df = _empty_trades_df()

    equity_df, drawdown_df = build_equity_curve(trades_df, initial_capital, start_date, end_date)
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
    benchmark_df = build_benchmark_comparison(benchmark_history, strategy_return_pct, start_date, end_date)

    summary_df = calculate_summary(
        trades_df,
        config,
        run_id=run_id,
        equity_df=equity_df,
        drawdown_df=drawdown_df,
        benchmark_df=benchmark_df,
    )
    summary_df["tickers_tested"] = tickers_tested

    append_market_data_health_artifact(health_results)

    write_csv_with_run_id(trades_df, BACKTEST_TRADES_FILE, run_id=run_id)
    write_csv_with_run_id(summary_df, BACKTEST_SUMMARY_FILE, run_id=run_id)
    write_csv_with_run_id(equity_df, BACKTEST_EQUITY_CURVE_FILE, run_id=run_id)
    write_csv_with_run_id(drawdown_df, BACKTEST_DRAWDOWN_FILE, run_id=run_id)
    write_csv_with_run_id(benchmark_df, BACKTEST_BENCHMARK_FILE, run_id=run_id)

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


if __name__ == "__main__":
    run()
