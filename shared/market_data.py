from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Protocol

import pandas as pd


Clock = Callable[[], datetime]
DownloadFunc = Callable[..., pd.DataFrame]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _to_utc_iso(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None

    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        return None

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(UTC)
    else:
        timestamp = timestamp.tz_convert(UTC)

    return timestamp.isoformat()


def _latest_index_timestamp(df: pd.DataFrame) -> str | None:
    if df.empty or df.index.empty:
        return None
    return _to_utc_iso(df.index.max())


@dataclass(frozen=True)
class MarketDataMetadata:
    source: str
    fetched_at: str
    as_of: str | None = None
    stale: bool = False
    error: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class MarketDataResult:
    ticker: str
    data: pd.DataFrame
    metadata: MarketDataMetadata

    @property
    def ok(self) -> bool:
        return self.metadata.error is None and not self.data.empty


class MarketDataProvider(Protocol):
    def fetch_history(
        self,
        ticker: str,
        *,
        period: str = "6mo",
        interval: str = "1d",
        auto_adjust: bool = True,
    ) -> MarketDataResult:
        ...


class YFinanceMarketDataProvider:
    def __init__(
        self,
        *,
        download_func: DownloadFunc | None = None,
        now_func: Clock = _utc_now,
        source: str = "yfinance",
        max_staleness_days: int | None = None,
        retries: int = 0,
    ) -> None:
        self._download_func = download_func
        self._now_func = now_func
        self._source = source
        self._max_staleness_days = max_staleness_days
        self._retries = max(0, int(retries))

    @property
    def download_func(self) -> DownloadFunc:
        if self._download_func is None:
            import yfinance as yf

            self._download_func = yf.download
        return self._download_func

    def fetch_history(
        self,
        ticker: str,
        *,
        period: str = "6mo",
        interval: str = "1d",
        auto_adjust: bool = True,
    ) -> MarketDataResult:
        retry_count = 0
        fetched_at = self._now_func().astimezone(UTC).isoformat()

        while True:
            try:
                data = self.download_func(
                    ticker,
                    period=period,
                    interval=interval,
                    auto_adjust=auto_adjust,
                    progress=False,
                )
                if data is None:
                    data = pd.DataFrame()
                as_of = _latest_index_timestamp(data)
                metadata = MarketDataMetadata(
                    source=self._source,
                    fetched_at=fetched_at,
                    as_of=as_of,
                    stale=self._is_stale(as_of),
                    retry_count=retry_count,
                )
                return MarketDataResult(ticker=ticker, data=data, metadata=metadata)
            except Exception as exc:
                if retry_count < self._retries:
                    retry_count += 1
                    continue

                metadata = MarketDataMetadata(
                    source=self._source,
                    fetched_at=fetched_at,
                    error=str(exc),
                    retry_count=retry_count,
                )
                return MarketDataResult(ticker=ticker, data=pd.DataFrame(), metadata=metadata)

    def _is_stale(self, as_of: str | None) -> bool:
        if self._max_staleness_days is None or as_of is None:
            return False

        as_of_dt = pd.Timestamp(as_of).to_pydatetime()
        return self._now_func().astimezone(UTC) - as_of_dt > timedelta(
            days=self._max_staleness_days
        )


_default_provider = YFinanceMarketDataProvider()


def fetch_price_history(
    ticker: str,
    *,
    period: str = "6mo",
    interval: str = "1d",
    auto_adjust: bool = True,
    provider: MarketDataProvider | None = None,
) -> MarketDataResult:
    resolved_provider = provider or _default_provider
    return resolved_provider.fetch_history(
        ticker,
        period=period,
        interval=interval,
        auto_adjust=auto_adjust,
    )
