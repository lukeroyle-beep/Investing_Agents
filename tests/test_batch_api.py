"""Synthetic finite-batch broker contracts, never live credentials or writes."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from brokers.etoro.autonomous_api import DirectBindingStore, DirectDemoError
from brokers.etoro.batch_api import BatchDemoAdapter
from brokers.etoro.batch_reads import _projection, _rate_arguments, _search_arguments
from execution.domain import CommandState
from execution.instruments import Instrument
from tests.test_autonomous_api import NOW, full_lookup, workflow as legacy_workflow
from tests.test_pilot_reads import direct_receipt

D = Decimal


def workflow(tmp_path, *, received=NOW, source=NOW):
    old, command, intent, transport, clock = legacy_workflow(tmp_path)
    bindings = DirectBindingStore(tmp_path / "batch-bindings.sqlite3")
    adapter = BatchDemoAdapter(expected_account_id="123", client=old.client,
        bindings=bindings, clock=lambda: clock[0], pre_send_guard=lambda: None)
    instrument = Instrument.create(internal_instrument_id=intent.internal_instrument_id,
        canonical_symbol="SYNBOND", exchange="NASDAQ", asset_type="etf", currency="USD", sector="fixed_income")
    spec = {"instrument_id": 1001, "symbol": "SYNBOND", "exchange": "NASDAQ", "asset_type": "etf",
        "currency": "USD", "sector": "fixed_income", "issuer": "synthetic-provider",
        "economic_class": "fixed_income", "class_source_reference": "synthetic-classification"}
    state_args, rate_args = _search_arguments(1001), _rate_arguments(1001)
    state_receipt = direct_receipt(tmp_path, state_args["path"], {"totalItems": 1, "items": [
        {"instrumentId": 1001, "internalSymbolFull": "SYNBOND", "isCurrentlyTradable": True,
         "isExchangeOpen": True, "isActiveInPlatform": True, "isBuyEnabled": True, "isDelisted": False}]},
        query=state_args["query"], start=received, end=received)
    rate_receipt = direct_receipt(tmp_path, rate_args["path"], {"results": [{"instrumentId": 1001,
        "bid": 99, "ask": 100, "date": source.isoformat(), "quoteType": "realtime"}]},
        query=rate_args["query"], start=received, end=received)
    quote, market = _projection(state_receipt=state_receipt, rate_receipt=rate_receipt,
        instrument=instrument, instrument_spec=spec, now=NOW)
    adapter.bind(command, intent=intent, instrument_id=1001, authorization_hash="synthetic-finite-mandate",
        expires_at=NOW + timedelta(seconds=30), evidence={"quote": quote,
        "market_state": market, "instrument": instrument, "instrument_spec": spec})
    bindings.authorize(command.command_id, {"authorized_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(seconds=20)).isoformat()})
    return adapter, command, intent, transport, clock


def test_generic_etf_submission_then_exact_full_fill(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    result = adapter.submit_order(intent, command)
    assert result.accepted_for_processing
    clock[0] += timedelta(seconds=2)
    fill = adapter.reconcile_command(command)
    assert fill.reconciled and fill.lifecycle_state == "filled"
    assert dict(fill.details)["remainder_terminal"] == "true"
    assert len(dict(fill.details)["lookup_sha256"]) == 64
    assert fill.executions[0].executed_at_source == "2026-10-27T09:00:01.0000001+00:00"
    assert len(transport.posts) == 1


@pytest.mark.parametrize("status", [9, 10])
def test_terminal_partial_reports_actual_principal_and_no_topup(tmp_path, status):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup["status"]["id"] = status
    position = transport.lookup["positionExecutions"][0]
    position["investedAmountCurrency"] = "6"
    position["remainingUnits"] = position["openingData"]["units"] = ".06"
    result = adapter.reconcile_command(command)
    assert result.reconciled and result.lifecycle_state == "partially_filled_terminal"
    assert result.executions[0].executed_amount == 6
    assert dict(result.details)["status_id"] == str(status)
    restarted = BatchDemoAdapter(expected_account_id="123", client=adapter.client,
        bindings=DirectBindingStore(adapter.bindings.path), clock=lambda: clock[0], pre_send_guard=lambda: None)
    assert restarted.reconcile_command(command) == result
    with pytest.raises(DirectDemoError, match="already_dispatched_lookup_only"):
        restarted.submit_order(intent, command)
    assert len(transport.posts) == 1


@pytest.mark.parametrize("status", [1, 2, 5, 6, 11, 12])
def test_nonterminal_partial_is_lookup_only(tmp_path, status):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup["status"]["id"] = status
    result = adapter.reconcile_command(command)
    assert not result.reconciled and not result.executions
    assert dict(result.details)["reason"] == "nonterminal_lookup_only"
    assert len(transport.posts) == 1


@pytest.mark.parametrize("status", [4, 7, 8])
def test_dated_terminal_no_fill_is_available_for_independent_account_reconciliation(tmp_path, status):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup.update(status={"id": status}, positionExecutions=[], totalCosts="0", lastUpdate="2026-10-27T09:00:01Z")
    result = adapter.reconcile_command(command)
    assert result.reconciled and not result.executions
    assert result.lifecycle_state in {"rejected", "cancelled"}
    assert dict(result.details)["remainder_terminal"] == "true"
    assert len(transport.posts) == 1


@pytest.mark.parametrize("change", ["missing_date", "earlier_date", "future_date", "positions", "conflicting_name", "missing_costs", "nonzero_costs"])
def test_terminal_no_fill_rejects_incomplete_or_contradictory_proof(tmp_path, change):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup.update(status={"id": 4}, positionExecutions=[], totalCosts="0", lastUpdate="2026-10-27T09:00:01Z")
    if change == "missing_date": del transport.lookup["lastUpdate"]
    elif change == "earlier_date": transport.lookup["lastUpdate"] = "2026-10-27T08:59:59Z"
    elif change == "future_date": transport.lookup["lastUpdate"] = "2026-10-27T09:00:03Z"
    elif change == "positions": transport.lookup["positionExecutions"] = full_lookup(command.logical_request_id)["positionExecutions"]
    elif change == "missing_costs": del transport.lookup["totalCosts"]
    elif change == "nonzero_costs": transport.lookup["totalCosts"] = "1"
    else: transport.lookup["status"]["name"] = "Filled"
    with pytest.raises(ValueError):
        adapter.reconcile_command(command)


@pytest.mark.parametrize("principal", ["0", "10", "11"])
def test_partial_rejects_zero_full_or_excess_principal(tmp_path, principal):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup["status"]["id"] = 9
    transport.lookup["positionExecutions"][0]["investedAmountCurrency"] = principal
    with pytest.raises(DirectDemoError):
        adapter.reconcile_command(command)


def test_terminal_no_fill_cannot_later_change_into_fill(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup.update(status={"id": 4}, positionExecutions=[], totalCosts="0", lastUpdate="2026-10-27T09:00:01Z")
    adapter.reconcile_command(command)
    transport.lookup = full_lookup(command.logical_request_id)
    with pytest.raises(DirectDemoError, match="terminal_outcome_changed"):
        adapter.reconcile_command(command)


@pytest.mark.parametrize("delay", [D("5.000001"), D("6")])
def test_identity_delay_cannot_bypass_final_five_second_handoff(tmp_path, delay):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    transport.identity_delay = float(delay)
    result = adapter.submit_order(intent, command)
    assert result.ambiguous and not transport.posts
    with pytest.raises(DirectDemoError, match="already_dispatched_lookup_only"):
        adapter.submit_order(intent, command)


def test_exact_five_second_handoff_boundary_passes(tmp_path):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    transport.identity_delay = 5
    assert adapter.submit_order(intent, command).accepted_for_processing
    assert len(transport.posts) == 1


def test_source_clock_not_replaced_with_receipt_clock(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path, source=NOW - timedelta(seconds=29))
    transport.identity_delay = 2
    result = adapter.submit_order(intent, command)
    assert result.ambiguous and not transport.posts


def test_recovery_after_unknown_uses_original_reference_only(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    transport.post_result = TimeoutError("synthetic-secret")
    result = adapter.submit_order(intent, command)
    assert result.ambiguous
    clock[0] += timedelta(seconds=2)
    result = adapter.reconcile_command(replace(command, state=CommandState.UNKNOWN))
    assert result.reconciled and result.lifecycle_state == "filled"
    assert transport.calls[-1].url.endswith("?referenceId=" + str(command.logical_request_id))
    assert len(transport.posts) == 1


def test_missing_quote_evidence_cannot_bind(tmp_path):
    old, command, intent, _, clock = legacy_workflow(tmp_path)
    adapter = BatchDemoAdapter(expected_account_id="123", client=old.client,
        bindings=DirectBindingStore(tmp_path / "batch-bindings.sqlite3"), clock=lambda: clock[0], pre_send_guard=lambda: None)
    with pytest.raises(DirectDemoError, match="bound_market_evidence"):
        adapter.bind(command, intent=intent, instrument_id=1001, authorization_hash="synthetic")


def request_hooks(adapter, command):
    return [item["request_hook"] for item in adapter.bindings.outcomes(command.command_id) if "request_hook" in item]


def test_durable_hook_uses_final_clock_after_identity_not_response_time(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    transport.identity_delay = 2
    original_send = transport.send
    def delayed_response(request):
        response = original_send(request)
        if request.method == "POST":
            clock[0] += timedelta(seconds=3)
        return response
    transport.send = delayed_response
    assert adapter.submit_order(intent, command).accepted_for_processing
    hook, = request_hooks(adapter, command)
    assert hook["request_hook_at"] == (NOW + timedelta(seconds=2)).isoformat()
    assert clock[0] == NOW + timedelta(seconds=5)
    assert hook["quote_source_at"] == NOW.isoformat()
    assert hook["quote_received_at"] == NOW.isoformat()
    assert D(hook["source_age_seconds"]) == D(2)
    assert D(hook["receipt_to_request_hook_seconds"]) == D(2)
    assert hook["clock_observation"] == "local_pre_transport_hook"
    assert hook["broker_receipt_observed"] is False
    reopened = BatchDemoAdapter(expected_account_id="123", client=adapter.client,
        bindings=DirectBindingStore(adapter.bindings.path), clock=lambda: clock[0], pre_send_guard=lambda: None)
    assert request_hooks(reopened, command) == [hook]


def test_durable_hook_survives_unknown_transport_and_is_not_replaced_on_retry(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    transport.identity_delay = 1
    transport.post_result = TimeoutError("synthetic-secret")
    assert adapter.submit_order(intent, command).ambiguous
    hook, = request_hooks(adapter, command)
    assert hook["request_hook_at"] == (NOW + timedelta(seconds=1)).isoformat()
    with pytest.raises(DirectDemoError, match="already_dispatched_lookup_only"):
        adapter.submit_order(intent, command)
    assert request_hooks(adapter, command) == [hook]
    assert len(transport.posts) == 1


def test_failed_handoff_has_no_successful_hook_or_send(tmp_path):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    transport.identity_delay = 6
    assert adapter.submit_order(intent, command).ambiguous
    assert not transport.posts and request_hooks(adapter, command) == []


@pytest.mark.parametrize("units,price,principal", [
    ("0.001", "100", "10"),
    ("0.1", "100", "9.99"),
    ("0.100000000000000000000000000001", "100", "10"),
])
def test_usd_denominations_and_rounding_require_exact_arithmetic_review(tmp_path, units, price, principal):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    position = transport.lookup["positionExecutions"][0]
    position["openingData"].update(units=units, avgPrice=price)
    position.update(remainingUnits=units, investedAmountCurrency=principal)
    with localcontext() as context:
        context.prec = 6
        with pytest.raises(DirectDemoError, match="arithmetic_rounding_or_denomination_requires_review"):
            adapter.reconcile_command(command)
    assert len(transport.posts) == 1
