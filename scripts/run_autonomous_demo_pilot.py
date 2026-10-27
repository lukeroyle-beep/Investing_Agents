"""One bounded standing-mandate cycle. The JSONL host supplies READS and usage only.

Broker mutation uses the dedicated official Demo API integration, never an MCP
placement token. Re-running after a claim performs lookup only. No scheduler,
credentials, ledger or kill switch is initialized by this worker.
"""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4, uuid5, NAMESPACE_URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.autonomous_api import DirectDemoAdapter, DirectDemoClient, DirectBindingStore, build_order_payload
from brokers.etoro.connector import instant, response_object
from brokers.etoro.pilot_account import build_pilot_account
from brokers.etoro.pilot_reads import DirectPilotReads, read_receipt_envelope
from execution.autonomous_pilot import AutonomousPilotOperator
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent, payload_hash
from execution.instruments import Instrument, InstrumentRegistry, BrokerInstrumentMapping
from execution.operator import OperatorEvidenceStore
from execution.pilot_assessment import assess_order
from execution.pilot_mandate import StandingPilotMandate, PilotUsageAmendment, PilotUsageWaiver, PilotBufferAmendment
from execution.pilot_policy import PilotCatalog, PilotClassification
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitch
from risk.pretrade import RiskLimits, QuoteEvidence
from scripts.run_demo_pilot import Host, NOW, INSTRUMENT, SYMBOL, CLASSIFICATIONS, emit, pilot_usage, rate_time
from scripts.setup_autonomous_demo_pilot import load_dedicated_credentials


class ReadOnlyHost(Host):
    def call(self, tool, arguments):
        if tool not in {"etoro_execute_read", "usage"}:
            raise ValueError("autonomous_host_read_only")
        return super().call(tool, arguments)


class DirectPilotHost:
    """Original SBUX reads performed in-process with private direct receipts."""

    def __init__(self, root, *, credentials):
        self.reader = DirectPilotReads(credentials=credentials,
            receipt_dir=root / "direct-read-receipts", clock=NOW)

    def read(self, path, **kwargs):
        return self.reader.read(path, **kwargs)

    def account(self, expected):
        profile = self.read("/api/v1/me")
        first = self.read("/api/v1/trading/info/demo/portfolio")
        pnl = self.read("/api/v1/trading/info/demo/pnl")
        last = self.read("/api/v1/trading/info/demo/portfolio")
        return build_pilot_account(profile, first, pnl, last, expected, NOW())

    def market(self, instrument):
        if (instrument.canonical_symbol != "SBUX"
                or instrument.internal_instrument_id != uuid5(NAMESPACE_URL, "etoro/demo-pilot-v1/instrument/1142")):
            raise ValueError("original_standing_instrument_required")
        receipt = self.read("/api/v1/market-data/search", query={
            "fields":"instrumentId,internalSymbolFull,isCurrentlyTradable,isExchangeOpen,isActiveInPlatform,isBuyEnabled,isDelisted",
            "internalSymbolFull":"SBUX", "pageSize":"100", "pageNumber":"1"})
        _, raw = read_receipt_envelope(receipt)
        start = instant(receipt["request_started_at"], "state_request_time")
        end = instant(receipt["response_received_at"], "state_receipt_time")
        if not 0 <= (end-start).total_seconds() <= 10 or end > NOW():
            raise ValueError("market_state_request_duration_exceeded")
        rows = raw.get("items", [])
        if not isinstance(rows, list) or raw.get("totalItems") != len(rows):
            raise ValueError("complete_market_search_required")
        matches = [row for row in rows if row.get("instrumentId") == 1142 and row.get("internalSymbolFull") == "SBUX"]
        if len(matches) != 1:
            raise ValueError("exact_market_state_missing")
        state = matches[0]
        if (not all(state.get(key) is True for key in
                ("isCurrentlyTradable", "isExchangeOpen", "isActiveInPlatform", "isBuyEnabled"))
                or state.get("isDelisted") is not False):
            raise ValueError("market_not_open_and_tradable")
        rate_receipt = self.read("/api/v2/market-data/rates", query={"instrumentIds":"1142"})
        _, rates = read_receipt_envelope(rate_receipt)
        if len(rates.get("results", [])) != 1:
            raise ValueError("exact_rate_required")
        rate = rates["results"][0]
        if rate.get("instrumentId") != 1142 or rate.get("quoteType") != "realtime":
            raise ValueError("exact_realtime_rate_required")
        quote = QuoteEvidence(payload_hash(rates), instrument.internal_instrument_id, rate_time(rate["date"]),
            rate["bid"], rate["ask"], True)
        return quote, {**state, "instrument_id":1142, "quote_type":"realtime", "quote_id":quote.quote_id,
            "received_at":receipt["request_started_at"], "clock_basis":"bounded_market_request_start",
            "rate_time_basis":"getRates.date_documented_UTC"}


def verify_reviewed_checkout(commit):
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("exact_reviewed_commit_required")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT,
        check=True, capture_output=True, text=True).stdout
    if head != commit or dirty.strip():
        raise ValueError("clean_exact_reviewed_checkout_required")


def existing_state(root):
    if not root.is_dir() or root.is_symlink():
        raise ValueError("existing_private_runtime_required")
    for name in ("pilot.sqlite3", "pilot-inception.json", "execution.sqlite3", "operator.sqlite3"):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("existing_pilot_state_missing_never_initialize")
    marker = json.loads((root / "pilot-inception.json").read_text())
    ledger = PilotLedger(root / "pilot.sqlite3", expected_account_id=marker["account_id"])
    if marker != {"policy":"demo-pilot-v1", "account_id":ledger.snapshot()["account_id"],
                  "inception_hash":payload_hash(ledger.snapshot()["inception"])}:
        raise ValueError("original_pilot_inception_marker_required")
    return ledger


def run(args):
    verify_reviewed_checkout(args.reviewed_commit)
    root = args.runtime.absolute()
    ledger = existing_state(root)
    with (root / "operator.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return locked_cycle(args, root, ledger)


def locked_cycle(args, root, ledger):
    state = ledger.snapshot()
    # The economic ledger is authoritative even if a process died before
    # returning its final success event. No new authorization is ever needed.
    store = ExecutionStore(root / "execution.sqlite3")
    completed = (store.get_command(state["submissions"][0]["command_id"])
                 if len(state["submissions"]) == 1 else None)
    if (completed is not None and completed.state == CommandState.RECONCILED
            and len(state["submissions"]) == 1 and state["submissions"][0]["reconciled"]
            and state["position_count"] == 1 and not state["unresolved"]):
        emit({"event":"already_completed", "first_fill_reconciled":True,
              "command_id":state["submissions"][0]["command_id"], "new_submission":False})
        return
    expected = state["account_id"]
    mandate = StandingPilotMandate.load(root / "standing-mandate.json", expected_account_id=expected)
    usage_waiver = None
    if getattr(args, "ignore_usage_limits", False):
        usage_waiver = PilotUsageWaiver.load(root / "pilot-usage-waiver.json", mandate=mandate)
        usage_waiver.validate(mandate=mandate, ledger=ledger, now=NOW())
        if args.usage_authorization != usage_waiver.source_reference:
            raise ValueError("usage_waiver_authorization_mismatch")
    buffer_amendment = None
    if getattr(args, "allow_unreported_buffer", False):
        buffer_amendment = PilotBufferAmendment.load(root / "pilot-buffer-amendment.json", mandate=mandate)
        buffer_amendment.validate(mandate=mandate, ledger=ledger, now=NOW())
        if getattr(args, "buffer_authorization", None) != buffer_amendment.source_reference:
            raise ValueError("buffer_amendment_authorization_mismatch")
    amendment_path = root / "pilot-usage-amendment.json"
    usage_amendment = (PilotUsageAmendment.load(amendment_path, mandate=mandate)
                       if usage_waiver is None and amendment_path.exists() else None)
    switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
    claims = state["submissions"]
    if len(claims) > 1:
        raise ValueError("single_mandate_scope_exceeded_lookup_manually")
    # Validate the human's selected work allowance before any host call or
    # credential load, including read-only recovery after an existing claim.
    if usage_waiver is None and args.work_floor_percent < Decimal(10):
        if usage_amendment is None:
            raise ValueError("lower_usage_floor_requires_bound_amendment")
        usage_amendment.validate(mandate=mandate, ledger=ledger, now=NOW())
        if args.usage_authorization != usage_amendment.source_reference:
            raise ValueError("lower_usage_floor_authorization_mismatch")
    if not claims:
        mandate.validate(ledger=ledger, kill_switch=switch, now=NOW())
        if usage_amendment is not None:
            usage_amendment.validate(mandate=mandate, ledger=ledger, now=NOW())
    direct_reads = getattr(args, "direct_reads", False)
    if direct_reads and usage_waiver is None:
        raise ValueError("direct_reads_require_existing_usage_waiver")
    host = None if direct_reads else ReadOnlyHost(root)
    usage = None if usage_waiver is not None else pilot_usage(host, args)
    if usage is not None and usage.validate_for_work(NOW()):
        raise ValueError("usage_floor_or_unknown_stop")
    credentials = load_dedicated_credentials(root / "demo-api-credentials.json")
    if direct_reads:
        host = DirectPilotHost(root, credentials=credentials)
    client = DirectDemoClient(credentials=credentials, receipt_dir=root / "direct-api-receipts")
    def final_guard():
        verify_reviewed_checkout(args.reviewed_commit)
        current = StandingPilotMandate.load(mandate.path, expected_account_id=expected)
        if current.mandate_hash != mandate.mandate_hash or not switch.status().writes_allowed:
            raise ValueError("standing_mandate_revoked_before_dispatch")
        if usage_waiver is not None:
            usage_waiver.validate(mandate=current, ledger=ledger, now=NOW())
        if buffer_amendment is not None:
            buffer_amendment.validate(mandate=current, ledger=ledger, now=NOW())
        if usage_amendment is not None:
            fresh_amendment = PilotUsageAmendment.load(amendment_path, mandate=current)
            if fresh_amendment.amendment_hash != usage_amendment.amendment_hash:
                raise ValueError("usage_amendment_changed_before_dispatch")
    adapter = DirectDemoAdapter(expected_account_id=expected, client=client,
        bindings=DirectBindingStore(root / "direct-api.sqlite3"), clock=NOW,
        pre_send_guard=final_guard)
    adapter.verify_demo_identity()
    registry = InstrumentRegistry(store)
    if claims:
        command = store.get_command(claims[0]["command_id"])
        if command is None:
            raise ValueError("claimed_command_missing_lookup_only")
        observed = adapter.reconcile_command(command)
        ledger.record_outcome(claims[0]["command_id"], {"lookup_status":observed.lifecycle_state,
            "terminal":observed.reconciled, "order_id":observed.broker_order_id})
        if not observed.reconciled or observed.lifecycle_state != "filled":
            emit({"event":"lookup_hold", "command_id":claims[0]["command_id"],
                  "broker_state":observed.lifecycle_state, "new_submission":False})
            return
    elif usage is not None and usage.validate_for_submission(NOW()):
        raise ValueError("usage_recovery_reserve_stop_before_new_submission")
    metadata = host.read("/api/v2/market-data/instruments", query={
        "instrumentsIds":",".join(str(x[0]) for x in CLASSIFICATIONS)})
    catalog = PilotCatalog(tuple(PilotClassification(*x, "reviewed-demo-pilot-v1-classifications")
        for x in CLASSIFICATIONS), metadata["result"])
    instrument = registry.register(Instrument.create(canonical_symbol=SYMBOL, exchange="NASDAQ",
        asset_type="equity", currency="USD", sector=catalog.items[INSTRUMENT].sector,
        internal_instrument_id=uuid5(NAMESPACE_URL, "etoro/demo-pilot-v1/instrument/"+str(INSTRUMENT))))
    if store.get_broker_mapping(instrument.internal_instrument_id, broker="etoro", environment=Environment.DEMO) is None:
        registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument, broker="etoro",
            environment=Environment.DEMO, broker_instrument_id=INSTRUMENT, exact_match_symbol=SYMBOL,
            metadata=response_object(metadata["result"]), resolved_at=NOW()))
    config = ExecutionConfig("1.0", True, True, "etoro_standing_demo_pilot", Environment.DEMO,
        True, ("equity", "etf"), ("buy",), Decimal(1))
    workflow = AutonomousPilotOperator(ledger=ledger, catalog=catalog,
        coordinator=ExecutionCoordinator(store=store, registry=registry, config=config),
        limits=RiskLimits.load(ROOT / "config/risk.yaml"), evidence_store=OperatorEvidenceStore(root / "operator.sqlite3"),
        staged_fills_path=root / "unused_staging.csv")
    if claims:
        sources = claims[0]["pre_account"]["raw_sources"]
        pre = build_pilot_account(sources["profile"], sources["first"], sources["pnl"], sources["last"], expected,
            instant(claims[0]["pre_account"]["capture_completed_at"], "pre_capture"))
    else:
        # A closed-market cycle ends before any cost or account preparation.
        host.market(instrument)
        payload = build_order_payload(INSTRUMENT, Decimal(10))
        eligibility = host.read("/api/v2/trading/info/demo/eligibility",
            body=json.dumps({"instrumentIds":[INSTRUMENT], "currency":"USD"}))
        costs = host.read("/api/v2/trading/info/demo/costs", body=json.dumps({**payload, "amount":10}))
        pre = host.account(expected)
        quote, market = host.market(instrument)
        usage = None if usage_waiver is not None else pilot_usage(host, args)
        assessment = assess_order(cost_receipt=costs, eligibility_receipt=eligibility,
            payload=payload, quote=quote, now=NOW(), allow_unreported_buffer=buffer_amendment is not None)
        intent = OrderIntent.create(strategy_id="demo-pilot-v1-standing", run_id=str(uuid4()),
            internal_instrument_id=instrument.internal_instrument_id, environment="demo", side="buy", order_type="market",
            sizing_method="fixed_notional", sizing_value="10", currency="USD", expires_at=NOW()+timedelta(minutes=3))
        command = workflow.prepare(intent=intent, account=pre, quote=quote, assessment=assessment, now=NOW())
        submitted = workflow.submit_under_mandate(command.command_id, adapter=adapter, mandate=mandate,
            account=pre, quote=quote, assessment=assessment, market_state=market,
            kill_switch=switch, now=NOW(), usage=usage, usage_amendment=usage_amendment,
            usage_waiver=usage_waiver, buffer_amendment=buffer_amendment)
        emit({"event":"submitted_once", "command_id":str(command.command_id), "state":str(submitted.state)})
        observed = adapter.reconcile_command(submitted)
        if not observed.reconciled or observed.lifecycle_state != "filled":
            emit({"event":"lookup_hold", "command_id":str(command.command_id),
                  "broker_state":observed.lifecycle_state, "new_submission":False})
            return
    post = host.account(expected)
    snapshot = workflow.reconcile_pilot(command.command_id, adapter=adapter, pre_account=pre, post_account=post, now=NOW())
    emit({"event":"reconciled", "command_id":str(command.command_id), "first_fill_reconciled":True,
          "pilot_pnl":snapshot["pilot_pnl"], "committed_capital":snapshot["committed_capital"],
          "position_count":snapshot["position_count"], "standing_mandate_exhausted":True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--reviewed-commit", required=True)
    parser.add_argument("--work-floor-percent", type=Decimal, choices=(Decimal(5), Decimal(10), Decimal(20)), default=Decimal(10))
    parser.add_argument("--usage-authorization", required=True)
    parser.add_argument("--ignore-usage-limits", action="store_true",
        help="Use an existing immutable human-authorized pilot usage waiver.")
    parser.add_argument("--direct-reads", action="store_true",
        help="Use bounded official API reads for original SBUX/1142; requires the existing usage waiver.")
    parser.add_argument("--allow-unreported-buffer", action="store_true",
        help="Use an existing explicit amendment for an unreported temporary broker cash reservation.")
    parser.add_argument("--buffer-authorization",
        help="Exact human source reference from the existing buffer amendment; never a credential.")
    try:
        run(parser.parse_args())
    except Exception as exc:
        # Transport exceptions are fixed reason codes; avoid printing arbitrary
        # OS paths, traceback locals, authentication data or remote response text.
        reason = str(exc) if isinstance(exc, ValueError) and str(exc).replace("_", "").isalnum() else "cycle_held"
        emit({"event":"stopped", "reason":reason, "error_type":type(exc).__name__,
              "recovery":"lookup_only_after_any_claim"})
        sys.exit(2)
