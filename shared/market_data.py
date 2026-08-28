from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Protocol

import pandas as pd

from shared.freshness import (
    CONTRADICTION_NOT_CHECKED,
    MODE_NO_TRADE,
    MODE_NORMAL,
    OUTCOME_FRESH,
    ExchangeCalendar,
    FreshnessConfig,
    assess_freshness,
    load_freshness_config,
    summarize_provider_error,
)
from shared.io_utils import write_csv, write_managed_csv_with_schema
from shared.paths import RUNTIME_CACHE_DIR, data_path
from shared.schemas import DATA_SOURCE_HEALTH_SCHEMA, validate_data_source_health


Clock = Callable[[], datetime]
DownloadFunc = Callable[..., pd.DataFrame]
RateLimitHook = Callable[[str, int], None]
SleepFunc = Callable[[float], None]
BackoffFunc = Callable[[int], float]


HEALTH_COLUMNS = [
    "ticker",
    "source",
    "data_kind",
    "error",
    "retry_count",
    "observation_time",
    "retrieval_time",
    "market_session",
    "calendar",
    "freshness_outcome",
    "contradiction_status",
    "mode",
    "reason",
    "stale",
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
    data_kind: str = "daily_research_price"
    market_session: str | None = None
    calendar: str = "XNYS"
    freshness_outcome: str = OUTCOME_FRESH
    contradiction_status: str = CONTRADICTION_NOT_CHECKED
    mode: str = MODE_NORMAL
    reason: str = ""


@dataclass(frozen=True)
class MarketDataResult:
    ticker: str
    data: pd.DataFrame
    metadata: MarketDataMetadata

    @property
    def ok(self) -> bool:
        return (
            self.metadata.error is None
            and not self.data.empty
            and self.metadata.mode != MODE_NO_TRADE
        )


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

    def __init__(
        self,
        cache_dir: str | os.PathLike[str] = RUNTIME_CACHE_DIR / "market_data",
    ) -> None:
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


@dataclass(frozen=True)
class NewsDataResult:
    ticker: str
    items: list[dict[str, Any]]
    metadata: MarketDataMetadata

    @property
    def ok(self) -> bool:
        return self.metadata.error is None and self.metadata.mode != MODE_NO_TRADE


class MarketDataProvider(Protocol):
    def fetch_history(
        self,
        ticker: str,
        *,
        period: str = "6mo",
        interval: str = "1d",
        auto_adjust: bool = True,
        start: str | None = None,
        end: str | None = None,
    ) -> MarketDataResult:
        ...

    def fetch_news(self, ticker: str, *, limit: int = 10) -> NewsDataResult:
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
        ticker_factory: Callable[[str], Any] | None = None,
        freshness_config: FreshnessConfig | None = None,
        calendar: ExchangeCalendar | None = None,
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
        self._ticker_factory = ticker_factory
        self._freshness_config = freshness_config
        self._calendar = calendar

    def _evaluate_metadata(
        self,
        *,
        as_of: str | None,
        fetched_at: str,
        data_kind: str,
        error: object | None = None,
        retry_count: int = 0,
    ) -> MarketDataMetadata:
        config = self._freshness_config or load_freshness_config()
        assessment = assess_freshness(
            source=self._source,
            data_kind=data_kind,
            observation_time=as_of,
            retrieval_time=fetched_at,
            now=self._now_func(),
            config=config,
            calendar=self._calendar,
            provider_error=error,
        )
        stale = assessment.freshness_outcome != OUTCOME_FRESH
        if self._max_staleness_days is not None and as_of:
            as_of_dt = pd.Timestamp(as_of).to_pydatetime()
            if as_of_dt.tzinfo is None:
                as_of_dt = as_of_dt.replace(tzinfo=UTC)
            stale = stale or (
                self._now_func().astimezone(UTC) - as_of_dt.astimezone(UTC)
                > timedelta(days=self._max_staleness_days)
            )
        return MarketDataMetadata(
            source=self._source,
            fetched_at=fetched_at,
            as_of=as_of,
            stale=stale,
            error=summarize_provider_error(error) if error is not None else None,
            retry_count=retry_count,
            data_kind=data_kind,
            market_session=assessment.market_session,
            calendar=assessment.calendar,
            freshness_outcome=(
                assessment.freshness_outcome if not stale else "stale"
            ),
            contradiction_status=assessment.contradiction_status,
            mode=(assessment.mode if not stale else MODE_NO_TRADE),
            reason=assessment.reason,
        )

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
        start: str | None = None,
        end: str | None = None,
    ) -> MarketDataResult:
        retry_count = 0
        fetched_at = self._now_func().astimezone(UTC).isoformat()
        data_kind = "daily_research_price" if interval.endswith("d") else "research_intraday"
        cached_data = self._get_cached(
            ticker,
            period=self._cache_period(period, start=start, end=end),
            interval=interval,
            auto_adjust=auto_adjust,
        )
        if cached_data is not None and not cached_data.empty:
            as_of = _latest_index_timestamp(cached_data)
            cached_metadata = self._evaluate_metadata(
                as_of=as_of,
                fetched_at=fetched_at,
                data_kind=data_kind,
            )
            if cached_metadata.mode == MODE_NORMAL:
                return MarketDataResult(
                    ticker=ticker,
                    data=cached_data,
                    metadata=cached_metadata,
                )

        while True:
            try:
                if self._rate_limit_hook is not None:
                    self._rate_limit_hook(ticker, retry_count)
                download_kwargs: dict[str, object] = {
                    "interval": interval,
                    "auto_adjust": auto_adjust,
                    "progress": False,
                }
                if start or end:
                    if start:
                        download_kwargs["start"] = start
                    if end:
                        download_kwargs["end"] = end
                else:
                    download_kwargs["period"] = period
                data = self.download_func(ticker, **download_kwargs)
                if data is None:
                    data = pd.DataFrame()
                as_of = _latest_index_timestamp(data)
                metadata = self._evaluate_metadata(
                    as_of=as_of,
                    fetched_at=fetched_at,
                    data_kind=data_kind,
                    retry_count=retry_count,
                )
                if self._cache is not None and not data.empty:
                    self._cache.set(
                        ticker,
                        data,
                        period=self._cache_period(period, start=start, end=end),
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
                    metadata = self._evaluate_metadata(
                        as_of=as_of,
                        fetched_at=fetched_at,
                        data_kind=data_kind,
                        error=str(exc),
                        retry_count=retry_count,
                    )
                    return MarketDataResult(ticker=ticker, data=cached_data, metadata=metadata)

                metadata = self._evaluate_metadata(
                    as_of=None,
                    fetched_at=fetched_at,
                    data_kind=data_kind,
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

    def fetch_news(self, ticker: str, *, limit: int = 10) -> NewsDataResult:
        fetched_at = self._now_func().astimezone(UTC).isoformat()
        try:
            ticker_obj = self.ticker_factory(ticker)
            raw_items = getattr(ticker_obj, "news", []) or []
            items = list(raw_items[:limit])
            as_of = self._latest_news_timestamp(items)
            return NewsDataResult(
                ticker=ticker,
                items=items,
                metadata=self._evaluate_metadata(
                    as_of=as_of,
                    fetched_at=fetched_at,
                    data_kind="research_news",
                ),
            )
        except Exception as exc:
            return NewsDataResult(
                ticker=ticker,
                items=[],
                metadata=self._evaluate_metadata(
                    as_of=None,
                    fetched_at=fetched_at,
                    data_kind="research_news",
                    error=str(exc),
                ),
            )

    @property
    def ticker_factory(self) -> Callable[[str], Any]:
        if self._ticker_factory is None:
            import yfinance as yf

            self._ticker_factory = yf.Ticker
        return self._ticker_factory

    def _cache_period(self, period: str, *, start: str | None, end: str | None) -> str:
        if start or end:
            return f"start={start or ''}|end={end or ''}"
        return period

    def _latest_news_timestamp(self, items: list[dict[str, Any]]) -> str | None:
        timestamps = [item.get("providerPublishTime") for item in items if item.get("providerPublishTime")]
        if not timestamps:
            return None
        return datetime.fromtimestamp(max(timestamps), UTC).isoformat()

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
    start: str | None = None,
    end: str | None = None,
) -> MarketDataResult:
    resolved_provider = provider or _default_provider
    kwargs: dict[str, object] = {
        "period": period,
        "interval": interval,
        "auto_adjust": auto_adjust,
    }
    if start is not None:
        kwargs["start"] = start
    if end is not None:
        kwargs["end"] = end
    return resolved_provider.fetch_history(ticker, **kwargs)


def fetch_news(
    ticker: str,
    *,
    limit: int = 10,
    provider: MarketDataProvider | None = None,
) -> NewsDataResult:
    resolved_provider = provider or _default_provider
    return resolved_provider.fetch_news(ticker, limit=limit)


def market_data_health_row(result: MarketDataResult | NewsDataResult) -> dict[str, object]:
    mode = MODE_NO_TRADE if result.metadata.stale else result.metadata.mode
    outcome = "stale" if result.metadata.stale else result.metadata.freshness_outcome
    return {
        "ticker": result.ticker,
        "source": result.metadata.source,
        "data_kind": result.metadata.data_kind,
        "error": summarize_provider_error(result.metadata.error or ""),
        "retry_count": result.metadata.retry_count,
        "observation_time": result.metadata.as_of or "",
        "retrieval_time": result.metadata.fetched_at,
        "market_session": result.metadata.market_session or "",
        "calendar": result.metadata.calendar,
        "freshness_outcome": outcome,
        "contradiction_status": result.metadata.contradiction_status,
        "mode": mode,
        "reason": result.metadata.reason,
        "stale": result.metadata.stale,
        "fetched_at": result.metadata.fetched_at,
        "as_of": result.metadata.as_of or "",
    }


def market_data_is_actionable(result: MarketDataResult | NewsDataResult) -> bool:
    return (
        not bool(result.metadata.error)
        and not result.metadata.stale
        and result.metadata.mode == MODE_NORMAL
    )


def write_market_data_health_artifact(
    results: list[MarketDataResult | NewsDataResult],
    path: str | os.PathLike[str] = data_path("data_source_health.csv"),
) -> None:
    output_path = Path(path)
    rows = [market_data_health_row(result) for result in results]
    output_df = validate_data_source_health(
        pd.DataFrame(rows, columns=HEALTH_COLUMNS),
        keep_extra_columns=False,
    )
    if output_path.name == "data_source_health.csv":
        write_managed_csv_with_schema(
            output_df,
            output_path,
            schema=DATA_SOURCE_HEALTH_SCHEMA,
            producer="Market Data Health",
        )
    else:
        write_csv(output_df, output_path)


def append_market_data_health_artifact(
    results: list[MarketDataResult | NewsDataResult],
    path: str | os.PathLike[str] = data_path("data_source_health.csv"),
) -> None:
    """Append market-data source health rows without erasing earlier agent evidence.

    Universe Agent starts each pipeline run by writing a fresh artifact. Later
    agents append their provider calls so the run has one visible health file
    covering price, news, and backtest fetches.
    """
    if not results:
        return

    output_path = Path(path)
    rows = [market_data_health_row(result) for result in results]
    new_df = pd.DataFrame(rows, columns=HEALTH_COLUMNS)
    existing_df = pd.read_csv(output_path, keep_default_na=False) if output_path.exists() else pd.DataFrame()
    combined_df = validate_data_source_health(
        pd.concat([existing_df, new_df], ignore_index=True),
        keep_extra_columns=False,
    )
    if output_path.name == "data_source_health.csv":
        write_managed_csv_with_schema(
            combined_df,
            output_path,
            schema=DATA_SOURCE_HEALTH_SCHEMA,
            producer="Market Data Health",
        )
    else:
        write_csv(combined_df, output_path)
