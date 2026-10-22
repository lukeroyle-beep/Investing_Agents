"""Bounded private response receipts for the repository's read-only collector."""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from brokers.etoro.client import HttpResponse
from brokers.etoro.endpoints import (
    GET_DEMO_DETAILED_PORTFOLIO, GET_SECURITIES_V2, POST_DEMO_AGGREGATE_PORTFOLIO,
)


class CaptureBoundaryError(RuntimeError):
    pass


def sync_directory(path: Path) -> None:
    """Persist new directory entries; unsupported filesystems fail closed."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_write(path: Path, body: bytes) -> None:
    """Create exclusively and restrict permissions before any bytes are written."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


def private_json(path: Path, value) -> None:
    private_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CaptureHttpTransport:
    """Do not forward credential headers through any redirect, even same-host."""

    def send(self, request):
        outbound = urllib.request.Request(request.url, data=request.body,
                                          headers=dict(request.headers), method=request.method)
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(outbound, timeout=request.timeout_seconds) as response:
                return HttpResponse(int(response.status), {}, response.read())
        except urllib.error.HTTPError as exc:
            return HttpResponse(int(exc.code), {}, exc.read())


class RecordingCaptureTransport:
    """One capture, at most 16 requests within a 15-second initiation deadline.

    Caller owns a newly created private directory. A request receipt is durable
    before send; a response body and receipt are durable before parsing. Headers
    are never serialized. An interrupted request is evidence of an attempted
    read, never permission for an automatic retry.
    """

    def __init__(self, directory: Path, *, transport=None, clock=time.monotonic):
        self.directory = directory
        self.transport = transport if transport is not None else CaptureHttpTransport()
        self.clock = clock
        self.deadline = clock() + 15
        self.request_count = 0

    def send(self, request):
        parsed = urllib.parse.urlsplit(request.url)
        allowed = {(endpoint.method, endpoint.path) for endpoint in (
            GET_DEMO_DETAILED_PORTFOLIO, GET_SECURITIES_V2, POST_DEMO_AGGREGATE_PORTFOLIO,
        )}
        remaining = self.deadline - self.clock()
        if (parsed.scheme != "https" or parsed.netloc != "public-api.etoro.com"
                or parsed.fragment or (request.method, parsed.path) not in allowed):
            raise CaptureBoundaryError("capture_route_not_allowed")
        if self.request_count >= 16 or remaining <= 0:
            raise CaptureBoundaryError("capture_budget_exhausted")
        sequence = self.request_count + 1
        stem = f"{sequence:03d}"
        receipt = {"sequence": sequence, "method": request.method,
                   "path": parsed.path, "query": parsed.query,
                   "requested_at": datetime.now(timezone.utc).isoformat(),
                   "request_body_sha256": hashlib.sha256(request.body or b"").hexdigest()}
        private_json(self.directory / f"{stem}-request.json", receipt)
        self.request_count = sequence
        try:
            response = self.transport.send(replace(
                request, timeout_seconds=min(request.timeout_seconds, remaining)))
        except Exception:
            private_json(self.directory / f"{stem}-failure.json", {
                "sequence": self.request_count, "outcome": "transport_exception",
                "observed_at": datetime.now(timezone.utc).isoformat(),
            })
            raise CaptureBoundaryError("capture_transport_failed") from None
        received = datetime.now(timezone.utc).isoformat()
        private_write(self.directory / f"{stem}-response.bin", response.body)
        private_json(self.directory / f"{stem}-response.json", {
            "sequence": self.request_count, "received_at": received,
            "status_code": response.status_code, "body_bytes": len(response.body),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
        })
        return response
