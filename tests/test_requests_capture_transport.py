"""Synthetic wire-level requests checks; no broker or account acceptance evidence."""
from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import json
import socket
import stat
import zlib
from datetime import datetime
from types import SimpleNamespace

import pytest
import requests
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse as Urllib3Response

from brokers.etoro import capture_transport as capture
from brokers.etoro.client import HttpRequest, HttpResponse


URL = "https://public-api.etoro.com/api/v1/trading/info/demo/portfolio"
PAYLOAD = b'{"synthetic":"response body"}'


def request():
    return HttpRequest("GET", URL, {
        "x-api-key": "synthetic-api-secret", "x-user-key": "synthetic-user-secret",
        "x-request-id": "00000000-0000-4000-8000-000000000001",
        "Accept": "application/json",
    }, 3.0)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Synthetic transport tests prohibit sockets and DNS")

    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, denied)
    for name in ("create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex",
                 "gethostbyaddr"):
        monkeypatch.setattr(socket, name, denied)


@pytest.fixture
def wire(monkeypatch):
    """Replace only pool I/O: Session.send and HTTPAdapter.send both execute."""
    state = SimpleNamespace(calls=[], sessions=[], session_closes=[], response_closes=[],
                            raw=None, source=None)
    original_session_send = requests.Session.send
    original_session_close = requests.Session.close
    original_response_close = requests.Response.close

    def session_send(session, prepared, **kwargs):
        state.sessions.append({"session": session, "prepared": prepared,
                               "kwargs": dict(kwargs), "trust_env": session.trust_env,
                               "auth": session.auth, "cookies": dict(session.cookies),
                               "proxies": dict(session.proxies)})
        return original_session_send(session, prepared, **kwargs)

    def session_close(session):
        state.session_closes.append(session)
        return original_session_close(session)

    def response_close(response):
        state.response_closes.append(response)
        return original_response_close(response)

    monkeypatch.setattr(requests.Session, "send", session_send)
    monkeypatch.setattr(requests.Session, "close", session_close)
    monkeypatch.setattr(requests.Response, "close", response_close)

    def install(*, status=200, body=PAYLOAD, encoding="identity", location=None,
                read_failure=None, open_failure=False, framed=False, stream_timeout=None,
                chunked_body=None):
        headers = {"Content-Encoding": encoding, "X-Synthetic-Secret": "response-header-secret",
                   "Retry-After": "1", "Set-Cookie": "ambient=synthetic-cookie"}
        if location is not None:
            headers["Location"] = location

        def urlopen(pool, **kwargs):
            state.calls.append({"pool": pool, **kwargs})
            if open_failure == "timeout":
                raise ReadTimeoutError(pool, kwargs["url"], "synthetic-open-secret")
            if open_failure:
                raise OSError("synthetic-open-secret")
            if framed or chunked_body is not None:
                # Exercise http.client and urllib3's real incomplete-body path.
                # This is an in-memory socket-shaped object, never a socket.
                if chunked_body is not None:
                    headers["Transfer-Encoding"] = "chunked"
                    framed_body = chunked_body
                else:
                    headers["Content-Length"] = str(len(body) + 7)
                    framed_body = body
                message = (f"HTTP/1.1 {status} Synthetic\r\n" + "".join(
                    f"{key}: {value}\r\n" for key, value in headers.items()
                ) + "\r\n").encode() + framed_body

                class BodyStream(io.BytesIO):
                    read_count = 0

                    def read1(self, amount=-1):
                        self.read_count += 1
                        if stream_timeout == "failed" or (stream_timeout == "partial"
                                                          and self.read_count > 1):
                            raise socket.timeout("synthetic-read-secret")
                        return super().read1(amount)

                state.source = BodyStream(message)

                class MemorySocket:
                    def makefile(self, *args, **kwargs):
                        return state.source

                original = http.client.HTTPResponse(MemorySocket(), method="GET")
                original.begin()
                state.raw = Urllib3Response(
                    body=original, original_response=original, status=status, headers=headers,
                    preload_content=False, decode_content=False, enforce_content_length=True,
                    pool=pool, request_method="GET", request_url=URL,
                )
            else:
                state.source = io.BytesIO(body)
                state.raw = Urllib3Response(body=state.source, status=status, headers=headers,
                                            preload_content=False, decode_content=False)
            if read_failure:
                reads = []

                def read1(amount, *, decode_content):
                    assert decode_content is False
                    reads.append(amount)
                    if read_failure == "partial" and len(reads) == 1:
                        return body
                    raise OSError("synthetic-read-secret")

                state.raw.read1 = read1
            return state.raw

        monkeypatch.setattr(HTTPSConnectionPool, "urlopen", urlopen)
        return state

    return install


def read_receipt(directory, suffix):
    return json.loads((directory / f"001-{suffix}.json").read_text())


def assert_private_files(directory):
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        body = path.read_bytes()
        for secret in (b"synthetic-api-secret", b"synthetic-user-secret",
                       b"response-header-secret", b"synthetic-cookie",
                       b"synthetic-read-secret", b"synthetic-open-secret"):
            assert secret not in body


def assert_closed(state):
    assert len(state.session_closes) == 1
    assert len(state.response_closes) == 1
    assert state.source.closed


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500, 503])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_real_requests_preserves_wire_bytes_and_records_decoded_view(tmp_path, wire, status, encoding):
    body = {"identity": lambda: PAYLOAD, "gzip": lambda: gzip.compress(PAYLOAD),
            "deflate": lambda: zlib.compress(PAYLOAD)}[encoding]()
    state = wire(status=status, body=body, encoding=encoding)
    recorder = capture.RecordingCaptureTransport(tmp_path)
    result = recorder.send(request())
    assert result == HttpResponse(status, {}, PAYLOAD)
    assert recorder.request_count == len(state.calls) == 1
    assert (tmp_path / "001-response.bin").read_bytes() == body
    assert (tmp_path / "001-decoded.bin").read_bytes() == PAYLOAD
    raw = read_receipt(tmp_path, "response")
    decoded = read_receipt(tmp_path, "decoded")
    assert raw["status_code"] == status
    assert raw["body_bytes"] == len(body)
    assert raw["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert raw["read_outcome"] == "complete"
    assert datetime.fromisoformat(raw["received_at"]).utcoffset() is not None
    assert decoded["body_bytes"] == len(PAYLOAD)
    assert decoded["body_sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert decoded["raw_body_sha256"] == raw["body_sha256"]
    assert_closed(state)
    assert_private_files(tmp_path)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [
    "https://public-api.etoro.com/api/v1/trading/info/demo/portfolio?redirected=1",
    "https://other.invalid/credential-receiver",
])
@pytest.mark.parametrize("encoding", ["identity", "gzip"])
def test_redirect_never_prepares_followup_or_consumes_raw_body(tmp_path, monkeypatch, wire,
                                                            status, location, encoding):
    body = gzip.compress(PAYLOAD) if encoding == "gzip" else PAYLOAD
    state = wire(status=status, body=body, encoding=encoding, location=location)

    def content_forbidden(*args, **kwargs):
        pytest.fail("requests must never consume or decode Response.content")

    monkeypatch.setattr(requests.Response, "iter_content", content_forbidden)
    result = capture.RecordingCaptureTransport(tmp_path).send(request())
    assert result.status_code == status and result.body == PAYLOAD
    assert len(state.calls) == len(state.sessions) == 1
    assert state.sessions[0]["prepared"].url == URL
    assert state.response_closes[0].next is None
    assert (tmp_path / "001-response.bin").read_bytes() == body
    assert_closed(state)
    assert_private_files(tmp_path)


def test_ambient_proxy_credentials_cookie_and_ca_settings_cannot_change_send(monkeypatch, wire):
    state = wire()
    original_init = requests.Session.__init__

    def contaminated_session(session):
        original_init(session)
        session.auth = ("synthetic-ambient-user", "synthetic-ambient-password")
        session.cookies.set("ambient", "synthetic-cookie")
        session.proxies["https"] = "http://synthetic-proxy.invalid:9876"

    monkeypatch.setattr(requests.Session, "__init__", contaminated_session)
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy",
                "all_proxy"):
        monkeypatch.setenv(key, "http://synthetic-proxy.invalid:9876")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/synthetic-ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/nonexistent/synthetic-curl-ca.pem")
    monkeypatch.setenv("NETRC", "/nonexistent/synthetic-netrc")

    def netrc_forbidden(*args, **kwargs):
        pytest.fail("ambient netrc must not be consulted")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", netrc_forbidden)
    result = capture.CaptureHttpTransport().send(request())
    assert result.body == PAYLOAD
    sent = state.sessions[0]
    assert sent["trust_env"] is False and sent["auth"] is None
    assert sent["cookies"] == sent["proxies"] == {}
    assert sent["kwargs"]["allow_redirects"] is False
    assert sent["kwargs"]["stream"] is True and sent["kwargs"]["verify"] is True
    assert not sent["kwargs"]["proxies"]
    headers = sent["prepared"].headers
    assert headers["User-Agent"] == requests.utils.default_user_agent()
    assert headers["Accept-Encoding"] == "gzip, deflate"
    assert headers["x-api-key"] == "synthetic-api-secret"
    assert headers["x-user-key"] == "synthetic-user-secret"
    assert not {name.lower() for name in headers} & {"authorization", "proxy-authorization", "cookie"}
    call = state.calls[0]
    assert call["redirect"] is False and call["preload_content"] is False
    assert call["decode_content"] is False and call["retries"].total == 0
    assert call["timeout"].connect_timeout == call["timeout"].read_timeout == 3.0
    assert call["pool"].cert_reqs == "CERT_REQUIRED"
    assert_closed(state)


@pytest.mark.parametrize("read_failure,expected", [("partial", PAYLOAD), ("failed", b"")])
def test_read_error_retains_exact_available_prefix_and_never_decodes(tmp_path, wire,
                                                                 read_failure, expected):
    state = wire(read_failure=read_failure, encoding="gzip")
    recorder = capture.RecordingCaptureTransport(tmp_path)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_body_read_failed$"):
        recorder.send(request())
    assert recorder.request_count == 1
    assert (tmp_path / "001-response.bin").read_bytes() == expected
    raw = read_receipt(tmp_path, "response")
    assert raw["read_outcome"] == read_failure
    assert raw["body_bytes"] == len(expected)
    assert raw["body_sha256"] == hashlib.sha256(expected).hexdigest()
    assert not list(tmp_path.glob("001-decoded.*"))
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert len(state.calls) == 1
    assert_closed(state)
    assert_private_files(tmp_path)


@pytest.mark.parametrize("encoding,body", [
    ("gzip", b"not-gzip"), ("deflate", b"not-deflate"), ("br", b"unrecognised-encoding"),
    ("gzip, deflate", gzip.compress(PAYLOAD)), ("gzip", gzip.compress(PAYLOAD)[:-4]),
])
def test_invalid_or_unsupported_encoding_keeps_raw_evidence_and_blocks(tmp_path, wire, encoding, body):
    state = wire(encoding=encoding, body=body)
    recorder = capture.RecordingCaptureTransport(tmp_path)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_decode_failed$"):
        recorder.send(request())
    assert (tmp_path / "001-response.bin").read_bytes() == body
    assert read_receipt(tmp_path, "response")["read_outcome"] == "complete"
    assert not list(tmp_path.glob("001-decoded.*"))
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert len(state.calls) == 1
    assert_closed(state)
    assert_private_files(tmp_path)


def test_transport_exception_is_attempted_once_and_sanitised(tmp_path, wire):
    state = wire(open_failure=True)
    recorder = capture.RecordingCaptureTransport(tmp_path)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_transport_failed$"):
        recorder.send(request())
    assert recorder.request_count == len(state.calls) == 1
    assert len(state.session_closes) == 1
    assert (tmp_path / "001-failure.json").exists()
    assert not list(tmp_path.glob("001-response.*"))
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert len(state.calls) == 1
    assert_private_files(tmp_path)


@pytest.mark.parametrize("stream_timeout,expected_outcome,expected_body", [
    (None, "partial", PAYLOAD),
    ("partial", "partial", PAYLOAD),
    ("failed", "failed", b""),
])
def test_real_urllib3_truncation_or_stream_timeout_retains_available_prefix(
    tmp_path, wire, stream_timeout, expected_outcome, expected_body,
):
    state = wire(framed=True, stream_timeout=stream_timeout)
    recorder = capture.RecordingCaptureTransport(tmp_path)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_body_read_failed$"):
        recorder.send(request())
    assert recorder.request_count == 1
    assert (tmp_path / "001-response.bin").read_bytes() == expected_body
    raw = read_receipt(tmp_path, "response")
    assert raw["read_outcome"] == expected_outcome
    assert raw["body_sha256"] == hashlib.sha256(expected_body).hexdigest()
    assert raw["body_bytes"] == len(expected_body)
    assert not list(tmp_path.glob("001-decoded.*"))
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert len(state.calls) == 1
    assert_closed(state)
    assert_private_files(tmp_path)


def test_real_adapter_read_timeout_never_retries_and_sanitises(tmp_path, wire):
    state = wire(open_failure="timeout")
    recorder = capture.RecordingCaptureTransport(tmp_path)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_transport_failed$"):
        recorder.send(request())
    assert recorder.request_count == 1
    assert (tmp_path / "001-failure.json").exists()
    assert not list(tmp_path.glob("001-response.*"))
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert len(state.calls) == len(state.session_closes) == 1
    assert not state.response_closes
    assert_private_files(tmp_path)


@pytest.mark.parametrize("chunked_body,expected_body", [
    (b"3\r\nabc\r\n6\r\nde", b"abcde"),
    (b"3\r\nabc\r", b"abc"),
    (b"3\r\nabc\r\n", b"abc"),
])
def test_real_urllib3_truncated_chunks_preserve_exact_body_prefix(tmp_path, wire,
                                                               chunked_body, expected_body):
    state = wire(chunked_body=chunked_body)
    recorder = capture.RecordingCaptureTransport(tmp_path)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_body_read_failed$"):
        recorder.send(request())
    assert state.raw.chunked is True
    assert (tmp_path / "001-response.bin").read_bytes() == expected_body
    raw = read_receipt(tmp_path, "response")
    assert raw["read_outcome"] == "partial"
    assert raw["body_bytes"] == len(expected_body)
    assert raw["body_sha256"] == hashlib.sha256(expected_body).hexdigest()
    assert not list(tmp_path.glob("001-decoded.*"))
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert recorder.request_count == len(state.calls) == 1
    assert_closed(state)
    assert_private_files(tmp_path)


def test_raw_body_and_receipt_are_durable_before_decoding(tmp_path, monkeypatch, wire):
    body = gzip.compress(PAYLOAD)
    wire(body=body, encoding="gzip")
    events = []
    original_write = capture.private_write
    original_decompress = gzip.decompress

    def write(path, data):
        original_write(path, data)
        events.append(path.name)

    def decompress(data):
        assert events == ["001-request.json", "001-response.bin", "001-response.json"]
        assert (tmp_path / "001-response.bin").read_bytes() == body
        assert read_receipt(tmp_path, "response")["body_sha256"] == hashlib.sha256(body).hexdigest()
        events.append("decode")
        return original_decompress(data)

    monkeypatch.setattr(capture, "private_write", write)
    monkeypatch.setattr(gzip, "decompress", decompress)
    result = capture.RecordingCaptureTransport(tmp_path).send(request())
    assert result.body == PAYLOAD
    assert events == ["001-request.json", "001-response.bin", "001-response.json", "decode",
                      "001-decoded.bin", "001-decoded.json"]


@pytest.mark.parametrize("failed_file", ["001-response.bin", "001-response.json"])
@pytest.mark.parametrize("sync_kind", [stat.S_IFREG, stat.S_IFDIR])
def test_raw_fsync_failure_prevents_decode_and_preserves_available_raw(tmp_path, monkeypatch,
                                                                   wire, failed_file, sync_kind):
    body = gzip.compress(PAYLOAD)
    state = wire(body=body, encoding="gzip")
    original_write = capture.private_write
    original_fsync = capture.os.fsync
    writing = []

    def write(path, data):
        writing.append(path.name)
        return original_write(path, data)

    def fsync(descriptor):
        kind = stat.S_IFMT(capture.os.fstat(descriptor).st_mode)
        if writing[-1] == failed_file and kind == sync_kind:
            raise OSError("synthetic fsync failure")
        return original_fsync(descriptor)

    def decode_forbidden(*args):
        pytest.fail("must not decode until both raw artifacts are durable")

    monkeypatch.setattr(capture, "private_write", write)
    monkeypatch.setattr(capture.os, "fsync", fsync)
    monkeypatch.setattr(gzip, "decompress", decode_forbidden)
    with pytest.raises(OSError, match="synthetic fsync failure"):
        capture.RecordingCaptureTransport(tmp_path).send(request())
    assert (tmp_path / "001-response.bin").read_bytes() == body
    assert not list(tmp_path.glob("001-decoded.*"))
    assert len(state.calls) == 1
    assert_closed(state)


def test_decoded_receipt_failure_never_returns_parser_body(tmp_path, monkeypatch, wire):
    state = wire(body=gzip.compress(PAYLOAD), encoding="gzip")
    original_json = capture.private_json

    def write_json(path, value):
        if path.name == "001-decoded.json":
            raise OSError("synthetic decoded receipt failure")
        return original_json(path, value)

    monkeypatch.setattr(capture, "private_json", write_json)
    with pytest.raises(OSError, match="synthetic decoded receipt failure"):
        capture.RecordingCaptureTransport(tmp_path).send(request())
    assert (tmp_path / "001-response.bin").exists()
    assert (tmp_path / "001-response.json").exists()
    assert (tmp_path / "001-decoded.bin").read_bytes() == PAYLOAD
    assert len(state.calls) == 1
    assert_closed(state)


def test_legacy_synthetic_transport_remains_compatible(tmp_path):
    class LegacyTransport:
        def send(self, outbound):
            return HttpResponse(200, {"X-Synthetic-Secret": "response-header-secret"}, PAYLOAD)

    result = capture.RecordingCaptureTransport(tmp_path, transport=LegacyTransport()).send(request())
    assert result == HttpResponse(200, {}, PAYLOAD)
    assert (tmp_path / "001-response.bin").read_bytes() == PAYLOAD
    assert (tmp_path / "001-decoded.bin").read_bytes() == PAYLOAD
    assert read_receipt(tmp_path, "response")["read_outcome"] == "complete"
    assert_private_files(tmp_path)


def test_slow_request_receipt_cannot_cross_initiation_deadline(tmp_path, monkeypatch, wire):
    state = wire()
    now = [0.0]
    recorder = capture.RecordingCaptureTransport(tmp_path, clock=lambda: now[0])
    original_json = capture.private_json

    def slow_receipt(path, value):
        original_json(path, value)
        if path.name == "001-request.json":
            now[0] = 16.0

    monkeypatch.setattr(capture, "private_json", slow_receipt)
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_budget_exhausted$"):
        recorder.send(request())
    assert state.calls == [] and recorder.request_count == 0
    assert (tmp_path / "001-request.json").exists()
    with pytest.raises(capture.CaptureBoundaryError, match="^capture_already_stopped$"):
        recorder.send(request())
    assert state.calls == []
