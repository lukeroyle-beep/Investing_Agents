"""Position-mark behaviour with example prices, not a market export.

The bars and the listing file under tests/fixtures/position_marks are labelled
example data. They exercise fresh, stale, failed, and unknown marks.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from agents.exit_agent import exit_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from agents.position_tracking_agent import position_tracking_agent
from shared.market_data import YFinanceMarketDataProvider
from shared.paths import project_path
from shared.schemas import validate_portfolio_state
from tests.helpers import (
    cash_state_frame,
    open_position_row,
    portfolio_monitor_row,
    write_csv,
    write_portfolio_monitor_csv,
    write_portfolio_state_csv,
)


EXAMPLE_UNIVERSE = Path(__file__).parent / "fixtures" / "position_marks" / "example_stock_universe.csv"
# Friday 24 Apr 2026, 21:00 UTC, after the New York cash close. Example clock only.
RUN_AT = datetime(2026, 4, 24, 21, 0, tzinfo=UTC)


class _ExampleInstrument:
    def __init__(self, currency: str | None, *, fail: bool = False) -> None:
        self.currency = currency
        self.fail = fail

    @property
    def fast_info(self) -> dict[str, str]:
        if self.fail:
            raise RuntimeError("example currency lookup failed")
        if self.currency is None:
            return {}
        return {"currency": self.currency}


class ExampleFeed:
    """In-memory stand-in for YFinanceMarketDataProvider. Example bars only."""

    def __init__(
        self,
        frames: dict[str, pd.DataFrame] | None = None,
        *,
        currency: str | None = "USD",
        currencies: dict[str, str | None] | None = None,
        fail: bool = False,
        currency_fail: bool = False,
    ) -> None:
        self.frames = frames or {}
        self.currency = currency
        self.currencies = currencies or {}
        self.fail = fail
        self.currency_fail = currency_fail
        self.downloads: list[tuple[str, dict[str, object]]] = []
        self.currency_lookups: list[str] = []
        self.inner = YFinanceMarketDataProvider(
            download_func=self._download,
            now_func=lambda: RUN_AT,
            ticker_factory=self._ticker,
            source="yfinance",
        )

    def _download(self, ticker: str, **kwargs: object) -> pd.DataFrame:
        self.downloads.append((str(ticker), dict(kwargs)))
        if self.fail:
            raise RuntimeError("example feed down")
        frame = self.frames.get(str(ticker))
        if frame is None:
            return pd.DataFrame()
        return frame

    def _ticker(self, symbol: str) -> _ExampleInstrument:
        self.currency_lookups.append(symbol)
        currency = self.currencies.get(symbol, self.currency)
        return _ExampleInstrument(currency, fail=self.currency_fail)

    def fetch_history(self, ticker: str, **kwargs: object):
        return self.inner.fetch_history(ticker, **kwargs)

    @property
    def ticker_factory(self):
        return self.inner.ticker_factory


def _bars(*pairs: tuple[str, float]) -> pd.DataFrame:
    dates, closes = zip(*pairs)
    return pd.DataFrame({"Close": list(closes)}, index=pd.to_datetime(list(dates)))


def _run_tracking(
    data_dir: Path,
    monkeypatch,
    *,
    feed: ExampleFeed,
    position: dict,
    previous: dict | None = None,
    universe: Path | None = EXAMPLE_UNIVERSE,
) -> pd.Series:
    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_EXAMPLE_MARK")
    monkeypatch.setattr(position_tracking_agent, "utc_now_iso", lambda: "2026-04-24T21:00:00+00:00")
    write_portfolio_state_csv(data_dir / "portfolio_state.csv", [position])
    if previous is not None:
        write_portfolio_monitor_csv(data_dir / "portfolio_monitor.csv", [previous])
    position_tracking_agent.run_position_tracking_agent(
        provider=feed,
        universe_path=universe,
        now=RUN_AT,
    )
    return pd.read_csv(data_dir / "portfolio_monitor.csv").iloc[0]


def test_real_universe_maps_london_listings_to_gbx_and_keeps_pence() -> None:
    listings = position_tracking_agent.load_listing_map()
    assert listings["AAPL"]["currency"] == "USD"
    assert listings["AZN.L"] == {"ticker": "AZN.L", "currency": "GBX"}
    assert listings["BRK-B"]["ticker"] == "BRK-B"
    assert project_path("data_sources", "stock_universe.csv").is_file()


def test_fresh_mark_uses_example_close_timestamp_and_source(
    isolated_workspace,
    monkeypatch,
) -> None:
    feed = ExampleFeed({"AAPL": _bars(("2026-04-23", 100.0), ("2026-04-24", 0.1 + 0.2))})
    position = open_position_row(quantity=3, entry_price=0.2, take_profit=1.0, stop_loss=0.1)
    row = _run_tracking(
        isolated_workspace / "data",
        monkeypatch,
        feed=feed,
        position=position,
    )

    assert row["mark_status"] == "fresh"
    assert row["mark_source"] == "yfinance"
    assert row["listing_currency"] == "USD"
    assert row["price_as_of"].startswith("2026-04-24T00:00:00")
    assert row["current_price"] == 0.3
    assert row["market_value"] == 0.9
    assert row["pnl_abs"] == 0.3
    assert "00000000000000004" not in (isolated_workspace / "data" / "portfolio_monitor.csv").read_text()
    assert feed.downloads[0][0] == "AAPL"
    assert feed.downloads[0][1]["end"] == "2026-04-25"
    assert feed.downloads[0][1]["start"] == "2026-04-03"

    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXAMPLE_EXIT")
    exit_agent.run_exit_agent()
    advice = pd.read_csv(isolated_workspace / "data" / "exit_advice.csv").iloc[0]
    assert advice["exit_action"] == "hold"

    write_csv(isolated_workspace / "data" / "cash_state.csv", cash_state_frame(balance=1000.0))
    monkeypatch.setattr(portfolio_equity_agent, "get_or_create_run_id", lambda: "RUN_EXAMPLE_EQUITY")
    portfolio_equity_agent.run_portfolio_equity_agent()
    equity = pd.read_csv(isolated_workspace / "data" / "portfolio_equity_history.csv").iloc[-1]
    assert equity["open_market_value"] == 0.9
    assert equity["total_equity"] == 1000.9


def test_fresh_mark_above_target_can_take_profit(isolated_workspace, monkeypatch) -> None:
    feed = ExampleFeed({"AAPL": _bars(("2026-04-24", 130.0))})
    position = open_position_row(take_profit=120.0, stop_loss=90.0)
    row = _run_tracking(isolated_workspace / "data", monkeypatch, feed=feed, position=position)
    assert row["mark_status"] == "fresh"
    assert row["current_price"] == 130.0
    assert row["highest_price_since_entry"] == 130.0

    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXAMPLE_EXIT")
    exit_agent.run_exit_agent()
    advice = pd.read_csv(isolated_workspace / "data" / "exit_advice.csv").iloc[0]
    assert advice["exit_action"] == "take_profit"


def test_stale_example_mark_is_flagged_and_not_actionable(isolated_workspace, monkeypatch) -> None:
    feed = ExampleFeed({"AAPL": _bars(("2026-04-22", 130.0))})
    position = open_position_row(
        current_price=105.0,
        take_profit=120.0,
        stop_loss=90.0,
        highest_price_since_entry=115.0,
        lowest_price_since_entry=92.0,
    )
    previous = portfolio_monitor_row(
        position,
        current_price=105.0,
        highest_price_since_entry=115.0,
        lowest_price_since_entry=92.0,
        price_as_of="2026-04-23T00:00:00+00:00",
        mark_source="example",
        mark_status="fresh",
    )
    data_dir = isolated_workspace / "data"
    row = _run_tracking(
        data_dir,
        monkeypatch,
        feed=feed,
        position=position,
        previous=previous,
    )

    assert row["mark_status"] == "stale"
    assert row["mark_source"] == "yfinance"
    assert row["current_price"] == 130.0
    assert row["price_as_of"].startswith("2026-04-22T00:00:00")
    assert row["highest_price_since_entry"] == 115.0
    assert row["lowest_price_since_entry"] == 92.0
    assert "precedes" in row["mark_reason"]
    alerts = pd.read_csv(data_dir / "position_alerts.csv")
    assert alerts["alert_type"].tolist() == ["stale"]

    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXAMPLE_EXIT")
    exit_agent.run_exit_agent()
    advice = pd.read_csv(data_dir / "exit_advice.csv").iloc[0]
    assert advice["exit_action"] == "review"
    assert advice["exit_reason"] == "stale_mark"
    assert advice["exit_action"] != "take_profit"

    write_csv(data_dir / "cash_state.csv", cash_state_frame())
    with pytest.raises(RuntimeError, match="stale or errored"):
        portfolio_equity_agent.run_portfolio_equity_agent()
    assert not (data_dir / "portfolio_equity_history.csv").exists()


def test_feed_failure_retains_last_price_only_with_an_error_flag(
    isolated_workspace,
    monkeypatch,
) -> None:
    feed = ExampleFeed(fail=True)
    position = open_position_row(current_price=105.0, stop_loss=90.0, take_profit=120.0)
    previous = portfolio_monitor_row(
        position,
        current_price=105.0,
        price_as_of="2026-04-23T00:00:00+00:00",
        mark_source="example",
        mark_status="fresh",
    )
    data_dir = isolated_workspace / "data"
    row = _run_tracking(
        data_dir,
        monkeypatch,
        feed=feed,
        position=position,
        previous=previous,
    )

    assert feed.downloads
    assert row["mark_status"] == "error"
    assert row["current_price"] == 105.0
    assert row["price_as_of"] == "2026-04-23T00:00:00+00:00"
    assert row["mark_source"] == "example"
    assert "example feed down" in row["mark_reason"]
    assert "last stored price retained and flagged" in row["mark_reason"]
    assert row["marked_at"] == "2026-04-24T21:00:00+00:00"

    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXAMPLE_EXIT")
    exit_agent.run_exit_agent()
    advice = pd.read_csv(data_dir / "exit_advice.csv").iloc[0]
    assert advice["exit_action"] == "review"
    assert advice["exit_reason"] == "mark_error"

    write_csv(data_dir / "cash_state.csv", cash_state_frame())
    with pytest.raises(RuntimeError, match="stale or errored"):
        portfolio_equity_agent.run_portfolio_equity_agent()


def test_unknown_symbol_is_flagged_and_does_not_call_the_feed(
    isolated_workspace,
    monkeypatch,
) -> None:
    feed = ExampleFeed({"AAPL": _bars(("2026-04-24", 130.0))})
    position = open_position_row(ticker="EXAMPLE_UNKNOWN", current_price=105.0, take_profit=110.0)
    data_dir = isolated_workspace / "data"
    row = _run_tracking(data_dir, monkeypatch, feed=feed, position=position)

    assert feed.downloads == []
    assert feed.currency_lookups == []
    assert row["mark_status"] == "error"
    assert row["ticker"] == "EXAMPLE_UNKNOWN"
    assert "unknown symbol" in row["mark_reason"]
    assert row["current_price"] == 105.0
    assert "last stored price retained and flagged" in row["mark_reason"]
    assert pd.isna(row["price_as_of"]) or str(row["price_as_of"]).strip() == ""

    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_EXAMPLE_EXIT")
    exit_agent.run_exit_agent()
    advice = pd.read_csv(data_dir / "exit_advice.csv").iloc[0]
    assert advice["exit_action"] == "review"
    assert advice["exit_reason"] == "mark_error"


def test_future_example_bar_is_not_used_when_an_earlier_close_exists(
    isolated_workspace,
    monkeypatch,
) -> None:
    feed = ExampleFeed(
        {
            "AAPL": _bars(
                ("2026-04-24", 111.0),
                ("2026-04-27", 999.0),
            )
        }
    )
    position = open_position_row()
    row = _run_tracking(isolated_workspace / "data", monkeypatch, feed=feed, position=position)

    assert row["mark_status"] == "fresh"
    assert row["current_price"] == 111.0
    assert row["price_as_of"].startswith("2026-04-24T00:00:00")
    assert 999.0 != row["current_price"]


def test_only_a_future_example_bar_is_an_error(isolated_workspace, monkeypatch) -> None:
    feed = ExampleFeed({"AAPL": _bars(("2026-04-27", 999.0))})
    position = open_position_row(current_price=105.0)
    row = _run_tracking(isolated_workspace / "data", monkeypatch, feed=feed, position=position)

    assert row["mark_status"] == "error"
    assert row["current_price"] == 105.0
    assert "no price at or before the run time" in row["mark_reason"]
    assert "999" not in str(row["current_price"])


def test_gbpence_quote_is_not_divided_into_pounds(isolated_workspace, monkeypatch) -> None:
    feed = ExampleFeed(
        {"AZN.L": _bars(("2026-04-24", 14050.5))},
        currencies={"AZN.L": "GBp"},
    )
    position = open_position_row(ticker="AZN.L", entry_price=13000.0, quantity=2)
    row = _run_tracking(isolated_workspace / "data", monkeypatch, feed=feed, position=position)

    assert feed.downloads[0][0] == "AZN.L"
    assert row["mark_status"] == "fresh"
    assert row["listing_currency"] == "GBX"
    assert row["current_price"] == 14050.5
    assert row["market_value"] == 28101.0
    assert "GBp" in row["mark_reason"]
    assert "no unit conversion" in row["mark_reason"]


def test_pound_quote_on_a_pence_listing_is_rejected(isolated_workspace, monkeypatch) -> None:
    feed = ExampleFeed(
        {"AZN.L": _bars(("2026-04-24", 140.5))},
        currencies={"AZN.L": "GBP"},
    )
    position = open_position_row(ticker="AZN.L", current_price=14000.0, entry_price=13000.0)
    previous = portfolio_monitor_row(
        position,
        current_price=14000.0,
        price_as_of="2026-04-23T00:00:00+00:00",
        mark_source="example",
        mark_status="fresh",
        listing_currency="GBX",
    )
    row = _run_tracking(
        isolated_workspace / "data",
        monkeypatch,
        feed=feed,
        position=position,
        previous=previous,
    )

    assert row["mark_status"] == "error"
    assert row["current_price"] == 14000.0
    assert row["current_price"] != 140.5
    assert row["current_price"] != 14050.0
    assert "was not converted" in row["mark_reason"]
    assert "last stored price retained and flagged" in row["mark_reason"]


def test_real_universe_symbol_is_the_provider_symbol(isolated_workspace, monkeypatch) -> None:
    feed = ExampleFeed(
        {"AZN.L": _bars(("2026-04-24", 14050.0))},
        currencies={"AZN.L": "GBp"},
    )
    position = open_position_row(ticker="azn.l", entry_price=13000.0)
    state = validate_portfolio_state(pd.DataFrame([position]), keep_extra_columns=False)
    write_csv(isolated_workspace / "data" / "portfolio_state.csv", state)
    monkeypatch.setattr(position_tracking_agent, "current_run_id", lambda: "RUN_EXAMPLE_MARK")
    monkeypatch.setattr(position_tracking_agent, "utc_now_iso", lambda: "2026-04-24T21:00:00+00:00")
    position_tracking_agent.run_position_tracking_agent(provider=feed, now=RUN_AT)

    row = pd.read_csv(isolated_workspace / "data" / "portfolio_monitor.csv").iloc[0]
    assert feed.downloads[0][0] == "AZN.L"
    assert row["ticker"] == "AZN.L"
    assert row["listing_currency"] == "GBX"
    assert row["current_price"] == 14050.0
    assert row["mark_status"] == "fresh"
