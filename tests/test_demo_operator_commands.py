from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.validate_demo_operator import exercise
from brokers.base import BrokerAccountSnapshot, BrokerPosition
from execution.domain import Environment
from tools import demo_account_preflight as preflight


def test_fixture_exercises_economic_writer_without_claiming_live_qualification(tmp_path):
    report = exercise(tmp_path / "exercise")
    assert report["functional_result"] == "pass"
    assert all(report["functional_checks"].values())
    assert report["broker_orders_submitted"] == 0
    assert report["human_approval_simulated"] is True
    assert report["live_demo_qualified"] is False
    assert report["qualification_credit"] is False
    assert report["return_target_pct"] is None
    with pytest.raises(FileExistsError):
        exercise(tmp_path / "exercise")


def test_disabled_preflight_does_not_load_credentials_or_create_control_stores(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(preflight.EtoroDemoReadOnlyConfig, "load",
                        lambda _: SimpleNamespace(enabled=False))
    monkeypatch.setattr(preflight.EtoroReadOnlyClient, "from_config",
                        lambda _: pytest.fail("disabled preflight must not create a client"))
    report_path = tmp_path / "preflight.json"
    ledger_path = tmp_path / "private" / "risk.sqlite3"
    assert preflight.main(["--output", str(report_path), "--baseline-db", str(ledger_path)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["submission_available"] is False
    assert report["account_evidence_complete"] is False
    assert "expected_demo_customer_id_required" in report["blockers"]
    assert list(tmp_path.rglob("*.sqlite3")) == []
    assert not ledger_path.parent.exists()
    before = report_path.read_bytes()
    with pytest.raises(SystemExit):
        preflight.main(["--output", str(report_path)])
    assert report_path.read_bytes() == before


def test_enabled_config_without_reviewed_customer_identity_never_reads_account(monkeypatch, capsys):
    monkeypatch.setattr(preflight.EtoroDemoReadOnlyConfig, "load",
                        lambda _: SimpleNamespace(enabled=True))
    monkeypatch.setattr(preflight.EtoroReadOnlyClient, "from_config",
                        lambda _: pytest.fail("missing account identity must block client creation"))
    assert preflight.main([]) == 2
    assert json.loads(capsys.readouterr().out)["broker_orders_submitted"] == 0


def test_capture_failure_is_redacted_and_cannot_be_qualified(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("secret response body and customer identifier")
    monkeypatch.setattr(preflight, "capture_pretrade_account_evidence", fail)
    report = preflight.preflight_report(client=object(), expected_demo_customer_id="private-customer")
    encoded = json.dumps(report)
    assert "secret response" not in encoded
    assert "private-customer" not in encoded
    assert report["capture_error_type"] == "RuntimeError"
    assert report["account_evidence_complete"] is False
    assert report["live_demo_qualified"] is False


def test_successful_narrow_capture_still_cannot_grant_execution(monkeypatch):
    now = datetime(2026, 10, 21, 14, tzinfo=timezone.utc)

    @dataclass
    class Evidence:
        snapshot: object
        capture_started_at: datetime = now
        capture_completed_at: datetime = now
        checked_pending_collections: tuple = ("fixture-empty-pending",)
        scope: str = "fixture-narrow-scope"

    snapshot = BrokerAccountSnapshot(
        snapshot_id="fixture", environment=Environment.DEMO, observed_at=now,
        currency="USD", equity=Decimal("100"), available_cash=Decimal("100"),
        balance=Decimal("100"), frozen_cash=Decimal("0"), current_pnl=Decimal("0"),
        used_margin=Decimal("0"), positions=(), pending_orders=(),
        pending_orders_complete=True, executions_complete=False)
    monkeypatch.setattr(preflight, "capture_pretrade_account_evidence",
                        lambda *args, **kwargs: Evidence(snapshot))
    report = preflight.preflight_report(client=object(), expected_demo_customer_id="123", now=now)
    assert report["restricted_position_and_empty_pending_evidence_captured"] is True
    assert report["pending_orders_complete"] is True
    assert report["executions_complete"] is False
    assert report["submission_available"] is False
    assert report["account_evidence_complete"] is False
    assert report["qualification_credit"] is False
    assert len(report["account_evidence_sha256"]) == 64
    # A valid, empty, unfunded Demo account must return blocked JSON rather
    # than divide zero by zero while evaluating configured exposure limits.
    empty = replace(snapshot, equity=Decimal("0"), available_cash=Decimal("0"), balance=Decimal("0"))
    monkeypatch.setattr(preflight, "capture_pretrade_account_evidence",
                        lambda *args, **kwargs: Evidence(empty))
    blocked = preflight.preflight_report(
        client=object(), expected_demo_customer_id="123", now=now,
        risk_limits=preflight.RiskLimits.load(preflight.PROJECT_ROOT / "config" / "risk.yaml"))
    assert "account_equity_not_positive" in blocked["blockers"]
    assert blocked["existing_exposure_checks"] is None
    assert blocked["submission_available"] is False


def test_preflight_preserves_baseline_and_exposes_existing_risk_breaches(tmp_path, monkeypatch):
    from execution.risk_baseline import RiskBaselineStore
    from risk.pretrade import RiskLimits

    now = datetime(2026, 10, 21, 14, tzinfo=timezone.utc)

    @dataclass
    class Evidence:
        snapshot: object
        capture_started_at: datetime = now
        capture_completed_at: datetime = now
        checked_pending_collections: tuple = ("fixture-empty-pending",)
        scope: str = "fixture-mixed-scope"

    snapshot = BrokerAccountSnapshot(
        snapshot_id="fixture-first", environment=Environment.DEMO, observed_at=now,
        currency="USD", equity=Decimal("100"), available_cash=Decimal("30"),
        balance=Decimal("30"), frozen_cash=Decimal("0"), current_pnl=Decimal("10"),
        used_margin=Decimal("60"), invested_amount=Decimal("60"), positions=(BrokerPosition(
            None, 1001, Decimal("1"), Decimal("70"), Decimal("1"), Decimal("10"), "USD"),),
        pending_orders=(), pending_orders_complete=True, executions_complete=False)
    evidence = Evidence(snapshot)
    monkeypatch.setattr(preflight, "capture_pretrade_account_evidence", lambda *a, **kw: evidence)
    ledger_path = tmp_path / "risk.sqlite3"
    args = dict(client=object(), expected_demo_customer_id="123", now=now,
                baseline_db=ledger_path,
                risk_limits=RiskLimits.load(preflight.PROJECT_ROOT / "config" / "risk.yaml"))
    first = preflight.preflight_report(**args)
    assert first["risk_baseline"]["observation_count"] == 1
    assert first["risk_baseline"]["daily_pnl"] is None
    assert first["risk_baseline"]["broker_history_verified"] is False
    assert "existing_gross_exposure_above_limit" in first["blockers"]
    assert "existing_single_instrument_issuer_floor_above_limit" in first["blockers"]
    assert first["existing_exposure_checks"]["gross_exposure"]["observed_fraction"] == "0.7"
    assert first["submission_available"] is False

    evidence = replace(evidence, snapshot=replace(snapshot, snapshot_id="fixture-second",
                       observed_at=now + timedelta(seconds=1)),
                       capture_completed_at=now + timedelta(seconds=1))
    second = preflight.preflight_report(**args)
    assert "risk_baseline_record_blocked" in second["blockers"]
    assert RiskBaselineStore(ledger_path, account_id="123").latest().observation_count == 1
    assert second["qualification_credit"] is False
