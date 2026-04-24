from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

import pandas as pd


Clock = Callable[[], datetime]
DownloadFunc = Callable[..., pd.DataFrame]
RateLimitHook = Callable[[str, int], None]
SleepFunc = Callable[[float], None]
BackoffFunc = Callable[[int], float]


HEALTH_COLUMNS = [
    "ticker",
    "source",
    "error",
    "stale",
    "retry_count",
    "fetched_at",
    "as_of",
]


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


class MarketDataCache(Protocol):
    def get(
        self,
        ticker: str,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> pd.DataFrame | None:
        ...

    def set(
        self,
        ticker: str,
        data: pd.DataFrame,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> None:
        ...


class InMemoryMarketDataCache:
    def __init__(self) -> None:
        self._frames: dict[tuple[str, str, str, bool, str], pd.DataFrame] = {}

    def get(
        self,
        ticker: str,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> pd.DataFrame | None:
        frame = self._frames.get((ticker.upper(), period, interval, auto_adjust, source))
        return None if frame is None else frame.copy()

    def set(
        self,
        ticker: str,
        data: pd.DataFrame,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> None:
        self._frames[(ticker.upper(), period, interval, auto_adjust, source)] = data.copy()


class FileMarketDataCache:
    """Small local cache for OHLCV frames.

    The cache stores one pandas pickle per request signature under a local
    directory. It is intentionally simple and opt-in so CSV files remain the
    operational source of truth for portfolio state and derived artifacts.
    """

    def __init__(self, cache_dir: str | os.PathLike[str] = "data/runtime/market_data_cache") -> None:
        self.cache_dir = Path(cache_dir)

    def get(
        self,
        ticker: str,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> pd.DataFrame | None:
        path = self._path_for(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
            source=source,
        )
        if not path.exists():
            return None
        try:
            return pd.read_pickle(path)
        except Exception:
            return None

    def set(
        self,
        ticker: str,
        data: pd.DataFrame,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
            source=source,
        )
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        data.to_pickle(temp_path)
        os.replace(temp_path, path)

    def _path_for(
        self,
        ticker: str,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
        source: str,
    ) -> Path:
        raw_key = "|".join([ticker.upper(), period, interval, str(auto_adjust), source])
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:16]
        safe_ticker = "".join(ch if ch.isalnum() else "_" for ch in ticker.upper())
        return self.cache_dir / f"{safe_ticker}_{digest}.pkl"


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
        cache: MarketDataCache | None = None,
        rate_limit_hook: RateLimitHook | None = None,
        sleep_func: SleepFunc | None = None,
        backoff_func: BackoffFunc | None = None,
    ) -> None:
        self._download_func = download_func
        self._now_func = now_func
        self._source = source
        self._max_staleness_days = max_staleness_days
        self._retries = max(0, int(retries))
        self._cache = cache
        self._rate_limit_hook = rate_limit_hook
        self._sleep_func = sleep_func
        self._backoff_func = backoff_func or (lambda retry_count: 0.0)

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
        cached_data = self._get_cached(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
        )
        if cached_data is not None and not cached_data.empty:
            as_of = _latest_index_timestamp(cached_data)
            stale = self._is_stale(as_of)
            if not stale:
                return MarketDataResult(
                    ticker=ticker,
                    data=cached_data,
                    metadata=MarketDataMetadata(
                        source=self._source,
                        fetched_at=fetched_at,
                        as_of=as_of,
                        stale=False,
                        retry_count=0,
                    ),
                )

        while True:
            try:
                if self._rate_limit_hook is not None:
                    self._rate_limit_hook(ticker, retry_count)
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
                if self._cache is not None and not data.empty:
                    self._cache.set(
                        ticker,
                        data,
                        period=period,
                        interval=interval,
                        auto_adjust=auto_adjust,
                        source=self._source,
                    )
                return MarketDataResult(ticker=ticker, data=data, metadata=metadata)
            except Exception as exc:
                if retry_count < self._retries:
                    retry_count += 1
                    delay_seconds = float(self._backoff_func(retry_count))
                    if delay_seconds > 0 and self._sleep_func is not None:
                        self._sleep_func(delay_seconds)
                    continue

                if cached_data is not None and not cached_data.empty:
                    as_of = _latest_index_timestamp(cached_data)
                    metadata = MarketDataMetadata(
                        source=self._source,
                        fetched_at=fetched_at,
                        as_of=as_of,
                        stale=True,
                        error=str(exc),
                        retry_count=retry_count,
                    )
                    return MarketDataResult(ticker=ticker, data=cached_data, metadata=metadata)

                metadata = MarketDataMetadata(
                    source=self._source,
                    fetched_at=fetched_at,
                    error=str(exc),
                    retry_count=retry_count,
                )
                return MarketDataResult(ticker=ticker, data=pd.DataFrame(), metadata=metadata)

    def _get_cached(
        self,
        ticker: str,
        *,
        period: str,
        interval: str,
        auto_adjust: bool,
    ) -> pd.DataFrame | None:
        if self._cache is None:
            return None
        return self._cache.get(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
            source=self._source,
        )

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


def market_data_health_row(result: MarketDataResult) -> dict[str, object]:
    return {
        "ticker": result.ticker,
        "source": result.metadata.source,
        "error": result.metadata.error or "",
        "stale": result.metadata.stale,
        "retry_count": result.metadata.retry_count,
        "fetched_at": result.metadata.fetched_at,
        "as_of": result.metadata.as_of or "",
    }


def write_market_data_health_artifact(
    results: list[MarketDataResult],
    path: str | os.PathLike[str] = "data/data_source_health.csv",
) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = [market_data_health_row(result) for result in results]
    pd.DataFrame(rows, columns=HEALTH_COLUMNS).to_csv(output_path, index=False)
