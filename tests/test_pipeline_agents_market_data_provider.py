from __future__ import annotations

import pandas as pd
import pytest

from agents.backtesting_agent import backtesting_agent
from agents.macro_agent import macro_agent
from agents.news_agent import news_agent
from agents.signal_agent import signal_agent
from shared.market_data import MarketDataMetadata, MarketDataResult, NewsDataResult


class FakeMarketDataProvider:
    def __init__(
        self,
        history_result: MarketDataResult | None = None,
        news_result: NewsDataResult | None = None,
    ) -> None:
        self.history_result = history_result
        self.news_result = news_result
        self.history_calls: list[dict[str, object]] = []
        self.news_calls: list[dict[str, object]] = []

    def fetch_history(self, ticker: str, **kwargs) -> MarketDataResult:
        self.history_calls.append({"ticker": ticker, **kwargs})
        assert self.history_result is not None
        return self.history_result

    def fetch_news(self, ticker: str, *, limit: int = 10) -> NewsDataResult:
        self.news_calls.append({"ticker": ticker, "limit": limit})
        assert self.news_result is not None
        return self.news_result


def _ohlcv_frame(days: int = 70) -> pd.DataFrame:
    close = [100.0 + float(i) for i in range(days)]
    volume = [1000.0 for _ in range(days - 1)] + [2000.0]
    index = pd.date_range("2026-01-01", periods=days, freq="D", name="Date")
    return pd.DataFrame(
        {
            "Open": [value - 0.5 for value in close],
            "High": [value + 1.0 for value in close],
            "Low": [value - 1.0 for value in close],
            "Close": close,
            "Volume": volume,
        },
        index=index,
    )


def _history_result(frame: pd.DataFrame | None = None, *, error: str | None = None) -> MarketDataResult:
    return MarketDataResult(
        ticker="TEST",
        data=frame if frame is not None else _ohlcv_frame(),
        metadata=MarketDataMetadata(
            source="fake",
            fetched_at="2026-05-01T12:00:00+00:00",
            as_of="2026-03-11T00:00:00+00:00",
            error=error,
        ),
    )


def test_signal_agent_fetches_prices_via_market_data_provider() -> None:
    provider = FakeMarketDataProvider(history_result=_history_result())

    result = signal_agent.fetch_signal_data("TEST", "Test Asset", market_data_provider=provider)

    assert provider.history_calls == [
        {
            "ticker": "TEST",
            "period": "6mo",
            "interval": "1d",
            "auto_adjust": True,
        }
    ]
    assert result is not None
    assert result["ticker"] == "TEST"
    assert result["checked_at"] == "2026-05-01T12:00:00+00:00"


def test_macro_agent_fetches_proxy_via_market_data_provider() -> None:
    provider = FakeMarketDataProvider(history_result=_history_result())

    result = macro_agent.fetch_market_proxy_data("SPY", "S&P 500", market_data_provider=provider)

    assert provider.history_calls == [
        {
            "ticker": "SPY",
            "period": "6mo",
            "interval": "1d",
            "auto_adjust": True,
        }
    ]
    assert result is not None
    assert result["above_ma50"] is True


def test_backtesting_agent_fetches_history_via_market_data_provider_with_dates() -> None:
    provider = FakeMarketDataProvider(history_result=_history_result())

    result = backtesting_agent.download_price_history(
        "TEST",
        "2026-01-01",
        "2026-04-01",
        market_data_provider=provider,
    )

    assert provider.history_calls == [
        {
            "ticker": "TEST",
            "period": "6mo",
            "interval": "1d",
            "auto_adjust": True,
            "start": "2026-01-01",
            "end": "2026-04-01",
        }
    ]
    assert list(result.columns) == ["Date", "Open", "High", "Low", "Close", "Volume", "ticker"]
    assert result["ticker"].unique().tolist() == ["TEST"]


def test_news_agent_fetches_news_via_market_data_provider() -> None:
    provider = FakeMarketDataProvider(
        news_result=NewsDataResult(
            ticker="TEST",
            items=[
                {
                    "title": "Test company reports earnings beat",
                    "publisher": "Example News",
                    "link": "https://example.test/news",
                    "providerPublishTime": 1770000000,
                }
            ],
            metadata=MarketDataMetadata(
                source="fake",
                fetched_at="2026-05-01T12:00:00+00:00",
                as_of="2026-02-02T08:00:00+00:00",
            ),
        )
    )

    result = news_agent.fetch_news_for_ticker("TEST", "Test Asset", market_data_provider=provider)

    assert provider.news_calls == [{"ticker": "TEST", "limit": 10}]
    assert len(result) == 1
    assert result[0]["news_category"] == "earnings_or_results"
    assert result[0]["checked_at"] == "2026-05-01T12:00:00+00:00"


def test_agents_skip_provider_errors() -> None:
    provider = FakeMarketDataProvider(
        history_result=_history_result(pd.DataFrame(), error="provider down")
    )

    assert signal_agent.fetch_signal_data("TEST", "Test Asset", market_data_provider=provider) is None
    assert macro_agent.fetch_market_proxy_data("TEST", "Test Asset", market_data_provider=provider) is None
    assert backtesting_agent.download_price_history(
        "TEST", "2026-01-01", "", market_data_provider=provider
    ).empty


def test_unexpected_provider_exception_fails_closed_with_redaction() -> None:
    class RaisingProvider:
        def fetch_history(self, *_args, **_kwargs):
            raise RuntimeError("x-api-key=do-not-log")

    with pytest.raises(RuntimeError) as exc_info:
        signal_agent.fetch_signal_data(
            "TEST",
            "Test Asset",
            market_data_provider=RaisingProvider(),
        )

    assert "do-not-log" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
