"""Synthetic direct-read transport and original-receipt boundaries."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from io import BytesIO
import json
from pathlib import Path
from uuid import uuid4

import pytest

from brokers.etoro import pilot_reads as reads
from brokers.etoro.autonomous_api import _json_body
from brokers.etoro.capture_transport import RawCaptureResponse
from brokers.etoro.client import EtoroCredentials, HttpRequest
from brokers.etoro.connector import response_object
from tests.test_pilot_account_source import NOW


KEYS = EtoroCredentials("synthetic-private-app-key", "synthetic-private-user-key")


class SyntheticReadTransport:
    def __init__(self, response, clock, end):
        self.response, self.clock, self.end, self.calls = response, clock, end, []

    def send(self, request):
        self.calls.append(request)
        self.clock[0] = self.end
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def direct_receipt(tmp_path, path, payload, *, start=NOW, end=NOW, **kwargs):
    """Return a real client receipt from synthetic bytes; never emulate MCP."""
    clock = [start]
    transport = SyntheticReadTransport(RawCaptureResponse(200, _json_body(payload)), clock, end)
    client = reads.DirectPilotReads(credentials=KEYS, receipt_dir=tmp_path / str(uuid4()),
        clock=lambda:clock[0], transport=transport)
    return client.read(path, **kwargs)


def client_for(tmp_path, response=None, *, elapsed=0, timeout=10):
    clock = [NOW]
    transport = SyntheticReadTransport(response or RawCaptureResponse(200, b'{"value":0.123456789012345678901234567890}'),
        clock, NOW+timedelta(seconds=elapsed))
    client = reads.DirectPilotReads(credentials=KEYS, receipt_dir=tmp_path / "receipts",
        clock=lambda:clock[0], transport=transport, timeout_seconds=timeout)
    return client, transport, clock


def test_original_direct_receipt_preserves_precision_clocks_and_private_bytes(tmp_path):
    client, transport, _ = client_for(tmp_path, elapsed=2)
    receipt = client.read("/api/v2/market-data/rates", query={"instrumentIds":"1142"})
    envelope, body = reads.read_receipt_envelope(receipt)
    assert receipt["tool"] == "etoro_direct_read"
    assert receipt["source_transport"] == envelope["source_transport"] == "official_demo_api"
    assert "content" not in envelope and "isError" not in envelope
    assert receipt["request_started_at"] == NOW.isoformat()
    assert receipt["response_received_at"] == (NOW+timedelta(seconds=2)).isoformat()
    assert body["value"] == D("0.123456789012345678901234567890")
    assert response_object(envelope) == body
    request, = transport.calls
    assert request.method == "GET" and request.body is None
    assert request.url == reads.BASE_URL + "/api/v2/market-data/rates?instrumentIds=1142"
    assert request.headers["x-request-id"] == receipt["request_id"]
    assert Path(receipt["raw_response"]["body_file"]).read_bytes() == transport.response.body
    retained = list(client.receipt_dir.iterdir())
    assert len(retained) == 4 and client.receipt_dir.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in retained)
    assert all(KEYS.api_key.encode() not in path.read_bytes() and KEYS.user_key.encode() not in path.read_bytes()
               for path in retained)


@pytest.mark.parametrize("path", sorted(reads.POST_PATHS))
def test_only_read_semantic_post_routes_preserve_original_body(tmp_path, path):
    client, transport, _ = client_for(tmp_path)
    body = ' {"instrumentId":1142, "amount":10.000000000000000001} '
    receipt = client.read(path, body=body)
    assert receipt["method"] == "POST" and receipt["arguments"] == {"path":path, "body":body}
    assert transport.calls[0].body == body.encode()
    reads.read_receipt_envelope(receipt)


def test_exact_mapping_body_is_encoded_without_float_conversion(tmp_path):
    client, transport, _ = client_for(tmp_path)
    receipt = client.read("/api/v2/trading/info/demo/costs", body={"amount":D("10.000000000000000001")})
    assert transport.calls[0].body == b'{"amount":10.000000000000000001}'
    assert receipt["arguments"]["body"] == transport.calls[0].body.decode()


@pytest.mark.parametrize("change", ["path", "body", "query"])
def test_receipt_arguments_remain_bound_to_actual_sent_request(tmp_path, change):
    if change == "query":
        receipt = direct_receipt(tmp_path, "/api/v2/market-data/rates", {"value":1}, query={"instrumentIds":"1142"})
        receipt["arguments"]["query"]["instrumentIds"] = "999"
    else:
        receipt = direct_receipt(tmp_path, "/api/v2/trading/info/demo/costs", {"value":1}, body='{"amount":10}')
        if change == "path":
            receipt["arguments"]["path"] = "/api/v2/trading/info/demo/eligibility"
        else:
            receipt["arguments"]["body"] = '{"amount":100}'
    with pytest.raises(reads.PilotReadError, match="provenance"):
        reads.read_receipt_envelope(receipt)


@pytest.mark.parametrize("timeout", [None, True, "10", 0, -1, 10.001, float("nan"), float("inf")])
def test_timeout_configuration_cannot_exceed_bounded_policy(tmp_path, timeout):
    with pytest.raises(reads.PilotReadError, match="timeout_invalid"):
        client_for(tmp_path, timeout=timeout)


@pytest.mark.parametrize("path,kwargs", [
    ("/api/v2/trading/execution/demo/orders", {"body":"{}"}),
    ("/api/v2/trading/execution/real/orders", {"body":"{}"}),
    ("/api/v1/trading/info/real/portfolio", {}),
    ("https://other.invalid/api/v1/me", {}),
    ("/api/v1/me?redirect=https://other.invalid", {}),
    ("/api/v1/me", {"query":{"account":"real"}}),
    ("/api/v1/trading/info/demo/portfolio", {"body":"{}"}),
    ("/api/v2/trading/info/demo/costs", {}),
    ("/api/v2/trading/info/demo/costs", {"query":{"x":"y"}, "body":"{}"}),
    ("/api/v2/trading/info/demo/eligibility", {"body":"[]"}),
    ("/api/v2/trading/info/demo/eligibility", {"body":'{"amount":NaN}'}),
    ("/api/v2/trading/info/demo/eligibility", {"body":'{"amount":1,"amount":2}'}),
    ("/api/v2/trading/info/demo/costs", {"body":{"amount":10.1}}),
    ("/api/v1/me", {"method":"POST"}),
    ("/api/v1/me", {"headers":{"Authorization":"other"}}),
    ("/api/v2/market-data/rates", {"query":{"instrumentIds":[1142]}}),
])
def test_route_method_body_and_header_substitutions_fail_before_send(tmp_path, path, kwargs):
    client, transport, _ = client_for(tmp_path)
    with pytest.raises(reads.PilotReadError):
        client.read(path, **kwargs)
    assert transport.calls == [] and list(client.receipt_dir.iterdir()) == []


@pytest.mark.parametrize("response", [
    RawCaptureResponse(302, b'{"redirect":true}'), RawCaptureResponse(429, b'{"error":true}'),
    RawCaptureResponse(500, b'{"error":true}'), RawCaptureResponse(200, b'{"partial":', read_outcome="partial"),
    RawCaptureResponse(200, b'compressed-original', content_encoding="gzip"),
    RawCaptureResponse(200, b'{"x":1,"x":2}'), RawCaptureResponse(200, b'{"x":NaN}'),
    RawCaptureResponse(200, b'not-json'), RawCaptureResponse(200, b'[]'), RawCaptureResponse(200, b'\xff'),
])
def test_error_and_unsupported_responses_are_retained_before_rejection_without_retry(tmp_path, response):
    client, transport, _ = client_for(tmp_path, response)
    with pytest.raises(reads.PilotReadError):
        client.read("/api/v1/me")
    assert len(transport.calls) == 1
    files = list(client.receipt_dir.glob("*.response.bin"))
    assert len(files) == 1 and files[0].read_bytes() == response.body
    assert len(list(client.receipt_dir.glob("*.response.json"))) == 1
    assert not list(client.receipt_dir.glob("*.receipt.json"))


def test_transport_exception_is_one_attempt_and_never_echoes_sensitive_message(tmp_path):
    client, transport, _ = client_for(tmp_path, TimeoutError("synthetic-private-user-key"))
    with pytest.raises(reads.PilotReadError, match="^direct_read_transport_failed$"):
        client.read("/api/v1/me")
    assert len(transport.calls) == 1
    failure, = client.receipt_dir.glob("*.failure.json")
    assert KEYS.user_key not in failure.read_text()


@pytest.mark.parametrize("elapsed", [-0.000001, 10.000001])
def test_original_request_duration_and_future_clock_never_admit(tmp_path, elapsed):
    client, transport, _ = client_for(tmp_path, elapsed=elapsed)
    with pytest.raises(reads.PilotReadError, match="bounded_complete"):
        client.read("/api/v1/me")
    assert len(transport.calls) == 1 and len(list(client.receipt_dir.glob("*.response.bin"))) == 1


@pytest.mark.parametrize("change", [
    lambda r:r.update(tool="etoro_execute_read"),
    lambda r:r.update(source_transport="mcp"),
    lambda r:r.update(method="POST"),
    lambda r:r["result"].update(body='{"value":999}'),
    lambda r:r["result"].update(isSuccess=False),
    lambda r:r["result"].update(bodyTruncated=True),
    lambda r:r["raw_response"].update(body_sha256="a"*64),
    lambda r:r["raw_response"].update(status_code=500),
    lambda r:r["raw_response"].update(content_encoding="gzip"),
    lambda r:r["raw_response"].update(read_outcome="partial"),
    lambda r:r.update(response_received_at=(NOW+timedelta(seconds=11)).isoformat()),
])
def test_receipt_provenance_rejects_modified_or_mislabeled_evidence(tmp_path, change):
    receipt = direct_receipt(tmp_path, "/api/v1/me", {"value":1})
    change(receipt)
    with pytest.raises(reads.PilotReadError, match="provenance"):
        reads.read_receipt_envelope(receipt)


def test_persistence_cannot_retimestamp_received_response(tmp_path, monkeypatch):
    client, _, clock = client_for(tmp_path, elapsed=2)
    original = reads.private_write
    def slow_write(path, body):
        result = original(path, body)
        clock[0] += timedelta(seconds=3)
        return result
    monkeypatch.setattr(reads, "private_write", slow_write)
    receipt = client.read("/api/v1/me")
    assert receipt["response_received_at"] == (NOW+timedelta(seconds=2)).isoformat()
    assert clock[0] == NOW+timedelta(seconds=5)


def test_transport_disables_environment_redirects_cookies_and_retries(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.invalid")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/untrusted/ca")
    stream = BytesIO(b'{"redirect":true}')
    class Response:
        status_code = 302
        headers = {"Location":"https://untrusted.invalid", "Set-Cookie":"session=value"}
        raw = type("Stream", (), {"read1":lambda self,n,decode_content:stream.read(n)})()
        def __enter__(self): return self
        def __exit__(self, *args): return False
    def send(session, request, **kwargs):
        assert session.trust_env is False and session.auth is None
        assert not session.proxies and not session.cookies
        assert session.get_adapter(request.url).max_retries.total == 0
        assert kwargs == dict(timeout=10, stream=True, allow_redirects=False, verify=True, proxies={})
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        calls.append(request)
        return Response()
    monkeypatch.setattr(reads._NoRedirectSession, "send", send)
    request = HttpRequest("GET", reads.BASE_URL+"/api/v1/me", {"x-request-id":str(uuid4())}, 10)
    response = reads.DirectPilotReadTransport().send(request)
    assert response.status_code == 302 and response.body == b'{"redirect":true}' and len(calls) == 1
    for invalid in (replace(request, url="http://public-api.etoro.com/api/v1/me"),
            replace(request, url=reads.BASE_URL+".untrusted.invalid/api/v1/me"),
            replace(request, url=reads.BASE_URL+"/api/v2/trading/execution/demo/orders", method="POST", body=b"{}"),
            replace(request, headers={"Cookie":"session=value"}), replace(request, timeout_seconds=11)):
        with pytest.raises(reads.PilotReadError):
            reads.DirectPilotReadTransport().send(invalid)
    assert len(calls) == 1


def test_stream_read_is_bounded_before_bytes_can_grow_without_limit(monkeypatch):
    monkeypatch.setattr(reads, "MAX_BODY_BYTES", 16)
    stream = BytesIO(b"x"*100)
    class Response:
        status_code = 200
        headers = {}
        raw = type("Stream", (), {"read1":lambda self,n,decode_content:stream.read(n)})()
        def __enter__(self): return self
        def __exit__(self, *args): return False
    monkeypatch.setattr(reads._NoRedirectSession, "send", lambda *args,**kwargs:Response())
    response = reads.DirectPilotReadTransport().send(HttpRequest("GET", reads.BASE_URL+"/api/v1/me", {}, 10))
    assert response.read_outcome == "bounded_read_exceeded" and len(response.body) == 17
    assert stream.tell() == 17
