"""Synthetic outcome contracts; no retained account data or broker access."""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tests.contracts.etoro.test_etoro_account_evidence import (
    NOW, _aggregate, _client, _detail, _page, _security,
)
from tests.test_demo_operator_capture import (
    FakeTransport, _response, setup as private_setup,
)
from tests.test_demo_pnl_capture import _assert_no_trading, _bodies, _responses
from tools import demo_operator_capture as launcher
from tools.demo_account_preflight import preflight_report
from tools.demo_pnl_preflight import pnl_preflight_report


def _assert_outcome(report, *, complete, accepted):
    assert report["capture_complete"] is complete
    assert report["restricted_capture_accepted"] is accepted
    assert report["submission_available"] is False
    assert report["qualification_credit"] is False
    assert report["broker_orders_submitted"] == 0
    for flag in ("account_evidence_complete", "preparation_available",
                 "approval_available", "baseline_created", "live_demo_qualified",
                 "real_execution_available"):
        if flag in report:
            assert report[flag] is False


def _limits():
    return SimpleNamespace(account_max_age_seconds=15,
                           max_gross_fraction=Decimal("0.5"),
                           max_net_fraction=Decimal("0.5"),
                           max_issuer_fraction=Decimal("0.05"))


def _run(setup, transport, **options):
    arguments = dict(config_path=setup.config, identity_path=setup.identity,
                     evidence_parent=setup.evidence, attempt="synthetic-outcome",
                     transport=transport)
    arguments.update(options)
    return launcher.run_capture(**arguments)


@pytest.mark.parametrize("timestamp,accepted,stale,future", [
    ("2026-10-21T14:30:00.0000000Z", True, 0, 0),
    ("2026-10-21T14:29:44.9999999Z", False, 1, 0),
    ("2026-10-21T14:30:00.0000001Z", False, 0, 1),
])
def test_pnl_completion_is_separate_from_freshness_and_trading(
        timestamp, accepted, stale, future):
    client, transport = _client(_bodies(timestamp=timestamp))
    saved = []
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001",
                                  now=NOW, evidence_sink=saved.append)
    _assert_outcome(report, complete=True, accepted=accepted)
    _assert_no_trading(report)
    assert len(transport.requests) == 5 and len(saved) == 1
    assert report["reconciliation"] == "PASS"
    assert report["valuation_freshness"] == ("PASS" if accepted else "FAIL")
    assert report["stale_valuation_count"] == stale
    assert report["future_valuation_count"] == future
    assert report["restricted_position_and_empty_pending_evidence_captured"] is accepted
    assert saved[0].position_valuations[0].timestamp == timestamp
    assert saved[0].observed_at is None


def test_existing_risk_breaches_do_not_revoke_fresh_scoped_capture():
    bodies = _bodies()
    for index in (0, 1, 4):
        bodies[index]["clientPortfolio"]["credit"] = 0
    client, transport = _client(bodies)
    saved = []
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001",
                                  now=NOW, evidence_sink=saved.append,
                                  risk_limits=_limits())
    _assert_outcome(report, complete=True, accepted=True)
    _assert_no_trading(report)
    assert len(transport.requests) == 5 and len(saved) == 1
    for name in ("gross_exposure", "net_exposure", "single_instrument_issuer_floor"):
        assert report["existing_exposure_checks"][name]["within_limit"] is False
        assert f"existing_{name}_above_limit" in report["blockers"]


def test_zero_equity_remains_a_risk_blocker_after_scoped_pnl_capture():
    bodies = _bodies()
    for index in (0, 1, 4):
        bodies[index]["clientPortfolio"]["credit"] = 0
    portfolio = bodies[1]["clientPortfolio"]
    portfolio["unrealizedPnL"] = -1000
    portfolio["positions"][0]["unrealizedPnL"].update(pnL=-1000, pnlAssetCurrency=-1000)
    client, _ = _client(bodies)
    saved = []
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001",
                                  now=NOW, evidence_sink=saved.append,
                                  risk_limits=_limits())
    _assert_outcome(report, complete=True, accepted=True)
    _assert_no_trading(report)
    assert saved[0].equity == 0
    assert "account_equity_not_positive" in report["blockers"]
    assert report["existing_exposure_checks"] is None


@pytest.mark.parametrize("failure,reads,code", [
    ("schema", 1, None),
    ("identity", 1, "demo_account_identity_mismatch"),
    ("transport", 1, None),
    ("late_identity", 5, "demo_account_identity_mismatch"),
    ("late_structure", 5, "account_changed_during_capture"),
    ("late_pnl_reconciliation", 5, "account_unrealized_pnl_evidence_mismatch"),
])
def test_no_acceptance_until_capture_helper_finishes_validation(failure, reads, code):
    bodies = _bodies()
    if failure == "schema":
        del bodies[0]["clientPortfolio"]["credit"]
    elif failure == "identity":
        bodies[0]["clientPortfolio"]["positions"][0]["CID"] = 9002
    elif failure == "transport":
        bodies[0] = _response({}, status=429)
    elif failure == "late_identity":
        bodies[4]["clientPortfolio"]["positions"][0]["CID"] = 9002
    elif failure == "late_structure":
        bodies[4]["clientPortfolio"]["credit"] += 1
    else:
        bodies[1]["clientPortfolio"]["unrealizedPnL"] += 1
    client, transport = _client(bodies)
    saved = []
    report = pnl_preflight_report(client=client, expected_demo_customer_id="9001",
                                  now=NOW, evidence_sink=saved.append)
    _assert_outcome(report, complete=False, accepted=False)
    _assert_no_trading(report)
    assert len(transport.requests) == reads and saved == []
    assert report["blockers"][0] == "broker_account_capture_failed"
    assert report["reconciliation"] == "NOT_VERIFIED"
    if code is not None:
        assert report["capture_failure_code"] == code


@pytest.mark.parametrize("failure", ["missing_key", "invalid_source", "invalid_attempt", "claimed_attempt"])
def test_pre_capture_failures_have_explicit_false_outcomes(private_setup, monkeypatch, failure):
    options = {}
    if failure == "missing_key":
        monkeypatch.delenv("ETORO_DEMO_API_KEY")
    elif failure == "invalid_source":
        options["account_source"] = "unsupported"
    elif failure == "invalid_attempt":
        options["attempt"] = "../invalid"
    else:
        destination = private_setup.evidence / "synthetic-outcome"
        destination.mkdir(mode=0o700)
        (destination / "retained-marker").write_bytes(b"existing synthetic attempt")
    before = {path: path.read_bytes() for path in private_setup.evidence.rglob("*") if path.is_file()}
    transport = FakeTransport()
    report = _run(private_setup, transport, **options)
    _assert_outcome(report, complete=False, accepted=False)
    assert report["broker_requests"] == 0 and transport.requests == []
    after = {path: path.read_bytes() for path in private_setup.evidence.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("after_write", [False, True])
def test_account_evidence_sink_failure_preserves_completion_but_withholds_acceptance(
        private_setup, monkeypatch, after_write):
    original_write = launcher.private_json

    def fail_account_evidence(path, value):
        if path.name == "account-evidence.json":
            if after_write:
                original_write(path, value)
            raise OSError("synthetic account evidence storage failure")
        original_write(path, value)

    monkeypatch.setattr(launcher, "private_json", fail_account_evidence)
    transport = FakeTransport(_responses())
    report = _run(private_setup, transport)
    _assert_outcome(report, complete=True, accepted=False)
    _assert_no_trading(report)
    assert report["broker_requests"] == len(transport.requests) == 5
    assert report["transport"]["all_attempted_reads_http_200"] is True
    assert report["blockers"][0] == "private_account_evidence_storage_failed"
    destination = private_setup.evidence / "synthetic-outcome"
    assert (destination / "account-evidence.json").exists() is after_write
    assert json.loads((destination / "readiness.json").read_text()) == report
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    retry = FakeTransport()
    refused = _run(private_setup, retry)
    _assert_outcome(refused, complete=False, accepted=False)
    assert refused["blockers"][0] == "attempt_already_exists_no_retry"
    assert retry.requests == []
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before


@pytest.mark.parametrize("after_write", [False, True])
def test_final_report_storage_failure_does_not_revoke_accepted_capture(
        private_setup, monkeypatch, after_write):
    original_write = launcher.private_json

    def fail_readiness(path, value):
        if path.name == "readiness.json":
            if after_write:
                original_write(path, value)
            raise OSError("synthetic final report storage failure")
        original_write(path, value)

    monkeypatch.setattr(launcher, "private_json", fail_readiness)
    transport = FakeTransport(_responses())
    report = _run(private_setup, transport)
    _assert_outcome(report, complete=True, accepted=True)
    _assert_no_trading(report)
    assert report["broker_requests"] == len(transport.requests) == 5
    assert report["blockers"][0] == "private_report_storage_failed"
    destination = private_setup.evidence / "synthetic-outcome"
    assert (destination / "account-evidence.json").is_file()
    assert (destination / "readiness.json").exists() is after_write
    if after_write:
        retained = json.loads((destination / "readiness.json").read_text())
        _assert_outcome(retained, complete=True, accepted=True)
        assert "private_report_storage_failed" not in retained["blockers"]
    before = {path.name: path.read_bytes() for path in destination.iterdir()}
    retry = FakeTransport()
    refused = _run(private_setup, retry)
    _assert_outcome(refused, complete=False, accepted=False)
    assert refused["blockers"][0] == "attempt_already_exists_no_retry"
    assert retry.requests == []
    assert {path.name: path.read_bytes() for path in destination.iterdir()} == before


@pytest.mark.parametrize("age,accepted", [(0, True), (16, False), (-1, False)])
def test_aggregate_helper_freshness_failure_prevents_completion(age, accepted):
    detail, aggregate = _detail(), _aggregate()
    aggregate["timestamp"] = (NOW - timedelta(seconds=age)).isoformat()
    client, transport = _client([detail, aggregate, _page(_security()), deepcopy(detail)])
    report = preflight_report(client=client, expected_demo_customer_id="9001",
                              now=NOW, risk_limits=_limits())
    _assert_outcome(report, complete=accepted, accepted=accepted)
    assert len(transport.requests) == 4
    if accepted:
        assert "existing_single_instrument_issuer_floor_above_limit" in report["blockers"]
    else:
        assert report["capture_failure_code"] == "aggregate_account_snapshot_stale_or_future"


def test_unconfigured_aggregate_preflight_has_false_outcomes():
    report = preflight_report(now=NOW)
    _assert_outcome(report, complete=False, accepted=False)
    assert "read_only_client_unavailable_or_disabled" in report["blockers"]


def test_aggregate_zero_equity_does_not_revoke_valid_empty_account_capture():
    detail, aggregate = _detail(), _aggregate()
    detail["clientPortfolio"].update(credit=0, positions=[])
    aggregate["instrumentAggregates"] = []
    aggregate["accountTotals"].update(accountAvailableCash=0, accountBalance=0,
                                      accountCurrentPnl=0, accountTotalValue=0,
                                      accountTotalUsedMargin=0)
    client, transport = _client([detail, aggregate, deepcopy(detail)])
    report = preflight_report(client=client, expected_demo_customer_id="9001",
                              now=NOW, risk_limits=_limits())
    _assert_outcome(report, complete=True, accepted=True)
    assert len(transport.requests) == 3
    assert report["position_count"] == 0
    assert report["existing_exposure_checks"] is None
    assert "account_equity_not_positive" in report["blockers"]


def test_accepted_synthetic_capture_keeps_operator_exit_two(private_setup, monkeypatch, capsys):
    original_run = launcher.run_capture
    transport = FakeTransport(_responses())

    def run_with_fake_transport(**options):
        return original_run(**options, transport=transport)

    monkeypatch.setattr(launcher, "run_capture", run_with_fake_transport)
    status = launcher.main([
        "--config", str(private_setup.config), "--identity-file", str(private_setup.identity),
        "--evidence-parent", str(private_setup.evidence), "--attempt", "synthetic-outcome",
    ])
    report = json.loads(capsys.readouterr().out)
    assert status == 2
    _assert_outcome(report, complete=True, accepted=True)
    _assert_no_trading(report)
    assert report["broker_requests"] == len(transport.requests) == 5
