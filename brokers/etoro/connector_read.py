"""Supported connector reads without constructing or extracting credentials.

The host supplies execute_read(**arguments); this client neither authenticates
locally nor retries requests. The connector currently cannot supply the required
CID header on instrument-breakdown, so that capability fails before transport.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from brokers.etoro.client import EtoroReadOnlyClient, EtoroConfigurationError, EtoroClientError
from brokers.etoro.eligibility import validated_ids
from brokers.etoro.endpoints import (
    READ_ONLY_ENDPOINTS, GET_DEMO_POSITION_CURRENCIES,
    POST_DEMO_AGGREGATE_PORTFOLIO, POST_DEMO_ELIGIBILITY,
)
from execution.domain import Environment


def _exact_json(value):
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if isinstance(value, str):
            value = json.loads(value, parse_float=Decimal,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, ArithmeticError):
        # Raw response is retained by _read before parsing. Diagnostics never
        # include private payload text or an underlying decoder exception.
        raise EtoroClientError("connector_response_json_invalid") from None
    if isinstance(value, float):
        raise EtoroClientError("connector_raw_decimal_json_required")
    if isinstance(value, Mapping):
        return {key: _exact_json_nested(item) for key, item in value.items()}
    raise EtoroClientError("connector_response_object_required")


def _exact_json_nested(value):
    if isinstance(value, float):
        raise EtoroClientError("connector_raw_decimal_json_required")
    if isinstance(value, Mapping):
        return {key: _exact_json_nested(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_exact_json_nested(item) for item in value]
    return value


class EtoroConnectorReadOnlyClient(EtoroReadOnlyClient):
    environment = Environment.DEMO
    read_only = True

    def __init__(self, *, execute_read, receipt_sink=None, clock=None):
        self.execute_read = execute_read
        self.receipt_sink = receipt_sink
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @classmethod
    def from_config(cls, *args, **kwargs):
        raise EtoroConfigurationError("connector_host_callback_required_no_local_credentials")

    @staticmethod
    def require_component_capture_capability():
        raise EtoroConfigurationError("connector_cannot_supply_required_CID_header")

    def _read(self, endpoint, params):
        if endpoint not in READ_ONLY_ENDPOINTS:
            raise EtoroConfigurationError("connector_endpoint_not_allowlisted")
        if endpoint == GET_DEMO_POSITION_CURRENCIES:
            # CID is a header, never invent a query-string substitute or drop
            # identity to make a request pass. No network call is made here.
            self.require_component_capture_capability()
        arguments = {"path": endpoint.path}
        if endpoint == POST_DEMO_AGGREGATE_PORTFOLIO:
            if params:
                raise EtoroConfigurationError("aggregate_account_read_must_be_unfiltered")
            arguments["body"] = json.dumps({"conversionMode": "eToroApp", "pnlLevel": "pnl",
                "accountLevel": "totals", "instrumentLevel": "details", "mirrorLevel": "details"})
        elif endpoint == POST_DEMO_ELIGIBILITY:
            if set(params) != {"instrumentIds"}:
                raise EtoroConfigurationError("eligibility_exact_ids_required")
            arguments["body"] = json.dumps({"instrumentIds": validated_ids(params["instrumentIds"]), "currency": "USD"})
        elif endpoint.method != "GET":
            raise EtoroConfigurationError("connector_mutation_denied")
        elif params:
            arguments["query"] = {key: str(value) for key, value in params.items()}
        started = self.clock()
        response = self.execute_read(**arguments)  # exactly one supported call
        completed = self.clock()
        if self.receipt_sink:
            # Retain the exact connector response before inspecting status or
            # decoding numbers. Failed reads remain inspectable without retry.
            self.receipt_sink({"operation": endpoint.operation, "request": arguments,
                "started_at": started, "completed_at": completed, "raw_response": response})
        if isinstance(response, Mapping) and "content" in response:
            if response.get("isError"):
                raise EtoroClientError("connector_tool_error")
            content = response["content"]
            if not isinstance(content, (list, tuple)) or not all(isinstance(part, Mapping) for part in content):
                raise EtoroClientError("connector_single_raw_response_required")
            parts = [part.get("text") for part in content if part.get("type") == "text"]
            if len(parts) != 1 or not isinstance(parts[0], str):
                raise EtoroClientError("connector_single_raw_response_required")
            response = parts[0]
        envelope = _exact_json(response)
        if (envelope.get("statusCode") != 200 or envelope.get("error")
                or envelope.get("bodyTruncated") is True):
            raise EtoroClientError("connector_read_unsuccessful_or_incomplete_no_retry")
        return _exact_json(envelope.get("body"))
