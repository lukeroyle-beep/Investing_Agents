"""Real operator/adapter/journal integration using wholly synthetic broker I/O."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger
from agents.journal_agent.execution_journal import ExecutionJournal
from brokers.etoro.autonomous_api import DirectBindingStore, DirectDemoClient
from brokers.etoro.batch_api import BatchDemoAdapter
from brokers.etoro.batch_reads import DirectBatchReads
from brokers.etoro.pilot_account import build_pilot_account
from execution.batch_assessment import assess_batch_order
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent, payload_hash
from execution.finite_demo_mandate import FiniteDemoMandate, FiniteOrderCandidate
from execution.finite_demo_operator import FiniteDemoOperator
from execution.instruments import BrokerInstrumentMapping, InstrumentRegistry
from execution.operator import OperatorEvidenceStore
from execution.pilot_policy import PilotCatalog, PilotClassification
from execution.store import ExecutionStore
from execution.usage_reserve import PilotUsageAdmission, UsageWindow
from risk.kill_switch import KillSwitch, RESET_ACKNOWLEDGEMENT
from risk.pretrade import RiskLimits
from shared.paths import RISK_CONFIG_PATH
from tests.test_autonomous_api import SyntheticTransport, KEYS, full_lookup, raw
from tests.test_batch_assessment import batch_inputs, NOW
from tests.test_batch_reads import MarketTransport
from tests.test_demo_pilot_operator import account_at
from tests.test_pilot_account_source import change
from tests.test_pilot_assessment import change_response

COMMIT = "a" * 40


def account_with_fills(when, fills=()):
    original = account_at(when)
    items = [deepcopy(original.raw_sources[k]) for k in ("profile", "first", "pnl", "last")]
    template = account_at(when, post=True)
    samples = [deepcopy(template.raw_sources[k]) for k in ("profile", "first", "pnl", "last")]
    import json
    for index in (1, 2, 3):
        envelope = json.loads(samples[index]["result"]["content"][0]["text"])
        position = json.loads(envelope["body"])["clientPortfolio"]["positions"][-1]
        def edit(data):
            p = data["clientPortfolio"]
            p["credit"] = str(D(900) - sum((D(f[2]) + D(".01") for f in fills), D(0)))
            for instrument_id, position_id, amount in fills:
                new = deepcopy(position)
                new.update(instrumentID=instrument_id, positionID=position_id,
                           amount=str(amount), units=str(D(amount) / 100))
                if "unrealizedPnL" in new:
                    new["unrealizedPnL"].update(exposureInAccountCurrency=str(D(amount)-D(".02")),
                        exposureInAssetCurrency=str(D(amount)-D(".02")),
                        marginInAccountCurrency=str(amount), marginInAssetCurrency=str(amount))
                p["positions"].append(new)
            if "unrealizedPnL" in p:
                p["unrealizedPnL"] = str(D(2) - D(".02") * len(fills))
        change(items, index, edit)
    return build_pilot_account(*items, expected_account_id="123", now=when)


class MultiTransport(SyntheticTransport):
    def send(self, request):
        if request.method == "POST" and self.post_result is None:
            self.calls.append(request)
            return raw({"orderId": 41 + len(self.posts), "referenceId": request.headers["x-request-id"]})
        return super().send(request)


def workflow(tmp_path, *, max_submissions=3, max_per_day=3):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    specs, instruments = [], []
    for iid, symbol, kind, exposure in ((1004, "SYNA", "equity", "equity"),
                                      (8801, "SYNBOND", "etf", "fixed_income")):
        inputs = batch_inputs(iid, symbol, asset_type=kind, economic_class=exposure)
        instrument, spec = inputs["instrument"], inputs["instrument_spec"]
        registry.register(instrument)
        registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument,
            broker="etoro", environment="demo", broker_instrument_id=iid,
            exact_match_symbol=symbol, metadata={"source": "synthetic"}, resolved_at=NOW))
        specs.append(spec)
        instruments.append(instrument)
    classifications = tuple(PilotClassification(s["instrument_id"], s["symbol"], s["issuer"],
        s["sector"], s["asset_type"], s["class_source_reference"]) for s in specs)
    classifications += (PilotClassification(1002, "MSFT", "legacy-Microsoft", "technology", "equity", "synthetic"),)
    catalog = PilotCatalog(classifications, {"results": [{"instrumentId": c.instrument_id,
        "symbol": c.symbol, "type": "ETF" if c.asset_type == "etf" else "Stocks"} for c in classifications],
        "pagination": {"hasNext": False}})
    pre = account_with_fills(NOW)
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", pre, catalog.legacy_breaches(pre), NOW)
    ledger.enable_finite_mandates(source_reference="synthetic-migration-consent", reviewed_commit=COMMIT, now=NOW)
    scope = {"instruments": specs, "max_submissions": max_submissions, "max_per_day": max_per_day,
        "per_order_cap": "20", "total_commitment": "100", "max_positions": 5,
        "min_cash_fraction": ".20", "loss_stop": "25", "order_types": ["market"]}
    mandate = FiniteDemoMandate.create(tmp_path / "finite.json", account_id="123",
        source_reference="synthetic-finite-human-authority", issued_at=NOW, expires_at=NOW+timedelta(hours=1),
        ledger=ledger, reviewed_commit=COMMIT, scope=scope)
    journal = ExecutionJournal.create(tmp_path / "journal.sqlite3", ledger=ledger, store=store, runtime_root=tmp_path)
    config = ExecutionConfig("1.0", True, True, "finite_demo_v2", Environment.DEMO,
                             True, ("equity", "etf"), ("buy",), D(1))
    op = FiniteDemoOperator(journal=journal, reviewed_commit=COMMIT, ledger=ledger, catalog=catalog,
        coordinator=ExecutionCoordinator(store=store, registry=registry, config=config),
        limits=RiskLimits.load(RISK_CONFIG_PATH), evidence_store=OperatorEvidenceStore(tmp_path / "operator.sqlite3"),
        staged_fills_path=tmp_path / "unused.csv")
    switch = KillSwitch(state_path=tmp_path / "kill-switch.json", audit_path=tmp_path / "kill-switch.jsonl")
    switch.initialize_engaged(operator_id="synthetic", reason="test", now=NOW)
    switch.reset(operator_id="synthetic", reason="test mandate", acknowledgement=RESET_ACKNOWLEDGEMENT, now=NOW)
    clock = [NOW]
    transport = MultiTransport(clock)
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "wire", transport=transport, clock=lambda: clock[0])
    bindings = DirectBindingStore(tmp_path / "direct.sqlite3")
    adapter = BatchDemoAdapter(expected_account_id="123", client=client, bindings=bindings,
        clock=lambda: clock[0], pre_send_guard=lambda: None)
    return SimpleNamespace(**locals())


def prepare(f, index=0, fills=()):
    spec, instrument = f.specs[index], f.instruments[index]
    now = f.clock[0]
    market = MarketTransport(f.clock, [
        {"items": [{"instrumentId": spec["instrument_id"], "internalSymbolFull": spec["symbol"],
            "isCurrentlyTradable": True, "isExchangeOpen": True, "isActiveInPlatform": True,
            "isBuyEnabled": True, "isDelisted": False}], "totalItems": 1},
        {"results": [{"instrumentId": spec["instrument_id"], "quoteType": "realtime",
            "date": now.isoformat(), "ask": 100, "bid": 100}]}])
    host = DirectBatchReads(credentials=KEYS, receipt_dir=f.tmp_path / ("market-"+str(len(f.transport.posts))),
        clock=lambda: f.clock[0], transport=market)
    quote, market_state = host.market(instrument, spec)
    args = batch_inputs(spec["instrument_id"], spec["symbol"], asset_type=spec["asset_type"],
                        economic_class=spec["economic_class"])
    for name in ("cost_receipt", "eligibility_receipt"):
        args[name]["request_started_at"] = now.isoformat()
        args[name]["response_received_at"] = now.isoformat()
    change_response(args, "cost", lambda data: data.update(lastUpdated=now.isoformat()))
    args.update(quote=quote, now=f.clock[0])
    assessment = assess_batch_order(**args)
    account = account_with_fills(f.clock[0], fills)
    intent = OrderIntent.create(strategy_id="synthetic-finite", run_id="test-"+str(len(f.transport.posts)),
        internal_instrument_id=instrument.internal_instrument_id, environment="demo", side="buy", order_type="market",
        sizing_method="fixed_notional", sizing_value="10", currency="USD", expires_at=now+timedelta(minutes=3))
    command = f.op.prepare(intent=intent, account=account, quote=quote, assessment=assessment,
                           mandate=f.mandate, market_state=market_state, now=f.clock[0])
    usage = PilotUsageAdmission(observed_at=f.clock[0], windows=(UsageWindow("test", D(90), True),),
        available_window_ids=("test",), source_reference="synthetic-usage", source_sha256="a"*64)
    return SimpleNamespace(**locals())


def submit(p):
    return p.f.op.submit_under_mandate(p.command.command_id, adapter=p.f.adapter, mandate=p.f.mandate,
        account=p.account, quote=p.quote, assessment=p.assessment, market_state=p.market_state,
        kill_switch=p.f.switch, now=p.f.clock[0], usage=p.usage)


def finish(p, previous=(), *, amount="10", status=3):
    f = p.f
    command = f.store.get_command(p.command.command_id)
    lookup = full_lookup(command.logical_request_id)
    order_id = int(command.broker_order_id)
    position_id = 500 + len(previous)
    lookup.update(orderId=order_id)
    lookup["asset"]["instrumentId"] = p.spec["instrument_id"]
    lookup["status"]["id"] = status
    lookup["positionExecutions"][0].update(positionId=position_id, investedAmountCurrency=amount,
                                         remainingUnits=str(D(amount)/100))
    lookup["positionExecutions"][0]["openingData"].update(orderId=order_id, units=str(D(amount)/100),
        executionTime=(f.clock[0] + timedelta(milliseconds=500)).isoformat())
    if status in {4, 7, 8}:
        lookup["positionExecutions"] = []
        lookup["totalCosts"] = "0"
        lookup["lastUpdate"] = (f.clock[0] + timedelta(milliseconds=500)).isoformat()
        fills = tuple(previous)
    else:
        fills = (*previous, (p.spec["instrument_id"], position_id, amount))
    f.transport.lookup = lookup
    f.clock[0] += timedelta(seconds=10)
    post = account_with_fills(f.clock[0], fills)
    result = f.op.reconcile_finite(command.command_id, adapter=f.adapter,
                                   pre_account=p.account, post_account=post, now=f.clock[0])
    return result, fills, post


def test_two_distinct_instruments_one_mandate_no_per_order_confirmation(tmp_path):
    f = workflow(tmp_path)
    inception = payload_hash(f.ledger.snapshot()["inception"])
    first = prepare(f)
    assert submit(first).state == CommandState.ACKNOWLEDGED
    state, fills, _ = finish(first)
    assert not state["unresolved"] and state["position_count"] == 1
    second = prepare(f, 1, fills)
    assert submit(second).state == CommandState.ACKNOWLEDGED
    state, _, _ = finish(second, fills)
    assert len(f.transport.posts) == 2
    assert len(state["submissions"]) == 2 and state["position_count"] == 2
    assert state["committed_capital"] == D("20.02") and not state["unresolved"]
    assert payload_hash(state["inception"]) == inception
    assert all(f.journal.is_complete(c["command_id"]) for c in state["submissions"])
    for command in (first.command, second.command):
        approval = f.store.get_command_approval(command.command_id)
        assert dict(approval.limits)["authorization_kind"] == "finite_mandate"
        assert dict(approval.limits)["mandate_hash"] == f.mandate.mandate_hash
    with pytest.raises(Exception, match="fresh_unclaimed"):
        submit(first)
    assert len(f.transport.posts) == 2


@pytest.mark.parametrize("status,amount", [(9, "6"), (10, "6"), (4, "0"), (7, "0"), (8, "0")])
def test_terminal_partial_or_zero_fill_journal_then_next_candidate(tmp_path, status, amount):
    f = workflow(tmp_path)
    first = prepare(f)
    submit(first)
    state, fills, _ = finish(first, amount=amount, status=status)
    assert not state["unresolved"] and len(f.transport.posts) == 1
    assert f.journal.is_complete(first.command.command_id)
    second = prepare(f, 1, fills)
    submit(second)
    assert len(f.transport.posts) == 2


def test_unknown_outcome_consumes_attempt_and_blocks_next_candidate(tmp_path):
    f = workflow(tmp_path)
    first = prepare(f)
    f.transport.post_result = TimeoutError("synthetic lost reply")
    assert submit(first).state == CommandState.UNKNOWN
    with pytest.raises(Exception, match="reconciled|unresolved"):
        prepare(f, 1)
    with pytest.raises(Exception, match="fresh_unclaimed"):
        submit(first)
    assert len(f.transport.posts) == 1 and f.ledger.snapshot()["unresolved"]


def test_journal_failure_after_economics_holds_then_replay_recovers(tmp_path, monkeypatch):
    f = workflow(tmp_path)
    first = prepare(f)
    submit(first)
    original = f.journal.record_reconciliation
    monkeypatch.setattr(f.journal, "record_reconciliation", lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError, match="disk failure"):
        finish(first)
    state = f.ledger.snapshot()
    assert state["submissions"][0]["reconciled"] and not state["submissions"][0]["journal_complete"]
    assert state["unresolved"] and len(f.transport.posts) == 1
    monkeypatch.setattr(f.journal, "record_reconciliation", original)
    original(first.command.command_id)
    terminal = f.journal.terminal(first.command.command_id)
    f.ledger.mark_journal_complete(str(first.command.command_id), journal_reference=payload_hash(terminal), now=f.clock[0])
    assert not f.ledger.snapshot()["unresolved"] and len(f.transport.posts) == 1


def test_finite_count_exhaustion_after_reconciled_fills(tmp_path):
    f = workflow(tmp_path, max_submissions=1, max_per_day=1)
    first = prepare(f)
    submit(first)
    _, fills, _ = finish(first)
    with pytest.raises(Exception, match="submission_budget|daily_submission"):
        prepare(f, 1, fills)
    assert len(f.transport.posts) == 1


def test_final_identity_delay_cannot_age_quote_past_wire_gate(tmp_path):
    f = workflow(tmp_path)
    first = prepare(f)
    f.transport.identity_delay = 6
    with pytest.raises(Exception, match="expired|handoff"):
        submit(first)
    assert len(f.transport.posts) == 0 and f.ledger.snapshot()["unresolved"]


@pytest.mark.parametrize("status,amount", [(3, "10"), (9, "6"), (7, "0")])
def test_crash_after_economic_commit_recovers_without_any_network(tmp_path, monkeypatch, status, amount):
    f = workflow(tmp_path)
    first = prepare(f)
    submit(first)
    transition = f.store.transition_command
    def fail_terminal(command_id, state, **kwargs):
        if state in {CommandState.FILLED, CommandState.PARTIALLY_FILLED, CommandState.CANCEL_PENDING}:
            raise OSError("synthetic operational disk crash")
        return transition(command_id, state, **kwargs)
    monkeypatch.setattr(f.store, "transition_command", fail_terminal)
    with pytest.raises(OSError, match="operational disk"):
        finish(first, amount=amount, status=status)
    assert f.ledger.snapshot()["submissions"][0]["reconciled"]
    calls = len(f.transport.calls)
    monkeypatch.setattr(f.store, "transition_command", transition)
    recovered = FiniteDemoOperator.recover_committed(first.command.command_id, ledger=f.ledger,
        store=f.store, evidence=f.op.evidence, journal=f.journal, now=f.clock[0])
    assert not recovered["unresolved"] and f.journal.is_complete(first.command.command_id)
    assert len(f.transport.calls) == calls and len(f.transport.posts) == 1


def test_stale_account_is_rejected_even_with_fresh_market(tmp_path):
    f = workflow(tmp_path)
    p = prepare(f)
    with pytest.raises(Exception, match="account_evidence_older_than_15_seconds"):
        f.op._admission(mandate=f.mandate,
            candidate=FiniteOrderCandidate(
                "unused", p.spec["instrument_id"], D(10), D(".01"), p.intent.intent_hash),
            account=p.account, now=f.clock[0]+timedelta(seconds=12))
