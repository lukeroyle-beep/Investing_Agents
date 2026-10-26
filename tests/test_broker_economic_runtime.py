"""Synthetic exact broker economics, never a live account or order."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from agents.fill_agent.broker_runtime import (
    BrokerEconomicRuntime, BrokerEconomicError, ReconciledBrokerAccount, JOURNAL, SCHEMAS,
)
from agents.fill_agent.live_broker_admission import assert_legacy_fill_inputs_admissible, LiveBrokerEconomicApplicationBlocked
from brokers.base import BrokerExecution
from brokers.etoro.account_evidence import ComponentCashObservation, qualify_pnl_component_time_evidence
from brokers.etoro.schemas import EtoroDetailedPortfolio
from execution.domain import BrokerCommand, CommandState, Environment
from tests.contracts.etoro.test_etoro_account_evidence import NOW, _detail, _security, _page
from tests.contracts.etoro.test_etoro_pnl_capture import _capture


def full_account(*, cash="9000", holdings=((7001, 1001, "10", "1000"),), at=NOW, cash_at=None, mark_at=None):
    detail = _detail()
    detail["clientPortfolio"]["credit"] = cash
    detail["clientPortfolio"]["positions"] = [dict(positionID=pid, CID=9001,
        instrumentID=instrument, mirrorID=0, units=units, amount=amount, isBuy=True,
        leverage=1, settlementTypeID=1) for pid, instrument, units, amount in holdings]
    pnl = deepcopy(detail)
    pnl["clientPortfolio"].update(accountCurrencyId=1, unrealizedPnL="0")
    for position in pnl["clientPortfolio"]["positions"]:
        position.update(totalFees="0", totalExternalFees="0", totalExternalTaxes="0")
        position["unrealizedPnL"] = {
            "pnL": "0", "pnlAssetCurrency": "0", "exposureInAccountCurrency": position["amount"],
            "exposureInAssetCurrency": position["amount"], "marginInAccountCurrency": position["amount"],
            "marginInAssetCurrency": position["amount"], "marginCurrencyId": 1, "assetCurrencyId": 1,
            "closeRate": "100", "closeConversionRate": "1",
            "timestamp": (mark_at or {}).get(position["positionID"], at).isoformat()}
    evidence, _ = _capture(now=at, detail=detail, pnl=pnl,
                           metadata=_page(*[_security(identifier) for identifier in sorted({x[1] for x in holdings})]))
    cash_at = cash_at or at
    cash_proof = ComponentCashObservation(demo_customer_id="9001", available_cash=D(cash), observed_at=cash_at,
        source_endpoint="/api/v1/trading/info/demo/aggregate", source_timestamp_field="timestamp",
        source_sha256="a" * 64, source_timestamp=cash_at.isoformat())
    proof = qualify_pnl_component_time_evidence(evidence, assessed_at=at, cash_observation=cash_proof)
    return ReconciledBrokerAccount.from_component(proof, EtoroDetailedPortfolio.parse(detail), detailed_payload=detail)


def order():
    payload = dict(action="open", transaction="buy", leverage=1, orderCurrency="usd",
                   settlementType="real", instrumentId=1002, amount="10", orderType="mkt")
    command = BrokerCommand.create(intent_hash="synthetic-intent", operation="submit_order",
                                   broker="etoro", environment="demo", broker_payload=payload)
    command = replace(command, state=CommandState.FILLED, broker_order_id="order-1", broker_reference_id="reference-1")
    execution = BrokerExecution(broker_execution_id="execution-1", broker_order_id="order-1",
        broker_instrument_id=1002, broker_position_id="7002", broker_reference_id="reference-1",
        environment=Environment.DEMO, side="long", action="buy", units=D("0.1"), price=D("100"),
        executed_at=NOW + timedelta(seconds=1), price_rate_id="rate-1", fees=D("0.01"), taxes=D("0.02"),
        currency="USD", executed_amount=D("10"),
        executed_at_source=(NOW + timedelta(seconds=1)).isoformat())
    return command, payload, execution


def after(*, cash="8989.97", units="0.1", principal="10", at=NOW + timedelta(seconds=2), old_units="10", cash_at=None, mark_at=None):
    return full_account(cash=cash, holdings=((7001, 1001, old_units, "1000"), (7002, 1002, units, principal)), at=at, cash_at=cash_at, mark_at=mark_at)


def apply(runtime, *, post=None, execution=None, command=None, payload=None):
    original_command, original_payload, original_execution = order()
    return runtime.apply_confirmed(executions=(execution or original_execution,),
        command=command or original_command, broker_payload=payload or original_payload,
        post_account=post or after(), now=NOW + timedelta(seconds=2))


def byte_state(root):
    return {p.name: p.read_bytes() for p in root.iterdir() if p.is_file() and p.name != ".broker-runtime.lock"}


def test_initialization_is_full_account_decimal_bound_and_never_default_cash(tmp_path):
    initial = full_account(cash="9000.123456789123456789")
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=initial, now=NOW)
    state = runtime.snapshot()
    assert state["cash_balance"] == D("9000.123456789123456789")
    assert len(state["positions"]) == 1
    assert state["positions"][0].units == D("10")
    assert state["positions"][0].invested_amount == D("1000")
    assert state["processed_execution_ids"] == ()
    assert "9000.123456789123456789" in (runtime.root / "cash_state.csv").read_text()
    before = byte_state(runtime.root)
    with pytest.raises(BrokerEconomicError, match="no_overwrite"):
        BrokerEconomicRuntime.initialize(runtime.root, account=initial, now=NOW)
    assert byte_state(runtime.root) == before


def test_stale_or_unbound_initialization_does_not_create_runtime(tmp_path):
    initial = full_account()
    with pytest.raises(BrokerEconomicError, match="fresh_reconciled"):
        BrokerEconomicRuntime.initialize(tmp_path / "stale", account=initial, now=NOW + timedelta(seconds=16))
    assert not (tmp_path / "stale").exists()
    wrong = replace(initial, detailed=replace(initial.detailed, snapshot_id="wrong"))
    with pytest.raises(BrokerEconomicError, match="retained_source"):
        BrokerEconomicRuntime.initialize(tmp_path / "wrong", account=wrong, now=NOW)
    assert not (tmp_path / "wrong").exists()


def test_terminal_fill_applied_exactly_once_with_all_existing_holdings_and_costs(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    first = apply(runtime, command=command, payload=payload, execution=execution)
    assert first.reconciled and first.applied_execution_ids == ("execution-1",)
    assert first.cash_balance == D("8989.97")
    state = runtime.snapshot()
    assert {p.broker_position_id for p in state["positions"]} == {"7001", "7002"}
    assert state["positions"][0].invested_amount == D("1000")
    assert state["processed_execution_ids"] == ("execution-1",)
    before = byte_state(runtime.root)
    reopened = BrokerEconomicRuntime(runtime.root)
    replay = apply(reopened, command=command, payload=payload, execution=execution)
    assert replay.replay and replay.applied_execution_ids == () and replay.reconciled
    assert byte_state(runtime.root) == before


def test_reported_execution_principal_wins_over_rounded_average_price(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    execution = replace(execution, price=D("99.999999999999999999999"))
    assert apply(runtime, command=command, payload=payload, execution=execution).cash_balance == D("8989.97")
    text = (runtime.root / "trade_fills.csv").read_text()
    assert "99.999999999999999999999" in text


@pytest.mark.parametrize("post", [after(cash="8989.96"), after(units="0.2"), after(principal="9.99"), after(old_units="9")])
def test_post_broker_mismatch_changes_no_canonical_files_or_dedup_markers(tmp_path, post):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    before = byte_state(runtime.root)
    with pytest.raises(BrokerEconomicError, match="not_reconciled"):
        apply(runtime, post=post)
    assert byte_state(runtime.root) == before
    assert not (runtime.root / JOURNAL).exists()


def test_changed_execution_replay_or_missing_principal_fails(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    with pytest.raises(BrokerEconomicError, match="economic_values"):
        apply(runtime, execution=replace(execution, executed_amount=None), command=command, payload=payload)
    apply(runtime, execution=execution, command=command, payload=payload)
    before = byte_state(runtime.root)
    with pytest.raises(BrokerEconomicError, match="replay_conflict"):
        apply(runtime, execution=replace(execution, taxes=D("0.01")), command=command, payload=payload)
    assert byte_state(runtime.root) == before


def test_interrupted_csv_transaction_recovers_once_without_resubmission(tmp_path, monkeypatch):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    original = runtime._atomic_write
    def interrupted(name, text):
        if name == "trade_fills.csv":
            raise OSError("synthetic interruption")
        original(name, text)
    monkeypatch.setattr(runtime, "_atomic_write", interrupted)
    with pytest.raises(OSError, match="synthetic"):
        apply(runtime, execution=execution, command=command, payload=payload)
    assert (runtime.root / JOURNAL).exists()
    reopened = BrokerEconomicRuntime(runtime.root)
    reopened.recover()
    assert not (runtime.root / JOURNAL).exists()
    state = reopened.snapshot()
    assert state["cash_balance"] == D("8989.97")
    assert state["processed_execution_ids"] == ("execution-1",)
    before = byte_state(runtime.root)
    assert apply(reopened, execution=execution, command=command, payload=payload).replay
    assert byte_state(runtime.root) == before


def test_external_mutation_or_account_manifest_change_is_never_overwritten(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    (runtime.root / "cash_state.csv").write_text("changed")
    with pytest.raises(BrokerEconomicError, match="integrity"):
        runtime.snapshot()
    assert (runtime.root / "cash_state.csv").read_text() == "changed"


def test_legacy_runner_refuses_versioned_runtime_even_before_broker_staging(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    with pytest.raises(LiveBrokerEconomicApplicationBlocked):
        assert_legacy_fill_inputs_admissible(manual_fills_path=runtime.root / "manual_fills.csv",
                                            broker_fills_path=runtime.root / "broker_fills.csv")


def test_initialization_interruption_never_exposes_requested_runtime(tmp_path, monkeypatch):
    original = BrokerEconomicRuntime._atomic_write
    def interrupted(self, name, text):
        if name == "portfolio_state.csv":
            raise OSError("synthetic initialization interruption")
        original(self, name, text)
    monkeypatch.setattr(BrokerEconomicRuntime, "_atomic_write", interrupted)
    destination = tmp_path / "requested"
    with pytest.raises(OSError, match="initialization"):
        BrokerEconomicRuntime.initialize(destination, account=full_account(), now=NOW)
    assert not destination.exists()
    retained = list(tmp_path.glob(".broker-init-*"))
    assert len(retained) == 1 and (retained[0] / JOURNAL).exists()
    monkeypatch.setattr(BrokerEconomicRuntime, "_atomic_write", original)
    recovered = BrokerEconomicRuntime(retained[0])
    recovered.recover()
    assert recovered.snapshot()["cash_balance"] == D("9000")
    assert not destination.exists()  # No unapproved path promotion or new baseline.


def test_terminal_command_cannot_apply_a_second_different_execution_set(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    apply(runtime, execution=execution, command=command, payload=payload)
    before = byte_state(runtime.root)
    another = replace(execution, broker_execution_id="execution-2", executed_at=NOW + timedelta(seconds=3),
                      executed_at_source=(NOW + timedelta(seconds=3)).isoformat())
    post = after(cash="8979.94", units="0.2", principal="20", at=NOW + timedelta(seconds=4))
    with pytest.raises(BrokerEconomicError, match="execution_set_changed"):
        runtime.apply_confirmed(executions=(another,), command=command, broker_payload=payload,
                                post_account=post, now=NOW + timedelta(seconds=4))
    assert byte_state(runtime.root) == before


def test_redistributing_principal_cannot_reuse_retained_detail_hash(tmp_path):
    initial = full_account(holdings=((7001, 1001, "10", "1000"), (7003, 1003, "1", "100")))
    first, second = initial.detailed.positions
    forged_detail = replace(initial.detailed, positions=(replace(first, amount=D("999")),
                                                       replace(second, amount=D("101"))))
    forged = replace(initial, detailed=forged_detail)
    with pytest.raises(BrokerEconomicError, match="retained_source"):
        BrokerEconomicRuntime.initialize(tmp_path / "forged", account=forged, now=NOW)
    assert not (tmp_path / "forged").exists()
    raw = deepcopy(initial.detailed_payload)
    raw["clientPortfolio"]["positions"][0]["amount"] = "999"
    raw["clientPortfolio"]["positions"][1]["amount"] = "101"
    forged = replace(initial, detailed=EtoroDetailedPortfolio.parse(raw), detailed_payload=raw)
    with pytest.raises(BrokerEconomicError, match="source_binding"):
        BrokerEconomicRuntime.initialize(tmp_path / "forged", account=forged, now=NOW)
    assert not (tmp_path / "forged").exists()


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"cash_at": NOW}, "post_cash_observation_precedes_execution"),
    ({"mark_at": {7002: NOW}}, "post_position_valuation_precedes_execution"),
])
def test_post_collection_time_cannot_mask_pre_execution_source_clock(tmp_path, kwargs, reason):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    before = byte_state(runtime.root)
    with pytest.raises(BrokerEconomicError, match=reason):
        apply(runtime, post=after(**kwargs))
    assert byte_state(runtime.root) == before
    assert not (runtime.root / JOURNAL).exists()


def test_unchanged_position_may_have_older_fresh_mark_while_changed_sources_follow_execution(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    execution_at = NOW + timedelta(seconds=1)
    receipt = apply(runtime, post=after(cash_at=execution_at, mark_at={7001: NOW, 7002: execution_at}))
    assert receipt.reconciled and receipt.applied_execution_ids == ("execution-1",)


def _source_seventh_digit(moment, digit="1"):
    return moment.isoformat(timespec="microseconds").replace("+00:00", digit + "+00:00")


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"cash_at": NOW + timedelta(seconds=1)}, "post_cash_observation_precedes_execution"),
    ({"mark_at": {7002: NOW + timedelta(seconds=1)}}, "post_position_valuation_precedes_execution"),
])
def test_seventh_execution_timestamp_digit_cannot_be_truncated_for_reconciliation(tmp_path, kwargs, reason):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    execution = replace(execution, executed_at_source=_source_seventh_digit(execution.executed_at))
    before = byte_state(runtime.root)
    with pytest.raises(BrokerEconomicError, match=reason):
        apply(runtime, post=after(**kwargs), command=command, payload=payload, execution=execution)
    assert byte_state(runtime.root) == before


def test_exact_execution_source_is_retained_and_must_match_datetime_view(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    source = _source_seventh_digit(execution.executed_at)
    bad = replace(execution, executed_at_source=_source_seventh_digit(execution.executed_at + timedelta(seconds=1)))
    with pytest.raises(BrokerEconomicError, match="timestamp_view_mismatch"):
        apply(runtime, command=command, payload=payload, execution=bad)
    assert apply(runtime, command=command, payload=payload, execution=replace(execution, executed_at_source=source)).reconciled
    assert source in (runtime.root / "trade_fills.csv").read_text()


def test_actual_broker_runtime_requires_original_execution_time_source(tmp_path):
    runtime = BrokerEconomicRuntime.initialize(tmp_path / "v2", account=full_account(), now=NOW)
    command, payload, execution = order()
    before = byte_state(runtime.root)
    with pytest.raises(BrokerEconomicError, match="execution_source_timestamp_required"):
        apply(runtime, command=command, payload=payload, execution=replace(execution, executed_at_source=None))
    assert byte_state(runtime.root) == before
    assert not (runtime.root / JOURNAL).exists()
