"""One serial, finite-mandate Demo cycle using the official direct API.

The worker never initializes the original runtime, enables a switch, obtains a
hosted placement token, changes an approved order type, or tops up a partial
fill. An existing incomplete claim is recovered instead of sending a new one.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from uuid import NAMESPACE_URL, uuid4, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.journal_agent.execution_journal import ExecutionJournal
from brokers.etoro.autonomous_api import DirectBindingStore, DirectDemoClient, build_order_payload
from brokers.etoro.batch_api import BatchDemoAdapter
from brokers.etoro.batch_reads import DirectBatchReads
from brokers.etoro.connector import instant, response_object
from brokers.etoro.pilot_account import build_pilot_account
from execution.batch_assessment import assess_batch_order
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import Environment, OrderIntent, canonical_json
from execution.finite_demo_mandate import FiniteDemoMandate
from execution.finite_demo_operator import FiniteDemoOperator
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.operator import OperatorEvidenceStore
from execution.pilot_policy import PilotCatalog, PilotClassification
from execution.store import ExecutionStore
from execution.usage_reserve import PilotUsageAdmission
from risk.kill_switch import KillSwitch
from risk.pretrade import RiskLimits
from scripts.run_autonomous_demo_pilot import verify_reviewed_checkout
from scripts.run_demo_pilot import Host
from scripts.setup_autonomous_demo_pilot import load_dedicated_credentials
from scripts.setup_finite_demo_pilot import (
    JOURNAL_FILE, MANDATE_FILE, existing_runtime, load_catalog, operator_lock,
)


def now():
    return datetime.now(timezone.utc)


def emit(value):
    print(canonical_json(value), flush=True)


class UsageOnlyHost(Host):
    """The JSON-lines host supplies quota evidence, never a broker operation."""
    def call(self, tool, arguments):
        if tool != "usage" or arguments != {}:
            raise ValueError("finite_host_usage_only")
        return super().call(tool, arguments)


def usage_evidence(host, mandate):
    original = host.usage()
    # The finite scope does not inherit the original experiment's optional
    # usage waiver. Unknown quota evidence blocks a new submission.
    return PilotUsageAdmission(original.observed_at, original.windows,
        original.available_window_ids, original.source_reference, original.source_sha256,
        floor_percent=Decimal(20), recovery_reserve_percent=Decimal(5),
        authorization_reference=mandate.source_reference)


def account(reader, expected):
    profile = reader.read("/api/v1/me")
    first = reader.read("/api/v1/trading/info/demo/portfolio")
    pnl = reader.read("/api/v1/trading/info/demo/pnl")
    last = reader.read("/api/v1/trading/info/demo/portfolio")
    return build_pilot_account(profile, first, pnl, last, expected, now())


def original_account(claim, expected):
    retained = claim["pre_account"]
    sources = retained["raw_sources"]
    return build_pilot_account(sources["profile"], sources["first"], sources["pnl"],
        sources["last"], expected, instant(retained["capture_completed_at"], "pre_capture"))


def registered_instrument(store, registry, spec, metadata):
    instrument = store.find_instrument(symbol=spec["symbol"], exchange=spec["exchange"])
    if instrument is None:
        instrument = registry.register(Instrument.create(canonical_symbol=spec["symbol"],
            exchange=spec["exchange"], asset_type=spec["asset_type"], currency=spec["currency"],
            sector=spec["sector"], internal_instrument_id=uuid5(NAMESPACE_URL,
                "etoro/finite-demo-v2/instrument/" + str(spec["instrument_id"]))))
    if (instrument.canonical_symbol != spec["symbol"] or instrument.exchange != spec["exchange"]
            or instrument.asset_type != spec["asset_type"] or instrument.currency != spec["currency"]
            or instrument.sector != spec["sector"]):
        raise ValueError("finite_existing_registry_identity_mismatch")
    mapping = store.get_broker_mapping(instrument.internal_instrument_id,
        broker="etoro", environment=Environment.DEMO)
    if mapping is None:
        registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument,
            broker="etoro", environment=Environment.DEMO, broker_instrument_id=spec["instrument_id"],
            exact_match_symbol=spec["symbol"], metadata=response_object(metadata["result"]), resolved_at=now()))
    elif mapping.broker_instrument_id != spec["instrument_id"] or mapping.exact_match_symbol != spec["symbol"]:
        raise ValueError("finite_existing_broker_mapping_mismatch")
    return instrument


def pending_claim(state):
    claims = [row for row in state["submissions"] if not row["reconciled"]
        or row.get("mandate_policy") == "finite-demo-v2" and not row.get("journal_complete")]
    if len(claims) > 1:
        raise ValueError("multiple_incomplete_claims_require_review_lookup_only")
    return claims[0] if claims else None


def locked_cycle(args, root, ledger):
    state = ledger.snapshot()
    expected = state["account_id"]
    mandate = FiniteDemoMandate.load(root / MANDATE_FILE, expected_account_id=expected)
    if (mandate.ledger_path != ledger.path.resolve() or mandate.reviewed_commit != args.reviewed_commit):
        raise ValueError("finite_original_runtime_and_reviewed_commit_required")
    classifications, catalog_hash = load_catalog(root, mandate, ledger)
    store = ExecutionStore(root / "execution.sqlite3")
    journal = ExecutionJournal(root / JOURNAL_FILE, ledger=ledger, store=store, runtime_root=root)
    journal.ensure_ready()
    claim = pending_claim(state)
    switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
    if claim is None:
        mandate.validate(ledger=ledger, kill_switch=switch, now=now(), reviewed_commit=args.reviewed_commit)
        specs = [item for item in mandate.scope["instruments"] if item["instrument_id"] == args.instrument_id]
        if len(specs) != 1:
            raise ValueError("finite_instrument_not_authorized")
        spec = specs[0]
        if not isinstance(args.amount, Decimal) or not args.amount.is_finite() or args.amount <= 0:
            raise ValueError("finite_positive_exact_amount_required")
        usage_host = UsageOnlyHost(root)
        usage = usage_evidence(usage_host, mandate)
        if usage.validate_for_work(now()):
            raise ValueError("finite_usage_floor_or_unknown_stop")
    else:
        command = store.get_command(claim["command_id"])
        if command is None:
            raise ValueError("claimed_command_missing_lookup_only")
        # Reconstruct only genuine finite-v2 provenance. Legacy records remain
        # intact and cannot be retroactively labelled with a new mandate.
        if claim.get("mandate_policy") != "finite-demo-v2":
            raise ValueError("legacy_incomplete_claim_requires_original_recovery")
        if claim.get("mandate_hash") != mandate.mandate_hash:
            raise ValueError("original_finite_mandate_required_for_recovery")
        if claim["reconciled"]:
            snapshot = FiniteDemoOperator.recover_committed(command.command_id, ledger=ledger,
                store=store, evidence=OperatorEvidenceStore(root / "operator.sqlite3"),
                journal=journal, now=now())
            emit({"event": "journal_recovery_complete", "command_id": str(command.command_id),
                "new_submission": False, "unresolved": snapshot["unresolved"]})
            return snapshot
    credentials = load_dedicated_credentials(root / "demo-api-credentials.json")
    reader = DirectBatchReads(credentials=credentials, receipt_dir=root / "direct-read-receipts", clock=now)
    client = DirectDemoClient(credentials=credentials, receipt_dir=root / "direct-api-receipts", clock=now)

    def final_guard():
        verify_reviewed_checkout(args.reviewed_commit)
        current = FiniteDemoMandate.load(root / MANDATE_FILE, expected_account_id=expected)
        current.validate_binding(ledger=ledger, now=now(), reviewed_commit=args.reviewed_commit)
        if (current.mandate_hash != mandate.mandate_hash or not switch.status().writes_allowed
                or load_catalog(root, current, ledger)[1] != catalog_hash):
            raise ValueError("finite_authority_or_catalog_changed_before_dispatch")
        if usage.validate_for_submission(now()):
            raise ValueError("finite_usage_evidence_expired_before_dispatch")

    adapter = BatchDemoAdapter(expected_account_id=expected, client=client,
        bindings=DirectBindingStore(root / "direct-api.sqlite3"), clock=now, pre_send_guard=final_guard)
    adapter.verify_demo_identity()
    metadata = reader.read("/api/v2/market-data/instruments", query={
        "instrumentsIds": ",".join(str(row["instrument_id"]) for row in classifications)})
    catalog = PilotCatalog(tuple(PilotClassification(**row) for row in classifications), metadata["result"])
    registry = InstrumentRegistry(store)
    evidence = OperatorEvidenceStore(root / "operator.sqlite3")
    workflow = FiniteDemoOperator(ledger=ledger, catalog=catalog, journal=journal,
        reviewed_commit=args.reviewed_commit,
        coordinator=ExecutionCoordinator(store=store, registry=registry,
            config=ExecutionConfig("1.0", True, True, "etoro_finite_demo_v2", Environment.DEMO,
                True, ("equity", "etf"), ("buy",), Decimal(1))),
        limits=RiskLimits.load(ROOT / "config/risk.yaml"), evidence_store=evidence,
        staged_fills_path=root / "unused_staging.csv")
    if claim is not None:
        pre = original_account(claim, expected)
        post = account(reader, expected)
        snapshot = workflow.reconcile_finite(command.command_id, adapter=adapter,
            pre_account=pre, post_account=post, now=now())
        emit({"event": "recovery_cycle_complete", "command_id": str(command.command_id),
            "new_submission": False, "unresolved": snapshot["unresolved"]})
        return snapshot
    instrument = registered_instrument(store, registry, spec, metadata)
    # Market/session eligibility is checked before cost/account preparation.
    reader.market(instrument, spec)
    payload = build_order_payload(spec["instrument_id"], args.amount)
    eligibility = reader.read("/api/v2/trading/info/demo/eligibility", body=json.dumps({
        "instrumentIds": [spec["instrument_id"]], "currency": "USD"}))
    costs = reader.read("/api/v2/trading/info/demo/costs", body=payload)
    pre = account(reader, expected)
    usage = usage_evidence(usage_host, mandate)
    if usage.validate_for_submission(now()):
        raise ValueError("finite_usage_recovery_reserve_stop")
    quote, market = reader.market(instrument, spec)
    assessment = assess_batch_order(instrument_spec=spec, instrument=instrument,
        cost_receipt=costs, eligibility_receipt=eligibility, payload=payload, quote=quote,
        quote_currency=spec["currency"], account_currency="USD", now=now())
    intent = OrderIntent.create(strategy_id="finite-demo-v2", run_id=str(uuid4()),
        internal_instrument_id=instrument.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value=args.amount, currency="USD",
        expires_at=min(now() + timedelta(minutes=3), mandate.expires_at))
    command = workflow.prepare(intent=intent, account=pre, quote=quote, assessment=assessment,
        mandate=mandate, market_state=market, now=now())
    submitted = workflow.submit_under_mandate(command.command_id, adapter=adapter, mandate=mandate,
        account=pre, quote=quote, assessment=assessment, market_state=market, kill_switch=switch,
        now=now(), usage=usage)
    emit({"event": "submitted_once", "command_id": str(command.command_id), "state": str(submitted.state)})
    post = account(reader, expected)
    snapshot = workflow.reconcile_finite(command.command_id, adapter=adapter,
        pre_account=pre, post_account=post, now=now())
    emit({"event": "cycle_complete", "command_id": str(command.command_id),
        "new_submission": True, "unresolved": snapshot["unresolved"],
        "position_count": snapshot["position_count"], "committed_capital": snapshot["committed_capital"]})
    return snapshot


def run(args):
    verify_reviewed_checkout(args.reviewed_commit)
    root, _ = existing_runtime(args.runtime)
    with operator_lock(root):
        root, ledger = existing_runtime(root)
        return locked_cycle(args, root, ledger)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_finite_worker_arguments")


def main(argv=None):
    parser = _Parser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--reviewed-commit", required=True)
    parser.add_argument("--instrument-id", type=int, required=True)
    parser.add_argument("--amount", type=Decimal, required=True)
    try:
        run(parser.parse_args(argv))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum() else "finite_cycle_held"
        emit({"event": "stopped", "reason": reason, "error_type": type(exc).__name__,
            "recovery": "lookup_only_after_any_claim"})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
