"""Offline PnL launcher and report checks; all broker responses are synthetic."""
from __future__ import annotations

import hashlib
import io
import json
import stat
from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from brokers.etoro.capture_transport import CaptureBoundaryError, RecordingCaptureTransport
from brokers.etoro.client import HttpRequest, HttpResponse
from brokers.etoro.endpoints import GET_DEMO_PNL_PORTFOLIO
from risk.pretrade import RiskLimits
from tests.contracts.etoro.test_etoro_account_evidence import NOW, _client, _detail, _page, _security
from tests.contracts.etoro.test_etoro_pnl_schema import _pnl
from tests.contracts.etoro.test_etoro_pnl_capture import _currency_for
from tests.test_demo_operator_capture import FakeTransport, _response, setup as private_setup
from tools import demo_operator_capture as launcher
from tools.demo_pnl_preflight import pnl_preflight_report


def _bodies(*, timestamp=None):
    first, pnl = _detail(), _pnl()
    if timestamp is not None:
        pnl["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"] = timestamp
    return [first, pnl, _page(_security()), _currency_for(first), deepcopy(first)]


def _responses():
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "0Z")
    return [_response(body) for body in _bodies(timestamp=stamp)]


def _run(setup, transport=None, **kwargs):
    return launcher.run_capture(config_path=setup.config, identity_path=setup.identity,
                                evidence_parent=setup.evidence, attempt="synthetic-pnl",
                                transport=transport, **kwargs)


def _assert_no_trading(report):
    assert report["status"] == report["trading_readiness"] == "BLOCKED"
    assert report["broker_orders_submitted"] == 0
    for flag in ("submission_available", "preparation_available", "approval_available",
                 "baseline_created", "qualification_credit", "account_evidence_complete",
                 "real_execution_available", "live_demo_qualified"):
        assert report[flag] is False
    assert report["account_observed_at"] is None
    assert report["account_observed_at_reason"] == "whole_account_observation_time_unavailable"
    assert "whole_account_observation_time_unavailable" in report["blockers"]
    assert "trusted_full_account_daily_pnl_missing" in report["blockers"]
    assert "trusted_full_account_peak_equity_missing" in report["blockers"]


@pytest.mark.parametrize("timestamp,fresh,stale,future", [
    ("2026-10-21T14:30:00.0000000Z", 1, 0, 0),
    ("2026-10-21T14:30:00.0000001Z", 0, 0, 1),
    ("2026-10-21T14:29:44.9999999Z", 0, 1, 0),
])
def test_report_separates_reconciliation_freshness_and_readiness(timestamp, fresh, stale, future):
    client, transport = _client(_bodies(timestamp=timestamp))
    private_evidence = []
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001", now=NOW,
                                  evidence_sink=private_evidence.append)
    _assert_no_trading(report)
    assert len(transport.requests) == 5
    assert report["structural_cash_consistency"] == report["reconciliation"] == "PASS"
    assert report["valuation_freshness"] == ("PASS" if fresh else "FAIL")
    assert report["restricted_position_and_empty_pending_evidence_captured"] is bool(fresh)
    assert report["fresh_valuation_count"] == fresh
    assert report["stale_valuation_count"] == stale
    assert report["future_valuation_count"] == future
    assert len(private_evidence) == 1
    assert private_evidence[0].position_valuations[0].timestamp == timestamp
    assert private_evidence[0].observed_at is None
    if not fresh:
        assert report["blockers"][0] == "position_valuations_stale_or_future"
        assert report["existing_exposure_checks"] is None
    encoded = json.dumps(report)
    for private in ('"9001"', '"7001"', '"1001"', '"10020"', '"9000"', timestamp):
        assert private not in encoded


def test_reports_per_instrument_exposure_floor_after_combining_multiple_positions():
    bodies = _bodies()
    for body in (bodies[0], bodies[1], bodies[4]):
        extra = deepcopy(body["clientPortfolio"]["positions"][0])
        extra["positionID"] = 7002
        body["clientPortfolio"]["positions"].append(extra)
    bodies[1]["clientPortfolio"]["unrealizedPnL"] = 40
    bodies[3] = _currency_for(bodies[0])
    client, _ = _client(bodies)
    limits = RiskLimits.load(launcher.PROJECT_ROOT / "config/risk.yaml")
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001", now=NOW,
                                  risk_limits=limits)
    checks = report["existing_exposure_checks"]
    expected = Decimal("2040") / Decimal("11040")
    assert Decimal(checks["single_instrument_issuer_floor"]["observed_fraction"]) == expected
    assert Decimal(checks["gross_exposure"]["observed_fraction"]) == expected
    _assert_no_trading(report)


@pytest.mark.parametrize("timestamp", [None, "2026-10-21T14:30:00.0000000", "2026-10-21T14:30:00-00:00"])
def test_ambiguous_position_time_cannot_borrow_account_or_receipt_time(timestamp):
    bodies = _bodies()
    portfolio = bodies[1]["clientPortfolio"]
    portfolio["timestamp"] = NOW.isoformat()
    portfolio["positions"][0]["unrealizedPnL"]["timestamp"] = timestamp
    client, transport = _client(bodies)
    sink = []
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001", now=NOW,
                                  evidence_sink=sink.append)
    _assert_no_trading(report)
    assert len(transport.requests) == 2
    assert report["capture_error_type"] == "EtoroSchemaError"
    assert report["reconciliation"] == report["valuation_freshness"] == "NOT_VERIFIED"
    assert report["restricted_position_and_empty_pending_evidence_captured"] is False
    assert sink == []


def test_default_launcher_uses_pnl_and_retains_exact_private_evidence(private_setup, monkeypatch):
    import execution.store
    import tools.demo_account_preflight

    def prohibited(*args, **kwargs):
        pytest.fail("read-only PnL capture must not create execution or baseline state")

    monkeypatch.setattr(execution.store, "ExecutionStore", prohibited)
    monkeypatch.setattr(tools.demo_account_preflight, "RiskBaselineStore", prohibited)
    responses = _responses()
    transport = FakeTransport(responses)
    report = _run(private_setup, transport)
    _assert_no_trading(report)
    assert report["valuation_freshness"] == report["reconciliation"] == "PASS"
    assert report["source_kind"] == "broker_demo_pnl_read_only_capture"
    assert report["broker_requests"] == len(transport.requests) == 5
    assert transport.requests[1].url.endswith(GET_DEMO_PNL_PORTFOLIO.path)
    assert all(request.method == "GET" and request.body is None for request in transport.requests)
    assert report["transport"] == {
        "requests_attempted": 5, "responses_received": 5,
        "complete_http_200_responses": 5, "all_attempted_reads_http_200": True}
    destination = private_setup.evidence / "synthetic-pnl"
    setup_receipt = json.loads((destination / "setup-receipt.json").read_text())
    assert setup_receipt["account_source"] == "pnl" and setup_receipt["max_read_retries"] == 0
    evidence = json.loads((destination / "account-evidence.json").read_text())
    assert evidence["observed_at"] is None and "snapshot" not in evidence
    assert evidence["demo_customer_id"] == "9001"
    original = json.loads(responses[1].body)["clientPortfolio"]["positions"][0]["unrealizedPnL"]["timestamp"]
    assert evidence["position_valuations"][0]["timestamp"] == original
    assert len(original.split(".")[1].removesuffix("Z")) == 7
    assert json.loads((destination / "readiness.json").read_text()) == report
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    for sequence, response in enumerate(responses, 1):
        stem = f"{sequence:03d}"
        assert (destination / f"{stem}-response.bin").read_bytes() == response.body
        receipt = json.loads((destination / f"{stem}-response.json").read_text())
        assert receipt["body_sha256"] == hashlib.sha256(response.body).hexdigest()
        request = json.loads((destination / f"{stem}-request.json").read_text())
        if request["path"].startswith("/api/v1/trading/info/demo/"):
            assert request["query"] == ""
    assert {path.name for path in private_setup.root.iterdir()} == {"config", "runtime"}
    for path in destination.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for secret in (b"synthetic-api-secret", b"synthetic-user-secret", b"synthetic-response-secret"):
            assert secret not in path.read_bytes()


def test_default_main_performs_pnl_path_with_actual_requests_adapter(private_setup, monkeypatch, capsys):
    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.response import HTTPResponse

    pending = deque(_responses())
    calls = []

    def urlopen(pool, **kwargs):
        assert pool.host == "public-api.etoro.com"
        calls.append(kwargs)
        response = pending.popleft()
        return HTTPResponse(body=io.BytesIO(response.body), status=response.status_code,
                            headers={}, preload_content=False, decode_content=False)

    monkeypatch.setattr(HTTPSConnectionPool, "urlopen", urlopen)
    status = launcher.main([
        "--config", str(private_setup.config), "--identity-file", str(private_setup.identity),
        "--evidence-parent", str(private_setup.evidence), "--attempt", "synthetic-pnl",
    ])
    assert status == 2
    report = json.loads(capsys.readouterr().out)
    _assert_no_trading(report)
    assert report["valuation_freshness"] == "PASS"
    assert len(calls) == 5 and calls[1]["url"] == GET_DEMO_PNL_PORTFOLIO.path
    assert all(call["method"] == "GET" and call["body"] is None for call in calls)
    assert all(call["redirect"] is False and call["retries"].total == 0 for call in calls)


def test_redirect_from_pnl_is_recorded_once_without_any_followup(private_setup, monkeypatch):
    from urllib3.connectionpool import HTTPSConnectionPool
    from urllib3.response import HTTPResponse

    calls = []
    first = _response(_detail())

    def urlopen(pool, **kwargs):
        assert pool.host == "public-api.etoro.com"
        calls.append(kwargs)
        assert len(calls) <= 2
        if len(calls) == 1:
            return HTTPResponse(body=io.BytesIO(first.body), status=200,
                                headers={}, preload_content=False, decode_content=False)
        assert kwargs["url"] == GET_DEMO_PNL_PORTFOLIO.path
        return HTTPResponse(body=io.BytesIO(b"private-redirect-body"), status=302,
                            headers={"Location": "https://other.invalid/never-follow"},
                            preload_content=False, decode_content=False)

    monkeypatch.setattr(HTTPSConnectionPool, "urlopen", urlopen)
    report = _run(private_setup)
    assert report["broker_requests"] == len(calls) == 2
    assert all(call["redirect"] is False and call["retries"].total == 0 for call in calls)
    assert report["transport"]["all_attempted_reads_http_200"] is False
    assert report["reconciliation"] == report["valuation_freshness"] == "NOT_VERIFIED"
    assert report["blockers"][0] == "broker_account_capture_failed"
    destination = private_setup.evidence / "synthetic-pnl"
    assert (destination / "002-response.bin").read_bytes() == b"private-redirect-body"
    assert not list(destination.glob("003-*"))
    assert "private-redirect-body" not in json.dumps(report)


@pytest.mark.parametrize("failure", [429, 503, "timeout"])
def test_pnl_failure_is_terminal_without_retry_or_aggregate_fallback(private_setup, failure):
    responses = _responses()
    responses[1] = TimeoutError("private-timeout-details") if failure == "timeout" else _response({}, status=failure)
    transport = FakeTransport(responses)
    report = _run(private_setup, transport)
    assert report["broker_requests"] == len(transport.requests) == 2
    assert report["blockers"][0] == "broker_account_capture_failed"
    assert not list((private_setup.evidence / "synthetic-pnl").glob("003-*"))
    assert "private-timeout-details" not in json.dumps(report)


def test_pnl_attempt_is_single_use_even_after_complete_successful_transport(private_setup):
    first_transport = FakeTransport(_responses())
    first = _run(private_setup, first_transport)
    assert first["broker_requests"] == 5
    destination = private_setup.evidence / "synthetic-pnl"
    original = {path.name: path.read_bytes() for path in destination.iterdir()}
    second_transport = FakeTransport(_responses())
    second = _run(private_setup, second_transport)
    assert second["blockers"][0] == "attempt_already_exists_no_retry"
    assert second["broker_requests"] == 0 and second_transport.requests == []
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == original


@pytest.mark.parametrize("missing", ["ETORO_DEMO_API_KEY", "ETORO_DEMO_USER_KEY"])
def test_pnl_does_not_inherit_connector_credentials(private_setup, monkeypatch, missing):
    monkeypatch.delenv(missing)
    monkeypatch.setenv("ETORO_API_KEY", "unrelated-connection-key")
    monkeypatch.setenv("ETORO_USER_KEY", "unrelated-connection-user")
    transport = FakeTransport()
    report = _run(private_setup, transport)
    assert report["broker_requests"] == 0 and transport.requests == []
    assert not list(private_setup.evidence.iterdir())


@pytest.mark.parametrize("method,body,query", [
    ("POST", None, ""), ("GET", b"{}", ""),
    ("GET", None, "?instrumentIds=1001"), ("GET", None, "?cid=9001"),
])
def test_recorder_rejects_filtered_or_modified_pnl_request_before_send(tmp_path, method, body, query):
    transport = FakeTransport()
    recorder = RecordingCaptureTransport(tmp_path, transport=transport)
    request = HttpRequest(method=method,
                          url="https://public-api.etoro.com" + GET_DEMO_PNL_PORTFOLIO.path + query,
                          headers={}, body=body, timeout_seconds=1)
    with pytest.raises(CaptureBoundaryError, match="capture_route_not_allowed"):
        recorder.send(request)
    assert recorder.request_count == 0 and transport.requests == []
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("field,original,changed", [
    ("units", "10", "10.000000000000000001"),
    ("amount", "1000", "1000.000000000000000001"),
    ("credit", "9000", "9000.000000000000000001"),
])
def test_json_decimal_numbers_cannot_be_rounded_into_structural_equality(field, original, changed):
    bodies = _bodies()
    raw = json.dumps(bodies[1]).replace(f'"{field}": {original}', f'"{field}": {changed}')
    assert changed in raw and f'"{field}": "{changed}"' not in raw
    bodies[1] = HttpResponse(status_code=200, headers={}, body=raw.encode())
    client, transport = _client(bodies)
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001", now=NOW)
    assert len(transport.requests) == 5
    assert report["capture_failure_code"] == "pnl_account_structure_or_cash_mismatch"
    assert report["reconciliation"] == "NOT_VERIFIED"
