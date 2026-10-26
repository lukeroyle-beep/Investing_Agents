from __future__ import annotations

import csv
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from brokers.etoro.connector import ConnectorBindingStore, ConnectorEvidenceError, EtoroConnectorAdapter, response_object
from execution.accounting import AccountValuation, HistoryCoverage, REQUIRED_EVENT_KINDS, evaluate_performance
from execution.connector_operator import ConnectorDemoOperator
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent, SizingMethod
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.operator import OperatorEvidenceError, OperatorEvidenceStore
from execution.store import ExecutionStore
from execution.usage_reserve import UsageAdmission, UsageWindow
from risk.approval import ApprovalError
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import AccountEvidence, QuoteEvidence, RiskLimits
from risk.submission_gate import GovernanceWritePolicy, SubmissionBlockedError
from shared.paths import RISK_CONFIG_PATH

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
D = Decimal


def account_at(now, *, equity=D("10000"), snapshot="account"):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    def valuation(sid, when, value):
        return AccountValuation("123", sid, "USD", value, D(0), D(0), value, D(0), D(0),
                                "synthetic-reconciled", "synthetic-time", valuation_at=when)
    opening, current = valuation("opening", midnight, D("10000")), valuation(snapshot, now, equity)
    history = HistoryCoverage("123", "USD", midnight, now, "0", "1", "synthetic-history",
                              "synthetic-complete-contract", "synthetic-hash", (), REQUIRED_EVENT_KINDS,
                              True, True, True)
    performance = evaluate_performance(opening=opening, current=current, history=history, now=now)
    return AccountEvidence(snapshot, Environment.DEMO, now, "USD", equity, equity,
        None, None, (), (), True, contract_version="account-risk-v2", account_id="123",
        performance=performance, invested_amount=D(0), unrealised_pnl=D(0),
        estimated_order_costs=D("0.01"), positions_complete=True)


def workflow(tmp_path):
    now = [NOW]
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = Instrument.create(canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity",
                                   currency="USD", sector="technology")
    registry.register(instrument)
    registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument, broker="etoro",
        environment="demo", broker_instrument_id=1001, exact_match_symbol="AAPL", metadata={"source": "synthetic"}, resolved_at=NOW))
    coordinator = ExecutionCoordinator(store=store, registry=registry,
        config=ExecutionConfig("1.0", True, True, "etoro_demo_manual", Environment.DEMO,
                               True, ("equity", "etf"), ("buy",), D(1)))
    operator = ConnectorDemoOperator(coordinator=coordinator, limits=RiskLimits.load(RISK_CONFIG_PATH),
        evidence_store=OperatorEvidenceStore(tmp_path / "operator.sqlite3"), staged_fills_path=tmp_path / "fills.csv")
    intent = OrderIntent.create(strategy_id="synthetic", run_id="run", internal_instrument_id=instrument.internal_instrument_id,
        environment="demo", side="buy", order_type="market", sizing_method="fixed_notional", sizing_value="10",
        currency="USD", expires_at=NOW + timedelta(minutes=5))
    quote = QuoteEvidence("quote", instrument.internal_instrument_id, NOW, D("99.9"), D(100), True)
    account = replace(account_at(NOW), issuer_by_instrument=((str(instrument.internal_instrument_id), "Apple"),))
    command = operator.prepare(intent=intent, account=account, quote=quote,
                               source_kind=operator.source_kind, now=NOW)
    assert command.state == CommandState.AWAITING_APPROVAL
    preview = {"account": "demo", "verdict": "ready", "token": "synthetic-secret-token",
        "expiresAt": (NOW + timedelta(minutes=2)).isoformat(), "warnings": [{"code": "W8BEN"}],
        "confirmation": {"market": {"instrumentId": 1001, "symbol": "AAPL"}, "direction": "buy", "orderType": "mkt",
            "amount": 10, "leverage": 1, "estimatedUnits": "0.1", "quote": {"ask": "100", "bid": "99.9", "asOf": NOW.isoformat()},
            "totalUpfrontCost": "0.01", "costs": [{"type": "transactionFee", "value": "0.01", "currency": "USD"}],
            "balance": {"availableCash": "10000", "required": "10.01", "sufficient": True}}}
    eligibility = {"currency": "USD", "notFoundInstrumentIds": [], "notFoundSymbols": [], "eligibilities": [{
        "instrumentId": 1001, "symbol": "AAPL", "allowOpenPosition": True,
        "allowedOrderQuantityType": "all", "tradeUnitType": "units", "minPositionExposure": "10",
        "maxUnitsPerOrder": "5945", "unitsQuantityType": "fractional", "leverageConfigs": [{
            "direction": "long", "settlementType": "real", "leverageValues": [1], "isPotential": False, "minPositionAmount": "10"}]}]}
    calls = {"place": 0, "lookup": 0}
    outcome = {"outcome": "executed", "orderId": 42, "referenceId": "00000000-0000-4000-8000-000000000042"}
    lookup = {"accountId": 123, "orderId": 42, "action": "open", "transaction": "buy", "type": "mkt",
        "asset": {"instrumentId": 1001, "symbol": "AAPL", "side": "long", "settlementType": "real", "leverage": 1, "currency": "USD"},
        "orderCurrency": "USD", "requestedAmount": "10", "status": {"id": 3}, "positionExecutions": [{
            "positionId": 500, "state": "open", "investedAmountCurrency": "10", "addedFunds": "0", "remainingUnits": "0.1",
            "openingData": {"orderId": 42, "units": "0.1", "avgPrice": "100",
                "avgConversionRate": "1", "executionTime": (NOW + timedelta(seconds=1)).isoformat(), "priceId": 700,
                "fees": "0.01", "taxes": "0"}}]}
    def place(args):
        assert args == {"token": "synthetic-secret-token"}
        calls["place"] += 1
        if isinstance(outcome.get("error"), Exception):
            raise outcome["error"]
        return deepcopy(outcome)
    def read(query):
        calls["lookup"] += 1
        assert query == {"orderId": "42"}
        return deepcopy(lookup)
    adapter = EtoroConnectorAdapter(bindings=ConnectorBindingStore(tmp_path / "connector.sqlite3"),
        expected_account_id="123", prepare_trade=lambda args: deepcopy(preview), place_trade=place,
        lookup_order=read, clock=lambda: now[0], get_profile=lambda args: {"statusCode": 200,
            "profile": {"demoCid": 123, "scopes": ["etoro-public:trade.demo:write"]}})
    bound = operator.bind_preview(command.command_id, adapter=adapter, preview=preview, eligibility=eligibility, now=NOW)
    return operator, command, adapter, quote, now, calls, outcome, lookup, preview, eligibility, bound


def approve(items):
    op, cmd, adapter, _, _, _, _, _, _, _, bound = items
    return op.approve(cmd.command_id, adapter=adapter, confirmation_hash=bound["confirmation_hash"],
                      approver="Luke", human_confirmed=True, now=NOW)


def submit(items, *, account=None, market_changes=None, usage_percent=D("50")):
    op, cmd, adapter, quote, now, *_ = items
    quote = replace(quote, observed_at=now[0])
    market = dict(instrument_id=1001, quote_type="realtime", quote_id=quote.quote_id,
        received_at=now[0].isoformat(), isOpen=True, isCurrentlyTradable=True,
        isExchangeOpen=True, isActiveInPlatform=True, isBuyEnabled=True, isDelisted=False)
    market.update(market_changes or {})
    account = account or account_at(now[0], snapshot="fresh")
    account = replace(account, issuer_by_instrument=((str(quote.internal_instrument_id), "Apple"),))
    return op.submit(cmd.command_id, adapter=adapter, account=account, quote=quote,
        market_state=market, kill_switch=KillSwitchStatus(False, True, Environment.DEMO, 1, "synthetic", now[0]),
        governance=GovernanceWritePolicy("demo_manual", True, True, True), now=now[0],
        usage=UsageAdmission(now[0], (UsageWindow("weekly", usage_percent, primary=True),), ("weekly",), "synthetic-usage", "a" * 64))


def test_connector_full_cycle_stages_once_without_economic_writes(tmp_path):
    items = workflow(tmp_path)
    op, cmd, adapter, _, now, calls, *_ = items
    approval = approve(items)
    assert approval.expires_at == NOW + timedelta(minutes=2)
    assert b"synthetic-secret-token" not in (tmp_path / "connector.sqlite3").read_bytes()
    assert submit(items).state == CommandState.ACKNOWLEDGED
    now[0] += timedelta(seconds=2)
    receipt = op.reconcile(cmd.command_id, adapter=adapter, now=now[0])
    assert receipt.staged_execution_ids == ("order:42:position:500:open",)
    assert receipt.broker_terminal and not receipt.economic_reconciliation_verified and not receipt.qualification_credit
    op.reconcile(cmd.command_id, adapter=adapter, now=now[0])
    with op.staged_fills_path.open() as f:
        assert len(list(csv.DictReader(f))) == 1
    assert calls["place"] == 1
    assert not (tmp_path / "portfolio_state.csv").exists()


def test_refresh_after_human_delay_preserves_original_approval(tmp_path):
    items = workflow(tmp_path)
    original = approve(items)
    items[4][0] += timedelta(seconds=40)
    assert submit(items).state == CommandState.ACKNOWLEDGED
    persisted = items[0].coordinator.store.get_command_approval(items[1].command_id)
    assert persisted == original
    refresh = items[0].evidence.read_observations(items[1].command_id)[0]
    assert refresh["pre_submit_refresh"]["account"]["snapshot_id"] == "fresh"


@pytest.mark.parametrize("market_changes", [{"quote_type": "delayed"}, {"isExchangeOpen": False}, {"isBuyEnabled": False}])
def test_closed_delayed_or_disabled_market_cannot_submit(tmp_path, market_changes):
    items = workflow(tmp_path)
    approve(items)
    with pytest.raises(SubmissionBlockedError, match="market"):
        submit(items, market_changes=market_changes)
    assert items[5]["place"] == 0


def test_unknown_without_connector_reference_never_invents_lookup_or_resends(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    items[6]["error"] = TimeoutError("lost response")
    with pytest.raises(ConnectorEvidenceError, match="placement_response_unavailable"):
        submit(items)
    op, cmd, adapter = items[:3]
    assert op.coordinator.store.get_command(cmd.command_id).state == CommandState.UNKNOWN
    with pytest.raises(ConnectorEvidenceError, match="recovery_handle_unavailable"):
        op.reconcile(cmd.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    adapter.arm_token(cmd.command_id, "synthetic-secret-token")
    with pytest.raises((SubmissionBlockedError, ConnectorEvidenceError)):
        submit(items)
    assert items[5] == {"place": 1, "lookup": 0}


@pytest.mark.parametrize("status", [5, 9, 10])
def test_partial_cumulative_positions_are_not_staged_as_new_fill_events(tmp_path, status):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    items[7]["status"]["id"] = status
    receipt = items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    assert not receipt.broker_terminal and not receipt.staged_execution_ids
    with items[0].staged_fills_path.open() as f:
        assert list(csv.DictReader(f)) == []


@pytest.mark.parametrize("field", ["fees", "taxes", "avgConversionRate"])
def test_missing_fill_cost_or_conversion_is_unknown_not_zero(tmp_path, field):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    del items[7]["positionExecutions"][0]["openingData"][field]
    with pytest.raises(ConnectorEvidenceError, match="unknown"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    assert not items[0].staged_fills_path.exists()


def test_account_and_approved_lookup_mismatch_never_stage(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    items[7]["accountId"] = 999
    with pytest.raises(ConnectorEvidenceError, match="mismatch"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))


def test_expired_preview_or_unconfirmed_warning_never_sends(tmp_path):
    items = workflow(tmp_path)
    with pytest.raises(ApprovalError, match="exact_preview"):
        items[0].approve(items[1].command_id, adapter=items[2], confirmation_hash="wrong",
                         approver="Luke", human_confirmed=True, now=NOW)
    approve(items)
    items[4][0] += timedelta(minutes=2)
    with pytest.raises(ConnectorEvidenceError, match="expired"):
        submit(items)
    assert items[5]["place"] == 0


def test_fresh_account_loss_blocks_previously_approved_order(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    with pytest.raises(SubmissionBlockedError, match="risk_rejected"):
        submit(items, account=account_at(NOW, equity=D("9800"), snapshot="loss"))
    assert items[5]["place"] == 0


def test_raw_json_precision_and_http_fail_closed():
    assert response_object('{"number":0.123456789012345678901234567890}')["number"] == D("0.123456789012345678901234567890")
    with pytest.raises(ConnectorEvidenceError):
        response_object({"statusCode": 403, "body": "{}"})


def test_seventh_execution_fraction_is_preserved_and_staged(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    source = "2026-10-26T15:00:01.1234567Z"
    items[7]["positionExecutions"][0]["openingData"]["executionTime"] = source
    result = items[2].reconcile_command(items[0].coordinator.store.get_command(items[1].command_id))
    assert result.executions[0].executed_at_source == source
    assert result.executions[0].executed_at.microsecond == 123456
    items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    with items[0].staged_fills_path.open() as handle:
        assert next(csv.DictReader(handle))["fill_timestamp"] == source


def test_execution_one_seventh_digit_after_now_cannot_stage(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    items[7]["positionExecutions"][0]["openingData"]["executionTime"] = "2026-10-26T15:00:02.0000001Z"
    with pytest.raises(OperatorEvidenceError, match="outside the command lifetime"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    assert not items[0].staged_fills_path.exists()


def test_execution_unrepresentable_source_precision_is_not_truncated(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    items[7]["positionExecutions"][0]["openingData"]["executionTime"] = "2026-10-26T15:00:01.12345678Z"
    with pytest.raises(ConnectorEvidenceError, match="precision_required"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))


def test_connector_operator_rejects_execution_without_original_source_time(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    original = items[2].reconcile_command
    def missing_source(command):
        result = original(command)
        return replace(result, executions=tuple(replace(item, executed_at_source=None) for item in result.executions))
    items[2].reconcile_command = missing_source
    with pytest.raises(OperatorEvidenceError, match="original_connector_execution_source"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    assert not items[0].staged_fills_path.exists()


def test_connector_store_blocks_repeat_place_after_process_restart(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    op, command, adapter = items[:3]
    current = op.coordinator.store.get_command(command.command_id)
    restarted = EtoroConnectorAdapter(bindings=ConnectorBindingStore(tmp_path / "connector.sqlite3"),
        expected_account_id="123", prepare_trade=adapter.prepare_trade, place_trade=adapter.place_trade,
        lookup_order=adapter.lookup_order, get_profile=adapter.get_profile, clock=lambda: NOW)
    restarted.arm_token(command.command_id, "synthetic-secret-token")
    intent = op.coordinator.store.get_intent(command.intent_hash)
    with pytest.raises(ConnectorEvidenceError, match="already_dispatched"):
        restarted.submit_order(intent, current)
    assert items[5]["place"] == 1


def test_delay_after_fresh_checks_cannot_dispatch_stale_evidence(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    items[2].clock = lambda: NOW + timedelta(seconds=16)
    with pytest.raises(ConnectorEvidenceError, match="authorization_expired"):
        submit(items)
    assert items[5]["place"] == 0
    assert items[0].coordinator.store.get_command(items[1].command_id).state == CommandState.UNKNOWN


@pytest.mark.parametrize("profile", [
    {"statusCode": 200, "profile": {"demoCid": 999, "scopes": ["etoro-public:trade.demo:write"]}},
    {"statusCode": 200, "profile": {"demoCid": 123, "scopes": ["etoro-public:trade.real:write"]}},
    {"statusCode": 401, "profile": {"demoCid": 123, "scopes": ["etoro-public:trade.demo:write"]}},
])
def test_changed_connection_identity_or_scope_cannot_dispatch(tmp_path, profile):
    items = workflow(tmp_path)
    approve(items)
    items[2].get_profile = lambda args: profile
    with pytest.raises(ConnectorEvidenceError, match="identity_or_write_scope"):
        submit(items)
    assert items[5]["place"] == 0


def test_profile_delay_rechecks_deadline_and_does_not_retain_personal_fields(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    def profile(args):
        items[4][0] += timedelta(seconds=16)
        return {"statusCode": 200, "profile": {"demoCid": 123,
            "firstName": "PRIVATE-NAME-MARKER", "dateOfBirth": "PRIVATE-DATE-MARKER",
            "scopes": ["etoro-public:trade.demo:write"]}}
    items[2].get_profile = profile
    with pytest.raises(ConnectorEvidenceError, match="authorization_expired"):
        submit(items)
    assert items[5]["place"] == 0
    data = (tmp_path / "connector.sqlite3").read_bytes()
    assert b"PRIVATE-NAME-MARKER" not in data and b"PRIVATE-DATE-MARKER" not in data


def test_profile_identity_is_checked_before_requesting_preview(tmp_path):
    items = workflow(tmp_path)
    adapter = items[2]
    adapter.get_profile = lambda args: {"statusCode": 200, "profile": {"demoCid": 999, "scopes": []}}
    adapter.prepare_trade = lambda args: pytest.fail("preview must not be requested")
    intent = items[0].coordinator.store.get_intent(items[1].intent_hash)
    with pytest.raises(ConnectorEvidenceError, match="identity_or_write_scope"):
        adapter.preview(intent=intent, instrument_id=1001)


@pytest.mark.parametrize("field", ["investedAmountCurrency", "addedFunds", "remainingUnits"])
def test_missing_principal_or_unchanged_position_proof_cannot_stage(tmp_path, field):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    del items[7]["positionExecutions"][0][field]
    with pytest.raises(ConnectorEvidenceError, match="unknown"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    assert not items[0].staged_fills_path.exists()


def test_same_stable_position_identity_with_changed_totals_is_rejected(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    before = items[0].staged_fills_path.read_bytes()
    items[7]["positionExecutions"][0]["openingData"]["fees"] = "0.02"
    with pytest.raises(OperatorEvidenceError, match="contradictory"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=3))
    assert items[0].staged_fills_path.read_bytes() == before


def test_ambiguous_x1_settlement_is_never_prepared_for_approval(tmp_path):
    items = workflow(tmp_path)
    op, cmd, adapter, _, _, calls, _, _, preview, eligibility, _ = items
    eligibility["eligibilities"][0]["leverageConfigs"].append({
        "direction": "long", "settlementType": "cfd", "leverageValues": [1], "isPotential": False, "minPositionAmount": "10"})
    with pytest.raises(ConnectorEvidenceError, match="unambiguous"):
        op.bind_preview(cmd.command_id, adapter=adapter, preview=preview, eligibility=eligibility, now=NOW)
    assert calls["place"] == 0


@pytest.mark.parametrize("remaining", [D("20"), D("24.99"), None])
def test_usage_floor_reserves_recovery_before_any_send(tmp_path, remaining):
    items = workflow(tmp_path)
    approve(items)
    with pytest.raises(SubmissionBlockedError, match="usage_floor"):
        submit(items, usage_percent=remaining)
    assert items[5]["place"] == 0


def test_crash_after_durable_response_before_ack_recovers_actual_reference(tmp_path, monkeypatch):
    items = workflow(tmp_path)
    approve(items)
    op, cmd, adapter = items[:3]
    transition = op.coordinator.store.transition_command
    def crash(*args, **kwargs):
        if len(args) > 1 and args[1] == CommandState.ACKNOWLEDGED:
            raise OSError("synthetic crash after response")
        return transition(*args, **kwargs)
    monkeypatch.setattr(op.coordinator.store, "transition_command", crash)
    with pytest.raises(OSError, match="synthetic crash"):
        submit(items)
    assert op.coordinator.store.get_command(cmd.command_id).broker_reference_id is None
    monkeypatch.setattr(op.coordinator.store, "transition_command", transition)
    receipt = op.reconcile(cmd.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    assert receipt.broker_terminal
    assert op.coordinator.store.get_command(cmd.command_id).broker_reference_id == items[6]["referenceId"]
    assert items[5] == {"place": 1, "lookup": 1}


def test_placement_exception_cannot_echo_secret_token(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    items[6]["error"] = ValueError("request token=synthetic-secret-token")
    with pytest.raises(ConnectorEvidenceError) as caught:
        submit(items)
    assert "synthetic-secret-token" not in str(caught.value)
    assert b"synthetic-secret-token" not in (tmp_path / "connector.sqlite3").read_bytes()


def test_market_state_deadline_is_rechecked_at_actual_dispatch(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    items[2].clock = lambda: NOW + timedelta(seconds=2)
    with pytest.raises(ConnectorEvidenceError, match="authorization_expired"):
        submit(items, market_changes={"received_at": (NOW - timedelta(seconds=14)).isoformat()})
    assert items[5]["place"] == 0


def test_wrong_environment_response_cannot_select_a_recovery_lookup(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    items[6]["account"] = "real"
    with pytest.raises(ConnectorEvidenceError, match="environment_mismatch"):
        submit(items)
    with pytest.raises(ConnectorEvidenceError, match="environment_mismatch"):
        items[0].reconcile(items[1].command_id, adapter=items[2], now=NOW + timedelta(seconds=2))
    assert items[5]["lookup"] == 0


def test_mcp_raw_json_takes_precedence_over_rounded_structured_duplicate():
    response = response_object({"content": [{"type": "text", "text": '{"x":0.123456789012345678901}'}],
                                "structuredContent": {"x": 0.12345678901234568}})
    assert response["x"] == D("0.123456789012345678901")
    with pytest.raises(ConnectorEvidenceError, match="exact_decimal"):
        response_object({"x": 0.12345678901234568})


def test_non_amount_intent_cannot_be_silently_converted_to_dollars(tmp_path):
    items = workflow(tmp_path)
    intent = items[0].coordinator.store.get_intent(items[1].intent_hash)
    invalid = OrderIntent.create(strategy_id=intent.strategy_id, run_id=intent.run_id,
        internal_instrument_id=intent.internal_instrument_id, environment=intent.environment,
        side=intent.side, order_type=intent.order_type, sizing_method=SizingMethod.UNITS,
        sizing_value=intent.sizing_value, currency=intent.currency, expires_at=intent.expires_at)
    with pytest.raises(ConnectorEvidenceError, match="fixed_notional"):
        items[2].preview(intent=invalid, instrument_id=1001)


def test_one_preview_token_cannot_bind_two_different_commands(tmp_path):
    items = workflow(tmp_path)
    bound = items[10]
    with pytest.raises(ConnectorEvidenceError, match="another_command"):
        items[2].bindings.bind(uuid4(), bound)


def test_equivalent_decimal_spellings_do_not_duplicate_or_conflict(tmp_path):
    items = workflow(tmp_path)
    approve(items)
    submit(items)
    op, cmd, adapter = items[:3]
    op.reconcile(cmd.command_id, adapter=adapter, now=NOW + timedelta(seconds=2))
    before = op.staged_fills_path.read_bytes()
    opening = items[7]["positionExecutions"][0]["openingData"]
    opening.update(units="0.10", avgPrice="100.00", fees="0.010", taxes="0.000")
    op.reconcile(cmd.command_id, adapter=adapter, now=NOW + timedelta(seconds=3))
    assert op.staged_fills_path.read_bytes() == before
