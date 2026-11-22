"""Example-data checks for point-in-time membership boundaries and price gaps.

The series and symbols in the example tests are fixtures. They are not market
prices and they are not index constituents. The final test loads the retrieved
S&P 500 table and is labelled as such.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from agents.backtesting_agent import backtesting_agent as agent
from shared.market_data import MarketDataMetadata, MarketDataResult
from shared.freshness import MODE_NO_TRADE
from shared.sp500_membership import (
    ANCHOR_DATE,
    MEMBERSHIP_FILE,
    RETRIEVAL_DATE,
    SPLICE_DATE,
    build_membership_frame,
    members_on,
)
from strategy.domain import StrategyValidationError
from strategy.validation import PointInTimeUniverse


def _example_history(dates, start: float = 100.0) -> pd.DataFrame:
    rows = []
    for index, day in enumerate(dates):
        close = start + index
        rows.append({
            "Date": day,
            "Open": close,
            "High": close + 1.0,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 1000 + index,
        })
    return pd.DataFrame(rows)


def _example_row(symbol: str, start, *, end=None, delisted=None) -> dict[str, object]:
    def stamp(value):
        if value is None:
            return None
        return pd.Timestamp(value).tz_localize("UTC").isoformat()

    return {
        "internal_instrument_id": f"example:{symbol}",
        "symbol": symbol,
        "exchange": "EXAMPLE",
        "effective_from": stamp(start),
        "effective_to": stamp(end),
        "delisted_at": stamp(delisted),
    }


def _example_universe() -> tuple[pd.DatetimeIndex, PointInTimeUniverse]:
    dates = pd.bdate_range("2024-01-02", periods=30)
    universe = PointInTimeUniverse.from_frame(pd.DataFrame([
        _example_row("STAY", "2020-01-01"),
        _example_row("ENTER", dates[10]),
        _example_row("LEFT", "2020-01-01", end=dates[18]),
        _example_row("GONE", "2020-01-01", delisted=dates[22]),
        _example_row("NODATA", "2020-01-01", delisted=dates[12]),
    ]))
    return dates, universe


def _listed(universe: PointInTimeUniverse, symbol: str, session) -> bool:
    return symbol in set(universe.members(session)["symbol"].astype(str))


def test_example_effective_dates_include_the_start_and_exclude_the_end():
    dates, universe = _example_universe()
    assert not _listed(universe, "ENTER", dates[9])
    assert _listed(universe, "ENTER", dates[10])
    assert _listed(universe, "LEFT", dates[17])
    assert not _listed(universe, "LEFT", dates[18])
    assert _listed(universe, "GONE", dates[21])
    assert not _listed(universe, "GONE", dates[22])
    assert not _listed(universe, "NODATA", dates[12])
    assert _listed(universe, "NODATA", dates[11])


def test_example_replay_trades_additions_and_stops_at_removal_and_delisting():
    dates, universe = _example_universe()
    priced = PointInTimeUniverse.from_frame(
        universe.frame.loc[universe.frame["symbol"] != "NODATA"].reset_index(drop=True)
    )
    histories = {
        "STAY": _example_history(dates),
        "ENTER": _example_history(dates),
        "LEFT": _example_history(dates, start=80.0),
        "GONE": _example_history(dates, start=60.0),
    }
    settings = {
        **agent.DEFAULT_CONFIG,
        "ma_fast_window": 2,
        "ma_slow_window": 3,
        "return_lookback_window": 3,
        "stop_loss_pct": 50.0,
        "take_profit_pct": 80.0,
        "max_hold_days": 2,
        "position_size_pct": 10.0,
        "initial_capital": 100000.0,
    }
    result = agent.evaluate_partition(
        histories, settings, str(dates[0].date()), str(dates[-1].date()), universe=priced,
    )
    trades = result.accepted_trades
    assert set(trades["ticker"]) == {"STAY", "ENTER", "LEFT", "GONE"}
    enter = pd.to_datetime(trades.loc[trades["ticker"] == "ENTER", "entry_date"])
    left = pd.to_datetime(trades.loc[trades["ticker"] == "LEFT", "exit_date"])
    gone = pd.to_datetime(trades.loc[trades["ticker"] == "GONE", "exit_date"])
    assert enter.min() >= pd.Timestamp(dates[10])
    assert left.max() < pd.Timestamp(dates[18])
    assert gone.max() < pd.Timestamp(dates[22])
    for record in trades.to_dict(orient="records"):
        assert _listed(priced, record["ticker"], record["entry_date"])
        assert _listed(priced, record["ticker"], record["exit_date"])


def test_example_missing_delisted_prices_are_counted_and_block_a_low_coverage_run():
    dates, universe = _example_universe()
    histories = {
        "STAY": _example_history(dates),
        "ENTER": _example_history(dates),
        "LEFT": _example_history(dates),
        "GONE": _example_history(dates),
    }
    coverage = agent.assess_point_in_time_price_coverage(
        universe, histories, dates, str(dates[0].date()), str(dates[-1].date()),
    )
    assert coverage.missing == ("NODATA",)
    assert coverage.partial == ()
    assert coverage.covered_count == 4
    assert coverage.member_count == 5
    assert "NODATA" in coverage.uncovered
    with pytest.raises(StrategyValidationError, match="NODATA"):
        agent.require_point_in_time_price_coverage(coverage, 0.90)
    agent.require_point_in_time_price_coverage(coverage, 0.80)
    priced = agent.universe_priced_members(universe, set(coverage.covered))
    assert "NODATA" not in set(priced.frame["symbol"])
    assert coverage.missing == ("NODATA",)
    assert agent.universe_bias_label(universe, coverage) == "point_in_time_incomplete_prices"
    assert "survivorship bias remains" in agent.format_price_coverage(coverage, 0.95)


def test_example_partial_delisted_history_is_not_treated_as_covered():
    dates, universe = _example_universe()
    gone = _example_history(dates).drop(index=5).reset_index(drop=True)
    histories = {
        "STAY": _example_history(dates),
        "ENTER": _example_history(dates),
        "LEFT": _example_history(dates),
        "GONE": gone,
    }
    coverage = agent.assess_point_in_time_price_coverage(
        universe, histories, dates, str(dates[0].date()), str(dates[-1].date()),
        {"NODATA": "example provider error"},
    )
    assert coverage.symbols("partial") == ("GONE",)
    assert coverage.unusable == ("NODATA",)
    assert "GONE" in coverage.uncovered
    assert "NODATA" in coverage.uncovered


def test_static_mode_is_labelled_survivor_biased():
    assert agent.universe_bias_label(None) == "survivor_biased_static"
    assert agent.configured_point_in_time_universe(agent.load_backtest_config()) is None
    assert str(agent.load_backtest_config().get("point_in_time_universe_file") or "") == ""


def test_stale_history_is_kept_only_when_a_completed_listing_is_allowed():
    index = pd.bdate_range("2022-01-03", periods=3)

    class Stub:
        def fetch_history(self, ticker, **kwargs):
            frame = pd.DataFrame(
                {
                    "Open": [10.0, 11.0, 12.0],
                    "High": [11.0, 12.0, 13.0],
                    "Low": [9.0, 10.0, 11.0],
                    "Close": [10.5, 11.5, 12.5],
                    "Volume": [100, 100, 100],
                    "Dividends": [0.0, 0.0, 0.0],
                },
                index=index,
            )
            return MarketDataResult(
                ticker=ticker,
                data=frame,
                metadata=MarketDataMetadata(
                    source="example",
                    fetched_at="2026-11-22T00:00:00+00:00",
                    as_of="2022-01-05T00:00:00+00:00",
                    stale=True,
                    mode=MODE_NO_TRADE,
                    freshness_outcome="stale",
                    reason="latest observation session precedes the latest completed session",
                ),
            )

    dropped = agent.download_price_history(
        "GONE", "2022-01-01", "2022-01-31", market_data_provider=Stub(), allow_stale=False,
    )
    kept = agent.download_price_history(
        "GONE", "2022-01-01", "2022-01-31", market_data_provider=Stub(), allow_stale=True,
    )
    assert dropped.empty
    assert len(kept) == 3
    assert kept["ticker"].unique().tolist() == ["GONE"]


def test_retrieved_sp500_table_matches_its_anchors_and_known_changes():
    saved = pd.read_csv(MEMBERSHIP_FILE, dtype=str).fillna("")
    built = build_membership_frame()
    pd.testing.assert_frame_equal(saved, built, check_dtype=False)
    universe = PointInTimeUniverse.from_frame(pd.read_csv(MEMBERSHIP_FILE))
    assert len(members_on(saved, ANCHOR_DATE)) == 505
    assert len(members_on(saved, date(2022, 1, 1))) == 505
    assert len(members_on(saved, SPLICE_DATE)) == 503
    assert len(members_on(saved, RETRIEVAL_DATE)) == 503
    assert "TWTR" in members_on(saved, date(2022, 10, 31))
    assert "TWTR" not in members_on(saved, date(2022, 11, 1))
    assert "FB" in members_on(saved, date(2022, 6, 8))
    assert "META" not in members_on(saved, date(2022, 6, 8))
    assert "FB" not in members_on(saved, date(2022, 6, 9))
    assert "META" in members_on(saved, date(2022, 6, 9))
    assert "SIVB" in members_on(saved, date(2023, 3, 14))
    assert "SIVB" not in members_on(saved, date(2023, 3, 15))
    assert "DDOG" not in members_on(saved, date(2025, 7, 8))
    assert "DDOG" in members_on(saved, date(2025, 7, 9))
    assert "PSKY" in members_on(saved, date(2026, 10, 5))
    assert "SKYD" not in members_on(saved, date(2026, 10, 5))
    assert "PSKY" not in members_on(saved, date(2026, 10, 6))
    assert "SKYD" in members_on(saved, date(2026, 10, 6))
    assert set(universe.members("2022-10-31")["symbol"]) >= {"TWTR", "AAPL"}
    assert "TWTR" not in set(universe.members("2022-11-01")["symbol"])
    with pytest.raises(StrategyValidationError, match="before point-in-time"):
        agent.enforce_point_in_time_bounds(MEMBERSHIP_FILE, "2020-01-01", "2022-12-31")
    with pytest.raises(StrategyValidationError, match="after point-in-time"):
        agent.enforce_point_in_time_bounds(MEMBERSHIP_FILE, "2022-01-01", "2026-11-23")
    agent.enforce_point_in_time_bounds(MEMBERSHIP_FILE, "2022-01-01", "2026-11-22")
