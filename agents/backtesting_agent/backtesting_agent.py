from __future__ import annotations

from datetime import datetime, timezone
from math import isnan
from typing import Any

import pandas as pd
import yfinance as yf

from shared.io_utils import load_yaml, write_csv_with_run_id
from shared.paths import config_path, data_path
from shared.run_context import get_or_create_run_id


BACKTEST_CONFIG_FILE = config_path("backtesting.yaml")
BACKTEST_TRADES_FILE = data_path("backtest_trades.csv")
BACKTEST_SUMMARY_FILE = data_path("backtest_summary.csv")


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

    if not config:
        return {
            "enabled": True,
            "tickers": ["AAPL", "MSFT", "NVDA", "AMZN"],
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
        }

    return config


def download_price_history(ticker: str, start_date: str, end_date: str) -> pd.DataFrame:
    df = yf.download(
        ticker,
        start=start_date,
        end=end_date or None,
        auto_adjust=True,
        progress=False,
    )

    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()

    required_cols = {"Date", "Open", "High", "Low", "Close"}
    if not required_cols.issubset(df.columns):
        return pd.DataFrame()

    df = df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    df["Date"] = pd.to_datetime(df["Date"])
    df["ticker"] = ticker

    return df


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

        pnl_pct = ((exit_price / entry_price) - 1.0) * 100.0
        notional = position_size_pct / 100.0
        pnl_weighted_pct = pnl_pct * notional

        trades.append(
            {
                "ticker": ticker,
                "side": "long",
                "entry_date": entry_date.date().isoformat(),
                "exit_date": exit_date.date().isoformat(),
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "stop_price": round(stop_price, 4),
                "take_profit_price": round(take_profit_price, 4),
                "hold_days": int(hold_days),
                "exit_reason": exit_reason,
                "trade_return_pct": round(pnl_pct, 4),
                "weighted_return_pct": round(pnl_weighted_pct, 4),
            }
        )

        # move past the exit day to avoid overlapping same-ticker positions
        exit_idx = df.index[df["Date"] == pd.Timestamp(exit_date)]
        if len(exit_idx) > 0:
            i = int(exit_idx[0]) + 1
        else:
            i = j + 1

    return trades


def calculate_summary(
    trades_df: pd.DataFrame,
    config: dict[str, Any],
    run_id: str,
) -> pd.DataFrame:
    if trades_df.empty:
        return pd.DataFrame(
            [
                {
                    "run_id": run_id,
                    "generated_at": utc_now_iso(),
                    "tickers_requested": len(config.get("tickers", [])),
                    "tickers_tested": 0,
                    "total_trades": 0,
                    "winners": 0,
                    "losers": 0,
                    "win_rate_pct": 0.0,
                    "avg_trade_return_pct": 0.0,
                    "median_trade_return_pct": 0.0,
                    "avg_weighted_return_pct": 0.0,
                    "profit_factor": 0.0,
                    "avg_hold_days": 0.0,
                    "start_date": config.get("start_date", ""),
                    "end_date": config.get("end_date", "") or "",
                    "position_size_pct": safe_float(config.get("position_size_pct"), 10.0),
                    "stop_loss_pct": safe_float(config.get("stop_loss_pct"), 8.0),
                    "take_profit_pct": safe_float(config.get("take_profit_pct"), 15.0),
                    "max_hold_days": safe_int(config.get("max_hold_days"), 30),
                }
            ]
        )

    winners_df = trades_df[trades_df["trade_return_pct"] > 0].copy()
    losers_df = trades_df[trades_df["trade_return_pct"] < 0].copy()

    gross_profit = winners_df["trade_return_pct"].sum()
    gross_loss = abs(losers_df["trade_return_pct"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    summary = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "generated_at": utc_now_iso(),
                "tickers_requested": len(config.get("tickers", [])),
                "tickers_tested": trades_df["ticker"].nunique(),
                "total_trades": len(trades_df),
                "winners": int((trades_df["trade_return_pct"] > 0).sum()),
                "losers": int((trades_df["trade_return_pct"] < 0).sum()),
                "win_rate_pct": round((trades_df["trade_return_pct"] > 0).mean() * 100.0, 2),
                "avg_trade_return_pct": round(trades_df["trade_return_pct"].mean(), 4),
                "median_trade_return_pct": round(trades_df["trade_return_pct"].median(), 4),
                "avg_weighted_return_pct": round(trades_df["weighted_return_pct"].mean(), 4),
                "profit_factor": round(profit_factor, 4),
                "avg_hold_days": round(trades_df["hold_days"].mean(), 2),
                "start_date": config.get("start_date", ""),
                "end_date": config.get("end_date", "") or "",
                "position_size_pct": safe_float(config.get("position_size_pct"), 10.0),
                "stop_loss_pct": safe_float(config.get("stop_loss_pct"), 8.0),
                "take_profit_pct": safe_float(config.get("take_profit_pct"), 15.0),
                "max_hold_days": safe_int(config.get("max_hold_days"), 30),
            }
        ]
    )

    return summary


def run() -> None:
    run_id = get_or_create_run_id()
    print(f"Run ID: {run_id}")

    config = load_backtest_config()

    if not bool(config.get("enabled", True)):
        print("Backtesting disabled in config.")
        return

    tickers = [str(t).strip().upper() for t in config.get("tickers", []) if str(t).strip()]
    start_date = str(config.get("start_date", "2022-01-01")).strip()
    end_date = str(config.get("end_date", "")).strip()

    position_size_pct = safe_float(config.get("position_size_pct"), 10.0)
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
    tickers_tested = 0

    for ticker in tickers:
        print(f"Backtesting {ticker}...")
        history_df = download_price_history(ticker, start_date, end_date)

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
        )

        if ticker_trades:
            all_trades.extend(ticker_trades)

        tickers_tested += 1

    trades_df = pd.DataFrame(all_trades)
    if trades_df.empty:
        trades_df = pd.DataFrame(
            columns=[
                "ticker",
                "side",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "stop_price",
                "take_profit_price",
                "hold_days",
                "exit_reason",
                "trade_return_pct",
                "weighted_return_pct",
            ]
        )

    summary_df = calculate_summary(trades_df, config, run_id=run_id)

    write_csv_with_run_id(trades_df, BACKTEST_TRADES_FILE, run_id=run_id)
    write_csv_with_run_id(summary_df, BACKTEST_SUMMARY_FILE, run_id=run_id)

    total_trades = len(trades_df)
    win_rate = 0.0 if total_trades == 0 else round((trades_df["trade_return_pct"] > 0).mean() * 100.0, 2)

    print("\nBacktesting Agent finished.")
    print(f"Saved backtest trades to: {BACKTEST_TRADES_FILE}")
    print(f"Saved backtest summary to: {BACKTEST_SUMMARY_FILE}")
    print("\nRun summary:")
    print(f"Run ID: {run_id}")
    print(f"Tickers requested: {len(tickers)}")
    print(f"Tickers tested: {tickers_tested}")
    print(f"Total trades: {total_trades}")
    print(f"Win rate: {win_rate}%")


if __name__ == "__main__":
    run()