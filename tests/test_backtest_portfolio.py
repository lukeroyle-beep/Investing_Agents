from __future__ import annotations

import pandas as pd
import pytest

from strategy.backtest_portfolio import PortfolioReplayError, replay_portfolio


SESSIONS = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])


def _trade(ticker="AAA", entry="2024-01-02", exit_="2024-01-04", **kwargs):
    return {
        "ticker": ticker, "side": "long", "entry_date": entry, "exit_date": exit_,
        "entry_execution_price": 100.0, "exit_execution_price": 110.0,
        "shares": 5.0, "transaction_cost": 0.0, **kwargs,
    }


def _prices(values, sessions=SESSIONS):
    return pd.DataFrame({"Date": sessions, "Close": values})


def test_open_loss_is_marked_before_recovery_and_exit_is_flat():
    result = replay_portfolio(
        pd.DataFrame([_trade(net_pnl=999999)]),
        {"AAA": _prices([100, 50, 110])}, 1000, SESSIONS,
    )

    assert result.equity_curve["equity"].tolist() == [1000, 750, 1050]
    assert result.equity_curve["cash"].tolist() == [500, 500, 1050]
    assert result.equity_curve["unrealised_pnl"].tolist() == [0, -250, 0]
    assert result.equity_curve["realised_pnl"].tolist() == [0, 0, 50]
    assert result.equity_curve["open_positions"].tolist() == [1, 1, 0]
    assert result.equity_curve["exposure_notional"].tolist() == [500, 250, 0]
    assert result.drawdown["drawdown_pct"].min() == -25
    assert result.accepted_trades.iloc[0]["net_pnl"] == 50
    assert result.equity_curve["turnover_notional"].sum() == 1050


def test_allocation_is_cash_constrained_and_independent_of_candidate_order():
    # BBB has the spectacular future gain, but AAA wins the documented tie.
    trades = pd.DataFrame([
        _trade("BBB", shares=8, exit_execution_price=1000),
        _trade("AAA", shares=8, exit_execution_price=90),
    ])
    histories = {"AAA": _prices([100, 80, 90]), "BBB": _prices([100, 500, 1000])}
    first = replay_portfolio(trades, histories, 1000, SESSIONS)
    second = replay_portfolio(trades.iloc[::-1], histories, 1000, SESSIONS)

    assert first.accepted_trades["ticker"].tolist() == ["AAA"]
    assert first.rejected_trades["ticker"].tolist() == ["BBB"]
    assert first.rejected_trades.iloc[0]["rejection_reason"] == "insufficient_cash"
    assert first.equity_curve["cash"].min() >= 0
    pd.testing.assert_frame_equal(first.accepted_trades, second.accepted_trades)
    pd.testing.assert_frame_equal(first.equity_curve, second.equity_curve)


def test_same_day_exit_proceeds_cannot_fund_opening_trade():
    trades = pd.DataFrame([
        _trade("AAA", exit_="2024-01-03", shares=8),
        _trade("BBB", entry="2024-01-03", shares=8),
    ])
    result = replay_portfolio(trades, {"AAA": _prices([100, 110, 110])}, 1000, SESSIONS)

    assert result.accepted_trades["ticker"].tolist() == ["AAA"]
    assert result.rejected_trades["ticker"].tolist() == ["BBB"]
    assert result.equity_curve["cash"].tolist() == [200, 1080, 1080]


def test_round_trip_fees_are_debited_on_each_leg_and_first_day_loss_has_drawdown():
    result = replay_portfolio(
        pd.DataFrame([_trade(transaction_cost=10.5)]),
        {"AAA": _prices([100, 100, 110])}, 1000, SESSIONS,
    )

    assert result.accepted_trades.iloc[0]["entry_fee"] == 5
    assert result.accepted_trades.iloc[0]["exit_fee"] == 5.5
    assert result.equity_curve["equity"].tolist() == [995, 995, 1039.5]
    assert result.equity_curve["entry_fees"].tolist() == [5, 0, 0]
    assert result.equity_curve["exit_fees"].tolist() == [0, 0, 5.5]
    assert result.drawdown.iloc[0]["peak_equity"] == 1000
    assert result.drawdown.iloc[0]["drawdown_pct"] == pytest.approx(-0.5)


def test_budget_reserves_entry_fee_and_does_not_borrow():
    result = replay_portfolio(
        pd.DataFrame([_trade(shares=10, transaction_cost=2.1)]), {}, 1000, SESSIONS,
    )
    assert result.accepted_trades.empty
    assert result.rejected_trades.iloc[0]["rejection_reason"] == "insufficient_cash"
    assert result.equity_curve["equity"].tolist() == [1000, 1000, 1000]


def test_same_session_entry_exit_needs_no_end_of_day_mark():
    result = replay_portfolio(
        pd.DataFrame([_trade(exit_="2024-01-02", transaction_cost=10.5)]),
        {}, 1000, SESSIONS,
    )
    assert result.equity_curve["equity"].tolist() == [1039.5, 1039.5, 1039.5]
    assert result.equity_curve["open_positions"].tolist() == [0, 0, 0]
    assert result.equity_curve["turnover_notional"].tolist() == [1050, 0, 0]
    assert result.equity_curve["entry_fees"].sum() == 5
    assert result.equity_curve["exit_fees"].sum() == 5.5


def test_explicit_fees_preserve_fixed_charges():
    result = replay_portfolio(
        pd.DataFrame([_trade(entry_fee=2, exit_fee=2, transaction_cost=4)]),
        {"AAA": _prices([100, 100, 110])}, 1000, SESSIONS,
    )
    assert result.equity_curve["equity"].tolist() == [998, 998, 1046]


def test_overlapping_same_ticker_candidate_is_rejected():
    trades = pd.DataFrame([
        _trade(shares=2),
        _trade(entry="2024-01-03", shares=2),
    ])
    result = replay_portfolio(trades, {"AAA": _prices([100, 100, 110])}, 1000, SESSIONS)
    assert len(result.accepted_trades) == 1
    assert result.rejected_trades.iloc[0]["rejection_reason"] == "position_already_open"


def test_missing_mark_fails_instead_of_hiding_an_open_loss():
    with pytest.raises(PortfolioReplayError, match="missing held-session close"):
        replay_portfolio(
            pd.DataFrame([_trade()]), {"AAA": _prices([100, 110], SESSIONS[[0, 2]])},
            1000, SESSIONS,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_invalid_close_fails(bad):
    with pytest.raises(PortfolioReplayError, match="close"):
        replay_portfolio(pd.DataFrame([_trade()]), {"AAA": _prices([100, bad, 110])}, 1000, SESSIONS)


@pytest.mark.parametrize("field,bad", [
    ("shares", -1), ("shares", float("inf")), ("entry_execution_price", 0),
    ("exit_execution_price", float("nan")), ("transaction_cost", -2),
])
def test_invalid_execution_inputs_fail(field, bad):
    with pytest.raises(PortfolioReplayError):
        replay_portfolio(pd.DataFrame([_trade(**{field: bad})]), {}, 1000, SESSIONS)


@pytest.mark.parametrize("sessions", [[], SESSIONS[[0, 0, 1, 2]], SESSIONS[::-1]])
def test_invalid_session_grid_fails(sessions):
    with pytest.raises(PortfolioReplayError, match="sessions"):
        replay_portfolio(pd.DataFrame(), {}, 1000, sessions)


def test_duplicate_price_sessions_and_candidates_fail():
    with pytest.raises(PortfolioReplayError, match="duplicate price session"):
        replay_portfolio(
            pd.DataFrame([_trade()]), {"AAA": _prices([100, 100, 110], SESSIONS[[0, 0, 2]])},
            1000, SESSIONS,
        )
    with pytest.raises(PortfolioReplayError, match="duplicate ticker"):
        replay_portfolio(pd.DataFrame([_trade(), _trade()]), {}, 1000, SESSIONS)


def test_explicit_trade_identity_is_unique_across_tickers():
    with pytest.raises(PortfolioReplayError, match="trade_id"):
        replay_portfolio(
            pd.DataFrame([_trade("AAA", trade_id="same"), _trade("BBB", trade_id="same")]),
            {}, 1000, SESSIONS,
        )


@pytest.mark.parametrize("changes", [
    {"entry_date": "2024-01-01"}, {"exit_date": "2024-01-05"},
    {"entry_date": "2024-01-04", "exit_date": "2024-01-02"},
    {"signal_date": "2024-01-02"}, {"side": "short"}, {"entry_fee": 1},
    {"entry_fee": 1, "exit_fee": 1, "transaction_cost": 3},
])
def test_incomplete_or_inconsistent_lifecycles_fail(changes):
    with pytest.raises(PortfolioReplayError):
        replay_portfolio(pd.DataFrame([_trade(**changes)]), {}, 1000, SESSIONS)


def test_no_trades_reports_only_supplied_sessions_and_zero_drawdown():
    sessions = pd.to_datetime(["2024-01-05", "2024-01-08"])
    result = replay_portfolio(pd.DataFrame(), {}, 1000, sessions)
    assert result.equity_curve["date"].tolist() == ["2024-01-05", "2024-01-08"]
    assert result.equity_curve["equity"].tolist() == [1000, 1000]
    assert result.drawdown["drawdown_pct"].tolist() == [0, 0]
    assert result.accepted_trades.empty and result.rejected_trades.empty
