"""Sanitised readiness for the P&L bracket; no baseline or order conversion."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from brokers.etoro.account_evidence import capture_pnl_account_evidence
from execution.domain import payload_hash
from tools.demo_account_preflight import SAFE_CAPTURE_FAILURES, UNPROVEN_GATES

PNL_FAILURES = SAFE_CAPTURE_FAILURES | {
    "pnl_account_identity_unproven_without_positions",
    "pnl_account_structure_or_cash_mismatch", "account_valuation_limit_not_valid",
    "account_capture_requires_demo_read_only_client",
    "account_currency_metadata_coverage_mismatch", "account_currency_metadata_inconsistent",
}


def pnl_preflight_report(*, client, expected_demo_customer_id, now=None,
                         risk_limits=None, evidence_sink=None):
    report = {
        "source_kind": "broker_demo_pnl_read_only_capture",
        "report_generated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "account_observed_at": None,
        "account_observed_at_reason": "whole_account_observation_time_unavailable",
        "status": "BLOCKED", "broker_orders_submitted": 0,
        "submission_available": False, "preparation_available": False,
        "approval_available": False, "baseline_created": False,
        "real_execution_available": False, "live_demo_qualified": False,
        "qualification_credit": False, "account_evidence_complete": False,
        "restricted_position_and_empty_pending_evidence_captured": False,
        "structural_cash_consistency": "NOT_VERIFIED",
        "reconciliation": "NOT_VERIFIED", "valuation_freshness": "NOT_VERIFIED",
        "trading_readiness": "BLOCKED",
        "blockers": ["whole_account_observation_time_unavailable", *UNPROVEN_GATES],
    }
    try:
        evidence = capture_pnl_account_evidence(
            client, expected_demo_customer_id=expected_demo_customer_id, now=now,
            account_max_age_seconds=(risk_limits.account_max_age_seconds if risk_limits else 15),
        )
    except Exception as exc:
        report["blockers"].insert(0, "broker_account_capture_failed")
        report["capture_error_type"] = type(exc).__name__
        reason = str(exc).split(":", 1)[0].strip()
        if reason in PNL_FAILURES:
            report["capture_failure_code"] = reason
        return report
    # Store the lossless evidence only in the launcher's owner-only directory.
    # No identity, amount, holding or original position timestamp is published.
    if evidence_sink is not None:
        try:
            evidence_sink(evidence)
        except OSError:
            report["blockers"].insert(0, "private_account_evidence_storage_failed")
            return report
    report.update({
        "account_evidence_sha256": payload_hash(evidence),
        "structural_cash_consistency": "PASS",
        "reconciliation": "PASS",
        "reconciliation_scope": [
            "exact_identity_position_and_cash_consistency",
            "per_position_pnl_matches_portfolio_total",
            "guide_cash_and_equity_calculation",
        ],
        "broker_margin_verified_against_invested": False,
        "valuation_freshness": "PASS" if evidence.valuations_fresh else "FAIL",
        "restricted_position_and_empty_pending_evidence_captured": evidence.valuations_fresh,
        "position_count": len(evidence.positions), "pending_order_count": 0,
        "pending_orders_complete": True, "executions_complete": False,
        "capture_started_at": evidence.capture_started_at.isoformat(),
        "capture_completed_at": evidence.capture_completed_at.isoformat(),
        "checked_pending_collections": list(evidence.checked_pending_collections),
        "supported_account_scope": evidence.scope,
        "valuation_count": len(evidence.position_valuations),
        "fresh_valuation_count": sum(evidence.valuation_freshness),
        "stale_valuation_count": sum(age > evidence.valuation_max_age_seconds for age in evidence.valuation_ages_seconds),
        "future_valuation_count": sum(age < 0 for age in evidence.valuation_ages_seconds),
        "valuation_max_age_seconds": str(evidence.valuation_max_age_seconds),
        "valuation_timestamp_precision": "exact_decimal_seconds_original_text_preserved",
        "existing_exposure_checks": None,
    })
    if not evidence.valuations_fresh:
        report["blockers"].insert(0, "position_valuations_stale_or_future")
        return report
    if evidence.equity <= 0:
        report["blockers"].append("account_equity_not_positive")
        return report
    if risk_limits is not None:
        exposures = {}
        for position in evidence.positions:
            exposures[position.broker_instrument_id] = exposures.get(position.broker_instrument_id, Decimal("0")) + position.current_exposure
        gross = sum((abs(value) for value in exposures.values()), Decimal("0"))
        checks = {
            "gross_exposure": (gross / evidence.equity, risk_limits.max_gross_fraction),
            "net_exposure": (abs(sum(exposures.values())) / evidence.equity, risk_limits.max_net_fraction),
            "single_instrument_issuer_floor": (max(map(abs, exposures.values())) / evidence.equity, risk_limits.max_issuer_fraction),
        }
        report["existing_exposure_checks"] = {
            name: {"observed_fraction": str(observed), "limit_fraction": str(limit),
                   "within_limit": observed <= limit}
            for name, (observed, limit) in checks.items()
        }
        report["blockers"].extend(f"existing_{name}_above_limit" for name, (observed, limit) in checks.items() if observed > limit)
    return report
