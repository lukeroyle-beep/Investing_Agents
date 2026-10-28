"""Synthetic end-to-end pilot lifecycle. No callbacks make network requests."""
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger, PilotLedgerError
from brokers.etoro.connector import ConnectorBindingStore, ConnectorEvidenceError, EtoroConnectorAdapter
from brokers.etoro.pilot_account import build_pilot_account
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent, payload_hash
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.operator import OperatorEvidenceError, OperatorEvidenceStore
from execution.pilot_operator import DemoPilotOperator
from execution.pilot_policy import PilotCatalog, PilotClassification, PilotPolicyError
from execution.store import ExecutionStore
from execution.usage_reserve import UsageAdmission, UsageWindow
from risk.approval import ApprovalError
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import QuoteEvidence, RiskLimits
from risk.submission_gate import GovernanceWritePolicy, SubmissionBlockedError
from scripts.validate_connector_demo import SyntheticConnectorHost, REFERENCE
from shared.paths import RISK_CONFIG_PATH
from tests.test_pilot_account_source import NOW, change, sources


D = Decimal


def account_at(when, *, post=False, account_id="123", extra_cash=D(0)):
    """Generate fresh synthetic raw receipts; never re-date real evidence."""
    items = sources()
    delta = when - NOW
    for item in items:
        for field in ("request_started_at", "response_received_at"):
            from datetime import datetime
            item[field] = (datetime.fromisoformat(item[field]) + delta).isoformat()
    change(items, 0, lambda p: p.update(demoCid=int(account_id)))
    for index in (1, 2, 3):
        def edit(payload):
            p = payload["clientPortfolio"]
            p["credit"] = str((D("889.99") if post else D(900)) + extra_cash)
            legacy = p["positions"][0]
            legacy.update(positionID=600, instrumentID=1002, CID=int(account_id))
            if "unrealizedPnL" in legacy:
                legacy["unrealizedPnL"]["timestamp"] = (when - timedelta(seconds=2)).isoformat()
            if post:
                new = {"positionID": 500, "CID": int(account_id), "instrumentID": 1001,
                    "mirrorID": 0, "units": "0.1", "amount": "10", "isBuy": True,
                    "leverage": "1", "settlementTypeID": 1, "totalFees": "0",
                    "totalExternalFees": "0.01", "totalExternalTaxes": "0"}
                if "unrealizedPnL" in legacy:
                    new["unrealizedPnL"] = {
                        "pnL": "-0.02", "pnlAssetCurrency": "-0.02",
                        "exposureInAccountCurrency": "9.98", "exposureInAssetCurrency": "9.98",
                        "marginInAccountCurrency": "10", "marginInAssetCurrency": "10",
                        "marginCurrencyId": 1, "assetCurrencyId": 1,
                        "closeRate": "99.8", "closeConversionRate": "1",
                        "timestamp": (when - timedelta(seconds=2)).isoformat()}
                    p["unrealizedPnL"] = "1.98"
                p["positions"].append(new)
        change(items, index, edit)
    return build_pilot_account(*items, expected_account_id=account_id, now=when)


class PilotHost(SyntheticConnectorHost):
    def __init__(self, at=NOW):
        super().__init__(at)
        self.initial_time = at
        self.lose_response = False
        self.preview_reference = False
        self.preview_tokens = set()

    def prepare(self, arguments):
        result = super().prepare(arguments)
        result["token"] = f"SYNTHETIC-NONSECRET-TOKEN-{self.calls['prepare']}"
        self.preview_tokens.add(result["token"])
        if self.preview_reference:
            result["referenceId"] = REFERENCE
        result["confirmation"]["balance"]["availableCash"] = "900"
        return result

    def place(self, arguments):
        assert arguments["token"] in self.preview_tokens
        self.calls["place"] += 1
        if self.lose_response:
            raise TimeoutError("SYNTHETIC_LOST_RESPONSE")
        return {"account": "demo", "outcome": "executed", "orderId": 42, "referenceId": REFERENCE}

    def lookup(self, arguments):
        assert arguments in ({"orderId": "42"}, {"referenceId": REFERENCE})
        result = super().lookup({"orderId": "42"})
        result["positionExecutions"][0]["openingData"]["executionTime"] = (self.initial_time + timedelta(seconds=1)).isoformat()
        return result


def workflow(tmp_path, *, reference=False, bind=True, at=NOW):
    host = PilotHost(at)
    host.preview_reference = reference
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    for broker_id, symbol in ((1001, "AAPL"), (1002, "MSFT")):
        instrument = registry.register(Instrument.create(canonical_symbol=symbol, exchange="NASDAQ",
            asset_type="equity", currency="USD", sector="technology"))
        registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument,
            broker="etoro", environment="demo", broker_instrument_id=broker_id,
            exact_match_symbol=symbol, metadata={"source": "synthetic_only"}, resolved_at=at))
        if broker_id == 1001:
            target = instrument
    catalog = PilotCatalog((
        PilotClassification(1001, "AAPL", "apple", "technology", "equity", "synthetic-only"),
        PilotClassification(1002, "MSFT", "microsoft", "technology", "equity", "synthetic-only")),
        {"results": [{"instrumentId": 1001, "symbol": "AAPL", "type": "Stocks"},
                     {"instrumentId": 1002, "symbol": "MSFT", "type": "Stocks"}],
         "pagination": {"hasNext": False}})
    pre = account_at(at)
    ledger = PilotLedger.create(tmp_path / "pilot.sqlite3", pre, catalog.legacy_breaches(pre), at)
    config = ExecutionConfig("1.0", True, True, "etoro_demo_manual", Environment.DEMO,
        True, ("equity", "etf"), ("buy",), D(1))
    def make_operator():
        execution_store = ExecutionStore(tmp_path / "execution.sqlite3")
        return DemoPilotOperator(coordinator=ExecutionCoordinator(store=execution_store,
            registry=InstrumentRegistry(execution_store), config=config),
            limits=RiskLimits.load(RISK_CONFIG_PATH), catalog=catalog,
            ledger=PilotLedger(tmp_path / "pilot.sqlite3", expected_account_id="123"),
            evidence_store=OperatorEvidenceStore(tmp_path / "operator.sqlite3"),
            staged_fills_path=tmp_path / "staged.csv")
    def make_adapter():
        return EtoroConnectorAdapter(bindings=ConnectorBindingStore(tmp_path / "connector.sqlite3"),
            expected_account_id="123", get_profile=host.profile, prepare_trade=host.prepare,
            place_trade=host.place, lookup_order=host.lookup, clock=lambda: host.now)
    op, adapter = make_operator(), make_adapter()
    intent = OrderIntent.create(strategy_id="synthetic_pilot", run_id="synthetic_run",
        internal_instrument_id=target.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=at + timedelta(minutes=5))
    quote = QuoteEvidence("synthetic-pilot-quote", target.internal_instrument_id, at, D(100), D(100), True)
    preview = adapter.preview(intent=intent, instrument_id=1001)
    command = op.prepare(intent=intent, account=pre, quote=quote, preview=preview, now=at)
    eligibility = {"currency": "USD", "notFoundInstrumentIds": [], "notFoundSymbols": [],
        "eligibilities": [{"instrumentId": 1001, "symbol": "AAPL", "allowOpenPosition": True,
            "allowedOrderQuantityType": "all", "tradeUnitType": "units", "minPositionExposure": "10",
            "maxUnitsPerOrder": "1000", "unitsQuantityType": "fractional", "leverageConfigs": [{
                "direction": "long", "settlementType": "real", "leverageValues": [1],
                "isPotential": False, "minPositionAmount": "10"}]}]}
    f = SimpleNamespace(op=op, adapter=adapter, host=host, pre=pre, intent=intent, quote=quote,
        command=command, preview=preview, eligibility=eligibility, make_operator=make_operator,
        make_adapter=make_adapter, bound=None)
    if bind:
        bind_preview(f)
    return f


def bind_preview(f):
    f.bound = f.op.bind_preview(f.command.command_id, adapter=f.adapter, preview=f.preview,
        eligibility=f.eligibility, now=f.host.now)


def approve(f):
    return f.op.approve(f.command.command_id, adapter=f.adapter,
        confirmation_hash=f.bound["confirmation_hash"], approver="SYNTHETIC_TEST_OPERATOR",
        human_confirmed=True, now=f.host.now)


def submit(f, *, account=None, quote=None, remaining=D(50)):
    now = f.host.now
    quote = quote or replace(f.quote, observed_at=now)
    account = account or account_at(now)
    return f.op.submit(f.command.command_id, adapter=f.adapter, account=account, quote=quote,
        market_state={"instrument_id": 1001, "quote_type": "realtime", "quote_id": quote.quote_id,
            "received_at": now.isoformat(), "isCurrentlyTradable": True,
            "isExchangeOpen": True, "isActiveInPlatform": True, "isBuyEnabled": True, "isDelisted": False},
        kill_switch=KillSwitchStatus(False, True, Environment.DEMO, 1, "synthetic", now),
        governance=GovernanceWritePolicy("demo_manual", True, True, True), now=now,
        usage=UsageAdmission(now, (UsageWindow("weekly", remaining, primary=True),),
            ("weekly",), "synthetic-tool-evidence", "a" * 64))


def test_full_pilot_cycle_reconciles_actual_fees_cash_and_position_ids(tmp_path):
    f = workflow(tmp_path)
    assert f.command.state == CommandState.AWAITING_APPROVAL
    assert f.op.ledger.snapshot()["inception"]["legacy_breaches"]
    assert f.op.ledger.snapshot()["inception"]["historical_whole_account_daily_loss"] is None
    approve(f)
    assert submit(f).state == CommandState.ACKNOWLEDGED
    assert f.op.ledger.snapshot()["unresolved"]
    f.host.now = NOW + timedelta(seconds=10)
    result = f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
        pre_account=f.pre, post_account=account_at(f.host.now, post=True), now=f.host.now)
    assert result["unresolved"] is False
    assert result["committed_capital"] == D("10.01")
    assert result["pilot_pnl"] == D("-0.03")
    assert [p["position_id"] for p in result["positions"]] == ["500"]
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.RECONCILED
    assert f.host.calls["place"] == 1 and f.host.calls["lookup"] == 1
    # Exact lookup replay never writes a second attributed execution or entry.
    again = f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
        pre_account=f.pre, post_account=account_at(f.host.now, post=True), now=f.host.now)
    assert again["committed_capital"] == D("10.01")
    assert len(again["positions"]) == 1 and f.host.calls["place"] == 1


def test_lost_response_restarts_with_original_reference_lookup_only(tmp_path):
    f = workflow(tmp_path, reference=True)
    approve(f)
    f.host.lose_response = True
    with pytest.raises(ConnectorEvidenceError, match="lookup_only"):
        submit(f)
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.UNKNOWN
    f.op, f.adapter = f.make_operator(), f.make_adapter()
    assert f.op.ledger.snapshot()["unresolved"]
    with pytest.raises((ValueError, SubmissionBlockedError)):
        submit(f)
    f.host.now = NOW + timedelta(seconds=10)
    result = f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
        pre_account=f.pre, post_account=account_at(f.host.now, post=True), now=f.host.now)
    assert result["unresolved"] is False
    assert f.host.calls["place"] == 1 and f.host.calls["lookup"] == 1


def test_lost_response_without_broker_handle_retains_recovery_hold(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    f.host.lose_response = True
    with pytest.raises(ConnectorEvidenceError, match="lookup_only"):
        submit(f)
    f.op, f.adapter = f.make_operator(), f.make_adapter()
    f.host.now = NOW + timedelta(seconds=10)
    with pytest.raises(ConnectorEvidenceError, match="recovery_handle_unavailable"):
        f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
            pre_account=f.pre, post_account=account_at(f.host.now, post=True), now=f.host.now)
    assert f.op.ledger.snapshot()["unresolved"]
    assert f.host.calls["place"] == 1 and f.host.calls["lookup"] == 0


@pytest.mark.parametrize("remaining,allowed", [(D("24.999"), False), (D(25), True)])
def test_usage_floor_plus_existing_recovery_reserve_before_any_send(tmp_path, remaining, allowed):
    f = workflow(tmp_path)
    approve(f)
    if allowed:
        assert submit(f, remaining=remaining).state == CommandState.ACKNOWLEDGED
    else:
        with pytest.raises(SubmissionBlockedError, match="usage_floor"):
            submit(f, remaining=remaining)
        assert not f.op.ledger.snapshot()["submissions"]
    assert f.host.calls["place"] == int(allowed)


def test_approval_is_per_exact_preview_and_is_never_inferred(tmp_path):
    f = workflow(tmp_path)
    with pytest.raises(ApprovalError, match="explicit human"):
        f.op.approve(f.command.command_id, adapter=f.adapter,
            confirmation_hash=f.bound["confirmation_hash"], approver="SYNTHETIC_TEST_OPERATOR",
            human_confirmed=False, now=NOW)
    with pytest.raises(ApprovalError):
        submit(f)
    assert f.host.calls["place"] == 0


def test_wrong_fresh_demo_account_never_claims_or_sends(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    with pytest.raises(SubmissionBlockedError, match="same_demo_account"):
        submit(f, account=account_at(NOW, account_id="999"))
    assert f.host.calls["place"] == 0 and not f.op.ledger.snapshot()["submissions"]


def test_changed_connected_identity_after_approval_is_fail_closed(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    f.adapter.get_profile = lambda _: {"statusCode": 200, "profile": {
        "demoCid": 999, "scopes": ["etoro-public:trade.demo:write"]}}
    with pytest.raises(ConnectorEvidenceError, match="identity_or_write_scope"):
        submit(f)
    assert f.host.calls["place"] == 0
    assert f.op.ledger.snapshot()["unresolved"]


def test_expired_quote_fails_before_pilot_claim(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    stale = replace(f.quote, observed_at=NOW - timedelta(seconds=31))
    with pytest.raises(SubmissionBlockedError, match="realtime_market"):
        submit(f, quote=stale)
    assert f.host.calls["place"] == 0 and not f.op.ledger.snapshot()["submissions"]


def test_wrong_quote_instrument_fails_before_pilot_claim(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    wrong = replace(f.quote, internal_instrument_id=uuid4())
    with pytest.raises((ValueError, SubmissionBlockedError)):
        submit(f, quote=wrong)
    assert f.host.calls["place"] == 0 and not f.op.ledger.snapshot()["submissions"]


def test_more_expensive_bound_preview_cannot_use_original_cheap_reserve(tmp_path):
    f = workflow(tmp_path, bind=False)
    confirmation = f.preview["confirmation"]
    confirmation.update(totalUpfrontCost="91", costs=[{"type": "transactionFee", "value": "91", "currency": "USD"}])
    confirmation["balance"]["required"] = "101"
    with pytest.raises((ValueError, SubmissionBlockedError)):
        bind_preview(f)
        approve(f)
        submit(f)
    assert f.host.calls["place"] == 0 and not f.op.ledger.snapshot()["submissions"]


def test_unreconciled_submission_blocks_new_command_even_on_next_london_day(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    submit(f)
    f.op = f.make_operator()
    f.host.now = NOW + timedelta(days=1)
    new_intent = OrderIntent.create(strategy_id="synthetic_pilot", run_id="different_run",
        internal_instrument_id=f.intent.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=f.host.now + timedelta(minutes=5))
    with pytest.raises((PilotLedgerError, PilotPolicyError)):
        f.op.prepare(intent=new_intent, account=account_at(f.host.now),
            quote=replace(f.quote, observed_at=f.host.now),
            preview=f.host.prepare({"account": "demo", "instrumentId": 1001, "direction": "buy",
                "amount": 10.0, "leverage": 1, "orderType": "mkt"}), now=f.host.now)
    assert f.host.calls["place"] == 1


def test_successful_reconciliation_does_not_allow_second_submission_same_day(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    submit(f)
    f.host.now = NOW + timedelta(seconds=10)
    post = account_at(f.host.now, post=True)
    f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
        pre_account=f.pre, post_account=post, now=f.host.now)
    new_intent = OrderIntent.create(strategy_id="synthetic_pilot", run_id="second_run",
        internal_instrument_id=f.intent.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=f.host.now + timedelta(minutes=5))
    with pytest.raises(PilotPolicyError, match="daily_submission"):
        f.op.prepare(intent=new_intent, account=post, quote=replace(f.quote, observed_at=f.host.now),
            preview=f.preview, now=f.host.now)
    assert f.host.calls["place"] == 1


def test_terminal_lookup_does_not_release_hold_when_whole_account_cash_differs(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    submit(f)
    f.host.now = NOW + timedelta(seconds=10)
    with pytest.raises(PilotLedgerError, match="cash_reconciliation"):
        f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter, pre_account=f.pre,
            post_account=account_at(f.host.now, post=True, extra_cash=D(1)), now=f.host.now)
    assert f.op.ledger.snapshot()["unresolved"]
    assert f.host.calls["place"] == 1


def test_two_already_approved_commands_share_one_durable_pilot_claim(tmp_path):
    first = workflow(tmp_path)
    second = copy(first)
    second.intent = OrderIntent.create(strategy_id="synthetic_pilot", run_id="second_approved_run",
        internal_instrument_id=first.intent.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=NOW + timedelta(minutes=5))
    second.preview = second.adapter.preview(intent=second.intent, instrument_id=1001)
    second.command = second.op.prepare(intent=second.intent, account=second.pre,
        quote=second.quote, preview=second.preview, now=NOW)
    bind_preview(second)
    approve(first)
    approve(second)
    submit(first)
    with pytest.raises((PilotLedgerError, SubmissionBlockedError)):
        submit(second)
    assert first.host.calls["place"] == 1
    assert len(first.op.ledger.snapshot()["submissions"]) == 1


def test_identity_refresh_cannot_dispatch_after_evidence_deadline(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    profile = f.host.profile
    def delayed_profile(arguments):
        f.host.now += timedelta(seconds=16)
        return profile(arguments)
    f.adapter.get_profile = delayed_profile
    with pytest.raises(ConnectorEvidenceError, match="authorization_expired"):
        submit(f)
    assert f.host.calls["place"] == 0
    assert f.op.ledger.snapshot()["unresolved"]


def test_exact_future_execution_timestamp_cannot_release_pilot_claim(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    submit(f)
    f.host.now = NOW + timedelta(seconds=10)
    lookup = f.host.lookup
    def future_execution(arguments):
        result = lookup(arguments)
        result["positionExecutions"][0]["openingData"]["executionTime"] = "2026-10-26T14:00:10.0000001Z"
        return result
    f.adapter.lookup_order = future_execution
    with pytest.raises((OperatorEvidenceError, PilotLedgerError), match="timestamp|execution_outside"):
        f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
            pre_account=f.pre, post_account=account_at(f.host.now, post=True), now=f.host.now)
    assert f.op.ledger.snapshot()["unresolved"]


def test_partial_fill_lookup_retains_whole_account_reconciliation_hold(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    submit(f)
    f.host.now = NOW + timedelta(seconds=10)
    lookup = f.host.lookup
    def partial(arguments):
        result = lookup(arguments)
        result["status"] = {"id": 5}
        return result
    f.adapter.lookup_order = partial
    with pytest.raises(SubmissionBlockedError, match="confirmed_full_fill"):
        f.op.reconcile_pilot(f.command.command_id, adapter=f.adapter,
            pre_account=f.pre, post_account=account_at(f.host.now, post=True), now=f.host.now)
    assert f.op.ledger.snapshot()["unresolved"]
    assert not f.op.ledger.snapshot()["positions"]


@pytest.mark.parametrize("at", [datetime(2026, 7, 1, 22, 59, 59, tzinfo=timezone.utc),
    datetime(2026, 10, 26, 23, 59, 59, tzinfo=timezone.utc)])
def test_crossing_london_midnight_during_identity_read_cannot_send(tmp_path, at):
    f = workflow(tmp_path, at=at)
    approve(f)
    profile = f.host.profile
    def midnight_profile(arguments):
        f.host.now += timedelta(seconds=2)
        return profile(arguments)
    f.adapter.get_profile = midnight_profile
    with pytest.raises(ConnectorEvidenceError, match="authorization_expired"):
        submit(f)
    snapshot = f.op.ledger.snapshot()
    assert len(snapshot["submissions"]) == 1
    assert snapshot["submissions"][0]["london_day"] == at.date().isoformat()
    assert snapshot["unresolved"]
    assert f.host.calls["place"] == 0


def test_catalog_entries_are_immutable_and_raw_evidence_tampering_blocks_submission(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    catalog = f.op.catalog
    with pytest.raises(TypeError):
        catalog.items[1001] = replace(catalog.items[1001], issuer="different")
    catalog.evidence["metadata"]["results"][0]["symbol"] = "ALTERED"
    with pytest.raises(PilotPolicyError, match="classification_evidence_changed"):
        submit(f)
    assert f.host.calls["place"] == 0 and not f.op.ledger.snapshot()["submissions"]


def test_replacing_valid_catalog_after_approval_requires_new_preparation(tmp_path):
    f = workflow(tmp_path)
    approve(f)
    original = f.op.catalog
    replacements = tuple(replace(item, source_reference="new-synthetic-review") for item in original.items.values())
    f.op.catalog = PilotCatalog(replacements, deepcopy(original.evidence["metadata"]))
    f.op.catalog.validate()
    with pytest.raises(SubmissionBlockedError, match="catalog_changed"):
        submit(f)
    assert f.host.calls["place"] == 0 and not f.op.ledger.snapshot()["submissions"]


@pytest.mark.parametrize("runtime_mode", ["new", "resume", "corrupt_marker"])
@pytest.mark.parametrize("work_floor", [D(10), D(20)])
def test_json_bridge_initializes_real_local_runtime_and_holds_exact_approval(tmp_path, monkeypatch, runtime_mode, work_floor):
    """Exercise runner wiring with local-only host; invalid approval cannot send."""
    import io
    import json
    import scripts.run_demo_pilot as bridge

    fake = PilotHost()
    emitted = []
    calls = []
    eligibility = {"currency": "USD", "notFoundInstrumentIds": [], "notFoundSymbols": [],
        "eligibilities": [{"instrumentId": 1001, "symbol": "AAPL", "allowOpenPosition": True,
            "allowedOrderQuantityType": "all", "tradeUnitType": "units", "minPositionExposure": "10",
            "maxUnitsPerOrder": "1000", "unitsQuantityType": "fractional", "leverageConfigs": [{
                "direction": "long", "settlementType": "real", "leverageValues": [1],
                "isPotential": False, "minPositionAmount": "10"}]}]}
    class LocalHost:
        def __init__(self, root):
            self.directory = root / "synthetic-captures"
            self.directory.mkdir()
        def usage(self):
            calls.append("usage")
            return UsageAdmission(NOW, (UsageWindow("weekly", D(24) if work_floor == D(10) else D(50), primary=True),),
                ("weekly",), "synthetic-tool-evidence", "a" * 64)
        def read(self, path, **kwargs):
            calls.append(path)
            if path == "/api/v2/market-data/instruments":
                return {"result": {"results": [
                    {"instrumentId": 1001, "symbol": "AAPL", "type": "Stocks"},
                    {"instrumentId": 1002, "symbol": "MSFT", "type": "Stocks"}],
                    "pagination": {"hasNext": False}}}
            if path == "/api/v2/trading/info/demo/eligibility":
                return {"result": eligibility}
            pytest.fail(f"unexpected source read {path}")
        def account(self, expected):
            assert expected == "123"
            calls.append("account")
            return account_at(fake.now)
        def market(self, instrument):
            calls.append("market")
            return QuoteEvidence("synthetic-bridge-quote", instrument.internal_instrument_id,
                fake.now - timedelta(seconds=24), D(100), D(100), True), {"received_at":fake.now.isoformat()}
        def profile_callback(self, args):
            return fake.profile(args)
        def call(self, tool, arguments):
            calls.append(tool)
            assert tool == "etoro_prepare_trade", "unapproved bridge must not place an order"
            # A seven-second prepare would expire a previously captured rate.
            # The final market read must occur after that operation.
            fake.now += timedelta(seconds=7)
            return {"result": fake.prepare(arguments)}

    monkeypatch.setattr(bridge, "Host", LocalHost)
    monkeypatch.setattr(bridge, "NOW", lambda: fake.now)
    monkeypatch.setattr(bridge, "INSTRUMENT", 1001)
    monkeypatch.setattr(bridge, "SYMBOL", "AAPL")
    monkeypatch.setattr(bridge, "CLASSIFICATIONS", ((1001, "AAPL", "Apple", "technology", "equity"),
        (1002, "MSFT", "Microsoft", "technology", "equity")))
    monkeypatch.setattr(bridge, "emit", emitted.append)
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO("{}\n"))
    identity = tmp_path / "synthetic-identity.json"
    identity.write_text(json.dumps(sources()[0]))
    runtime = tmp_path / "runtime"
    if runtime_mode != "new":
        runtime.mkdir()
        previous = PilotLedger.create(runtime / "pilot.sqlite3", account_at(NOW), [], NOW)
        marker = {"policy": "demo-pilot-v1", "account_id": "123",
            "inception_hash": payload_hash(previous.snapshot()["inception"])}
        if runtime_mode == "corrupt_marker":
            marker["inception_hash"] = "0" * 64
        (runtime / "pilot-inception.json").write_text(json.dumps(marker))
    expected_error = "pilot_inception_marker_mismatch" if runtime_mode == "corrupt_marker" else "exact_human_approval_required"
    with pytest.raises(ValueError, match=expected_error):
        bridge.run(SimpleNamespace(runtime=runtime, identity_receipt=identity, recover=None,
            initialize=runtime_mode == "new", work_floor_percent=work_floor,
            usage_authorization="synthetic-explicit-user-floor"))
    if runtime_mode == "corrupt_marker":
        assert not emitted and fake.calls["place"] == 0
        assert not PilotLedger(runtime / "pilot.sqlite3", expected_account_id="123").snapshot()["submissions"]
        return
    assert len(emitted) == 1 and emitted[0]["status"] == "AWAITING EXACT-ORDER APPROVAL"
    assert emitted[0]["confirmation"]["amount"] == "10"
    assert emitted[0]["limits"]["order"] == 100
    assert emitted[0]["limits"]["usage_floor_percent"] == work_floor
    assert emitted[0]["limits"]["recovery_reserve_percent"] == D(5)
    snapshot = PilotLedger(runtime / "pilot.sqlite3", expected_account_id="123").snapshot()
    assert snapshot["position_count"] == 0 and not snapshot["submissions"]
    marker = json.loads((runtime / "pilot-inception.json").read_text())
    assert marker["inception_hash"] == payload_hash(snapshot["inception"])
    commands = ExecutionStore(runtime / "execution.sqlite3").list_commands()
    assert len(commands) == 1 and commands[0].state == CommandState.AWAITING_APPROVAL
    assert "etoro_place_trade" not in calls and fake.calls["place"] == 0
    assert not (runtime / "kill-switch.json").exists()


@pytest.mark.parametrize("floor,reference", [(D(10), None), (D(10), " "), (D(9), "authorized"), (10.0, "authorized")])
def test_bridge_rejects_unapproved_or_unsupported_floor_before_any_host_access(monkeypatch, floor, reference):
    import scripts.run_demo_pilot as bridge
    monkeypatch.setattr(bridge, "Host", lambda _: pytest.fail("invalid profile must not access host"))
    with pytest.raises(ValueError, match="pilot_usage"):
        bridge.run(SimpleNamespace(work_floor_percent=floor, usage_authorization=reference))


def test_pilot_usage_profile_preserves_original_source_and_unknown_windows():
    import scripts.run_demo_pilot as bridge
    raw = UsageAdmission(NOW, (UsageWindow("weekly", D(24), True), UsageWindow("extra", None)),
        ("weekly", "extra"), "synthetic-tool", "a" * 64)
    selected = bridge.pilot_usage(SimpleNamespace(usage=lambda: raw),
        SimpleNamespace(work_floor_percent=D(10), usage_authorization="synthetic-user-instruction"))
    assert selected.observed_at == raw.observed_at and selected.source_sha256 == raw.source_sha256
    assert selected.source_reference == raw.source_reference and selected.windows == raw.windows
    assert selected.available_window_ids == raw.available_window_ids
    assert selected.authorization_reference == "synthetic-user-instruction"
    assert "usage_window_allowance_unknown" in selected.validate_for_submission(NOW)


@pytest.mark.parametrize("existing", [None, "pilot-inception.json", "execution.sqlite3", "connector.sqlite3",
    "operator.sqlite3", "kill-switch.json", "kill-switch.jsonl"])
def test_bridge_missing_ledger_requires_explicit_first_initialization_and_no_prior_state(tmp_path, monkeypatch, existing):
    import scripts.run_demo_pilot as bridge
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    if existing is not None:
        (runtime / existing).write_text("synthetic-existing-state")
    monkeypatch.setattr(bridge, "Host", lambda _: pytest.fail("state gate must precede any host requests"))
    with pytest.raises(ValueError, match="pilot_ledger_missing"):
        bridge.run(SimpleNamespace(runtime=runtime, identity_receipt=tmp_path / "unused",
            recover=None, initialize=existing is not None))
    assert not (runtime / "pilot.sqlite3").exists()


@pytest.mark.parametrize("buckets", [{"primary_missing": {"secondary": {"usedPercent": 10}}},
    {"known": {"primary": {"usedPercent": 10}}, "unknown": {"primary": None}}])
def test_bridge_missing_available_primary_usage_remains_unknown(tmp_path, monkeypatch, buckets):
    import scripts.run_demo_pilot as bridge
    host = bridge.Host(tmp_path)
    host.call = lambda tool, arguments: {"request_started_at": NOW.isoformat(),
        "response_received_at": NOW.isoformat(), "result": {"rateLimitsByLimitId": buckets}}
    usage = host.usage()
    assert usage.minimum_remaining_percent is None
    assert "usage_window_allowance_unknown" in usage.validate_for_submission(NOW)


@pytest.mark.parametrize("seconds,accepted", [(10, True), (10.000001, False)])
def test_bridge_market_freshness_uses_bounded_request_start(tmp_path, monkeypatch, seconds, accepted):
    import scripts.run_demo_pilot as bridge
    monkeypatch.setattr(bridge, "NOW", lambda: NOW)
    monkeypatch.setattr(bridge, "INSTRUMENT", 1001)
    monkeypatch.setattr(bridge, "SYMBOL", "AAPL")
    host = bridge.Host(tmp_path)
    start = NOW - timedelta(seconds=seconds)
    def read(path, **kwargs):
        if path == "/api/v1/market-data/search":
            return {"request_started_at": start.isoformat(), "response_received_at": NOW.isoformat(),
                "result": {"totalItems": 1, "items": [{"instrumentId": 1001, "internalSymbolFull": "AAPL",
                    "isCurrentlyTradable": True, "isExchangeOpen": True, "isActiveInPlatform": True,
                    "isBuyEnabled": True, "isDelisted": False}]}}
        assert path == "/api/v2/market-data/rates"
        return {"result": {"results": [{"instrumentId": 1001, "quoteType": "realtime",
            "bid": "100", "ask": "100", "date": NOW.replace(tzinfo=None).isoformat()}]}}
    host.read = read
    instrument = Instrument.create(canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity",
        currency="USD", sector="technology")
    if accepted:
        quote, state = host.market(instrument)
        assert state["received_at"] == start.isoformat()
        assert state["clock_basis"] == "bounded_market_request_start"
        assert quote.observed_at == NOW
    else:
        with pytest.raises(ValueError, match="market_state_request_duration"):
            host.market(instrument)


@pytest.mark.parametrize("tool", ["etoro_prepare_trade", "etoro_place_trade"])
def test_bridge_recursively_redacts_tokens_without_rounding_raw_json_money(tmp_path, monkeypatch, tool):
    import io
    import json
    import scripts.run_demo_pilot as bridge
    precise = "123456789.12345678912345678912345678900"
    nested = '{"confirmationToken":"SYNTHETIC-INNER-TOKEN","money":'+precise+'}'
    body = json.dumps({"token": "SYNTHETIC-OUTER-TOKEN", "nested": nested})
    raw = {"content": [{"type": "text", "text": json.dumps({"body": body})}]}
    monkeypatch.setattr(bridge.sys, "stdin", io.StringIO(json.dumps(raw) + "\n"))
    monkeypatch.setattr(bridge, "NOW", lambda: NOW)
    monkeypatch.setattr(bridge, "emit", lambda _: None)
    host = bridge.Host(tmp_path)
    arguments = {"token": "SYNTHETIC-ARGUMENT-TOKEN"} if tool == "etoro_place_trade" else {"account": "demo"}
    returned = host.call(tool, arguments)
    saved = next(host.directory.glob("*.json")).read_text()
    assert "SYNTHETIC-INNER-TOKEN" not in saved and "SYNTHETIC-OUTER-TOKEN" not in saved
    assert "SYNTHETIC-ARGUMENT-TOKEN" not in saved
    assert precise in saved and "REDACTED_IN_MEMORY_ONLY" in saved
    assert returned["result"] == raw


@pytest.mark.parametrize("broker_status", [1, 4])
def test_bridge_recovery_looks_up_before_account_capture_and_keeps_nonfilled_hold(tmp_path, monkeypatch, broker_status):
    import json
    import scripts.run_demo_pilot as bridge

    f = workflow(tmp_path)
    approve(f)
    submit(f)
    before = f.op.ledger.snapshot()
    f.host.now = NOW + timedelta(seconds=10)
    emitted, calls = [], []
    class RecoveryHost:
        def __init__(self, root):
            self.directory = root / "synthetic-recovery"
        def usage(self):
            return UsageAdmission(f.host.now, (UsageWindow("weekly", D(50), primary=True),),
                ("weekly",), "synthetic-tool-evidence", "a" * 64)
        def read(self, path, **kwargs):
            calls.append(path)
            if path == "/api/v2/market-data/instruments":
                return {"result": deepcopy(f.op.catalog.evidence["metadata"])}
            assert path == "/api/v2/trading/info/demo/orders:lookup"
            result = f.host.lookup(kwargs["query"])
            result["status"] = {"id": broker_status}
            result["positionExecutions"] = []
            return {"result": result}
        def profile_callback(self, args):
            calls.append("profile")
            return f.host.profile(args)
        def account(self, expected):
            pytest.fail("a nonfilled recovery must not attempt an empty-pending account capture")
        def call(self, tool, arguments):
            pytest.fail("recovery must never prepare or place an order")

    monkeypatch.setattr(bridge, "Host", RecoveryHost)
    monkeypatch.setattr(bridge, "NOW", lambda: f.host.now)
    monkeypatch.setattr(bridge, "INSTRUMENT", 1001)
    monkeypatch.setattr(bridge, "SYMBOL", "AAPL")
    monkeypatch.setattr(bridge, "CLASSIFICATIONS", ((1001, "AAPL", "Apple", "technology", "equity"),
        (1002, "MSFT", "Microsoft", "technology", "equity")))
    monkeypatch.setattr(bridge, "uuid5", lambda namespace, name: f.intent.internal_instrument_id)
    monkeypatch.setattr(bridge, "emit", emitted.append)
    identity = tmp_path / "synthetic-identity.json"
    identity.write_text(json.dumps(sources()[0]))
    marker = {"policy": "demo-pilot-v1", "account_id": "123", "inception_hash": payload_hash(before["inception"])}
    (tmp_path / "pilot-inception.json").write_text(json.dumps(marker))
    bridge.run(SimpleNamespace(runtime=tmp_path, identity_receipt=identity,
        recover=str(f.command.command_id), initialize=False))
    assert len(emitted) == 1 and emitted[0]["event"] == "lookup_hold"
    assert emitted[0]["recovery"] == "lookup_only_no_resubmission"
    assert calls == ["/api/v2/market-data/instruments", "profile", "/api/v2/trading/info/demo/orders:lookup"]
    after = f.op.ledger.snapshot()
    assert after == before and after["unresolved"]
    assert f.host.calls["place"] == 1 and f.host.calls["lookup"] == 1
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.ACKNOWLEDGED


@pytest.mark.parametrize("stamp,valid", [("2026-10-26T14:00:00", True),
    ("2026-10-26T15:00:00+01:00", True), ("2026-10-26T14:00:00-00:00", False),
    ("2026-10-26T14:00:00.0000001Z", False), ("2026-10-26T13:59:29.9999999Z", False)])
def test_bridge_rate_time_only_applies_documented_utc_semantics_with_exact_bounds(monkeypatch, stamp, valid):
    import scripts.run_demo_pilot as bridge
    monkeypatch.setattr(bridge, "NOW", lambda: NOW)
    if valid:
        assert bridge.rate_time(stamp) == NOW
    else:
        with pytest.raises(ValueError):
            bridge.rate_time(stamp)
