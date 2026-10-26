"""Offline connector lifecycle and exact-decimal Fill Agent demonstration.

Every profile, account, cash clock, history, approval, price and execution here
is SYNTHETIC. No network, credentials or real connector tools are used. This
does not resolve the live cash-clock, history, transport or risk blockers.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.fill_agent.broker_runtime import BrokerEconomicRuntime, ReconciledBrokerAccount
from brokers.etoro.account_evidence import (
    ComponentCashObservation, qualify_pnl_component_time_evidence, validate_pnl_account_evidence,
)
from brokers.etoro.connector import ConnectorBindingStore, EtoroConnectorAdapter
from brokers.etoro.pnl_currency_metadata import parse_demo_position_currencies
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS, EtoroDetailedPortfolio, EtoroPnlPortfolio, EtoroSecurity
from execution.account_bridge import component_risk_account
from execution.accounting import (
    AccountValuation, HistoryCoverage, REQUIRED_EVENT_KINDS, evaluate_performance,
    valuation_from_component_time,
)
from execution.connector_operator import ConnectorDemoOperator
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import CommandState, Environment, OrderIntent, canonical_json, payload_hash
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.operator import OperatorEvidenceStore
from execution.store import ExecutionStore
from execution.usage_reserve import UsageAdmission, UsageWindow
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import QuoteEvidence, RiskLimits
from risk.submission_gate import GovernanceWritePolicy

D = Decimal
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)
REFERENCE = "00000000-0000-4000-8000-000000000042"


def synthetic_component_account(now, *, after_fill=False):
    """Build complete *invented* source payloads, parsed by production contracts."""
    cash = "9989.99" if after_fill else "10000"
    positions = [{"positionID": 7001, "CID": 123, "instrumentID": 1002,
        "mirrorID": 0, "units": "0.1", "amount": "10", "isBuy": True,
        "leverage": 1, "settlementTypeID": 1}]
    if after_fill:
        positions.append({**positions[0], "positionID": 500, "instrumentID": 1001})
    detail_raw = {"clientPortfolio": {"credit": cash, "bonusCredit": 0, "mirrors": [],
        **{name: [] for name in DEMO_PENDING_COLLECTIONS}, "positions": positions}}
    pnl_raw = deepcopy(detail_raw)
    pnl_raw["clientPortfolio"].update(accountCurrencyId=1, unrealizedPnL="0")
    for position in pnl_raw["clientPortfolio"]["positions"]:
        position.update(totalFees="0.01" if position["positionID"] == 500 else "0",
                        totalExternalFees="0", totalExternalTaxes="0")
        position["unrealizedPnL"] = {"pnL": "0", "pnlAssetCurrency": "0",
            "exposureInAccountCurrency": "10", "exposureInAssetCurrency": "10",
            "marginInAccountCurrency": "10", "marginInAssetCurrency": "10",
            "marginCurrencyId": 1, "assetCurrencyId": 1, "closeRate": "100",
            "closeConversionRate": "1", "timestamp": now.isoformat()}
    currencies_raw = {"accountCurrency": "USD", "mirrors": [], "instruments": [
        {"instrumentId": p["instrumentID"], "orders": [], "positions": [{
            "positionId": p["positionID"], "instrumentId": p["instrumentID"],
            "assetCurrency": "USD", "direction": "long", "leverage": 1,
            "settlementType": "real", "units": p["units"]}]} for p in positions]}
    detail = EtoroDetailedPortfolio.parse(detail_raw)
    securities = tuple(EtoroSecurity.parse({"instrumentId": p["instrumentID"],
        "symbol": "AAPL" if p["instrumentID"] == 1001 else "MSFT", "type": "Stocks", "exchangeId": 1})
        for p in positions)
    evidence = validate_pnl_account_evidence(first=detail, last=detail,
        pnl=EtoroPnlPortfolio.parse(pnl_raw), securities=securities,
        currencies=parse_demo_position_currencies(currencies_raw, "123", detail.positions),
        expected_demo_customer_id="123", capture_started_at=now, capture_completed_at=now)
    # This endpoint and timestamp are deliberately fictional. Production does
    # not acquire authority merely by passing a local collection timestamp.
    cash_raw = {"source": "SYNTHETIC_ONLY", "cash": cash, "cashObservedAt": now.isoformat()}
    proof = qualify_pnl_component_time_evidence(evidence, assessed_at=now,
        cash_observation=ComponentCashObservation("123", D(cash), now,
            "/api/synthetic/demo/cash", "cashObservedAt", payload_hash(cash_raw), now.isoformat()))
    return proof, detail, {"source_kind": "synthetic_only", "detail": detail_raw,
                          "pnl": pnl_raw, "currencies": currencies_raw, "cash": cash_raw}


class SyntheticConnectorHost:
    """Fixed local callbacks. This class contains no network implementation."""

    def __init__(self, now):
        self.now = now
        self.calls = {"profile": 0, "prepare": 0, "place": 0, "lookup": 0}

    def profile(self, arguments):
        self.calls["profile"] += 1
        return {"statusCode": 200, "profile": {"demoCid": 123,
            "scopes": ["etoro-public:trade.demo:write"]}}

    def prepare(self, arguments):
        assert arguments == {"account": "demo", "instrumentId": 1001, "direction": "buy",
                             "amount": 10.0, "leverage": 1, "orderType": "mkt"}
        self.calls["prepare"] += 1
        return {"account": "demo", "verdict": "ready", "token": "SYNTHETIC-NONSECRET-TOKEN",
            "expiresAt": (self.now + timedelta(minutes=2)).isoformat(), "warnings": [],
            "confirmation": {"market": {"instrumentId": 1001, "symbol": "AAPL"},
                "direction": "buy", "orderType": "mkt", "amount": "10", "leverage": 1,
                "estimatedUnits": "0.1", "quote": {"ask": "100", "bid": "100", "asOf": self.now.isoformat()},
                "totalUpfrontCost": "0.01", "costs": [{"type": "transactionFee", "value": "0.01", "currency": "USD"}],
                "balance": {"availableCash": "10000", "required": "10.01", "sufficient": True}}}

    def place(self, arguments):
        assert arguments == {"token": "SYNTHETIC-NONSECRET-TOKEN"}
        self.calls["place"] += 1
        return {"account": "demo", "outcome": "executed", "orderId": 42, "referenceId": REFERENCE}

    def lookup(self, arguments):
        assert arguments == {"orderId": "42"}
        self.calls["lookup"] += 1
        return {"accountId": 123, "orderId": 42, "action": "open", "transaction": "buy", "type": "mkt",
            "asset": {"instrumentId": 1001, "symbol": "AAPL", "side": "long",
                "settlementType": "real", "leverage": 1, "currency": "USD"},
            "orderCurrency": "USD", "requestedAmount": "10", "status": {"id": 3},
            "positionExecutions": [{"positionId": 500, "state": "open", "investedAmountCurrency": "10",
                "addedFunds": "0", "remainingUnits": "0.1", "openingData": {"orderId": 42,
                    # Rounded broker average price is deliberately not the
                    # authoritative principal used for the cash movement.
                    "units": "0.1", "avgPrice": "99.999999", "avgConversionRate": "1", "priceId": 700,
                    "executionTime": "2026-10-26T15:00:01.1234567Z", "fees": "0.01", "taxes": "0"}}]}


def exercise(output_dir, *, crash_after_response=False):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    control = output / "synthetic_control"
    host = SyntheticConnectorHost(NOW)
    config = ExecutionConfig("1.0", True, True, "etoro_demo_manual", Environment.DEMO,
                             True, ("equity", "etf"), ("buy",), D(1))
    limits = RiskLimits.load(PROJECT_ROOT / "config" / "risk.yaml")

    def operator():
        store = ExecutionStore(control / "execution.sqlite3")
        return ConnectorDemoOperator(coordinator=ExecutionCoordinator(store=store,
            registry=InstrumentRegistry(store), config=config), limits=limits,
            evidence_store=OperatorEvidenceStore(control / "operator.sqlite3"),
            staged_fills_path=output / "staged_executions.csv")

    workflow = operator()
    registry = workflow.coordinator.registry
    instruments = {}
    for broker_id, symbol in ((1001, "AAPL"), (1002, "MSFT")):
        instrument = registry.register(Instrument.create(canonical_symbol=symbol,
            exchange="NASDAQ", asset_type="equity", currency="USD", sector="technology"))
        registry.record_mapping(BrokerInstrumentMapping.from_resolution(instrument=instrument,
            broker="etoro", environment=Environment.DEMO, broker_instrument_id=broker_id,
            exact_match_symbol=symbol, metadata={"source": "synthetic_only"}, resolved_at=NOW))
        instruments[broker_id] = instrument
    pre_proof, pre_detail, pre_sources = synthetic_component_account(NOW)
    (output / "synthetic_pre_sources.json").write_text(canonical_json(pre_sources) + "\n")
    current = valuation_from_component_time(pre_proof, now=NOW, reconciliation_reference="synthetic-reconciled-pre")
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    opening = AccountValuation("123", "synthetic-opening", "USD", D("10000"), D("10"), D(0),
        D("10010"), D("10"), D("10"), "synthetic-reconciled-opening", "synthetic-opening-clock", valuation_at=midnight)
    history = HistoryCoverage("123", "USD", midnight, NOW, "0", "1", "synthetic-history",
        "synthetic-complete-all-categories", payload_hash({"synthetic_events": []}), (), REQUIRED_EVENT_KINDS,
        True, True, True)
    performance = evaluate_performance(opening=opening, current=current, history=history, now=NOW)
    account = component_risk_account(pre_proof, registry=registry,
        instruments_by_broker_id={1002: instruments[1002]}, performance=performance,
        estimated_order_costs=D("0.01"), issuer_by_instrument={
            str(instruments[1001].internal_instrument_id): "Apple",
            str(instruments[1002].internal_instrument_id): "Microsoft"})
    intent = OrderIntent.create(strategy_id="synthetic_only", run_id="synthetic_connector",
        internal_instrument_id=instruments[1001].internal_instrument_id, environment="demo",
        side="buy", order_type="market", sizing_method="fixed_notional", sizing_value="10",
        currency="USD", expires_at=NOW + timedelta(minutes=5))
    quote = QuoteEvidence("synthetic-quote", instruments[1001].internal_instrument_id, NOW, D("100"), D("100"), True)
    command = workflow.prepare(intent=intent, account=account, quote=quote,
        source_kind=workflow.source_kind, now=NOW)
    adapter = EtoroConnectorAdapter(bindings=ConnectorBindingStore(control / "connector.sqlite3"),
        expected_account_id="123", get_profile=host.profile, prepare_trade=host.prepare,
        place_trade=host.place, lookup_order=host.lookup, clock=lambda: host.now)
    eligibility = {"currency": "USD", "notFoundInstrumentIds": [], "notFoundSymbols": [],
        "eligibilities": [{"instrumentId": 1001, "symbol": "AAPL", "allowOpenPosition": True,
            "allowedOrderQuantityType": "all", "tradeUnitType": "units", "minPositionExposure": "10",
            "maxUnitsPerOrder": "1000", "unitsQuantityType": "fractional", "leverageConfigs": [{
                "direction": "long", "settlementType": "real", "leverageValues": [1],
                "isPotential": False, "minPositionAmount": "10"}]}]}
    bound = workflow.bind_preview(command.command_id, adapter=adapter,
        preview=adapter.preview(intent=intent, instrument_id=1001), eligibility=eligibility, now=NOW)
    workflow.approve(command.command_id, adapter=adapter, confirmation_hash=bound["confirmation_hash"],
        approver="SIMULATED_OPERATOR_NOT_A_HUMAN_APPROVAL", human_confirmed=True, now=NOW)
    runtime = BrokerEconomicRuntime.initialize(output / "synthetic_fill_runtime",
        account=ReconciledBrokerAccount.from_component(pre_proof, pre_detail,
            detailed_payload=pre_sources["detail"]), now=NOW)
    transition = workflow.coordinator.store.transition_command
    if crash_after_response:
        def simulated_crash(command_id, state, **kwargs):
            if state == CommandState.ACKNOWLEDGED:
                raise OSError("SYNTHETIC_CRASH_AFTER_DURABLE_RESPONSE")
            return transition(command_id, state, **kwargs)
        workflow.coordinator.store.transition_command = simulated_crash
    submission = dict(adapter=adapter, account=account, quote=quote, now=NOW,
        market_state={"instrument_id": 1001, "quote_type": "realtime", "quote_id": quote.quote_id,
            "received_at": NOW.isoformat(), "isOpen": True, "isCurrentlyTradable": True,
            "isExchangeOpen": True, "isActiveInPlatform": True, "isBuyEnabled": True, "isDelisted": False},
        kill_switch=KillSwitchStatus(False, True, Environment.DEMO, 1, "synthetic-only", NOW),
        governance=GovernanceWritePolicy("demo_manual", True, True, True),
        usage=UsageAdmission(NOW, (UsageWindow("weekly", D("50"), primary=True),), ("weekly",),
                             "synthetic-usage", "a" * 64))
    try:
        workflow.submit(command.command_id, **submission)
    except OSError as exc:
        if not crash_after_response or str(exc) != "SYNTHETIC_CRASH_AFTER_DURABLE_RESPONSE":
            raise
        assert workflow.coordinator.store.get_command(command.command_id).broker_reference_id is None
    finally:
        workflow.coordinator.store.transition_command = transition
    host.now += timedelta(seconds=2)
    staged = workflow.reconcile(command.command_id, adapter=adapter, now=host.now)
    restarted = operator()
    replay = restarted.reconcile(command.command_id, adapter=adapter, now=host.now)
    terminal = restarted.coordinator.store.get_command(command.command_id)
    executions = adapter.reconcile_command(terminal).executions
    post_proof, post_detail, post_sources = synthetic_component_account(host.now, after_fill=True)
    (output / "synthetic_post_sources.json").write_text(canonical_json(post_sources) + "\n")
    arguments = {"executions": executions, "command": terminal,
        "broker_payload": restarted.evidence.read_preparation(command.command_id)["broker_payload"],
        "post_account": ReconciledBrokerAccount.from_component(post_proof, post_detail,
            detailed_payload=post_sources["detail"]), "now": host.now}
    economic = runtime.apply_confirmed(**arguments)
    first_state = runtime.snapshot()
    again = runtime.apply_confirmed(**arguments)
    final_state = runtime.snapshot()
    positions = final_state["positions"]
    checks = {
        "one_synthetic_send": host.calls["place"] == 1,
        "profile_rechecked_before_send": host.calls["profile"] == 2,
        "restart_uses_lookup": host.calls["lookup"] == 3,
        "terminal_full_fill": terminal.state in {CommandState.FILLED, CommandState.RECONCILED},
        "durable_broker_reference_reconciles": terminal.broker_reference_id == REFERENCE,
        "one_staged_execution": staged.staged_execution_ids == replay.staged_execution_ids == ("order:42:position:500:open",),
        "economic_reconciled": economic.reconciled and again.reconciled,
        "exact_cash_reconciles": final_state["cash_balance"] == D("9989.99"),
        "principal_uses_explicit_broker_amount": executions[0].executed_amount == D("10")
            and executions[0].units * executions[0].price != executions[0].executed_amount,
        "exact_execution_source_preserved": executions[0].executed_at_source == "2026-10-26T15:00:01.1234567Z",
        "existing_holding_retained": len(positions) == 2 and any(
            x.broker_position_id == "7001" and x.units == D("0.1") for x in positions),
        "fill_replay_idempotent": first_state == final_state and not again.applied_execution_ids,
        "one_processed_execution": final_state["processed_execution_ids"] == ("order:42:position:500:open",),
        "no_qualification_credit": not staged.qualification_credit and not replay.qualification_credit,
    }
    report = {"source_kind": "synthetic_only", "human_approval_simulated": True,
        "simulated_crash_after_durable_response": crash_after_response,
        "cash_clock_and_complete_history_simulated": True, "clock_simulated": True,
        "live_broker_submissions": 0, "live_fills": 0, "live_economic_reconciliation": False,
        "synthetic_callbacks": host.calls, "functional_checks": checks,
        "functional_result": "pass" if all(checks.values()) else "fail",
        "live_demo_qualified": False, "qualification_credit": False,
        "open_positions_synthetic": len(positions), "pending_orders_synthetic": 0,
        "economic_receipt": economic, "final_state": final_state}
    (output / "validation.json").write_text(canonical_json(report) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--simulate-crash-after-response", action="store_true")
    args = parser.parse_args(argv)
    result = exercise(args.output_dir, crash_after_response=args.simulate_crash_after_response)
    print(json.dumps(json.loads(canonical_json(result)), indent=2))
    return 0 if result["functional_result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
