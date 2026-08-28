from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest

from agents.fill_agent import fill_agent
from brokers.etoro.adapter import EtoroDemoExecutionAdapter
from brokers.etoro.client import (
    EtoroCredentials,
    EtoroDemoReadOnlyConfig,
    EtoroReadOnlyClient,
    HttpRequest,
    HttpResponse,
)
from brokers.etoro.endpoints import (
    CANCEL_DEMO_ORDER_V3,
    CLOSE_DEMO_POSITION_V1,
    LOOKUP_DEMO_ORDER_V2,
    SUBMIT_DEMO_ORDER_V3,
)
from brokers.etoro.execution_client import (
    EtoroAmbiguousOutcome,
    EtoroDemoExecutionClient,
    EtoroDemoWriteConfig,
)
from execution.coordinator import ExecutionConfig, ExecutionCoordinator
from execution.domain import BrokerCommand, CommandState, OrderIntent
from execution.fill_ingestion import stage_broker_executions
from execution.instruments import (
    BrokerInstrumentMapping,
    Instrument,
    InstrumentRegistry,
)
from execution.store import ExecutionStore
from shared.paths import ETORO_DEMO_WRITE_CONFIG_PATH


NOW = datetime(2026, 8, 28, 14, 30, tzinfo=timezone.utc)
REQUEST_ID = UUID("00000000-0000-4000-8000-000000000111")


class FakeTransport:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _response(value: object, status: int) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers={},
        body=json.dumps(value).encode("utf-8"),
    )


def _write_config() -> EtoroDemoWriteConfig:
    return EtoroDemoWriteConfig(
        enabled=True,
        write_methods_enabled=True,
        base_url="https://fake.etoro.invalid",
        api_key_environment_variable="ETORO_DEMO_WRITE_API_KEY",
        user_key_environment_variable="ETORO_DEMO_WRITE_USER_KEY",
        account_reads_per_minute=48,
        writes_per_minute=10,
        timeout_seconds=1,
        max_lookup_retries=0,
    )


def _execution_client(transport: FakeTransport) -> EtoroDemoExecutionClient:
    return EtoroDemoExecutionClient(
        credentials=EtoroCredentials(api_key="write-api", user_key="write-user"),
        config=_write_config(),
        transport=transport,
        request_id_factory=lambda: UUID("00000000-0000-4000-8000-000000000222"),
        sleeper=lambda _: None,
        jitter=lambda low, high: low,
    )


def _read_client() -> EtoroReadOnlyClient:
    return EtoroReadOnlyClient(
        credentials=EtoroCredentials(api_key="read-api", user_key="read-user"),
        config=EtoroDemoReadOnlyConfig(
            enabled=True,
            base_url="https://fake.etoro.invalid",
            api_key_environment_variable="ETORO_DEMO_API_KEY",
            user_key_environment_variable="ETORO_DEMO_USER_KEY",
            account_reads_per_minute=48,
            market_reads_per_minute=96,
            writes_per_minute=10,
            timeout_seconds=1,
            max_read_retries=0,
        ),
        transport=FakeTransport([]),
        sleeper=lambda _: None,
    )


def _accepted() -> dict[str, object]:
    return {
        "token": "00000000-0000-4000-8000-000000000333",
        "orderId": 13902598,
        "referenceId": str(REQUEST_ID),
    }


def _lookup(status_id: int = 3) -> dict[str, object]:
    executions = []
    if status_id in {3, 5, 9, 10}:
        executions = [
            {
                "positionId": 9001,
                "state": "open",
                "openingData": {
                    "orderId": 13902598,
                    "executionTime": "2026-08-28T14:30:01Z",
                    "units": 1.25,
                    "avgPrice": 80,
                    "priceId": 987654321,
                    "fees": 0.2,
                    "taxes": 0.1,
                },
            }
        ]
    names = {
        1: "Received",
        3: "Filled",
        4: "Rejected",
        5: "PartiallyFilled",
        6: "PendingCancel",
        7: "Canceled",
        9: "CanceledPartiallyFilled",
        10: "RejectedPartiallyFilled",
    }
    return {
        "orderId": 13902598,
        "action": "open",
        "transaction": "buy",
        "type": "mkt",
        "status": {
            "id": status_id,
            "name": names.get(status_id, "Placed"),
            "errorCode": 623 if status_id in {4, 10} else 0,
            "errorMessage": "synthetic rejection" if status_id in {4, 10} else None,
        },
        "asset": {
            "symbol": "AAPL",
            "instrumentId": 1001,
            "currency": "USD",
        },
        "requestedAmount": 100,
        "requestedUnits": None,
        "totalCosts": 0.3,
        "positionExecutions": executions,
        "requestTime": "2026-08-28T14:30:00Z",
        "lastUpdate": "2026-08-28T14:30:01Z",
    }


def _open_payload() -> dict[str, object]:
    return {
        "action": "open",
        "transaction": "buy",
        "instrumentId": 1001,
        "settlementType": "real",
        "orderType": "mkt",
        "leverage": 1,
        "amount": "100",
        "orderCurrency": "usd",
    }


def test_gate_b_endpoint_snapshot_is_versioned_and_demo_only():
    assert SUBMIT_DEMO_ORDER_V3.path == "/api/v3/trading/execution/demo/orders"
    assert LOOKUP_DEMO_ORDER_V2.path == "/api/v2/trading/info/demo/orders:lookup"
    assert CLOSE_DEMO_POSITION_V1.path.startswith(
        "/api/v1/trading/execution/demo/market-close-orders"
    )
    assert CANCEL_DEMO_ORDER_V3.method == "DELETE"
    assert all(
        "/real/" not in endpoint.path
        for endpoint in (
            SUBMIT_DEMO_ORDER_V3,
            LOOKUP_DEMO_ORDER_V2,
            CLOSE_DEMO_POSITION_V1,
            CANCEL_DEMO_ORDER_V3,
        )
    )


def test_checked_in_demo_write_config_is_disabled_and_credential_separated():
    config = EtoroDemoWriteConfig.load(ETORO_DEMO_WRITE_CONFIG_PATH)
    assert not config.enabled
    assert not config.write_methods_enabled
    assert config.api_key_environment_variable.startswith("ETORO_DEMO_WRITE_")
    assert config.user_key_environment_variable.startswith("ETORO_DEMO_WRITE_")


def test_submit_uses_persisted_request_id_and_202_is_only_queued():
    transport = FakeTransport([_response(_accepted(), 202)])
    result = _execution_client(transport).submit_open_order(
        _open_payload(), request_id=REQUEST_ID
    )
    assert result.order_id == "13902598"
    assert result.reference_id == str(REQUEST_ID)
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.headers["x-request-id"] == str(REQUEST_ID)
    assert json.loads(request.body) == _open_payload()


def test_ambiguous_submit_is_not_replayed_and_is_resolved_by_reference_lookup():
    transport = FakeTransport(
        [TimeoutError("lost 202"), _response(_lookup(3), 200)]
    )
    client = _execution_client(transport)
    with pytest.raises(EtoroAmbiguousOutcome):
        client.submit_open_order(_open_payload(), request_id=REQUEST_ID)
    lookup = client.lookup_order(reference_id=str(REQUEST_ID))
    assert lookup.status.status_id == 3
    assert [request.method for request in transport.requests] == ["POST", "GET"]
    assert f"referenceId={REQUEST_ID}" in transport.requests[1].url


def test_close_position_uses_active_v1_route_and_explicit_partial_units():
    transport = FakeTransport(
        [
            _response(
                {
                    "token": "00000000-0000-4000-8000-000000000444",
                    "orderForClose": {
                        "orderID": 55001,
                        "positionID": 9001,
                        "instrumentID": 1001,
                        "unitsToDeduct": 0.5,
                    },
                },
                200,
            )
        ]
    )
    result = _execution_client(transport).close_position(
        position_id="9001",
        instrument_id=1001,
        units=Decimal("0.5"),
        request_id=REQUEST_ID,
    )
    request = transport.requests[0]
    assert result.order_id == "55001"
    assert result.units_to_deduct == Decimal("0.5")
    assert request.method == "POST"
    assert request.url.endswith(
        "/api/v1/trading/execution/demo/market-close-orders/positions/9001"
    )
    assert request.headers["x-request-id"] == str(REQUEST_ID)
    assert json.loads(request.body) == {
        "InstrumentID": 1001,
        "UnitsToDeduct": "0.5",
    }


def test_cancel_uses_v3_delete_without_body_and_timeout_is_never_replayed():
    accepted_transport = FakeTransport(
        [_response({"orderId": 13902598, "token": "cancel-token"}, 202)]
    )
    result = _execution_client(accepted_transport).cancel_order(
        order_id="13902598", request_id=REQUEST_ID
    )
    request = accepted_transport.requests[0]
    assert result.order_id == "13902598"
    assert result.reference_id == str(REQUEST_ID)
    assert request.method == "DELETE"
    assert request.url.endswith("/api/v3/trading/execution/demo/orders/13902598")
    assert request.body is None

    ambiguous_transport = FakeTransport([TimeoutError("lost cancel acknowledgement")])
    with pytest.raises(EtoroAmbiguousOutcome):
        _execution_client(ambiguous_transport).cancel_order(
            order_id="13902598", request_id=REQUEST_ID
        )
    assert len(ambiguous_transport.requests) == 1


@pytest.mark.parametrize(
    ("status_id", "execution_count"),
    [(1, 0), (3, 1), (4, 0), (5, 1), (6, 0), (7, 0), (9, 1), (10, 1)],
)
def test_lookup_contract_covers_async_terminal_and_race_states(
    status_id, execution_count
):
    lookup = _execution_client(
        FakeTransport([_response(_lookup(status_id), 200)])
    ).lookup_order(order_id="13902598")
    assert lookup.status.status_id == status_id
    assert len(lookup.executions) == execution_count


def test_coordinator_reconciles_fill_and_fill_agent_ingests_once(
    tmp_path, isolated_workspace, monkeypatch
):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(
        Instrument.create(
            canonical_symbol="AAPL",
            exchange="NASDAQ",
            asset_type="equity",
            currency="USD",
        )
    )
    registry.record_mapping(
        BrokerInstrumentMapping.from_resolution(
            instrument=instrument,
            broker="etoro",
            environment="demo",
            broker_instrument_id=1001,
            exact_match_symbol="AAPL",
            metadata={"instrumentId": 1001},
            resolved_at=NOW,
        )
    )
    intent = OrderIntent.create(
        strategy_id="quality_v1",
        run_id="RUN_GATE_B",
        internal_instrument_id=instrument.internal_instrument_id,
        environment="demo",
        side="buy",
        order_type="market",
        sizing_method="fixed_notional",
        sizing_value="100",
        currency="USD",
        expires_at=NOW + timedelta(minutes=5),
    )
    store.save_intent(intent)
    command = BrokerCommand.create(
        intent_hash=intent.intent_hash,
        operation="submit_order",
        broker="etoro",
        environment="demo",
        broker_payload=_open_payload(),
        logical_request_id=REQUEST_ID,
    )
    store.save_command(command)
    for state in ("awaiting_approval", "approved", "submission_pending"):
        command = store.transition_command(command.command_id, state)
    coordinator = ExecutionCoordinator(
        store=store,
        registry=registry,
        config=ExecutionConfig(
            schema_version="1.0",
            intent_import_enabled=True,
            broker_writes_enabled=True,
            adapter="etoro_demo_manual",
            environment="demo",
            require_internal_instrument_id=True,
            allowed_asset_types=("equity", "etf"),
            allowed_sides=("buy",),
            allowed_leverage=Decimal("1"),
        ),
    )
    adapter = EtoroDemoExecutionAdapter(
        client=_read_client(),
        execution_client=_execution_client(
            FakeTransport([_response(_accepted(), 202), _response(_lookup(3), 200)])
        ),
        registry=registry,
    )
    command = coordinator.submit_pending_command(
        command=command, intent=intent, adapter=adapter, now=NOW
    )
    assert command.state == CommandState.ACKNOWLEDGED
    command, result = coordinator.reconcile_command(command=command, adapter=adapter)
    assert command.state == CommandState.RECONCILED
    assert len(result.executions) == 1

    data_dir = isolated_workspace / "data"
    staged = stage_broker_executions(
        data_dir / "broker_fills.csv",
        executions=result.executions,
        ticker_by_instrument_id={1001: "AAPL"},
        broker="etoro",
    )
    staged = stage_broker_executions(
        data_dir / "broker_fills.csv",
        executions=result.executions,
        ticker_by_instrument_id={1001: "AAPL"},
        broker="etoro",
    )
    assert len(staged) == 1
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: "RUN_GATE_B")
    fill_agent.run_fill_agent()
    fill_agent.run_fill_agent()
    processed = pd.read_csv(data_dir / "processed_fills.csv")
    trade_fills = pd.read_csv(data_dir / "trade_fills.csv")
    assert len(processed) == 1
    assert len(trade_fills) == 1
    assert trade_fills.iloc[0]["broker_order_id"] == 13902598
    assert trade_fills.iloc[0]["environment"] == "demo"
