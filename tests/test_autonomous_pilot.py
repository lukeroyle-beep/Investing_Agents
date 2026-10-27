"""Synthetic full lifecycle, including no-resend and runtime restart boundaries."""
import json
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid5, NAMESPACE_URL

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.autonomous_api import DirectBindingStore, DirectDemoAdapter, DirectDemoClient, build_order_payload
from brokers.etoro.pilot_account import build_pilot_account
from execution.autonomous_pilot import AutonomousPilotOperator
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent, payload_hash
from execution.instruments import Instrument, InstrumentRegistry, BrokerInstrumentMapping
from execution.operator import OperatorEvidenceStore
from execution.pilot_assessment import assess_order
from execution.pilot_mandate import StandingPilotMandate, PilotMandateError
from execution.pilot_policy import PilotCatalog, PilotClassification
from execution.store import ExecutionStore
from execution.usage_reserve import PilotUsageAdmission, UsageWindow
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from risk.pretrade import QuoteEvidence, RiskLimits
from shared.paths import RISK_CONFIG_PATH
from tests.test_autonomous_api import SyntheticTransport, KEYS, full_lookup
from tests.test_demo_pilot_operator import account_at
from tests.test_pilot_account_source import NOW, change, receipt

D = Decimal


def account(when, post=False):
    original = account_at(when, post=post)
    items = [deepcopy(original.raw_sources[key]) for key in ("profile", "first", "pnl", "last")]
    if post:
        for index in (1, 2, 3):
            change(items, index, lambda raw: raw["clientPortfolio"]["positions"][-1].update(instrumentID=1142))
    return build_pilot_account(*items, expected_account_id="123", now=when)


def assessment(quote):
    payload = build_order_payload(1142, D(10))
    cost = receipt("/api/v2/trading/info/demo/costs", {"instrumentId":1142, "symbol":"SBUX",
        "lastUpdated":NOW.isoformat(), "costs":[{"costType":"transactionFee", "value":0.01, "currency":"USD"},
            {"costType":"marketSpread", "value":0, "currency":"USD"}]}, 0, 0)
    cost["arguments"]["body"] = json.dumps({**payload, "amount":10})
    eligible = receipt("/api/v2/trading/info/demo/eligibility", {"currency":"USD",
        "notFoundInstrumentIds":[], "notFoundSymbols":[], "eligibilities":[{
            "instrumentId":1142, "symbol":"SBUX", "allowOpenPosition":True,
            "allowedOrderQuantityType":"all", "tradeUnitType":"units", "minPositionExposure":10,
            "maxUnitsPerOrder":1000, "unitsQuantityType":"fractional", "orderFillBehaviorType":"bestEffort",
            "additionalBufferPercent":None, "leverageConfigs":[{"direction":"long", "settlementType":"real",
                "leverageValues":[1], "isPotential":False, "minPositionAmount":10}]}]}, 0, 0)
    eligible["arguments"]["body"] = json.dumps({"instrumentIds":[1142], "currency":"USD"})
    return assess_order(cost_receipt=cost, eligibility_receipt=eligible, payload=payload, quote=quote, now=NOW)


@pytest.fixture
def f(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(Instrument.create(canonical_symbol="SBUX", exchange="NASDAQ", asset_type="equity",
        currency="USD", sector="consumer", internal_instrument_id=uuid5(NAMESPACE_URL, "etoro/demo-pilot-v1/instrument/1142")))
    registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument, broker="etoro", environment="demo",
        broker_instrument_id=1142, exact_match_symbol="SBUX", metadata={"source":"synthetic"}, resolved_at=NOW))
    catalog = PilotCatalog((PilotClassification(1142,"SBUX","Starbucks","consumer","equity","synthetic"),
        PilotClassification(1002,"MSFT","Microsoft","technology","equity","synthetic")),
        {"results":[{"instrumentId":1142,"symbol":"SBUX","type":"Stocks"},
            {"instrumentId":1002,"symbol":"MSFT","type":"Stocks"}],"pagination":{"hasNext":False}})
    pre = account(NOW)
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", pre, catalog.legacy_breaches(pre), NOW)
    switch = KillSwitch(state_path=tmp_path / "kill-switch.json", audit_path=tmp_path / "kill-switch.jsonl")
    switch.initialize_engaged(operator_id="synthetic", reason="test", now=NOW)
    switch.reset(operator_id="synthetic", reason="test standing auth", acknowledgement=RESET_ACKNOWLEDGEMENT, now=NOW)
    mandate = StandingPilotMandate.create(tmp_path / "standing-mandate.json", account_id="123", source_reference="synthetic human authorization", issued_at=NOW, ledger=ledger)
    config = ExecutionConfig("1.0",True,True,"etoro_standing_demo_pilot",Environment.DEMO,True,("equity",),("buy",),D(1))
    op = AutonomousPilotOperator(ledger=ledger, catalog=catalog,
        coordinator=ExecutionCoordinator(store=store, registry=registry, config=config), limits=RiskLimits.load(RISK_CONFIG_PATH),
        evidence_store=OperatorEvidenceStore(tmp_path / "operator.sqlite3"), staged_fills_path=tmp_path / "unused.csv")
    quote = QuoteEvidence("synthetic-quote", instrument.internal_instrument_id, NOW, D(100), D(100), True)
    assessed = assessment(quote)
    intent = OrderIntent.create(strategy_id="synthetic-standing", run_id="synthetic", internal_instrument_id=instrument.internal_instrument_id,
        environment="demo", side="buy", order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=NOW+timedelta(minutes=3))
    command = op.prepare(intent=intent, account=pre, quote=quote, assessment=assessed, now=NOW)
    clock = [NOW]
    transport = SyntheticTransport(clock)
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport, clock=lambda:clock[0])
    def guard():
        if not switch.status().writes_allowed:
            raise ValueError("revoked")
    adapter = DirectDemoAdapter(expected_account_id="123", client=client, bindings=DirectBindingStore(tmp_path / "direct-api.sqlite3"),
        clock=lambda:clock[0], pre_send_guard=guard)
    lookup = full_lookup(command.logical_request_id)
    lookup["asset"]["instrumentId"] = 1142
    lookup["positionExecutions"][0]["openingData"]["executionTime"] = (NOW+timedelta(seconds=1)).isoformat()
    transport.lookup = lookup
    return SimpleNamespace(root=tmp_path, op=op, ledger=ledger, switch=switch, mandate=mandate, pre=pre,
        quote=quote, assessed=assessed, command=command, transport=transport, adapter=adapter, clock=clock)


def submit(f, **overrides):
    values = dict(adapter=f.adapter, mandate=f.mandate, account=f.pre, quote=f.quote, assessment=f.assessed,
        market_state={"instrument_id":1142,"quote_type":"realtime","quote_id":f.quote.quote_id,
            "received_at":NOW.isoformat(),"isCurrentlyTradable":True,"isExchangeOpen":True,
            "isActiveInPlatform":True,"isBuyEnabled":True,"isDelisted":False}, kill_switch=f.switch, now=NOW,
        usage=PilotUsageAdmission(NOW,(UsageWindow("weekly",D(19),True),),("weekly",),"synthetic","a"*64,
            floor_percent=D(10), authorization_reference="synthetic human ten percent"))
    values.update(overrides)
    return f.op.submit_under_mandate(f.command.command_id, **values)


def reconcile(f):
    f.clock[0] = NOW+timedelta(seconds=8)
    return f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter, pre_account=f.pre,
        post_account=account(f.clock[0], post=True), now=f.clock[0])


def test_first_fill_reconciles_once_then_restart_is_complete(f, monkeypatch):
    from risk.approval import ApprovalAuthority
    monkeypatch.setattr(ApprovalAuthority, "issue", lambda *a,**k:pytest.fail("No simulated human click"))
    submitted = submit(f)
    assert submitted.state == CommandState.ACKNOWLEDGED
    snapshot = reconcile(f)
    assert snapshot["position_count"] == 1 and not snapshot["unresolved"]
    assert snapshot["pilot_pnl"] == D("-0.03")
    assert len(f.transport.posts) == 1
    with pytest.raises(PilotMandateError, match="one_attempt_exhausted"):
        f.mandate.validate(ledger=PilotLedger(f.ledger.path,"123"), kill_switch=f.switch, now=f.clock[0])
    from scripts.run_autonomous_demo_pilot import locked_cycle
    monkeypatch.setattr("scripts.run_autonomous_demo_pilot.ReadOnlyHost", lambda *a:pytest.fail("Completed mandate performs no host calls"))
    locked_cycle(SimpleNamespace(), f.root, f.ledger)
    assert len(f.transport.posts) == 1


def test_lost_response_recovers_same_actual_request_only(f):
    f.transport.post_result = TimeoutError("synthetic lost response")
    assert submit(f).state == CommandState.UNKNOWN
    assert f.ledger.snapshot()["unresolved"]
    assert not reconcile(f)["unresolved"]
    assert len(f.transport.posts) == 1
    assert any("referenceId="+str(f.command.logical_request_id) in x.url for x in f.transport.calls)
    with pytest.raises(RuntimeError):
        submit(f)
    assert len(f.transport.posts) == 1


@pytest.mark.parametrize("failure", ["identity", "stale", "revoked"])
def test_last_moment_guard_leaves_claim_without_any_post(f, failure):
    if failure == "identity":
        f.transport.profile["demoCid"] = 999
    elif failure == "stale":
        f.transport.identity_delay = 16
    else:
        f.adapter.pre_send_guard = lambda: False
    with pytest.raises(ValueError):
        submit(f)
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.UNKNOWN
    assert len(f.transport.posts) == 0 and f.ledger.snapshot()["unresolved"]
    with pytest.raises(PilotMandateError, match="one_attempt_exhausted"):
        f.mandate.validate(ledger=f.ledger, kill_switch=f.switch, now=f.clock[0])


def test_closed_market_and_insufficient_usage_do_not_claim(f):
    with pytest.raises(RuntimeError, match="fresh_open"):
        submit(f, market_state={})
    usage = PilotUsageAdmission(NOW,(UsageWindow("weekly",D(14),True),),("weekly",),"synthetic","a"*64,
        floor_percent=D(10), authorization_reference="synthetic")
    with pytest.raises(RuntimeError, match="usage"):
        submit(f, usage=usage)
    assert not f.ledger.snapshot()["submissions"] and not f.transport.posts


@pytest.mark.parametrize("changes", [{"ask":D(101)}, {"observed_at":NOW-timedelta(seconds=1)}])
def test_same_quote_id_cannot_replace_assessed_source(f, changes):
    quote = replace(f.quote, **changes)
    with pytest.raises(RuntimeError, match="dynamic_evidence_changed"):
        submit(f, quote=quote)
    intent = f.op.coordinator.store.get_intent(f.command.intent_hash)
    with pytest.raises(RuntimeError, match="exact_quote_mismatch"):
        f.op.prepare(intent=intent, account=f.pre, quote=quote, assessment=f.assessed, now=NOW)
    assert not f.ledger.snapshot()["submissions"] and not f.transport.posts


def test_worker_host_rejects_all_mcp_write_paths(tmp_path):
    from scripts.run_autonomous_demo_pilot import ReadOnlyHost
    host = ReadOnlyHost(tmp_path)
    for tool in ("etoro_prepare_trade", "etoro_place_trade", "etoro_execute_write"):
        with pytest.raises(ValueError, match="read_only"):
            host.call(tool,{})


def test_missing_runtime_never_reinitializes(tmp_path):
    from scripts.run_autonomous_demo_pilot import existing_state
    with pytest.raises(ValueError, match="missing_never_initialize"):
        existing_state(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_crash_after_send_claim_recovers_pending_command_without_resend(f, monkeypatch):
    original = f.op.coordinator.store.transition_command
    def crash(command_id, target, **kwargs):
        if target == CommandState.ACKNOWLEDGED:
            raise KeyboardInterrupt("synthetic process death after POST")
        return original(command_id, target, **kwargs)
    monkeypatch.setattr(f.op.coordinator.store, "transition_command", crash)
    with pytest.raises(KeyboardInterrupt):
        submit(f)
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.SUBMISSION_PENDING
    monkeypatch.setattr(f.op.coordinator.store, "transition_command", original)
    assert not reconcile(f)["unresolved"]
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.RECONCILED
    assert len(f.transport.posts) == 1


def test_crash_after_economic_commit_is_idempotent_and_finishes_command(f, monkeypatch):
    submit(f)
    original = f.op.coordinator.store.transition_command
    def crash(command_id, target, **kwargs):
        if target == CommandState.FILLED:
            raise KeyboardInterrupt("synthetic process death after ledger commit")
        return original(command_id, target, **kwargs)
    monkeypatch.setattr(f.op.coordinator.store, "transition_command", crash)
    with pytest.raises(KeyboardInterrupt):
        reconcile(f)
    assert f.ledger.snapshot()["submissions"][0]["reconciled"]
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.ACKNOWLEDGED
    monkeypatch.setattr(f.op.coordinator.store, "transition_command", original)
    assert not reconcile(f)["unresolved"]
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.RECONCILED
    assert len(f.transport.posts) == 1
