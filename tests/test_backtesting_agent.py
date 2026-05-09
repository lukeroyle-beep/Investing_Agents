from __future__ import annotations

import pandas as pd

from agents.backtesting_agent import backtesting_agent as agent


def _trend_history(days: int = 12) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=days, freq="D")
    closes = [100 + idx for idx in range(days)]
    rows = []
    for idx, close in enumerate(closes):
        rows.append(
            {
                "Date": dates[idx],
                "Open": close + 1,
                "High": close + 3,
                "Low": close - 1,
                "Close": close,
                "Volume": 1000 + idx,
            }
        )
    return pd.DataFrame(rows)


def test_backtest_entries_use_next_open_without_forward_looking_leakage():
    trades = agent.simulate_long_trades_for_ticker(
        df=_trend_history(),
        ticker="TEST",
        initial_capital=100000,
        position_size_pct=10,
        transaction_cost_bps=0,
        slippage_bps=0,
        stop_loss_pct=50,
        take_profit_pct=50,
        max_hold_days=2,
        ma_fast_window=2,
        ma_slow_window=3,
        return_lookback_window=3,
    )

    assert trades
    first_trade = trades[0]
    assert first_trade["signal_date"] == "2024-01-04"
    assert first_trade["entry_date"] == "2024-01-05"
    assert first_trade["entry_price"] == 105


def test_transaction_costs_and_slippage_reduce_net_returns():
    no_cost = agent.simulate_long_trades_for_ticker(
        df=_trend_history(),
        ticker="TEST",
        initial_capital=100000,
        position_size_pct=10,
        transaction_cost_bps=0,
        slippage_bps=0,
        stop_loss_pct=50,
        take_profit_pct=50,
        max_hold_days=2,
        ma_fast_window=2,
        ma_slow_window=3,
        return_lookback_window=3,
    )[0]
    with_cost = agent.simulate_long_trades_for_ticker(
        df=_trend_history(),
        ticker="TEST",
        initial_capital=100000,
        position_size_pct=10,
        transaction_cost_bps=10,
        slippage_bps=10,
        stop_loss_pct=50,
        take_profit_pct=50,
        max_hold_days=2,
        ma_fast_window=2,
        ma_slow_window=3,
        return_lookback_window=3,
    )[0]

    assert with_cost["entry_execution_price"] > with_cost["entry_price"]
    assert with_cost["exit_execution_price"] < with_cost["exit_price"]
    assert with_cost["transaction_cost"] > 0
    assert with_cost["slippage_cost"] > 0
    assert with_cost["trade_return_pct"] < no_cost["trade_return_pct"]


def test_equity_drawdown_exposure_turnover_and_benchmark_outputs_are_deterministic():
    trades_df = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "entry_date": "2024-01-02",
                "exit_date": "2024-01-03",
                "net_pnl": 100.0,
                "notional": 1000.0,
                "trade_return_pct": 10.0,
                "weighted_return_pct": 1.0,
                "hold_days": 2,
            },
            {
                "ticker": "BBB",
                "entry_date": "2024-01-03",
                "exit_date": "2024-01-04",
                "net_pnl": -50.0,
                "notional": 2000.0,
                "trade_return_pct": -2.5,
                "weighted_return_pct": -0.5,
                "hold_days": 2,
            },
        ]
    )

    equity_df, drawdown_df = agent.build_equity_curve(
        trades_df=trades_df,
        initial_capital=10000,
        start_date="2024-01-01",
        end_date="2024-01-04",
    )

    assert equity_df["equity"].tolist() == [10000.0, 10000.0, 10100.0, 10050.0]
    assert equity_df["open_positions"].tolist() == [0, 1, 2, 1]
    assert equity_df["turnover_pct"].sum() == 60.0
    assert drawdown_df["drawdown_pct"].min() < 0

    benchmark_df = agent.build_benchmark_comparison(
        {"SPY": _trend_history(4)},
        strategy_return_pct=float(equity_df.iloc[-1]["cumulative_return_pct"]),
        start_date="2024-01-01",
        end_date="2024-01-04",
    )
    summary_df = agent.calculate_summary(
        trades_df,
        {
            "tickers": ["AAA", "BBB"],
            "start_date": "2024-01-01",
            "end_date": "2024-01-04",
            "initial_capital": 10000,
            "position_size_pct": 10,
            "transaction_cost_bps": 5,
            "slippage_bps": 5,
        },
        run_id="run-test",
        equity_df=equity_df,
        drawdown_df=drawdown_df,
        benchmark_df=benchmark_df,
    )

    summary = summary_df.iloc[0]
    assert benchmark_df.iloc[0]["benchmark_ticker"] == "SPY"
    assert summary["winners"] == 1
    assert summary["losers"] == 1
    assert summary["strategy_return_pct"] == 0.5
    assert summary["turnover_pct"] == 60.0
    assert summary["max_drawdown_pct"] < 0
