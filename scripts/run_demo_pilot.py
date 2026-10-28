"""Interactive JSON-lines bridge to supported tools on the actual MCP host.

No network client or credentials are loaded. A host executes each emitted
tool_request once and returns the entire MCP result as one JSON input line.
At awaiting_approval it must stop and obtain the displayed exact user approval.
EOF, crash or uncertainty after a send are lookup-only obligations.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from uuid import uuid4, uuid5, NAMESPACE_URL

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.connector import ConnectorBindingStore, EtoroConnectorAdapter, response_object, instant
from brokers.etoro.pilot_account import build_pilot_account
from brokers.etoro.schemas import _pnl_timestamp, exact_datetime_epoch_seconds
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import Environment, OrderIntent, canonical_json, payload_hash
from execution.instruments import Instrument, InstrumentRegistry, BrokerInstrumentMapping
from execution.operator import OperatorEvidenceStore
from execution.pilot_operator import DemoPilotOperator
from execution.pilot_policy import PilotCatalog, PilotClassification
from execution.store import ExecutionStore
from execution.usage_reserve import UsageAdmission, UsageWindow, PilotUsageAdmission
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from risk.pretrade import RiskLimits, QuoteEvidence
from risk.submission_gate import GovernanceWritePolicy

D = Decimal
INSTRUMENT = 1142
SYMBOL = "SBUX"
NOW = lambda: datetime.now(timezone.utc)

# Explicit reviewed grouping, corroborated by live broker symbol/type metadata.
# Fund providers are grouped conservatively; no share-class ticker inference.
CLASSIFICATIONS = (
    (1004,"MSFT","Microsoft","technology","equity"),
    (8757,"MSFT.RTH","Microsoft","technology","equity"),
    (1142,"SBUX","Starbucks","consumer_discretionary","equity"),
    (1749,"CCEP","Coca-Cola Europacific Partners","consumer_staples","equity"),
    (2072,"RR.L","Rolls-Royce","industrials","equity"),
    (2110,"BA.L","BAE Systems","industrials","equity"),
    (3006,"QQQ","Invesco funds","diversified_funds","etf"),
    (3252,"VIG","Vanguard funds","diversified_funds","etf"),
    (4238,"VOO","Vanguard funds","diversified_funds","etf"),
    (100001,"ETH","Ethereum","crypto","crypto"),
)


def emit(value):
    print(canonical_json(value), flush=True)


def save(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as out:
        out.write(canonical_json(value))
        out.flush()
        os.fsync(out.fileno())


def redact_tokens(value):
    """Redact nested/JSON-encoded token fields without float conversion."""
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, dict):
            return '{'+','.join(json.dumps(k)+':'+encode(v) for k,v in item.items())+'}'
        if isinstance(item, list):
            return '['+','.join(encode(v) for v in item)+']'
        return json.dumps(item)
    if isinstance(value, dict):
        return {k:("REDACTED_IN_MEMORY_ONLY" if "token" in k.lower() else redact_tokens(v)) for k,v in value.items()}
    if isinstance(value, list):
        return [redact_tokens(x) for x in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value, parse_float=Decimal)
        except (ValueError, TypeError):
            return value
        if isinstance(parsed, (dict,list)):
            return encode(redact_tokens(parsed))
    return value


def rate_time(value):
    # The current getRates OpenAPI explicitly calls `date` a UTC timestamp.
    # Keep its raw spelling in the receipt; this semantic normalization applies
    # only to that documented route, never to aggregate cash/account timestamps.
    parsed = datetime.fromisoformat(str(value).replace('Z','+00:00'))
    normalized = str(value)+'Z' if parsed.tzinfo is None else str(value)
    spelling, epoch = _pnl_timestamp(normalized)
    if not D(0) <= exact_datetime_epoch_seconds(NOW())-epoch <= D(30):
        raise ValueError("rate_source_time_stale_or_future")
    return instant(spelling, "rate_time")


class Host:
    def __init__(self, root):
        self.directory = root / "captures" / str(uuid4())
        self.directory.mkdir(parents=True, mode=0o700)
        self.sequence = 0

    def call(self, tool, arguments):
        if tool not in {"etoro_execute_read", "etoro_prepare_trade", "etoro_place_trade", "usage"}:
            raise ValueError("unsupported_host_tool")
        self.sequence += 1
        start = NOW()
        emit({"event":"tool_request", "sequence":self.sequence, "tool":tool, "arguments":arguments})
        line = sys.stdin.readline()
        if not line:
            raise ValueError("host_response_missing_no_retry")
        result = json.loads(line, parse_float=Decimal)
        receipt = {"tool":tool, "arguments":arguments, "request_started_at":start.isoformat(),
                   "response_received_at":NOW().isoformat(), "result":result}
        retained = receipt
        if tool in {"etoro_prepare_trade", "etoro_place_trade"}:
            retained = {**receipt, "result":redact_tokens(result),
                "raw_response_sha256":payload_hash(result), "redaction":"sealed_order_tokens_only"}
            if tool == "etoro_place_trade":
                retained["arguments"] = {"token_hash":payload_hash(arguments["token"])}
        save(self.directory / f"{self.sequence:03d}-{tool}.json", retained)
        return receipt

    def read(self, path, **kwargs):
        return self.call("etoro_execute_read", {"path":path, **kwargs})

    def profile_callback(self, _):
        raw = response_object(self.read("/api/v1/me")["result"])
        return {"statusCode":200, "profile":{"demoCid":raw["demoCid"], "scopes":raw["scopes"]}}

    def account(self, expected):
        profile = self.read("/api/v1/me")
        first = self.read("/api/v1/trading/info/demo/portfolio")
        pnl = self.read("/api/v1/trading/info/demo/pnl")
        last = self.read("/api/v1/trading/info/demo/portfolio")
        return build_pilot_account(profile, first, pnl, last, expected, NOW())

    def market(self, instrument):
        receipt = self.read("/api/v1/market-data/search", query={
            "fields":"instrumentId,internalSymbolFull,isCurrentlyTradable,isExchangeOpen,isActiveInPlatform,isBuyEnabled,isDelisted",
            "internalSymbolFull":SYMBOL, "pageSize":"100", "pageNumber":"1"})
        raw = response_object(receipt["result"])
        state_start = instant(receipt["request_started_at"], "state_request_time")
        if not 0 <= (instant(receipt["response_received_at"], "state_receipt_time")-state_start).total_seconds() <= 10:
            raise ValueError("market_state_request_duration_exceeded")
        rows = raw.get("items", [])
        if raw.get("totalItems") != len(rows):
            raise ValueError("complete_market_search_required")
        matches = [x for x in rows if x.get("instrumentId") == INSTRUMENT and x.get("internalSymbolFull") == SYMBOL]
        if len(matches) != 1:
            raise ValueError("exact_market_state_missing")
        state = matches[0]
        if not all(state.get(k) is True for k in ("isCurrentlyTradable","isExchangeOpen","isActiveInPlatform","isBuyEnabled")) or state.get("isDelisted") is not False:
            raise ValueError("market_not_open_and_tradable")
        rates = response_object(self.read("/api/v2/market-data/rates", query={"instrumentIds":str(INSTRUMENT)})["result"])
        if len(rates.get("results", [])) != 1:
            raise ValueError("exact_rate_required")
        rate = rates["results"][0]
        if rate.get("instrumentId") != INSTRUMENT or rate.get("quoteType") != "realtime":
            raise ValueError("exact_realtime_rate_required")
        quote = QuoteEvidence(payload_hash(rates), instrument.internal_instrument_id, rate_time(rate["date"]),
            rate["bid"], rate["ask"], True)
        return quote, {**state, "instrument_id":INSTRUMENT, "quote_type":"realtime",
                       "quote_id":quote.quote_id, "received_at":receipt["request_started_at"],
                       "clock_basis":"bounded_market_request_start", "rate_time_basis":"getRates.date_documented_UTC"}

    def usage(self):
        receipt = self.call("usage", {})
        raw = response_object(receipt["result"])
        buckets = raw.get("rateLimitsByLimitId") or {"codex":raw.get("rateLimits")}
        windows = []
        for name, bucket in buckets.items():
            if not isinstance(bucket, dict):
                raise ValueError("usage_bucket_unavailable")
            for kind in ("primary", "secondary"):
                value = bucket.get(kind)
                if value is not None or kind == "primary":
                    used = value.get("usedPercent") if isinstance(value, dict) else None
                    windows.append(UsageWindow(name+":"+kind, None if used is None else max(D(0), D(100)-D(str(used))), kind=="primary"))
        return UsageAdmission(instant(receipt["request_started_at"], "usage_time"), tuple(windows),
            tuple(x.window_id for x in windows), str(self.directory), payload_hash(receipt))


def pilot_usage(host, args):
    """Bind an explicitly selected pilot floor to fresh, complete quota evidence."""
    original = host.usage()
    return PilotUsageAdmission(original.observed_at, original.windows,
        original.available_window_ids, original.source_reference, original.source_sha256,
        floor_percent=getattr(args, "work_floor_percent", D(20)),
        recovery_reserve_percent=original.recovery_reserve_percent,
        authorization_reference=getattr(args, "usage_authorization", "") or "")


def run(args):
    floor = getattr(args, "work_floor_percent", D(20))
    authorization = getattr(args, "usage_authorization", None)
    if not isinstance(floor, D) or floor not in (D(10), D(20)):
        raise ValueError("explicit_supported_pilot_usage_floor_required")
    if floor < D(20) and (not isinstance(authorization, str) or not authorization.strip()):
        raise ValueError("explicit_pilot_usage_authorization_required")
    root = args.runtime.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (root / "operator.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    ledger_path = root / "pilot.sqlite3"
    marker_path = root / "pilot-inception.json"
    prior_control = any(p.name not in {"operator.lock", "captures"} for p in root.iterdir())
    if not ledger_path.exists() and (args.recover or marker_path.exists() or prior_control or not args.initialize):
        raise ValueError("pilot_ledger_missing_restore_existing_state_never_reinitialize")
    pinned = response_object(json.loads(args.identity_receipt.read_text())["result"])
    expected = str(pinned["demoCid"])
    if pinned.get("realCid") == pinned.get("demoCid"):
        raise ValueError("distinct_demo_identity_required")
    host = Host(root)
    usage = pilot_usage(host, args)
    if usage.validate_for_work(NOW()):
        raise ValueError("usage_floor_or_unknown_stop")
    metadata = host.read("/api/v2/market-data/instruments", query={"instrumentsIds":",".join(str(x[0]) for x in CLASSIFICATIONS)})
    catalog = PilotCatalog(tuple(PilotClassification(*x, "reviewed-demo-pilot-v1-classifications") for x in CLASSIFICATIONS), metadata["result"])
    store = ExecutionStore(root / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    # Stable identity across previews and process restarts.
    instrument = registry.register(Instrument.create(canonical_symbol=SYMBOL, exchange="NASDAQ",
        asset_type="equity", currency="USD", sector=catalog.items[INSTRUMENT].sector,
        internal_instrument_id=uuid5(NAMESPACE_URL, "etoro/demo-pilot-v1/instrument/"+str(INSTRUMENT))))
    if store.get_broker_mapping(instrument.internal_instrument_id, broker="etoro", environment=Environment.DEMO) is None:
        registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument, broker="etoro",
            environment=Environment.DEMO, broker_instrument_id=INSTRUMENT, exact_match_symbol=SYMBOL,
            metadata=response_object(metadata["result"]), resolved_at=NOW()))
    adapter = EtoroConnectorAdapter(bindings=ConnectorBindingStore(root / "connector.sqlite3"), expected_account_id=expected,
        prepare_trade=lambda a:host.call("etoro_prepare_trade", a)["result"],
        place_trade=lambda a:host.call("etoro_place_trade", a)["result"],
        lookup_order=lambda q:host.read("/api/v2/trading/info/demo/orders:lookup", query=q)["result"],
        get_profile=host.profile_callback, clock=NOW)
    if args.recover:
        # Pending orders cannot satisfy the bounded empty-pending capture.
        # Resolve their actual broker handle first; never turn a pending lookup
        # into a failed capture or an invitation to place another order.
        ledger = PilotLedger(ledger_path, expected)
        pending_command = store.get_command(args.recover)
        if pending_command is None:
            raise ValueError("persisted_recovery_command_required")
        adapter.verify_demo_identity()
        observed = adapter.reconcile_command(pending_command)
        ledger.record_outcome(args.recover, {"lookup_status":observed.lifecycle_state,
            "terminal":observed.reconciled, "order_id":observed.broker_order_id})
        if not observed.reconciled or observed.lifecycle_state != "filled":
            emit({"event":"lookup_hold", "command_id":args.recover,
                  "broker_state":observed.lifecycle_state, "recovery":"lookup_only_no_resubmission"})
            return
    else:
        # Prepare first, then capture account and market evidence close to
        # admission. A valid but aging rate must not wait through preparation.
        eligibility = response_object(host.read("/api/v2/trading/info/demo/eligibility",
            body=json.dumps({"instrumentIds":[INSTRUMENT],"currency":"USD"}))["result"])
        intent = OrderIntent.create(strategy_id="demo-pilot-v1", run_id=str(uuid4()),
            internal_instrument_id=instrument.internal_instrument_id, environment="demo", side="buy", order_type="market",
            sizing_method="fixed_notional", sizing_value="10", currency="USD", expires_at=NOW()+timedelta(minutes=3))
        preview = adapter.preview(intent=intent, instrument_id=INSTRUMENT)
    account = host.account(expected)
    if ledger_path.exists():
        ledger = PilotLedger(ledger_path, expected)
    else:
        ledger = PilotLedger.create(ledger_path, account, catalog.legacy_breaches(account), NOW())
    marker = {"policy":"demo-pilot-v1", "account_id":expected,
              "inception_hash":payload_hash(ledger.snapshot()["inception"])}
    if marker_path.exists():
        if json.loads(marker_path.read_text()) != marker:
            raise ValueError("pilot_inception_marker_mismatch")
    else:
        save(marker_path, marker)
    config = ExecutionConfig("1.0", True, True, "etoro_demo_pilot_v1", Environment.DEMO, True,
        ("equity","etf"), ("buy",), D(1))
    workflow = DemoPilotOperator(ledger=ledger, catalog=catalog,
        coordinator=ExecutionCoordinator(store=store, registry=registry, config=config),
        limits=RiskLimits.load(ROOT / "config/risk.yaml"), evidence_store=OperatorEvidenceStore(root / "operator.sqlite3"),
        staged_fills_path=root / "unused_staging.csv")
    if args.recover:
        claim = next(x for x in ledger.snapshot()["submissions"] if x["command_id"] == args.recover)
        sources = claim["pre_account"]["raw_sources"]
        pre = build_pilot_account(sources["profile"], sources["first"], sources["pnl"], sources["last"], expected,
            instant(claim["pre_account"]["capture_completed_at"], "pre_capture"))
        snapshot = workflow.reconcile_pilot(args.recover, adapter=adapter, pre_account=pre, post_account=account, now=NOW())
        emit({"event":"reconciled", "pilot_pnl":snapshot["pilot_pnl"], "position_count":snapshot["position_count"]})
        return
    quote, state = host.market(instrument)
    account.validate_fresh(NOW())
    command = workflow.prepare(intent=intent, account=account, quote=quote, preview=preview, now=NOW())
    binding = workflow.bind_preview(command.command_id, adapter=adapter, preview=preview, eligibility=eligibility, now=NOW())
    # Binding may refresh identity. Do not present expired admission evidence.
    admission_now = NOW()
    account.validate_fresh(admission_now)
    if (not 0 <= (admission_now-quote.observed_at).total_seconds() <= 30
            or not 0 <= (admission_now-instant(state.get("received_at"), "market_time")).total_seconds() <= 15):
        raise ValueError("pilot_admission_evidence_expired_before_approval")
    proof = workflow.evidence.read_preparation(command.command_id)
    emit({"event":"awaiting_approval", "status":"AWAITING EXACT-ORDER APPROVAL", "command_id":str(command.command_id),
        "confirmation_hash":binding["confirmation_hash"], "confirmation":binding["confirmation"],
        "warnings":binding["warnings"], "expires_at":binding["expires_at"], "pilot_admission":proof["pilot_admission"],
        "limits":{"order":100,"combined":500,"positions":5,"cash_fraction":"0.20","loss_stop":25,
                  "daily_submissions":1,"usage_floor_percent":usage.floor_percent,
                  "recovery_reserve_percent":usage.recovery_reserve_percent}})
    # This is the only human gate. No default/elapsed-time approval exists.
    approval = json.loads(sys.stdin.readline())
    if approval != {"approve_confirmation_hash":binding["confirmation_hash"], "approver":"Luke"}:
        raise ValueError("exact_human_approval_required")
    workflow.approve(command.command_id, adapter=adapter, confirmation_hash=binding["confirmation_hash"],
        approver="Luke", human_confirmed=True, now=NOW())
    usage = pilot_usage(host, args)
    pre = host.account(expected)
    quote, state = host.market(instrument)
    switch = KillSwitch(state_path=root / "kill-switch.json", audit_path=root / "kill-switch.jsonl")
    if not switch.state_path.exists() and not switch.audit_path.exists():
        switch.initialize_engaged(operator_id="Luke", reason="new explicit Demo pilot", now=NOW())
        switch.reset(operator_id="Luke", reason="exact order approved: "+binding["confirmation_hash"],
                     acknowledgement=RESET_ACKNOWLEDGEMENT, now=NOW())
    submitted = workflow.submit(command.command_id, adapter=adapter, account=pre, quote=quote, market_state=state,
        kill_switch=switch.status(), governance=GovernanceWritePolicy("demo_manual",True,True,True), now=NOW(), usage=usage)
    emit({"event":"submitted_once", "command_id":str(command.command_id), "state":str(submitted.state)})
    post = host.account(expected)
    snapshot = workflow.reconcile_pilot(command.command_id, adapter=adapter, pre_account=pre, post_account=post, now=NOW())
    emit({"event":"reconciled", "command_id":str(command.command_id), "pilot_pnl":snapshot["pilot_pnl"],
          "committed_capital":snapshot["committed_capital"], "position_count":snapshot["position_count"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--identity-receipt", type=Path, required=True)
    parser.add_argument("--recover", help="Existing command ID; lookup only, never approval or submission")
    parser.add_argument("--initialize", action="store_true", help="Explicit first-ever inception in a new runtime; never resets prior state")
    parser.add_argument("--work-floor-percent", type=D, choices=(D(10), D(20)), default=D(20),
        help="Explicit pilot-only usage floor; the 5%% recovery reserve remains additional")
    parser.add_argument("--usage-authorization", help="User authorization reference required for the 10%% pilot floor")
    try:
        run(parser.parse_args())
    except Exception as exc:
        emit({"event":"stopped", "reason":str(exc), "error_type":type(exc).__name__, "recovery":"lookup_only_after_any_claim"})
        sys.exit(2)
