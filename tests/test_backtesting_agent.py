from __future__ import annotations

import pandas as pd
import pytest

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
                "shares": 10,
                "entry_execution_price": 100,
                "exit_execution_price": 110,
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
                "shares": 10,
                "entry_execution_price": 200,
                "exit_execution_price": 195,
            },
        ]
    )

    equity_df, drawdown_df = agent.build_equity_curve(
        trades_df=trades_df,
        initial_capital=10000,
        start_date="2024-01-01",
        end_date="2024-01-04",
        histories={
            "AAA": pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=4), "Close": [100, 100, 110, 110]}),
            "BBB": pd.DataFrame({"Date": pd.date_range("2024-01-01", periods=4), "Close": [200, 200, 200, 195]}),
        },
    )

    assert equity_df["equity"].tolist() == [10000.0, 10000.0, 10100.0, 10050.0]
    assert equity_df["open_positions"].tolist() == [0, 1, 1, 0]
    assert equity_df["turnover_pct"].sum() == 60.5
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
    assert summary["strategy_return_pct"] == pytest.approx(0.5)
    assert summary["turnover_pct"] == 60.5
    assert summary["max_drawdown_pct"] < 0


def _bars(closes, *, opens=None, highs=None, lows=None, dividends=None) -> pd.DataFrame:
    closes = [float(value) for value in closes]
    opens = [float(value) for value in (closes if opens is None else opens)]
    if highs is None:
        highs = [max(open_, close) + 1.0 for open_, close in zip(opens, closes)]
    if lows is None:
        lows = [min(open_, close) - 1.0 for open_, close in zip(opens, closes)]
    frame = pd.DataFrame(
        {
            "Date": pd.bdate_range("2024-01-02", periods=len(closes)),
            "Open": opens,
            "High": [float(value) for value in highs],
            "Low": [float(value) for value in lows],
            "Close": closes,
            "Volume": 1000,
        }
    )
    if dividends is not None:
        frame["Dividends"] = [float(value) for value in dividends]
    return frame


def _simulate(frame: pd.DataFrame, **overrides):
    params = dict(
        df=frame,
        ticker="TEST",
        initial_capital=100000,
        position_size_pct=10,
        transaction_cost_bps=0,
        slippage_bps=0,
        stop_loss_pct=8,
        take_profit_pct=15,
        max_hold_days=10,
        ma_fast_window=2,
        ma_slow_window=3,
        return_lookback_window=3,
    )
    params.update(overrides)
    return agent.simulate_long_trades_for_ticker(**params)


def test_gaps_through_stop_and_target_fill_at_the_open_intraday_levels_do_not():
    base = [100, 101, 102, 103, 104]
    base_high = [value + 1 for value in base]
    base_low = [value - 1 for value in base]

    target_gap = _simulate(_bars(base + [160, 161]))[0]
    assert target_gap["entry_price"] == 104
    assert target_gap["exit_reason"] == "take_profit_gap"
    assert target_gap["exit_price"] == 160
    assert target_gap["exit_price"] > target_gap["take_profit_price"]

    stop_gap = _simulate(_bars(base + [70, 71]))[0]
    assert stop_gap["exit_reason"] == "stop_gap"
    assert stop_gap["exit_price"] == 70
    assert stop_gap["exit_price"] < stop_gap["stop_price"]

    intraday_target = _simulate(_bars(
        base + [112, 113],
        opens=base + [110, 113],
        highs=base_high + [130, 114],
        lows=base_low + [109, 112],
    ))[0]
    assert intraday_target["exit_reason"] == "take_profit"
    assert intraday_target["exit_price"] == pytest.approx(104 * 1.15)

    intraday_stop = _simulate(_bars(
        base + [99, 98],
        opens=base + [100, 98],
        highs=base_high + [101, 99],
        lows=base_low + [90, 97],
    ))[0]
    assert intraday_stop["exit_reason"] == "stop_loss"
    assert intraday_stop["exit_price"] == pytest.approx(104 * 0.92)
    assert intraday_stop["exit_price"] > 90

    # Open stays between the levels while both the low and the high are touched.
    ambiguous = _simulate(_bars(
        base + [110, 111],
        opens=base + [100, 111],
        highs=base_high + [140, 112],
        lows=base_low + [90, 110],
    ))[0]
    assert ambiguous["exit_reason"] == "stop_loss"
    assert ambiguous["exit_price"] == pytest.approx(104 * 0.92)


def test_dividend_adjusted_closes_can_invent_a_signal_cash_dividends_do_not():
    closes = [100.0, 100.0, 100.0, 90.0, 95.0, 96.0]
    raw = _bars(closes)
    wide = dict(stop_loss_pct=50, take_profit_pct=50, max_hold_days=5)
    assert _simulate(raw, **wide) == []

    adjusted = raw.copy()
    factor = 1.0 - 10.0 / 90.0
    pre_dividend = adjusted.index[:3]
    for column in ("Open", "High", "Low", "Close"):
        adjusted.loc[pre_dividend, column] = adjusted.loc[pre_dividend, column] * factor
    invented = _simulate(adjusted, **wide)
    assert invented[0]["signal_date"] == "2024-01-05"
    assert invented[0]["entry_date"] == "2024-01-08"
    assert invented[0]["entry_price"] == pytest.approx(95.0)

    cash = raw.copy()
    cash["Dividends"] = [0.0, 0.0, 0.0, 10.0, 0.0, 0.0]
    assert _simulate(cash, **wide) == []


def test_normalised_history_keeps_the_traded_close_and_the_cash_dividend():
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    frame = pd.DataFrame(
        {
            "Open": [100.0, 90.0],
            "High": [101.0, 91.0],
            "Low": [99.0, 89.0],
            "Close": [100.0, 90.0],
            "Adj Close": [80.0, 90.0],
            "Volume": [10, 11],
            "Dividends": [0.0, 1.25],
            "Stock Splits": [0.0, 2.0],
        },
        index=index,
    )
    frame.index.name = "Date"

    out = agent._normalise_history_df(frame, "AAPL")

    assert out["Close"].tolist() == [100.0, 90.0]
    assert out["Open"].tolist() == [100.0, 90.0]
    assert out["Dividends"].tolist() == [0.0, 1.25]
    assert "Adj Close" not in out.columns
    assert "Stock Splits" not in out.columns


def test_stop_gap_on_ex_date_fills_at_the_open_and_books_dividend_cash():
    from strategy.backtest_portfolio import replay_portfolio

    frame = _bars(
        [100, 101, 102, 103, 104, 70, 72],
        dividends=[0, 0, 0, 0, 0, 1.5, 0],
    )
    trades = _simulate(frame)
    assert trades[0]["exit_reason"] == "stop_gap"
    assert trades[0]["exit_price"] == 70
    paid = replay_portfolio(
        pd.DataFrame(trades), {"TEST": frame}, 100000, pd.to_datetime(frame["Date"]),
    )
    unpaid_frame = frame.drop(columns=["Dividends"])
    unpaid = replay_portfolio(
        pd.DataFrame(trades), {"TEST": unpaid_frame}, 100000, pd.to_datetime(frame["Date"]),
    )
    entry = trades[0]["entry_date"]
    paid_trade = paid.accepted_trades.loc[paid.accepted_trades["entry_date"] == entry].iloc[0]
    unpaid_trade = unpaid.accepted_trades.loc[unpaid.accepted_trades["entry_date"] == entry].iloc[0]
    dividend_cash = paid_trade["shares"] * 1.5
    assert dividend_cash > 0
    assert paid_trade["exit_price"] == 70
    assert paid_trade["net_pnl"] == pytest.approx(unpaid_trade["net_pnl"] + dividend_cash)
    ex_date = trades[0]["exit_date"]
    paid_cash = paid.equity_curve.set_index("date").loc[ex_date, "cash"]
    unpaid_cash = unpaid.equity_curve.set_index("date").loc[ex_date, "cash"]
    assert paid_cash == pytest.approx(unpaid_cash + dividend_cash)


def test_benchmark_return_includes_dividends_after_the_entry_session():
    from strategy.domain import CostModel

    history = _bars(
        [100.0, 100.0, 100.0],
        dividends=[9.0, 1.5, 2.5],
    )
    costs = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0, tax_bps=0)
    start, end = history.iloc[0]["Date"], history.iloc[-1]["Date"]

    assert agent.partition_benchmark_return(history, start, end, costs) == pytest.approx(4.0)
    assert agent.calculate_benchmark_return(history) == pytest.approx(4.0)
