from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from shared.market_data import (
    FileMarketDataCache,
    InMemoryMarketDataCache,
    MarketDataMetadata,
    MarketDataResult,
    YFinanceMarketDataProvider,
    fetch_price_history,
    append_market_data_health_artifact,
    write_market_data_health_artifact,
)


def fixed_now() -> datetime:
    return datetime(2026, 4, 24, 21, 0, tzinfo=UTC)


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
    assert result.metadata.fetched_at == "2026-04-24T21:00:00+00:00"
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


def test_yfinance_provider_uses_deterministic_rate_limit_and_backoff_hooks() -> None:
    attempts = 0
    rate_limit_calls: list[tuple[str, int]] = []
    slept: list[float] = []

    def flaky_download(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary")
        return pd.DataFrame(
            {"Close": [100.0]}, index=pd.to_datetime(["2026-04-24"])
        )

    provider = YFinanceMarketDataProvider(
        download_func=flaky_download,
        now_func=fixed_now,
        retries=1,
        rate_limit_hook=lambda ticker, retry: rate_limit_calls.append((ticker, retry)),
        backoff_func=lambda retry: retry * 0.25,
        sleep_func=slept.append,
    )

    result = provider.fetch_history("AAPL")

    assert result.ok
    assert attempts == 2
    assert rate_limit_calls == [("AAPL", 0), ("AAPL", 1)]
    assert slept == [0.25]
    assert result.metadata.retry_count == 1


def test_yfinance_provider_reads_and_writes_in_memory_cache() -> None:
    frame = pd.DataFrame(
        {"Close": [100.0]}, index=pd.to_datetime(["2026-04-24"])
    )
    calls = 0

    def fake_download(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return frame

    cache = InMemoryMarketDataCache()
    provider = YFinanceMarketDataProvider(
        download_func=fake_download,
        now_func=fixed_now,
        cache=cache,
        max_staleness_days=1,
    )

    first = provider.fetch_history("AAPL")
    second = provider.fetch_history("AAPL")

    assert first.ok
    assert second.ok
    assert calls == 1
    assert second.data.equals(frame)


def test_file_market_data_cache_round_trips_frame(tmp_path) -> None:
    frame = pd.DataFrame(
        {"Open": [99.0], "Close": [100.0]},
        index=pd.to_datetime(["2026-04-24"]),
    )
    cache = FileMarketDataCache(tmp_path / "cache")

    cache.set(
        "AAPL",
        frame,
        period="1mo",
        interval="1d",
        auto_adjust=True,
        source="test",
    )

    cached = cache.get(
        "AAPL",
        period="1mo",
        interval="1d",
        auto_adjust=True,
        source="test",
    )

    assert cached is not None
    assert cached.equals(frame)


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

    assert not result.ok
    assert result.metadata.stale is True
    assert result.metadata.mode == "no_trade"


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


def test_write_market_data_health_artifact(tmp_path) -> None:
    path = tmp_path / "health.csv"
    result = MarketDataResult(
        ticker="AAPL",
        data=pd.DataFrame(),
        metadata=MarketDataMetadata(
            source="fake",
            fetched_at="2026-04-24T07:00:00+00:00",
            as_of="2026-04-23T00:00:00+00:00",
            stale=True,
            error="boom",
            retry_count=2,
        ),
    )

    write_market_data_health_artifact([result], path)

    rows = pd.read_csv(path)
    assert rows.iloc[0]["ticker"] == "AAPL"
    assert rows.iloc[0]["source"] == "fake"
    assert rows.iloc[0]["data_kind"] == "daily_research_price"
    assert rows.iloc[0]["error"] == "boom"
    assert bool(rows.iloc[0]["stale"]) is True
    assert rows.iloc[0]["retry_count"] == 2
    assert rows.iloc[0]["fetched_at"] == "2026-04-24T07:00:00+00:00"
    assert rows.iloc[0]["as_of"] == "2026-04-23T00:00:00+00:00"
    assert rows.iloc[0]["mode"] == "no_trade"


def test_append_market_data_health_artifact_preserves_existing_rows(tmp_path) -> None:
    path = tmp_path / "health.csv"
    first = MarketDataResult(
        ticker="AAPL",
        data=pd.DataFrame(),
        metadata=MarketDataMetadata(
            source="fake",
            fetched_at="2026-04-24T07:00:00+00:00",
            as_of="2026-04-23T00:00:00+00:00",
        ),
    )
    second = MarketDataResult(
        ticker="MSFT",
        data=pd.DataFrame(),
        metadata=MarketDataMetadata(
            source="fake",
            fetched_at="2026-04-24T07:01:00+00:00",
            error="provider down",
            retry_count=1,
        ),
    )

    write_market_data_health_artifact([first], path)
    append_market_data_health_artifact([second], path)

    rows = pd.read_csv(path)
    assert rows["ticker"].tolist() == ["AAPL", "MSFT"]
    assert rows.iloc[1]["error"] == "provider down"
    assert rows.iloc[1]["retry_count"] == 1
