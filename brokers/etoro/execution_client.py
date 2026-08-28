from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

import yaml

from brokers.etoro.client import (
    EtoroAuthenticationError,
    EtoroClientError,
    EtoroConfigurationError,
    EtoroCredentials,
    EtoroNotFoundError,
    EtoroRateLimitError,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    SlidingWindowLimiter,
    UrllibTransport,
)
from brokers.etoro.endpoints import (
    CANCEL_DEMO_ORDER_V3,
    CLOSE_DEMO_POSITION_V1,
    LOOKUP_DEMO_ORDER_V2,
    SUBMIT_DEMO_ORDER_V3,
)
from brokers.etoro.schemas import (
    EtoroAcceptedOrder,
    EtoroCloseSubmission,
    EtoroOrderLookup,
    EtoroSchemaError,
)
from execution.domain import Environment, canonical_json


class EtoroAmbiguousOutcome(EtoroClientError):
    """The request may have reached eToro and must be reconciled, not replayed."""

    def __init__(self, operation: str, request_id: UUID, reason: str) -> None:
        super().__init__(
            f"{operation} outcome is ambiguous; request_id={request_id}; "
            f"reconcile by reference/order identity ({reason})"
        )
        self.operation = operation
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class EtoroDemoWriteConfig:
    enabled: bool
    write_methods_enabled: bool
    base_url: str
    api_key_environment_variable: str
    user_key_environment_variable: str
    account_reads_per_minute: int
    writes_per_minute: int
    timeout_seconds: float
    max_lookup_retries: int

    @classmethod
    def load(cls, path: Path | str) -> "EtoroDemoWriteConfig":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            credentials = raw["credential_environment_variables"]
            rates = raw["rate_limits_per_minute"]
            config = cls(
                enabled=raw["enabled"] is True,
                write_methods_enabled=raw["write_methods_enabled"] is True,
                base_url=str(raw["base_url"]).rstrip("/"),
                api_key_environment_variable=str(credentials["api_key"]),
                user_key_environment_variable=str(credentials["user_key"]),
                account_reads_per_minute=int(rates["account_reads"]),
                writes_per_minute=int(rates["writes"]),
                timeout_seconds=float(raw["request_timeout_seconds"]),
                max_lookup_retries=int(raw["max_lookup_retries"]),
            )
            if str(raw["environment"]).lower() != "demo":
                raise EtoroConfigurationError("only eToro Demo writes are authorized")
            if str(raw["mode"]).lower() != "manual_approval":
                raise EtoroConfigurationError("Demo writes require manual_approval mode")
            if raw["real_routes_enabled"] is not False:
                raise EtoroConfigurationError("Real routes must remain disabled")
            if raw["per_order_human_approval_required"] is not True:
                raise EtoroConfigurationError("every Demo order requires human approval")
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, EtoroConfigurationError):
                raise
            raise EtoroConfigurationError(
                "eToro Demo write configuration is missing or invalid"
            ) from exc
        parsed = urllib.parse.urlparse(config.base_url)
        if parsed.scheme != "https" or parsed.netloc != "public-api.etoro.com":
            raise EtoroConfigurationError("configured eToro host is not pinned HTTPS")
        if config.account_reads_per_minute <= 0 or config.account_reads_per_minute > 48:
            raise EtoroConfigurationError("account-read rate limit exceeds local cap")
        if config.writes_per_minute <= 0 or config.writes_per_minute > 10:
            raise EtoroConfigurationError("write rate limit exceeds local cap")
        if config.timeout_seconds <= 0 or config.max_lookup_retries < 0:
            raise EtoroConfigurationError("timeout/retry configuration is invalid")
        for name in (
            config.api_key_environment_variable,
            config.user_key_environment_variable,
        ):
            if not name.startswith("ETORO_DEMO_WRITE_"):
                raise EtoroConfigurationError(
                    "Demo write credentials must use ETORO_DEMO_WRITE_* names"
                )
        return config


class EtoroDemoExecutionClient:
    environment = Environment.DEMO
    read_only = False

    def __init__(
        self,
        *,
        credentials: EtoroCredentials,
        config: EtoroDemoWriteConfig,
        transport: HttpTransport | None = None,
        request_id_factory: Callable[[], UUID] = uuid4,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if not config.enabled or not config.write_methods_enabled:
            raise EtoroConfigurationError("eToro Demo writes are disabled")
        self._credentials = credentials
        self.config = config
        self._transport = transport or UrllibTransport()
        self._request_id_factory = request_id_factory
        self._sleeper = sleeper
        self._jitter = jitter
        self._account_limiter = SlidingWindowLimiter(
            config.account_reads_per_minute,
            clock=monotonic_clock,
            sleeper=sleeper,
        )
        self._write_limiter = SlidingWindowLimiter(
            config.writes_per_minute,
            clock=monotonic_clock,
            sleeper=sleeper,
        )

    @classmethod
    def from_config(
        cls,
        path: Path | str,
        *,
        environment: Mapping[str, str] | None = None,
        transport: HttpTransport | None = None,
    ) -> "EtoroDemoExecutionClient":
        config = EtoroDemoWriteConfig.load(path)
        values = os.environ if environment is None else environment
        credentials = EtoroCredentials(
            api_key=str(values.get(config.api_key_environment_variable, "")),
            user_key=str(values.get(config.user_key_environment_variable, "")),
        )
        return cls(credentials=credentials, config=config, transport=transport)

    def _headers(self, request_id: UUID, *, body: bool) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "x-api-key": self._credentials.api_key,
            "x-user-key": self._credentials.user_key,
            "x-request-id": str(request_id),
        }
        if body:
            headers["content-type"] = "application/json"
        return headers

    @staticmethod
    def _retry_after(response: HttpResponse) -> float | None:
        value = next(
            (item for key, item in response.headers.items() if key.lower() == "retry-after"),
            None,
        )
        try:
            return None if value is None else min(60.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_json(response: HttpResponse, *, operation: str) -> Any:
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EtoroSchemaError(f"{operation} returned invalid JSON") from exc

    def _send_mutation(
        self,
        *,
        operation: str,
        method: str,
        path: str,
        request_id: UUID,
        body: Mapping[str, Any] | None,
        accepted_status: int,
    ) -> Any:
        self._write_limiter.acquire()
        encoded = canonical_json(body).encode("utf-8") if body is not None else None
        request = HttpRequest(
            method=method,
            url=f"{self.config.base_url}{path}",
            headers=self._headers(request_id, body=body is not None),
            timeout_seconds=self.config.timeout_seconds,
            body=encoded,
        )
        try:
            response = self._transport.send(request)
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise EtoroAmbiguousOutcome(operation, request_id, "transport failure") from exc
        if response.status_code == accepted_status:
            try:
                return self._decode_json(response, operation=operation)
            except EtoroSchemaError as exc:
                raise EtoroAmbiguousOutcome(
                    operation, request_id, "accepted response could not be parsed"
                ) from exc
        if response.status_code in {401, 403}:
            raise EtoroAuthenticationError(
                f"{operation} authentication/authorization failed "
                f"({response.status_code}); request_id={request_id}"
            )
        if response.status_code == 429:
            delay = self._retry_after(response)
            suffix = f"; retry_after={delay}" if delay is not None else ""
            raise EtoroRateLimitError(
                f"{operation} rate-limited; request_id={request_id}{suffix}"
            )
        if response.status_code >= 500:
            raise EtoroAmbiguousOutcome(operation, request_id, "server error")
        raise EtoroClientError(
            f"{operation} rejected ({response.status_code}); request_id={request_id}"
        )

    def _lookup(self, query: Mapping[str, str]) -> EtoroOrderLookup:
        reference_id = query.get("referenceId")
        for attempt in range(self.config.max_lookup_retries + 1):
            self._account_limiter.acquire()
            request_id = self._request_id_factory()
            request = HttpRequest(
                method="GET",
                url=(
                    f"{self.config.base_url}{LOOKUP_DEMO_ORDER_V2.path}?"
                    f"{urllib.parse.urlencode(query)}"
                ),
                headers=self._headers(request_id, body=False),
                timeout_seconds=self.config.timeout_seconds,
            )
            try:
                response = self._transport.send(request)
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                if attempt >= self.config.max_lookup_retries:
                    raise EtoroAmbiguousOutcome(
                        LOOKUP_DEMO_ORDER_V2.operation,
                        request_id,
                        "lookup transport failure",
                    ) from exc
                self._sleeper(self._jitter(0.0, min(60.0, 2.0**attempt)))
                continue
            if response.status_code == 200:
                return EtoroOrderLookup.parse(
                    self._decode_json(response, operation=LOOKUP_DEMO_ORDER_V2.operation),
                    reference_id=reference_id,
                )
            if response.status_code == 404:
                raise EtoroNotFoundError("Demo order was not found during reconciliation")
            if response.status_code in {401, 403}:
                raise EtoroAuthenticationError("Demo order lookup authorization failed")
            if response.status_code == 429 and attempt < self.config.max_lookup_retries:
                delay = self._retry_after(response)
                self._sleeper(
                    delay
                    if delay is not None
                    else self._jitter(0.0, min(60.0, 2.0**attempt))
                )
                continue
            if response.status_code >= 500 and attempt < self.config.max_lookup_retries:
                self._sleeper(self._jitter(0.0, min(60.0, 2.0**attempt)))
                continue
            raise EtoroClientError(
                f"Demo order lookup failed ({response.status_code})"
            )
        raise EtoroClientError("Demo order lookup failed")

    @staticmethod
    def _validate_open_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(payload)
        if value.get("action") != "open" or value.get("transaction") != "buy":
            raise EtoroConfigurationError("Gate B supports opening long positions only")
        if value.get("orderType", "mkt") != "mkt":
            raise EtoroConfigurationError("Gate B supports market opens only")
        if value.get("settlementType") != "real":
            raise EtoroConfigurationError("Gate B permits only unleveraged real settlement")
        if int(value.get("leverage", 1)) != 1:
            raise EtoroConfigurationError("Gate B leverage must be exactly 1")
        if value.get("symbol") not in {None, ""}:
            raise EtoroConfigurationError("Gate B submissions require immutable instrumentId")
        try:
            instrument_id = int(value["instrumentId"])
            amount = Decimal(str(value["amount"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise EtoroConfigurationError("Gate B payload identity/sizing is invalid") from exc
        if instrument_id <= 0 or not amount.is_finite() or amount <= 0:
            raise EtoroConfigurationError("Gate B payload identity/sizing is invalid")
        if str(value.get("orderCurrency", "")).lower() != "usd":
            raise EtoroConfigurationError("Gate B order currency must be USD")
        if any(value.get(name) not in {None, ""} for name in ("units", "contracts")):
            raise EtoroConfigurationError("Gate B amount sizing cannot include units/contracts")
        return value

    def submit_open_order(
        self, payload: Mapping[str, Any], *, request_id: UUID
    ) -> EtoroAcceptedOrder:
        accepted = self._send_mutation(
            operation=SUBMIT_DEMO_ORDER_V3.operation,
            method="POST",
            path=SUBMIT_DEMO_ORDER_V3.path,
            request_id=request_id,
            body=self._validate_open_payload(payload),
            accepted_status=202,
        )
        result = EtoroAcceptedOrder.parse(accepted)
        if result.reference_id != str(request_id):
            raise EtoroAmbiguousOutcome(
                SUBMIT_DEMO_ORDER_V3.operation,
                request_id,
                "referenceId did not echo the persisted request identity",
            )
        return result

    def lookup_order(
        self, *, order_id: str | None = None, reference_id: str | None = None
    ) -> EtoroOrderLookup:
        if bool(order_id) == bool(reference_id):
            raise ValueError("provide exactly one of order_id or reference_id")
        return self._lookup(
            {"orderId": str(order_id)}
            if order_id
            else {"referenceId": str(reference_id)}
        )

    def close_position(
        self,
        *,
        position_id: str,
        instrument_id: int,
        units: Decimal | None,
        request_id: UUID,
    ) -> EtoroCloseSubmission:
        if int(instrument_id) <= 0 or int(position_id) <= 0:
            raise EtoroConfigurationError("close identity is invalid")
        if units is not None and (not units.is_finite() or units <= 0):
            raise EtoroConfigurationError("partial-close units must be positive")
        body: dict[str, Any] = {"InstrumentID": int(instrument_id)}
        if units is not None:
            body["UnitsToDeduct"] = str(units)
        payload = self._send_mutation(
            operation=CLOSE_DEMO_POSITION_V1.operation,
            method="POST",
            path=CLOSE_DEMO_POSITION_V1.path.format(position_id=int(position_id)),
            request_id=request_id,
            body=body,
            accepted_status=200,
        )
        result = EtoroCloseSubmission.parse(payload)
        if result.position_id != str(position_id) or result.instrument_id != int(instrument_id):
            raise EtoroAmbiguousOutcome(
                CLOSE_DEMO_POSITION_V1.operation,
                request_id,
                "close acknowledgement identity mismatch",
            )
        return result

    def cancel_order(self, *, order_id: str, request_id: UUID) -> EtoroAcceptedOrder:
        if int(order_id) <= 0:
            raise EtoroConfigurationError("cancel order identity is invalid")
        payload = self._send_mutation(
            operation=CANCEL_DEMO_ORDER_V3.operation,
            method="DELETE",
            path=CANCEL_DEMO_ORDER_V3.path.format(order_id=int(order_id)),
            request_id=request_id,
            body=None,
            accepted_status=202,
        )
        raw = dict(payload)
        raw["referenceId"] = raw.get("referenceId") or str(request_id)
        result = EtoroAcceptedOrder.parse(raw)
        if result.order_id != str(order_id):
            raise EtoroAmbiguousOutcome(
                CANCEL_DEMO_ORDER_V3.operation,
                request_id,
                "cancel acknowledgement identity mismatch",
            )
        return result
