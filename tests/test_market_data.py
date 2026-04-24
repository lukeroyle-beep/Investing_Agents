from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from shared.market_data import YFinanceMarketDataProvider, fetch_price_history


def fixed_now() -> datetime:
    return datetime(2026, 4, 24, 7, 0, tzinfo=UTC)


def test_yfinance_provider_wraps_download_with_metadata() -> None:
    frame = pd.DataFrame(
        {"Close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-04-23", "2026-04-24"]),
    )
    calls: list[dict[str, object]] = []

    def fake_download(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return frame

    provider = YFinanceMarketDataProvider(
        download_func=fake_download,
        now_func=fixed_now,
    )

    result = provider.fetch_history("AAPL", period="1mo", interval="1d")

    assert result.ok
    assert result.ticker == "AAPL"
    assert result.data is frame
    assert result.metadata.source == "yfinance"
    assert result.metadata.fetched_at == "2026-04-24T07:00:00+00:00"
    assert result.metadata.as_of == "2026-04-24T00:00:00+00:00"
    assert result.metadata.stale is False
    assert result.metadata.error is None
    assert result.metadata.retry_count == 0
    assert calls == [
        {
            "args": ("AAPL",),
            "kwargs": {
                "period": "1mo",
                "interval": "1d",
                "auto_adjust": True,
                "progress": False,
            },
        }
    ]


def test_yfinance_provider_reports_error_and_retry_count() -> None:
    attempts = 0

    def flaky_download(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    provider = YFinanceMarketDataProvider(
        download_func=flaky_download,
        now_func=fixed_now,
        retries=2,
    )

    result = provider.fetch_history("MSFT")

    assert not result.ok
    assert result.data.empty
    assert result.metadata.error == "boom"
    assert result.metadata.retry_count == 2
    assert attempts == 3


def test_yfinance_provider_marks_stale_data_when_configured() -> None:
    frame = pd.DataFrame(
        {"Close": [100.0]},
        index=pd.to_datetime(["2026-04-20"]),
    )
    provider = YFinanceMarketDataProvider(
        download_func=lambda *_args, **_kwargs: frame,
        now_func=fixed_now,
        max_staleness_days=1,
    )

    result = provider.fetch_history("SPY")

    assert result.ok
    assert result.metadata.stale is True


def test_fetch_price_history_accepts_mock_provider() -> None:
    class FakeProvider:
        def fetch_history(self, ticker: str, **kwargs):
            return YFinanceMarketDataProvider(
                download_func=lambda *_args, **_kwargs: pd.DataFrame(
                    {"Close": [1.0]}, index=pd.to_datetime(["2026-04-24"])
                ),
                now_func=fixed_now,
                source="fake",
            ).fetch_history(ticker, **kwargs)

    result = fetch_price_history("FAKE", provider=FakeProvider())

    assert result.ok
    assert result.metadata.source == "fake"
