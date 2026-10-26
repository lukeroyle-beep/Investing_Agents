"""Read-only eToro Demo evidence check. This command has no submit capability."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brokers.etoro.account_evidence import capture_pretrade_account_evidence
from brokers.etoro.client import EtoroDemoReadOnlyConfig, EtoroReadOnlyClient
from execution.domain import dataclass_json, payload_hash
from execution.risk_baseline import RiskBaselineError, RiskBaselineStore
from risk.pretrade import RiskLimits
from shared.paths import ETORO_DEMO_CONFIG_PATH


UNPROVEN_GATES = (
    "trusted_full_account_daily_pnl_missing",
    "trusted_full_account_peak_equity_missing",
    "broker_execution_history_not_qualified",
    "market_session_evidence_not_bound",
    "economic_baseline_and_reconciliation_not_qualified",
    "issuer_and_sector_mapping_not_qualified",
    "per_order_human_approval_not_issued",
    "live_demo_submission_not_implemented_by_this_operator_workflow",
)

SAFE_CAPTURE_FAILURES = {
    "account_scope_not_supported", "demo_account_identity_mismatch",
    "expected_demo_account_identity_required", "account_changed_during_capture",
    "account_capture_timing_not_valid", "aggregate_account_snapshot_stale_or_future",
    "account_position_coverage_mismatch", "account_security_metadata_coverage_mismatch",
    "account_position_units_mismatch", "empty_pending_orders_contradict_reserved_cash",
    "account_cash_evidence_mismatch", "account_equity_identity_mismatch",
    "account_margin_evidence_mismatch", "account_unrealized_pnl_evidence_mismatch",
    "account_exposure_evidence_mismatch",
    "account_liquidation_evidence_mismatch",
}


def preflight_report(*, client=None, expected_demo_customer_id=None, now=None,
                     baseline_db=None, risk_limits=None):
    """Return an honest readiness report, never a trading authorization."""
    moment = now or datetime.now(timezone.utc)
    report = {
        "objective": "virtual_functionality",
        "return_target_pct": None,
        "source_kind": "broker_demo_read_only_preflight",
        "observed_at": moment.isoformat(),
        "broker_orders_submitted": 0,
        "submission_available": False,
        "real_execution_available": False,
        "live_demo_qualified": False,
        "qualification_credit": False,
        "account_evidence_complete": False,
        "capture_complete": False,
        "restricted_capture_accepted": False,
        "blockers": list(UNPROVEN_GATES),
    }
    if not expected_demo_customer_id:
        report["blockers"].insert(0, "expected_demo_customer_id_required")
    if client is None:
        report["blockers"].insert(0, "read_only_client_unavailable_or_disabled")
    if client is None or not expected_demo_customer_id:
        return report
    try:
        evidence = capture_pretrade_account_evidence(
            client, expected_demo_customer_id=str(expected_demo_customer_id), now=now
        )
    except Exception as exc:
        # Neither broker response bodies, customer identifiers nor credentials
        # belong in an exception report. The class identifies the failing layer.
        report["blockers"].insert(0, "broker_account_capture_failed")
        report["capture_error_type"] = type(exc).__name__
        reason = str(exc).split(":", 1)[0].strip()
        if reason in SAFE_CAPTURE_FAILURES:
            report["capture_failure_code"] = reason
        return report
    snapshot = evidence.snapshot
    report.update({
        # The aggregate capture helper already enforces valuation freshness.
        "capture_complete": True,
        "restricted_capture_accepted": True,
        "account_evidence_complete": False,  # daily loss/history still absent
        "restricted_position_and_empty_pending_evidence_captured": True,
        "account_evidence_sha256": payload_hash(json.loads(dataclass_json(evidence))),
        "position_count": len(snapshot.positions),
        "pending_order_count": len(snapshot.pending_orders),
        "pending_orders_complete": snapshot.pending_orders_complete,
        "executions_complete": snapshot.executions_complete,
        "capture_started_at": evidence.capture_started_at.isoformat(),
        "capture_completed_at": evidence.capture_completed_at.isoformat(),
        "checked_pending_collections": list(evidence.checked_pending_collections),
        "supported_account_scope": evidence.scope,
    })
    if risk_limits is not None and snapshot.equity <= 0:
        report["blockers"].append("account_equity_not_positive")
        report["existing_exposure_checks"] = None
    elif risk_limits is not None:
        gross = sum(abs(position.current_exposure) for position in snapshot.positions)
        net = sum(position.current_exposure for position in snapshot.positions)
        largest = max((abs(position.current_exposure) for position in snapshot.positions), default=0)
        checks = {
            "gross_exposure": (gross / snapshot.equity, risk_limits.max_gross_fraction),
            "net_exposure": (abs(net) / snapshot.equity, risk_limits.max_net_fraction),
            # One instrument can prove an issuer breach. Passing this lower
            # bound does not establish full issuer or sector compliance.
            "single_instrument_issuer_floor": (largest / snapshot.equity, risk_limits.max_issuer_fraction),
        }
        report["existing_exposure_checks"] = {
            name: {"observed_fraction": str(observed), "limit_fraction": str(limit),
                   "within_limit": observed <= limit}
            for name, (observed, limit) in checks.items()
        }
        for name, (observed, limit) in checks.items():
            if observed > limit:
                report["blockers"].append(f"existing_{name}_above_limit")
    if baseline_db is not None:
        try:
            store = RiskBaselineStore(baseline_db, account_id=str(expected_demo_customer_id))
            # No assertion of zero cash movements is invented. This can start
            # T0 or replay the same observation; later snapshots require the
            # separately qualified transaction-history integration.
            baseline = store.record(snapshot, account_id=str(expected_demo_customer_id),
                                    captured_at=evidence.capture_completed_at)
            report["risk_baseline"] = json.loads(dataclass_json(baseline))
        except RiskBaselineError:
            report["blockers"].append("risk_baseline_record_blocked")
            report["risk_baseline_status"] = "identity_integrity_freshness_or_flow_evidence_required"
        except Exception as exc:
            report["blockers"].append("risk_baseline_storage_failed")
            report["baseline_error_type"] = type(exc).__name__
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ETORO_DEMO_CONFIG_PATH)
    parser.add_argument("--expected-demo-customer-id")
    parser.add_argument("--baseline-db", type=Path,
                        help="Optional private prospective ledger; never rebases or assumes zero cash flows")
    parser.add_argument("--risk-config", type=Path, default=PROJECT_ROOT / "config" / "risk.yaml")
    parser.add_argument("--output", type=Path,
                        help="Optional new JSON report; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.output is not None and args.output.exists():
        parser.error("output already exists; select a new evidence path")
    client = None
    setup_error = None
    try:
        config = EtoroDemoReadOnlyConfig.load(args.config)
        # Do not load credentials or instantiate a transport on the default
        # disabled path, or without an independently specified Demo identity.
        if config.enabled and args.expected_demo_customer_id:
            client = EtoroReadOnlyClient.from_config(args.config)
    except Exception as exc:
        setup_error = type(exc).__name__
    risk_limits = None
    risk_setup_error = None
    try:
        risk_limits = RiskLimits.load(args.risk_config)
    except Exception as exc:
        risk_setup_error = type(exc).__name__
    report = preflight_report(client=client,
                              expected_demo_customer_id=args.expected_demo_customer_id,
                              baseline_db=args.baseline_db, risk_limits=risk_limits)
    if risk_setup_error:
        report["blockers"].append("risk_configuration_unavailable")
        report["risk_setup_error_type"] = risk_setup_error
    if setup_error:
        report["setup_error_type"] = setup_error
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
        args.output.chmod(0o600)
    print(encoded, end="")
    return 2  # This milestone cannot grant live Demo execution readiness.


if __name__ == "__main__":
    raise SystemExit(main())
