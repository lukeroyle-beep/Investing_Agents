from __future__ import annotations

import pandas as pd

from agents.universe_agent import universe_agent
from shared.market_data import MarketDataMetadata, MarketDataResult


class FakeMarketDataProvider:
    def __init__(self, result: MarketDataResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def fetch_history(self, ticker: str, **kwargs) -> MarketDataResult:
        self.calls.append({"ticker": ticker, **kwargs})
        return self.result


def _asset() -> dict[str, str]:
    return {
        "ticker": "TEST",
        "name": "Test Asset",
        "asset_class": "equity",
        "region": "US",
        "exchange": "NASDAQ",
        "source": "curated",
        "index_membership": "",
        "currency": "USD",
        "sector": "Technology",
    }


def test_fetch_asset_data_uses_market_data_provider() -> None:
    close = pd.Series([100.0 + float(i) for i in range(70)])
    frame = pd.DataFrame(
        {"Close": close.to_list()},
        index=pd.date_range("2026-01-01", periods=len(close), freq="D"),
    )
    provider = FakeMarketDataProvider(
        MarketDataResult(
            ticker="TEST",
            data=frame,
            metadata=MarketDataMetadata(
                source="fake",
                fetched_at="2026-04-24T07:00:00+00:00",
                as_of="2026-03-11T00:00:00+00:00",
            ),
        )
    )

    health_results: list[MarketDataResult] = []
    result = universe_agent.fetch_asset_data(
        _asset(), market_data_provider=provider, health_results=health_results
    )

    assert provider.calls == [
        {
            "ticker": "TEST",
            "period": "6mo",
            "interval": "1d",
            "auto_adjust": True,
        }
    ]
    assert result is not None
    assert result["ticker"] == "TEST"
    assert result["checked_at"] == "2026-04-24T07:00:00+00:00"
    assert result["score"] >= 5
    assert health_results == [provider.result]


def test_fetch_asset_data_skips_provider_errors() -> None:
    provider = FakeMarketDataProvider(
        MarketDataResult(
            ticker="TEST",
            data=pd.DataFrame(),
            metadata=MarketDataMetadata(
                source="fake",
                fetched_at="2026-04-24T07:00:00+00:00",
                error="provider down",
            ),
        )
    )

    assert universe_agent.fetch_asset_data(_asset(), market_data_provider=provider) is None
