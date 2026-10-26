"""Bounded private response receipts for the repository's read-only collector."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import time
import urllib.parse
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import requests

from brokers.etoro.client import HttpResponse
from brokers.etoro.endpoints import (
    GET_DEMO_DETAILED_PORTFOLIO, GET_DEMO_PNL_PORTFOLIO, GET_DEMO_POSITION_CURRENCIES, GET_SECURITIES_V2, POST_DEMO_AGGREGATE_PORTFOLIO,
)

from brokers.etoro.pnl_currency_metadata import PNL_CURRENCY_METADATA_QUERY, validate_demo_customer_id
from brokers.etoro.schemas import EtoroSchemaError


class CaptureBoundaryError(RuntimeError):
    pass


def sync_directory(path: Path) -> None:
    """Persist new directory entries; unsupported filesystems fail closed."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_write(path: Path, body: bytes) -> None:
    """Create exclusively and restrict permissions before any bytes are written."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


def private_json(path: Path, value) -> None:
    private_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


@dataclass(frozen=True, slots=True)
class RawCaptureResponse:
    """Content-encoded bytes, never a parsed/decoded shared HttpResponse.

    Encoding is an internal closed classification, not a retained header. A
    partial/failed read is evidence only and must never reach repository parsing.
    """

    status_code: int
    body: bytes = field(repr=False)
    content_encoding: str = "identity"
    read_outcome: str = "complete"
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class _NoRedirectSession(requests.Session):
    def resolve_redirects(self, *args, **kwargs):
        # Session.send invokes this even with allow_redirects=False to populate
        # Response._next. The normal resolver consumes response.content first,
        # losing the original compressed redirect bytes. Never enter it.
        return iter(())


class CaptureHttpTransport:
    """One ordinary requests send; no environment discovery or redirect work."""

    def send(self, request):
        with _NoRedirectSession() as session:
            session.trust_env = False  # Includes proxies, CA overrides and .netrc.
            session.auth = None
            session.proxies.clear()
            session.cookies.clear()
            # Keep the ordinary requests User-Agent; advertise only supported
            # content encodings, independently of optional installed decoders.
            session.headers["Accept-Encoding"] = "gzip, deflate"
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            outbound = session.prepare_request(requests.Request(
                request.method, request.url, headers=dict(request.headers), data=request.body,
            ))
            with session.send(outbound, timeout=request.timeout_seconds, stream=True,
                              allow_redirects=False, verify=True, proxies={}) as response:
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in {"identity", "gzip", "deflate"}:
                    encoding = "unsupported"
                chunks = []
                outcome = "complete"
                try:
                    # read1 yields available prefixes without hiding them in a
                    # large read buffer if the next socket read fails/truncates.
                    while chunk := response.raw.read1(65536, decode_content=False):
                        chunks.append(chunk)
                except Exception:
                    outcome = "partial" if chunks else "failed"
                return RawCaptureResponse(
                    int(response.status_code), b"".join(chunks), encoding, outcome,
                    datetime.now(timezone.utc).isoformat(),
                )


def _decode_body(response: RawCaptureResponse) -> bytes:
    if response.content_encoding == "identity":
        return response.body
    if response.content_encoding == "gzip":
        return gzip.decompress(response.body)
    if response.content_encoding == "deflate":
        return zlib.decompress(response.body)
    raise CaptureBoundaryError("capture_encoding_unsupported")


def _check_route(request):
    parsed = urllib.parse.urlsplit(request.url)
    allowed = {(endpoint.method, endpoint.path) for endpoint in (
        GET_DEMO_DETAILED_PORTFOLIO, GET_DEMO_PNL_PORTFOLIO, GET_DEMO_POSITION_CURRENCIES, GET_SECURITIES_V2, POST_DEMO_AGGREGATE_PORTFOLIO,
    )}
    if (parsed.scheme != "https" or parsed.netloc != "public-api.etoro.com"
            or parsed.fragment or (request.method, parsed.path) not in allowed):
        raise CaptureBoundaryError("capture_route_not_allowed")
    if parsed.path == GET_DEMO_POSITION_CURRENCIES.path:
        expected_query = {name: [value] for name, value in PNL_CURRENCY_METADATA_QUERY}
        if (request.body is not None
                or urllib.parse.parse_qs(parsed.query, keep_blank_values=True) != expected_query):
            raise CaptureBoundaryError("capture_route_not_allowed")
        try:
            validate_demo_customer_id(request.headers.get("CID"))
        except EtoroSchemaError as exc:
            raise CaptureBoundaryError("capture_route_not_allowed") from exc
    elif request.method == "POST":
        fixed_body = json.dumps({
            "conversionMode": "eToroApp", "pnlLevel": "pnl", "accountLevel": "totals",
            "instrumentLevel": "details", "mirrorLevel": "details",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if parsed.query or request.body != fixed_body:
            raise CaptureBoundaryError("capture_route_not_allowed")
    elif request.body is not None or (parsed.path in {GET_DEMO_DETAILED_PORTFOLIO.path, GET_DEMO_PNL_PORTFOLIO.path} and parsed.query):
        raise CaptureBoundaryError("capture_route_not_allowed")
    return parsed


class RecordingCaptureTransport:
    """One capture, at most 16 requests within a 15-second initiation deadline.

    Caller owns a newly created private directory. Request receipt precedes
    send; raw body and receipt precede decode; decoded body and receipt precede
    parsing. Headers are never serialized. Any failure is terminal for this
    recorder; its exclusive on-disk claim is never removed or resumed.
    """

    def __init__(self, directory: Path, *, transport=None, clock=time.monotonic):
        self.directory = directory
        self.transport = transport if transport is not None else CaptureHttpTransport()
        self.clock = clock
        self.deadline = clock() + 15
        self.request_count = 0
        self.stopped = False

    def send(self, request):
        if self.stopped:
            raise CaptureBoundaryError("capture_already_stopped")
        try:
            return self._send(request)
        except BaseException:
            self.stopped = True
            raise

    def _send(self, request):
        parsed = _check_route(request)
        remaining = self.deadline - self.clock()
        if (self.request_count >= 16 or remaining <= 0
                or not math.isfinite(request.timeout_seconds) or request.timeout_seconds <= 0):
            raise CaptureBoundaryError("capture_budget_exhausted")
        sequence = self.request_count + 1
        stem = f"{sequence:03d}"
        receipt = {"sequence": sequence, "method": request.method,
                   "path": parsed.path, "query": parsed.query,
                   "requested_at": datetime.now(timezone.utc).isoformat(),
                   "request_body_sha256": hashlib.sha256(request.body or b"").hexdigest()}
        private_json(self.directory / f"{stem}-request.json", receipt)
        # Disk synchronization consumes time too. A durable receipt is not
        # permission to initiate a request after the deadline.
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise CaptureBoundaryError("capture_budget_exhausted")
        self.request_count = sequence
        try:
            response = self.transport.send(replace(
                request, timeout_seconds=min(request.timeout_seconds, remaining)))
        except Exception:
            private_json(self.directory / f"{stem}-failure.json", {
                "sequence": sequence, "outcome": "transport_exception",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            })
            raise CaptureBoundaryError("capture_transport_failed") from None
        # Existing synthetic HttpResponse transports remain compatible: their
        # body is already identity encoded. The shared client interface and
        # general urllib/write transports are deliberately unchanged.
        if isinstance(response, HttpResponse):
            response = RawCaptureResponse(response.status_code, response.body)
        if not isinstance(response, RawCaptureResponse):
            raise CaptureBoundaryError("capture_response_type_invalid")
        private_write(self.directory / f"{stem}-response.bin", response.body)
        raw_hash = hashlib.sha256(response.body).hexdigest()
        private_json(self.directory / f"{stem}-response.json", {
            "sequence": sequence, "received_at": response.received_at,
            "status_code": response.status_code, "read_outcome": response.read_outcome,
            "body_bytes": len(response.body), "body_sha256": raw_hash,
        })
        if response.read_outcome != "complete":
            raise CaptureBoundaryError("capture_body_read_failed")
        try:
            decoded = _decode_body(response)
        except Exception:
            private_json(self.directory / f"{stem}-failure.json", {
                "sequence": sequence, "outcome": "decode_failed",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            })
            raise CaptureBoundaryError("capture_decode_failed") from None
        private_write(self.directory / f"{stem}-decoded.bin", decoded)
        private_json(self.directory / f"{stem}-decoded.json", {
            "sequence": sequence, "decoded_at": datetime.now(timezone.utc).isoformat(),
            "body_bytes": len(decoded), "body_sha256": hashlib.sha256(decoded).hexdigest(),
            "raw_body_sha256": raw_hash,
        })
        if response.status_code != 200:
            self.stopped = True
        return HttpResponse(response.status_code, {}, decoded)
