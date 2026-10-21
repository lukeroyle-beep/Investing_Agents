"""Exercise the Demo operator with a fake broker in a new isolated directory.

Approval, prices, account, clock and fills are simulated. No broker transport
or credentials are used, and this never contributes live qualification credit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brokers.base import BrokerExecution, BrokerReconciliationResult, BrokerSubmissionResult
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import Environment, OrderIntent
from execution.instruments import BrokerInstrumentMapping, Instrument, InstrumentRegistry
from execution.operator import DemoOperator, OperatorEvidenceStore
from execution.store import ExecutionStore
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import AccountEvidence, QuoteEvidence, RiskLimits
from risk.submission_gate import GovernanceWritePolicy


class FixtureBroker:
    """In-memory fixture only. Deliberately has no HTTP/network implementation."""

    broker_name = "fake"
    environment = Environment.DEMO
    read_only = False

    def __init__(self, moment):
        self.moment = moment
        self.submissions = 0
        self.lookups = 0

    def submit_order(self, intent, command):
        self.submissions += 1
        return BrokerSubmissionResult(
            request_id=command.logical_request_id, accepted_for_processing=True,
            broker_order_id="fixture-order-1",
            broker_reference_id=str(command.logical_request_id), raw_status="queued")

    def reconcile_command(self, command):
        self.lookups += 1
        fill = BrokerExecution(
            broker_execution_id="fixture-execution-1", broker_order_id="fixture-order-1",
            broker_instrument_id=1001, broker_position_id="fixture-position-1",
            broker_reference_id=str(command.logical_request_id), environment=Environment.DEMO,
            side="long", action="buy", units=Decimal("1"), price=Decimal("100"),
            executed_at=self.moment + timedelta(seconds=1), price_rate_id="fixture-rate-1",
            fees=Decimal("0.1"), taxes=Decimal("0"), currency="USD")
        return BrokerReconciliationResult(
            command_id=command.command_id, reconciled=True, lifecycle_state="filled",
            broker_order_id="fixture-order-1", broker_position_id="fixture-position-1",
            details=(), executions=(fill,))


def exercise(output_dir):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / "fixture_runtime"
    control = runtime / "control"
    fills = runtime / "state" / "broker_fills.csv"
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    config = ExecutionConfig(
        schema_version="1.0", intent_import_enabled=True, broker_writes_enabled=True,
        adapter="fake", environment=Environment.DEMO, require_internal_instrument_id=True,
        allowed_asset_types=("equity", "etf"), allowed_sides=("buy",), allowed_leverage=Decimal("1"))
    limits = RiskLimits.load(PROJECT_ROOT / "config" / "risk.yaml")

    def operator():
        store = ExecutionStore(control / "execution.sqlite3")
        coordinator = ExecutionCoordinator(store=store, registry=InstrumentRegistry(store), config=config)
        return DemoOperator(coordinator=coordinator, limits=limits,
                            evidence_store=OperatorEvidenceStore(control / "operator.sqlite3"),
                            staged_fills_path=fills)

    workflow = operator()
    registry = workflow.coordinator.registry
    instrument = registry.register(Instrument.create(
        canonical_symbol="FIXTURE", exchange="NASDAQ", asset_type="equity", currency="USD",
        sector="technology"))
    registry.record_mapping(BrokerInstrumentMapping.from_resolution(
        instrument=instrument, broker="fake", environment=Environment.DEMO,
        broker_instrument_id=1001, exact_match_symbol="FIXTURE", metadata={"source": "fixture"}))
    intent = OrderIntent.create(
        strategy_id="fixture_only", run_id="operator_fixture", internal_instrument_id=instrument.internal_instrument_id,
        environment=Environment.DEMO, side="buy", order_type="market", sizing_method="fixed_notional",
        sizing_value="100", currency="USD", expires_at=now + timedelta(minutes=5))
    account = AccountEvidence(
        snapshot_id="fixture-account", environment=Environment.DEMO, observed_at=now, currency="USD",
        equity=Decimal("100000"), cash=Decimal("100000"), daily_pnl=Decimal("0"),
        peak_equity=Decimal("100000"), positions=(), pending_orders=(), pending_orders_complete=True)
    quote = QuoteEvidence(quote_id="fixture-rate-1", internal_instrument_id=instrument.internal_instrument_id,
                          observed_at=now, bid=Decimal("100"), ask=Decimal("100"), regular_session_open=True)
    command = workflow.prepare(intent=intent, account=account, quote=quote, source_kind="fixture", now=now)
    # Explicitly simulated approval. Never record this as a real human/broker cycle.
    workflow.approve(command.command_id, approver="SIMULATED_FIXTURE_OPERATOR",
                     human_confirmed=True, now=now)
    broker = FixtureBroker(now)
    submission = dict(adapter=broker,
                      kill_switch=KillSwitchStatus(False, True, Environment.DEMO, 1, "fixture only", now),
                      regular_session_open=True,
                      governance=GovernanceWritePolicy("demo_manual", True, True, True), now=now)
    workflow.submit(command.command_id, **submission)
    first = workflow.reconcile(command.command_id, adapter=broker, now=now + timedelta(seconds=2))
    # Reconstruct all durable workflow objects and recover by lookup, not send.
    restarted = operator()
    second = restarted.reconcile(command.command_id, adapter=broker, now=now + timedelta(seconds=3))
    duplicate_blocked = False
    try:
        restarted.submit(command.command_id, **submission)
    except (ValueError, RuntimeError):
        duplicate_blocked = True

    # Fill Agent remains the sole economic writer. Run it only in the brand-new
    # fixture runtime, twice, to demonstrate durable fill deduplication.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("ETORO_")}
    environment["INVESTING_RUNTIME_DIR"] = str(runtime)
    environment["INVESTING_RUN_ID"] = "operator_fixture"
    economic_files = ("portfolio_state.csv", "cash_state.csv", "cash_ledger.csv",
                      "trade_fills.csv", "processed_fills.csv")
    snapshots = []
    for _ in range(2):
        subprocess.run([sys.executable, "-m", "agents.fill_agent.fill_agent"], cwd=PROJECT_ROOT,
                       env=environment, check=True, capture_output=True, text=True)
        snapshots.append({name: pd.read_csv(runtime / "state" / name, keep_default_na=False).to_dict("records")
                          for name in economic_files})
    state = pd.read_csv(runtime / "state" / "portfolio_state.csv")
    cash = Decimal(str(pd.read_csv(runtime / "state" / "cash_state.csv").iloc[0]["cash_balance"]))
    staged = pd.read_csv(fills)
    checks = {
        "one_fake_submission": broker.submissions == 1,
        "restart_uses_lookup": broker.lookups == 2,
        "duplicate_submission_blocked": duplicate_blocked,
        "one_staged_execution": len(staged) == 1,
        "one_processed_fill": len(snapshots[-1]["processed_fills.csv"]) == 1,
        "one_cash_ledger_entry": len(snapshots[-1]["cash_ledger.csv"]) == 1,
        "fill_replay_idempotent": snapshots[0] == snapshots[1],
        "fixture_cash_reconciles": cash == Decimal("99899.9"),
        "fixture_position_reconciles": len(state) == 1 and Decimal(str(state.iloc[0]["quantity"])) == 1,
        "no_qualification_credit": not first.qualification_credit and not second.qualification_credit,
    }
    report = {
        "objective": "virtual_functionality", "return_target_pct": None,
        "source_kind": "fixture", "human_approval_simulated": True, "clock_simulated": True,
        "broker_orders_submitted": 0, "fake_broker_submissions": broker.submissions,
        "functional_checks": checks, "functional_result": "pass" if all(checks.values()) else "fail",
        "live_demo_qualified": False, "qualification_credit": False,
        "economic_reconciliation_scope": "isolated_fixture_only",
        "receipt": asdict(second),
    }
    (output / "validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    report = exercise(args.output_dir)
    print(json.dumps(report, indent=2))
    return 0 if report["functional_result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
