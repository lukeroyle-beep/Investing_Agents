from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

import yaml

from brokers.etoro.endpoints import (
    GET_DEMO_AGGREGATE_PORTFOLIO,
    GET_MARKET_RATES,
    SEARCH_INSTRUMENTS,
    Endpoint,
    RateBucket,
)
from brokers.etoro.schemas import (
    EtoroMarketRatesResponse,
    EtoroPortfolioSnapshot,
    EtoroSchemaError,
    EtoroSearchResponse,
)
from execution.domain import Environment


class EtoroClientError(RuntimeError):
    """Sanitized eToro client failure; response bodies are never included."""


class EtoroConfigurationError(EtoroClientError):
    pass


class EtoroAuthenticationError(EtoroClientError):
    pass


class EtoroNotFoundError(EtoroClientError):
    pass


class EtoroRateLimitError(EtoroClientError):
    pass


class EtoroServerError(EtoroClientError):
    pass


class EtoroTransportError(EtoroClientError):
    pass


@dataclass(frozen=True, slots=True)
class EtoroDemoReadOnlyConfig:
    enabled: bool
    base_url: str
    api_key_environment_variable: str
    user_key_environment_variable: str
    account_reads_per_minute: int
    market_reads_per_minute: int
    writes_per_minute: int
    timeout_seconds: float
    max_read_retries: int

    @classmethod
    def load(cls, path: Path | str) -> "EtoroDemoReadOnlyConfig":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise EtoroConfigurationError(
                "eToro Demo configuration is missing or invalid"
            ) from exc
        try:
            credentials = raw["credential_environment_variables"]
            rates = raw["rate_limits_per_minute"]
            config = cls(
                enabled=raw["enabled"] is True,
                base_url=str(raw["base_url"]).rstrip("/"),
                api_key_environment_variable=str(credentials["api_key"]),
                user_key_environment_variable=str(credentials["user_key"]),
                account_reads_per_minute=int(rates["account_reads"]),
                market_reads_per_minute=int(rates["market_reads"]),
                writes_per_minute=int(rates["writes"]),
                timeout_seconds=float(raw["request_timeout_seconds"]),
                max_read_retries=int(raw["max_read_retries"]),
            )
            if str(raw["environment"]).lower() != "demo":
                raise EtoroConfigurationError("only eToro Demo is authorized")
            if str(raw["mode"]).lower() != "read_only":
                raise EtoroConfigurationError("Gate A must remain read-only")
            if raw["rest_authoritative"] is not True:
                raise EtoroConfigurationError("REST must remain authoritative")
            if raw["real_routes_enabled"] is not False:
                raise EtoroConfigurationError("Real routes must remain disabled")
            if raw["write_methods_enabled"] is not False:
                raise EtoroConfigurationError("write methods must remain disabled")
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EtoroConfigurationError):
                raise
            raise EtoroConfigurationError("eToro Demo configuration is incomplete") from exc
        if config.account_reads_per_minute > 48:
            raise EtoroConfigurationError("account-read limit exceeds the local safety cap")
        if config.market_reads_per_minute > 96:
            raise EtoroConfigurationError("market-read limit exceeds the local safety cap")
        if config.writes_per_minute > 10:
            raise EtoroConfigurationError("write limit exceeds the local safety cap")
        if min(
            config.account_reads_per_minute,
            config.market_reads_per_minute,
            config.writes_per_minute,
        ) <= 0:
            raise EtoroConfigurationError("rate limits must be positive")
        if config.timeout_seconds <= 0 or config.max_read_retries < 0:
            raise EtoroConfigurationError("timeout/retry configuration is invalid")
        parsed_url = urllib.parse.urlparse(config.base_url)
        if parsed_url.scheme != "https" or parsed_url.netloc != "public-api.etoro.com":
            raise EtoroConfigurationError("configured eToro base URL is not the pinned HTTPS host")
        if not config.api_key_environment_variable.startswith("ETORO_DEMO_"):
            raise EtoroConfigurationError("API key must use an ETORO_DEMO_* name")
        if not config.user_key_environment_variable.startswith("ETORO_DEMO_"):
            raise EtoroConfigurationError("user key must use an ETORO_DEMO_* name")
        return config


@dataclass(frozen=True, slots=True)
class EtoroCredentials:
    api_key: str = field(repr=False)
    user_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.api_key.strip() or not self.user_key.strip():
            raise EtoroConfigurationError("eToro Demo credentials are missing")

    @classmethod
    def from_environment(
        cls,
        config: EtoroDemoReadOnlyConfig,
        environment: Mapping[str, str] | None = None,
    ) -> "EtoroCredentials":
        values = os.environ if environment is None else environment
        return cls(
            api_key=str(values.get(config.api_key_environment_variable, "")),
            user_key=str(values.get(config.user_key_environment_variable, "")),
        )


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    timeout_seconds: float
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def send(self, request: HttpRequest) -> HttpResponse: ...


class UrllibTransport:
    def send(self, request: HttpRequest) -> HttpResponse:
        outbound = urllib.request.Request(
            request.url,
            headers=dict(request.headers),
            method=request.method,
            data=request.body,
        )
        try:
            with urllib.request.urlopen(
                outbound, timeout=request.timeout_seconds
            ) as response:
                return HttpResponse(
                    status_code=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                status_code=int(exc.code),
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        *,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
        window_seconds: float = 60.0,
    ) -> None:
        self.limit = limit
        self.clock = clock
        self.sleeper = sleeper
        self.window_seconds = window_seconds
        self._events: deque[float] = deque()
        self._lock = Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self.clock()
                while self._events and now - self._events[0] >= self.window_seconds:
                    self._events.popleft()
                if len(self._events) < self.limit:
                    self._events.append(now)
                    return
                delay = max(0.0, self.window_seconds - (now - self._events[0]))
            self.sleeper(delay)


class EtoroReadOnlyClient:
    environment = Environment.DEMO
    read_only = True

    def __init__(
        self,
        *,
        credentials: EtoroCredentials,
        config: EtoroDemoReadOnlyConfig,
        transport: HttpTransport | None = None,
        request_id_factory: Callable[[], UUID] = uuid4,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._credentials = credentials
        self.config = config
        self._transport = transport or UrllibTransport()
        self._request_id_factory = request_id_factory
        self._sleeper = sleeper
        self._jitter = jitter
        self._limiters = {
            RateBucket.ACCOUNT_READ: SlidingWindowLimiter(
                config.account_reads_per_minute,
                clock=monotonic_clock,
                sleeper=sleeper,
            ),
            RateBucket.MARKET_READ: SlidingWindowLimiter(
                config.market_reads_per_minute,
                clock=monotonic_clock,
                sleeper=sleeper,
            ),
        }

    @classmethod
    def from_config(
        cls,
        path: Path | str,
        *,
        environment: Mapping[str, str] | None = None,
        transport: HttpTransport | None = None,
    ) -> "EtoroReadOnlyClient":
        config = EtoroDemoReadOnlyConfig.load(path)
        if not config.enabled:
            raise EtoroConfigurationError("eToro Demo adapter is disabled")
        credentials = EtoroCredentials.from_environment(config, environment)
        return cls(credentials=credentials, config=config, transport=transport)

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float | None:
        value = next(
            (item for key, item in headers.items() if key.lower() == "retry-after"),
            None,
        )
        if value is None:
            return None
        try:
            return min(60.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return None

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "x-api-key": self._credentials.api_key,
            "x-user-key": self._credentials.user_key,
            "x-request-id": str(self._request_id_factory()),
        }

    def _get(self, endpoint: Endpoint, params: Mapping[str, Any]) -> Any:
        if endpoint.method != "GET":
            raise EtoroConfigurationError("Gate A endpoint is not read-only")
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{self.config.base_url}{endpoint.path}"
        if query:
            url = f"{url}?{query}"
        last_error: Exception | None = None
        for attempt in range(self.config.max_read_retries + 1):
            self._limiters[endpoint.rate_bucket].acquire()
            request = HttpRequest(
                method="GET",
                url=url,
                headers=self._headers(),
                timeout_seconds=self.config.timeout_seconds,
            )
            request_id = request.headers["x-request-id"]
            try:
                response = self._transport.send(request)
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
                if attempt >= self.config.max_read_retries:
                    raise EtoroTransportError(
                        f"{endpoint.operation} transport failed; request_id={request_id}"
                    ) from exc
                self._sleeper(self._jitter(0.0, min(60.0, 2.0**attempt)))
                continue
            status = response.status_code
            if status == 200:
                try:
                    return json.loads(response.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise EtoroSchemaError(
                        f"{endpoint.operation} returned invalid JSON; request_id={request_id}"
                    ) from exc
            if status in {401, 403}:
                raise EtoroAuthenticationError(
                    f"{endpoint.operation} authentication failed ({status}); "
                    f"request_id={request_id}"
                )
            if status == 404:
                raise EtoroNotFoundError(
                    f"{endpoint.operation} was not found; request_id={request_id}"
                )
            if status == 429:
                if attempt >= self.config.max_read_retries:
                    raise EtoroRateLimitError(
                        f"{endpoint.operation} remained rate-limited; request_id={request_id}"
                    )
                delay = self._retry_after(response.headers)
                if delay is None:
                    delay = self._jitter(0.0, min(60.0, 2.0**attempt))
                self._sleeper(delay)
                continue
            if 500 <= status <= 599:
                if attempt >= self.config.max_read_retries:
                    raise EtoroServerError(
                        f"{endpoint.operation} failed ({status}); request_id={request_id}"
                    )
                self._sleeper(self._jitter(0.0, min(60.0, 2.0**attempt)))
                continue
            raise EtoroClientError(
                f"{endpoint.operation} failed ({status}); request_id={request_id}"
            )
        raise EtoroTransportError(f"{endpoint.operation} failed") from last_error

    def search_instruments(self, exact_symbol: str) -> EtoroSearchResponse:
        symbol = str(exact_symbol).strip().upper()
        if not symbol:
            raise ValueError("exact_symbol must not be blank")
        payload = self._get(
            SEARCH_INSTRUMENTS,
            {
                "fields": (
                    "instrumentId,internalSymbolFull,internalExchangeName,"
                    "instrumentType,isDelisted,isCurrentlyTradable"
                ),
                "internalSymbolFull": symbol,
                "pageSize": 100,
                "pageNumber": 1,
            },
        )
        return EtoroSearchResponse.parse(payload)

    def get_market_rates(
        self, broker_instrument_ids: list[int] | tuple[int, ...]
    ) -> EtoroMarketRatesResponse:
        identifiers = tuple(int(item) for item in broker_instrument_ids)
        if not identifiers or len(identifiers) > 100 or any(item <= 0 for item in identifiers):
            raise ValueError("instrument IDs must contain between 1 and 100 positive IDs")
        payload = self._get(
            GET_MARKET_RATES,
            {"instrumentIds": ",".join(str(item) for item in identifiers)},
        )
        response = EtoroMarketRatesResponse.parse(payload)
        returned = {rate.instrument_id for rate in response.rates}
        if returned != set(identifiers):
            raise EtoroSchemaError("market-rate response did not exactly cover requested IDs")
        return response

    def get_demo_portfolio(self) -> EtoroPortfolioSnapshot:
        payload = self._get(GET_DEMO_AGGREGATE_PORTFOLIO, {})
        return EtoroPortfolioSnapshot.parse(payload)
