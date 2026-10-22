"""Offline launcher rehearsals; none establish eToro account acceptance."""
from __future__ import annotations

import hashlib
import io
import json
import stat
import urllib.error
import urllib.request
from collections import deque
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from brokers.etoro.capture_transport import (
    CaptureBoundaryError, CaptureHttpTransport, RecordingCaptureTransport,
    _NoRedirect,
)
from brokers.etoro.client import HttpRequest, HttpResponse
from brokers.etoro.endpoints import GET_DEMO_DETAILED_PORTFOLIO
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS
from tests.contracts.etoro.test_etoro_account_evidence import (
    _aggregate, _detail, _page, _security,
)
from tools import demo_operator_capture as launcher


class FakeTransport:
    def __init__(self, responses=()):
        self.responses = deque(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _response(value, *, status=200):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HttpResponse(status, {"Authorization": "synthetic-response-secret"}, body)


def _bracket():
    detail, aggregate = _detail(), _aggregate()
    aggregate["timestamp"] = datetime.now(timezone.utc).isoformat()
    return [_response(item) for item in
            (detail, aggregate, _page(_security()), deepcopy(detail))]


def _write_private(path, value):
    path.write_text(yaml.safe_dump(value) if path.suffix == ".yaml" else json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = launcher.PROJECT_ROOT
    root = tmp_path / "repository"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    for name in ("risk.yaml", "execution.yaml", "scheduler.yaml"):
        (config_dir / name).write_bytes((source / "config" / name).read_bytes())
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    config = runtime / "etoro-demo-read.yaml"
    value = yaml.safe_load((source / "config/brokers/etoro_demo.yaml").read_text())
    value.update(enabled=True, max_read_retries=0)
    _write_private(config, value)
    identity = runtime / "expected-demo-identity.json"
    _write_private(identity, {
        "expected_demo_customer_id": "9001",
        "verification_source": "Independently confirmed synthetic Demo identity",
        "verified_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    })
    evidence = runtime / "capture-evidence"
    evidence.mkdir(mode=0o700)
    monkeypatch.setattr(launcher, "PROJECT_ROOT", root)
    monkeypatch.setenv("ETORO_DEMO_API_KEY", "synthetic-api-secret")
    monkeypatch.setenv("ETORO_DEMO_USER_KEY", "synthetic-user-secret")
    return SimpleNamespace(root=root, config=config, identity=identity, evidence=evidence)


def _run(setup, transport, *, attempt="synthetic-one"):
    return launcher.run_capture(config_path=setup.config, identity_path=setup.identity,
                                evidence_parent=setup.evidence, attempt=attempt,
                                transport=transport)


def _assert_no_attempt(setup, transport, report):
    assert report["status"] == "BLOCKED"
    assert report["broker_requests"] == 0
    assert report["broker_orders_submitted"] == 0
    assert transport.requests == []
    assert list(setup.evidence.iterdir()) == []
    assert "synthetic-api-secret" not in json.dumps(report)
    assert "synthetic-user-secret" not in json.dumps(report)
    assert "9001" not in json.dumps(report)


@pytest.mark.parametrize("absent", ["config", "identity", "ETORO_DEMO_API_KEY", "ETORO_DEMO_USER_KEY"])
def test_missing_private_setup_or_exact_workflow_key_makes_no_attempt(setup, monkeypatch, absent):
    if absent in {"config", "identity"}:
        getattr(setup, absent).unlink()
    else:
        monkeypatch.delenv(absent)
        # Keys from another connection may not be selected as a fallback.
        monkeypatch.setenv("ETORO_API_KEY", "other-connection-secret")
        monkeypatch.setenv("ETORO_USER_KEY", "other-connection-secret")
    transport = FakeTransport()
    _assert_no_attempt(setup, transport, _run(setup, transport))


@pytest.mark.parametrize("field,value", [
    ("enabled", False), ("environment", "real"), ("mode", "read_write"),
    ("base_url", "https://public-api.etoro.com.evil.invalid"),
    ("base_url", "https://public-api.etoro.com/extra"),
    ("max_read_retries", 1), ("request_timeout_seconds", 16),
    ("request_timeout_seconds", float("nan")), ("request_timeout_seconds", float("inf")),
    ("rest_authoritative", False), ("websocket_enabled", True),
    ("real_routes_enabled", True), ("write_methods_enabled", True),
    ("credential_environment_variables", {"api_key": "ETORO_DEMO_OTHER", "user_key": "ETORO_DEMO_USER_KEY"}),
    ("credential_environment_variables", {"api_key": "ETORO_DEMO_API_KEY", "user_key": "ETORO_DEMO_OTHER"}),
])
def test_unsafe_private_configuration_fails_before_files_or_requests(setup, field, value):
    config = yaml.safe_load(setup.config.read_text())
    config[field] = value
    _write_private(setup.config, config)
    transport = FakeTransport()
    _assert_no_attempt(setup, transport, _run(setup, transport))


@pytest.mark.parametrize("filename,field,value", [
    ("execution.yaml", "intent_import_enabled", True),
    ("execution.yaml", "broker_writes_enabled", True),
    ("execution.yaml", "adapter", "etoro"),
    ("execution.yaml", "environment", "real"),
    ("scheduler.yaml", "enabled", True),
    ("scheduler.yaml", "broker_submission_enabled", True),
    ("scheduler.yaml", "environment", "real"),
])
def test_enabled_execution_or_scheduler_is_refused(setup, filename, field, value):
    path = setup.root / "config" / filename
    config = yaml.safe_load(path.read_text())
    config[field] = value
    path.write_text(yaml.safe_dump(config))
    transport = FakeTransport()
    report = _run(setup, transport)
    _assert_no_attempt(setup, transport, report)
    assert report["blockers"][0] == "execution_and_scheduling_must_remain_disabled"


@pytest.mark.parametrize("target", ["config", "identity", "evidence"])
def test_group_or_world_readable_setup_is_refused(setup, target):
    getattr(setup, target).chmod(0o755 if target == "evidence" else 0o644)
    transport = FakeTransport()
    report = _run(setup, transport)
    _assert_no_attempt(setup, transport, report)
    assert report["blockers"][0] == "private_setup_owner_only_required"


@pytest.mark.parametrize("location", ["outside_runtime", "symlink"])
def test_private_setup_must_stay_under_runtime_without_symlinks(setup, location):
    replacement = setup.root / "elsewhere.yaml"
    replacement.write_bytes(setup.config.read_bytes())
    replacement.chmod(0o600)
    if location == "symlink":
        setup.config.unlink()
        setup.config.symlink_to(replacement)
    else:
        setup.config = replacement
    transport = FakeTransport()
    _assert_no_attempt(setup, transport, _run(setup, transport))


@pytest.mark.parametrize("field,value", [
    ("expected_demo_customer_id", ""), ("expected_demo_customer_id", True),
    ("verification_source", " "), ("verified_at", "2026-10-21T12:00:00"),
    ("verified_at", "2999-01-01T00:00:00+00:00"),
])
def test_independent_identity_requires_value_provenance_and_aware_past_time(setup, field, value):
    identity = json.loads(setup.identity.read_text())
    identity[field] = value
    _write_private(setup.identity, identity)
    transport = FakeTransport()
    _assert_no_attempt(setup, transport, _run(setup, transport))


def test_independently_supplied_identity_mismatch_blocks_without_exposing_identity(setup):
    identity = json.loads(setup.identity.read_text())
    identity["expected_demo_customer_id"] = "9002"
    _write_private(setup.identity, identity)
    transport = FakeTransport(_bracket())
    report = _run(setup, transport)
    assert len(transport.requests) == report["broker_requests"] == 1
    assert report["capture_failure_code"] == "demo_account_identity_mismatch"
    assert report["preparation_available"] is False
    assert "9001" not in json.dumps(report) and "9002" not in json.dumps(report)
    assert (setup.evidence / "synthetic-one/001-response.bin").is_file()


def test_full_fake_bracket_retains_raw_hashes_receipts_and_never_creates_economic_state(setup, monkeypatch):
    import execution.store
    import tools.demo_account_preflight

    def prohibited(*args, **kwargs):
        pytest.fail("account capture must not create baseline or execution state")

    monkeypatch.setattr(execution.store, "ExecutionStore", prohibited)
    monkeypatch.setattr(tools.demo_account_preflight, "RiskBaselineStore", prohibited)
    responses = _bracket()
    transport = FakeTransport(responses)
    before = {path for path in setup.root.rglob("*") if path.is_file()}
    report = _run(setup, transport)
    destination = setup.evidence / "synthetic-one"
    assert report["restricted_position_and_empty_pending_evidence_captured"] is True
    assert report["broker_requests"] == len(transport.requests) == 4
    assert report["status"] == "BLOCKED"
    assert report["checked_pending_collections"] == list(DEMO_PENDING_COLLECTIONS)
    assert report["pending_orders_complete"] is True
    assert report["executions_complete"] is False
    assert report["account_evidence_complete"] is False
    for flag in ("submission_available", "preparation_available", "approval_available",
                 "baseline_created", "qualification_credit"):
        assert report[flag] is False
    assert report["broker_orders_submitted"] == 0
    assert "trusted_full_account_daily_pnl_missing" in report["blockers"]
    assert "issuer_and_sector_mapping_not_qualified" in report["blockers"]
    assert "existing_single_instrument_issuer_floor_above_limit" in report["blockers"]
    assert json.loads((destination / "readiness.json").read_text()) == report
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700

    expected = {destination / "setup-receipt.json", destination / "readiness.json"}
    for sequence, (request, response) in enumerate(zip(transport.requests, responses), 1):
        stem = f"{sequence:03d}"
        request_path = destination / f"{stem}-request.json"
        response_path = destination / f"{stem}-response.json"
        body_path = destination / f"{stem}-response.bin"
        expected.update((request_path, response_path, body_path))
        receipt = json.loads(request_path.read_text())
        result = json.loads(response_path.read_text())
        requested = datetime.fromisoformat(receipt["requested_at"])
        received = datetime.fromisoformat(result["received_at"])
        assert requested.tzinfo is not None and received.tzinfo is not None
        assert requested <= received
        assert body_path.read_bytes() == response.body
        assert result["body_sha256"] == hashlib.sha256(response.body).hexdigest()
        assert result["body_bytes"] == len(response.body)
        assert receipt["request_body_sha256"] == hashlib.sha256(request.body or b"").hexdigest()
        assert request.url.startswith("https://public-api.etoro.com/")
        assert 0 < request.timeout_seconds <= 15
    assert {path for path in setup.root.rglob("*") if path.is_file()} - before == expected
    for path in expected:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        contents = path.read_bytes()
        assert b"synthetic-api-secret" not in contents
        assert b"synthetic-user-secret" not in contents
        assert b"synthetic-response-secret" not in contents
        assert b"Authorization" not in contents


def test_parser_failure_keeps_raw_response_before_parsing_and_sanitizes_report(setup):
    raw = b'{broken-json "customer": "private-response-sentinel"'
    transport = FakeTransport([_response(raw)])
    report = _run(setup, transport)
    assert report["capture_error_type"] == "EtoroSchemaError"
    assert report["broker_requests"] == 1
    destination = setup.evidence / "synthetic-one"
    assert (destination / "001-response.bin").read_bytes() == raw
    assert json.loads((destination / "001-response.json").read_text())["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert "private-response-sentinel" not in json.dumps(report)


@pytest.mark.parametrize("failure", ["429", "timeout"])
def test_rate_limit_and_timeout_never_retry(setup, failure):
    response = (_response(b"rate-limited-private-body", status=429) if failure == "429"
                else TimeoutError("secret exception details"))
    transport = FakeTransport([response, *_bracket()])
    report = _run(setup, transport)
    assert report["broker_requests"] == len(transport.requests) == 1
    assert report["blockers"][0] == "broker_account_capture_failed"
    destination = setup.evidence / "synthetic-one"
    assert not list(destination.glob("002-*"))
    if failure == "timeout":
        receipt = json.loads((destination / "001-failure.json").read_text())
        assert receipt["outcome"] == "transport_exception"
        assert not (destination / "001-response.bin").exists()
    assert "secret exception details" not in json.dumps(report)
    assert "rate-limited-private-body" not in json.dumps(report)


def test_existing_attempt_claim_cannot_be_retried_or_overwritten(setup):
    destination = setup.evidence / "synthetic-one"
    destination.mkdir(mode=0o700)
    marker = destination / "setup-receipt.json"
    marker.write_bytes(b"original unfinished attempt")
    transport = FakeTransport(_bracket())
    report = _run(setup, transport)
    assert report["blockers"][0] == "attempt_already_exists_no_retry"
    assert report["broker_requests"] == 0 and transport.requests == []
    assert list(destination.iterdir()) == [marker]
    assert marker.read_bytes() == b"original unfinished attempt"


@pytest.mark.parametrize("attempt", ["", "../escape", "with space", "A", "x" * 65])
def test_attempt_names_cannot_escape_or_alias_evidence_directory(setup, attempt):
    transport = FakeTransport()
    report = _run(setup, transport, attempt=attempt)
    _assert_no_attempt(setup, transport, report)
    assert report["blockers"][0] == "invalid_attempt_name"


def _request(*, method="GET", url=None):
    return HttpRequest(method=method,
                       url=url or "https://public-api.etoro.com" + GET_DEMO_DETAILED_PORTFOLIO.path,
                       headers={"x-api-key": "synthetic-api-secret"}, timeout_seconds=15)


@pytest.mark.parametrize("url", ["https://public-api.etoro.com/redirected", "https://evil.invalid/"])
def test_redirect_handler_never_forwards_credentials_even_to_same_host(url):
    request = urllib.request.Request("https://public-api.etoro.com/initial",
                                     headers={"x-api-key": "synthetic-api-secret"})
    assert _NoRedirect().redirect_request(request, None, 302, "Found", {}, url) is None


def test_http_transport_installs_redirect_refusal_and_returns_first_http_error(monkeypatch):
    handlers, sent = [], []

    class Opener:
        def open(self, request, *, timeout):
            sent.append(request)
            raise urllib.error.HTTPError(request.full_url, 302, "Found",
                                         {"Location": "https://evil.invalid/"},
                                         io.BytesIO(b"original redirect body"))

    def build_opener(handler):
        handlers.append(handler)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    result = CaptureHttpTransport().send(_request())
    assert len(sent) == 1 and len(handlers) == 1
    assert isinstance(handlers[0], _NoRedirect)
    assert result.status_code == 302 and result.body == b"original redirect body"
    assert result.headers == {}


@pytest.mark.parametrize("method,url", [
    ("POST", "https://public-api.etoro.com/api/v3/trading/execution/demo/orders"),
    ("GET", "https://public-api.etoro.com/api/v1/trading/info/real/portfolio"),
    ("GET", "https://public-api.etoro.com/api/v2/trading/info/demo/orders:lookup"),
    ("GET", "https://other.invalid/api/v1/trading/info/demo/portfolio"),
    ("GET", "http://public-api.etoro.com/api/v1/trading/info/demo/portfolio"),
    ("GET", "https://public-api.etoro.com/api/v1/trading/info/demo/portfolio#fragment"),
    ("DELETE", "https://public-api.etoro.com/api/v1/trading/info/demo/portfolio"),
])
def test_recorder_rejects_every_nonallowlisted_route_without_files(tmp_path, method, url):
    transport = FakeTransport()
    recorder = RecordingCaptureTransport(tmp_path, transport=transport)
    with pytest.raises(CaptureBoundaryError, match="capture_route_not_allowed"):
        recorder.send(_request(method=method, url=url))
    assert recorder.request_count == 0 and transport.requests == []
    assert list(tmp_path.iterdir()) == []


def test_recorder_stops_before_request_17(tmp_path):
    transport = FakeTransport([_response({}) for _ in range(17)])
    recorder = RecordingCaptureTransport(tmp_path, transport=transport, clock=lambda: 10)
    for _ in range(16):
        recorder.send(_request())
    with pytest.raises(CaptureBoundaryError, match="capture_budget_exhausted"):
        recorder.send(_request())
    assert recorder.request_count == len(transport.requests) == 16
    assert not list(tmp_path.glob("017-*"))


def test_expired_capture_deadline_stops_before_send(tmp_path):
    times = iter((10, 25))
    transport = FakeTransport()
    recorder = RecordingCaptureTransport(tmp_path, transport=transport, clock=lambda: next(times))
    with pytest.raises(CaptureBoundaryError, match="capture_budget_exhausted"):
        recorder.send(_request())
    assert recorder.request_count == 0 and transport.requests == []
    assert list(tmp_path.iterdir()) == []


def test_request_receipt_write_failure_prevents_send_and_does_not_count_a_request(tmp_path, monkeypatch):
    import brokers.etoro.capture_transport as capture

    def fail_write(*args, **kwargs):
        raise OSError("simulated unavailable storage")

    transport = FakeTransport([_response({})])
    recorder = RecordingCaptureTransport(tmp_path, transport=transport)
    monkeypatch.setattr(capture, "private_json", fail_write)
    with pytest.raises(OSError):
        recorder.send(_request())
    assert recorder.request_count == 0 and transport.requests == []


def test_attempt_directory_sync_failure_prevents_network_and_preserves_claim(setup, monkeypatch):
    def fail_sync(path):
        assert path == setup.evidence
        raise OSError("simulated directory sync failure")

    monkeypatch.setattr(launcher, "sync_directory", fail_sync)
    transport = FakeTransport(_bracket())
    report = _run(setup, transport)
    assert report["blockers"][0] == "private_evidence_storage_unavailable"
    assert report["broker_requests"] == 0 and transport.requests == []
    assert (setup.evidence / "synthetic-one").is_dir()


def test_request_receipt_directory_sync_failure_blocks_send_but_preserves_file(tmp_path, monkeypatch):
    import brokers.etoro.capture_transport as capture

    def fail_sync(path):
        assert path == tmp_path
        raise OSError("simulated directory sync failure")

    monkeypatch.setattr(capture, "sync_directory", fail_sync)
    transport = FakeTransport([_response({})])
    recorder = RecordingCaptureTransport(tmp_path, transport=transport)
    with pytest.raises(OSError):
        recorder.send(_request())
    assert recorder.request_count == 0 and transport.requests == []
    assert (tmp_path / "001-request.json").exists()


def test_private_write_syncs_file_before_containing_directory(tmp_path, monkeypatch):
    import brokers.etoro.capture_transport as capture

    syncs = []
    original_fsync = capture.os.fsync

    def record_sync(descriptor):
        syncs.append(stat.S_IFMT(capture.os.fstat(descriptor).st_mode))
        original_fsync(descriptor)

    monkeypatch.setattr(capture.os, "fsync", record_sync)
    capture.private_write(tmp_path / "receipt.json", b"{}")
    assert syncs == [stat.S_IFREG, stat.S_IFDIR]
