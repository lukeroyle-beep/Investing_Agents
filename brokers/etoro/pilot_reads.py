"""Bounded official API reads with original bytes and explicit direct provenance.

The only POST routes are read-semantic eligibility/cost queries. Execution,
account mutation, credential discovery and MCP response emulation are absent.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID, uuid4

import requests

from brokers.etoro.autonomous_api import _json_body, _private_directory
from brokers.etoro.capture_transport import RawCaptureResponse, _NoRedirectSession, private_json, private_write
from brokers.etoro.client import EtoroCredentials, HttpRequest
from execution.domain import _aware_utc


BASE_URL = "https://public-api.etoro.com"
SOURCE_TRANSPORT = "official_demo_api"
MAX_BODY_BYTES = 8 * 1024 * 1024
GET_PATHS = frozenset({"/api/v1/me", "/api/v2/market-data/instruments",
    "/api/v1/market-data/search", "/api/v2/market-data/rates",
    "/api/v1/trading/info/demo/portfolio", "/api/v1/trading/info/demo/pnl"})
POST_PATHS = frozenset({"/api/v2/trading/info/demo/eligibility", "/api/v2/trading/info/demo/costs"})
_NO_QUERY = frozenset({"/api/v1/me", "/api/v1/trading/info/demo/portfolio", "/api/v1/trading/info/demo/pnl"})


class PilotReadError(ValueError):
    """Fixed diagnostic codes only; never echo credentials or remote content."""


def _json(value):
    def pairs(items):
        result = {}
        for key, child in items:
            if key in result:
                raise ValueError
            result[key] = child
        return result
    def invalid(_):
        raise ValueError
    try:
        result = json.loads(value, parse_float=Decimal, parse_constant=invalid, object_pairs_hook=pairs)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise PilotReadError("direct_read_original_json_object_required") from None


def _arguments(path, kwargs):
    if not isinstance(path, str) or path not in GET_PATHS | POST_PATHS or set(kwargs) - {"query", "body"}:
        raise PilotReadError("direct_read_route_or_arguments_not_allowed")
    arguments = {"path": path}
    query, body = kwargs.get("query"), kwargs.get("body")
    if path in GET_PATHS:
        if "body" in kwargs or (path in _NO_QUERY and "query" in kwargs):
            raise PilotReadError("direct_read_get_arguments_not_allowed")
        if "query" in kwargs:
            if (not isinstance(query, dict) or not query or len(query) > 32
                    or any(not isinstance(key, str) or not key or len(key) > 128
                           or type(value) not in {str, int} or len(str(value)) > 8192
                           for key, value in query.items())):
                raise PilotReadError("direct_read_query_invalid")
            arguments["query"] = deepcopy(query)
        return "GET", arguments, None
    if "query" in kwargs or "body" not in kwargs:
        raise PilotReadError("direct_read_post_body_required")
    try:
        encoded = body.encode("utf-8") if isinstance(body, str) else _json_body(body)
    except (ValueError, TypeError, UnicodeError):
        raise PilotReadError("direct_read_post_body_invalid") from None
    if len(encoded) > 65536:
        raise PilotReadError("direct_read_post_body_too_large")
    _json(encoded)
    arguments["body"] = encoded.decode("utf-8")
    return "POST", arguments, encoded


def _request_boundary(request):
    parsed = urlsplit(request.url)
    if (parsed.scheme != "https" or parsed.netloc != "public-api.etoro.com" or parsed.fragment
            or type(request.timeout_seconds) not in {int, float} or not math.isfinite(request.timeout_seconds)
            or not 0 < request.timeout_seconds <= 10):
        raise PilotReadError("direct_read_transport_boundary")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in query}) != len(query):
        raise PilotReadError("direct_read_transport_boundary")
    kwargs = {"query": dict(query)} if parsed.query else {}
    if request.body is not None:
        if not isinstance(request.body, bytes):
            raise PilotReadError("direct_read_transport_boundary")
        try:
            kwargs["body"] = request.body.decode("utf-8")
        except UnicodeError:
            raise PilotReadError("direct_read_transport_boundary") from None
    method, _, _ = _arguments(parsed.path, kwargs)
    if (request.method != method or set(key.lower() for key in request.headers)
            - {"x-api-key", "x-user-key", "x-request-id", "accept", "content-type"}):
        raise PilotReadError("direct_read_transport_boundary")


class DirectPilotReadTransport:
    """One allowlisted send; no redirect, retry, proxy, cookie or netrc use."""

    def send(self, request):
        _request_boundary(request)
        with _NoRedirectSession() as session:
            session.trust_env = False
            session.auth = None
            session.proxies.clear()
            session.cookies.clear()
            session.headers["Accept-Encoding"] = "identity"
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            outbound = session.prepare_request(requests.Request(request.method, request.url,
                headers=dict(request.headers), data=request.body))
            started = time.monotonic()
            with session.send(outbound, timeout=request.timeout_seconds, stream=True,
                              allow_redirects=False, verify=True, proxies={}) as response:
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in {"identity", "gzip", "deflate"}:
                    encoding = "unsupported"
                chunks, length, outcome = [], 0, "complete"
                try:
                    while True:
                        if time.monotonic() - started > request.timeout_seconds:
                            outcome = "bounded_read_exceeded"
                            break
                        chunk = response.raw.read1(min(65536, MAX_BODY_BYTES + 1 - length), decode_content=False)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        length += len(chunk)
                        if length > MAX_BODY_BYTES:
                            outcome = "bounded_read_exceeded"
                            break
                except Exception:
                    outcome = "partial" if chunks else "failed"
                return RawCaptureResponse(int(response.status_code), b"".join(chunks), encoding, outcome)


def read_receipt_envelope(receipt):
    """Validate a direct receipt and return (raw-text envelope, parsed object).

    This does not read files or invent an MCP wrapper. The retained raw-response
    digest binds the original UTF-8 body; the caller keeps its source timestamps.
    """
    try:
        if (not isinstance(receipt, dict) or receipt.get("tool") != "etoro_direct_read"
                or receipt.get("source_transport") != SOURCE_TRANSPORT):
            raise ValueError
        arguments = receipt["arguments"]
        method, normalized, request_body = _arguments(arguments["path"], {k:v for k,v in arguments.items() if k != "path"})
        if arguments != normalized or receipt["method"] != method:
            raise ValueError
        UUID(receipt["request_id"])
        query = arguments.get("query")
        expected_request = {"url":BASE_URL+arguments["path"]+(("?"+urlencode(query)) if query else ""),
            "method":method, "request_id":receipt["request_id"],
            "body_sha256":hashlib.sha256(request_body).hexdigest() if request_body is not None else None}
        if receipt.get("raw_request") != expected_request:
            raise ValueError
        start = _aware_utc(datetime.fromisoformat(receipt["request_started_at"].replace("Z", "+00:00")), "start")
        end = _aware_utc(datetime.fromisoformat(receipt["response_received_at"].replace("Z", "+00:00")), "end")
        if not 0 <= (end-start).total_seconds() <= 10:
            raise ValueError
        envelope, raw = receipt["result"], receipt["raw_response"]
        if (not isinstance(envelope, dict) or set(envelope) != {"statusCode", "isSuccess", "bodyTruncated", "body", "source_transport"}
                or type(envelope["statusCode"]) is not int or envelope["statusCode"] != 200
                or envelope["isSuccess"] is not True or envelope["bodyTruncated"] is not False
                or envelope["source_transport"] != SOURCE_TRANSPORT or not isinstance(envelope["body"], str)):
            raise ValueError
        body = envelope["body"].encode("utf-8")
        if (not isinstance(raw, dict) or type(raw.get("status_code")) is not int or raw["status_code"] != 200
                or raw.get("read_outcome") != "complete" or raw.get("content_encoding") != "identity"
                or not 0 < len(body) <= MAX_BODY_BYTES or type(raw.get("body_bytes")) is not int
                or raw["body_bytes"] != len(body) or raw.get("body_sha256") != hashlib.sha256(body).hexdigest()
                or not isinstance(raw.get("body_file"), str) or not raw["body_file"]):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise PilotReadError("direct_read_receipt_provenance_invalid") from None
    return envelope, _json(body)


class DirectPilotReads:
    """Dedicated injected keys; original complete receipts for pure consumers."""

    def __init__(self, *, credentials, receipt_dir, clock=lambda: datetime.now(timezone.utc),
                 timeout_seconds=10, transport=None):
        if not isinstance(credentials, EtoroCredentials):
            raise PilotReadError("direct_read_dedicated_credentials_required")
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 10):
            raise PilotReadError("direct_read_timeout_invalid")
        self._credentials, self.clock = credentials, clock
        self.receipt_dir = Path(receipt_dir)
        _private_directory(self.receipt_dir)
        self.timeout_seconds = timeout_seconds
        self.transport = transport if transport is not None else DirectPilotReadTransport()

    def read(self, path, **kwargs):
        method, arguments, body = _arguments(path, kwargs)
        query = arguments.get("query")
        url = BASE_URL + path + (("?" + urlencode(query)) if query else "")
        reference = str(uuid4())
        prefix = self.receipt_dir / ("read-" + reference)
        started = _aware_utc(self.clock(), "request_started_at")
        receipt = {"tool":"etoro_direct_read", "source_transport":SOURCE_TRANSPORT,
            "method":method, "request_id":reference, "arguments":arguments,
            "request_started_at":started.isoformat(), "raw_request":{
                "url":url, "method":method, "request_id":reference,
                "body_sha256":hashlib.sha256(body).hexdigest() if body is not None else None}}
        private_json(prefix.with_suffix(".request.json"), receipt)
        headers = {"x-api-key":self._credentials.api_key, "x-user-key":self._credentials.user_key,
                   "x-request-id":reference, "Accept":"application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = HttpRequest(method, url, headers, self.timeout_seconds, body)
        _request_boundary(request)
        try:
            response = self.transport.send(request)
        except Exception:
            private_json(prefix.with_suffix(".failure.json"), {"outcome":"transport_failed",
                "response_received_at":_aware_utc(self.clock(), "response_received_at").isoformat()})
            raise PilotReadError("direct_read_transport_failed") from None
        completed = _aware_utc(self.clock(), "response_received_at")
        if not isinstance(response, RawCaptureResponse) or not isinstance(response.body, bytes):
            raise PilotReadError("direct_read_original_response_bytes_required")
        body_path = prefix.with_suffix(".response.bin")
        private_write(body_path, response.body)
        raw = {"status_code":response.status_code, "content_encoding":response.content_encoding,
            "read_outcome":response.read_outcome, "body_sha256":hashlib.sha256(response.body).hexdigest(),
            "body_bytes":len(response.body), "body_file":str(body_path)}
        receipt.update(response_received_at=completed.isoformat(), raw_response=raw)
        private_json(prefix.with_suffix(".response.json"), receipt)
        if (not started <= completed or (completed-started).total_seconds() > self.timeout_seconds
                or type(response.status_code) is not int or response.status_code != 200
                or response.read_outcome != "complete" or response.content_encoding != "identity"
                or not 0 < len(response.body) <= MAX_BODY_BYTES):
            raise PilotReadError("direct_read_response_not_bounded_complete_success")
        try:
            text = response.body.decode("utf-8")
        except UnicodeError:
            raise PilotReadError("direct_read_original_utf8_required") from None
        receipt["result"] = {"statusCode":response.status_code, "isSuccess":True, "bodyTruncated":False,
            "body":text, "source_transport":SOURCE_TRANSPORT}
        read_receipt_envelope(receipt)
        private_json(prefix.with_suffix(".receipt.json"), receipt)
        return receipt
