from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from brokers.base import BrokerDataUnavailable, BrokerWriteDisabled
from brokers.etoro.adapter import EtoroDemoReadOnlyAdapter
from brokers.etoro.client import (
    EtoroAuthenticationError,
    EtoroCredentials,
    EtoroConfigurationError,
    EtoroDemoReadOnlyConfig,
    EtoroRateLimitError,
    EtoroReadOnlyClient,
    EtoroNotFoundError,
    EtoroServerError,
    EtoroTransportError,
    HttpRequest,
    HttpResponse,
)
from brokers.etoro.endpoints import (
    GET_DEMO_AGGREGATE_PORTFOLIO,
    GET_MARKET_RATES,
    READ_ONLY_ENDPOINTS,
    SEARCH_INSTRUMENTS,
)
from brokers.etoro.redaction import redact_text
from brokers.etoro.schemas import EtoroSchemaError
from execution.domain import BrokerCommand
from execution.instruments import Instrument, InstrumentMappingError, InstrumentRegistry
from execution.store import ExecutionStore
from shared.paths import ETORO_DEMO_CONFIG_PATH


FIXTURES = Path(__file__).parent / "fixtures"


def _json_response(name: str, status: int = 200, headers=None) -> HttpResponse:
    return HttpResponse(
        status_code=status,
        headers=headers or {},
        body=(FIXTURES / name).read_bytes(),
    )


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


def _config(**overrides) -> EtoroDemoReadOnlyConfig:
    values = {
        "enabled": True,
        "base_url": "https://fake.etoro.invalid",
        "api_key_environment_variable": "ETORO_DEMO_API_KEY",
        "user_key_environment_variable": "ETORO_DEMO_USER_KEY",
        "account_reads_per_minute": 48,
        "market_reads_per_minute": 96,
        "writes_per_minute": 10,
        "timeout_seconds": 1.0,
        "max_read_retries": 2,
    }
    values.update(overrides)
    return EtoroDemoReadOnlyConfig(**values)


def _client(transport: FakeTransport, **kwargs) -> EtoroReadOnlyClient:
    request_ids = iter(
        [
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("00000000-0000-4000-8000-000000000002"),
            UUID("00000000-0000-4000-8000-000000000003"),
            UUID("00000000-0000-4000-8000-000000000004"),
        ]
    )
    return EtoroReadOnlyClient(
        credentials=EtoroCredentials(api_key="demo-api-secret", user_key="demo-user-secret"),
        config=_config(**kwargs),
        transport=transport,
        request_id_factory=lambda: next(request_ids),
        sleeper=lambda _: None,
        jitter=lambda low, high: low,
    )


def test_endpoint_snapshot_is_operation_versioned_and_get_only():
    assert SEARCH_INSTRUMENTS.path == "/api/v1/market-data/search"
    assert GET_MARKET_RATES.path == "/api/v1/market-data/instruments/rates"
    assert (
        GET_DEMO_AGGREGATE_PORTFOLIO.path
        == "/api/v1/trading/info/demo/aggregate-portfolio"
    )
    assert {endpoint.method for endpoint in READ_ONLY_ENDPOINTS} == {"GET"}
    assert all("/real/" not in endpoint.path for endpoint in READ_ONLY_ENDPOINTS)


def test_checked_in_config_is_disabled_read_only_and_locally_throttled():
    config = EtoroDemoReadOnlyConfig.load(ETORO_DEMO_CONFIG_PATH)
    assert not config.enabled
    assert config.account_reads_per_minute == 48
    assert config.market_reads_per_minute == 96
    assert config.writes_per_minute == 10


def test_wrong_environment_or_write_enabled_config_is_rejected(tmp_path):
    raw = yaml.safe_load(ETORO_DEMO_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["environment"] = "real"
    path = tmp_path / "wrong_environment.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(EtoroConfigurationError, match="Demo"):
        EtoroDemoReadOnlyConfig.load(path)

    raw["environment"] = "demo"
    raw["write_methods_enabled"] = True
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(EtoroConfigurationError, match="write"):
        EtoroDemoReadOnlyConfig.load(path)


def test_exact_search_and_market_rates_use_auth_and_unique_request_ids():
    transport = FakeTransport(
        [_json_response("search_aapl.json"), _json_response("rates_aapl.json")]
    )
    client = _client(transport)
    search = client.search_instruments("AAPL")
    rates = client.get_market_rates((1001,))

    assert search.items[0].symbol == "AAPL"
    assert rates.rates[0].price_rate_id == "778899"
    assert "internalSymbolFull=AAPL" in transport.requests[0].url
    assert "instrumentIds=1001" in transport.requests[1].url
    assert transport.requests[0].headers["x-api-key"] == "demo-api-secret"
    assert transport.requests[0].headers["x-user-key"] == "demo-user-secret"
    assert (
        transport.requests[0].headers["x-request-id"]
        != transport.requests[1].headers["x-request-id"]
    )


def test_demo_portfolio_contract_parses_account_and_positions():
    transport = FakeTransport([_json_response("demo_portfolio.json")])
    snapshot = _client(transport).get_demo_portfolio()
    assert str(snapshot.totals.total_value) == "10000"
    assert snapshot.instrument_aggregates[0].instrument_id == 1001
    assert snapshot.instrument_aggregates[0].average_leverage == 1
    assert snapshot.snapshot_id


def test_retry_after_is_honoured_and_each_retry_gets_a_new_request_id():
    sleeps: list[float] = []
    transport = FakeTransport(
        [
            HttpResponse(status_code=429, headers={"Retry-After": "2"}, body=b"secret"),
            _json_response("search_aapl.json"),
        ]
    )
    request_ids = iter(
        [
            UUID("00000000-0000-4000-8000-000000000001"),
            UUID("00000000-0000-4000-8000-000000000002"),
        ]
    )
    client = EtoroReadOnlyClient(
        credentials=EtoroCredentials(api_key="api", user_key="user"),
        config=_config(),
        transport=transport,
        request_id_factory=lambda: next(request_ids),
        sleeper=sleeps.append,
        jitter=lambda low, high: low,
    )
    assert client.search_instruments("AAPL").items[0].instrument_id == 1001
    assert sleeps == [2.0]
    assert len({item.headers["x-request-id"] for item in transport.requests}) == 2


def test_auth_and_rate_errors_never_include_body_or_credentials():
    auth_transport = FakeTransport(
        [HttpResponse(status_code=401, headers={}, body=b"demo-api-secret payload-secret")]
    )
    with pytest.raises(EtoroAuthenticationError) as captured:
        _client(auth_transport).search_instruments("AAPL")
    assert "demo-api-secret" not in str(captured.value)
    assert "payload-secret" not in str(captured.value)

    rate_transport = FakeTransport(
        [HttpResponse(status_code=429, headers={}, body=b"secret")] * 3
    )
    with pytest.raises(EtoroRateLimitError):
        _client(rate_transport).search_instruments("AAPL")


@pytest.mark.parametrize(
    ("responses", "error_type"),
    [
        ([HttpResponse(status_code=404, headers={}, body=b"secret")], EtoroNotFoundError),
        ([HttpResponse(status_code=500, headers={}, body=b"secret")] * 3, EtoroServerError),
        ([TimeoutError("secret timeout")] * 3, EtoroTransportError),
    ],
)
def test_not_found_server_and_timeout_faults_fail_closed(responses, error_type):
    with pytest.raises(error_type) as captured:
        _client(FakeTransport(responses)).search_instruments("AAPL")
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(status_code=200, headers={}, body=b"not-json"),
        HttpResponse(
            status_code=200,
            headers={},
            body=json.dumps({"items": [{"instrumentId": 1}]}).encode(),
        ),
    ],
)
def test_malformed_or_drifted_contract_fails_closed(response):
    with pytest.raises(EtoroSchemaError):
        _client(FakeTransport([response])).search_instruments("AAPL")


def test_secret_redaction_covers_headers_environment_names_and_values():
    value = (
        "x-api-key=abc x-user-key: def ETORO_DEMO_API_KEY=ghi "
        "ETORO_REAL_USER_KEY=jkl hidden-value"
    )
    redacted = redact_text(value, ["hidden-value"])
    for secret in ("abc", "def", "ghi", "jkl", "hidden-value"):
        assert secret not in redacted
    credentials = EtoroCredentials(api_key="api-secret", user_key="user-secret")
    assert "api-secret" not in repr(credentials)
    assert "user-secret" not in repr(credentials)


def test_adapter_exact_mapping_quotes_and_incomplete_account_evidence(tmp_path):
    transport = FakeTransport(
        [
            _json_response("search_aapl.json"),
            _json_response("rates_aapl.json"),
            _json_response("demo_portfolio.json"),
        ]
    )
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
    adapter = EtoroDemoReadOnlyAdapter(client=_client(transport), registry=registry)
    mapping = adapter.resolve_instrument(instrument)
    assert mapping.broker_instrument_id == 1001
    quote = adapter.get_rates((instrument.internal_instrument_id,))[0]
    assert str(quote.ask) == "211.15"
    account = adapter.get_account_snapshot()
    assert account.environment == "demo"
    assert not account.pending_orders_complete
    assert not account.executions_complete
    with pytest.raises(BrokerDataUnavailable):
        adapter.get_pending_orders()


def test_adapter_snapshot_writes_only_a_non_economic_read_model(tmp_path):
    canonical = tmp_path / "portfolio_state.csv"
    canonical.write_bytes(b"position_id,quantity\nP1,2\n")
    before = canonical.read_bytes()
    transport = FakeTransport([_json_response("demo_portfolio.json")])
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    adapter = EtoroDemoReadOnlyAdapter(
        client=_client(transport),
        registry=InstrumentRegistry(store),
    )
    read_model = tmp_path / "broker_etoro_demo_read_model.json"
    adapter.capture_reconciliation_read_model(read_model)
    payload = json.loads(read_model.read_text(encoding="utf-8"))
    assert payload["authority"] == "broker_reconciliation_read_only"
    assert payload["pending_orders_complete"] is False
    assert canonical.read_bytes() == before


def test_adapter_rejects_inexact_exchange_and_all_write_capabilities(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(
        Instrument.create(
            canonical_symbol="AAPL",
            exchange="LSE",
            asset_type="equity",
            currency="GBP",
        )
    )
    adapter = EtoroDemoReadOnlyAdapter(
        client=_client(FakeTransport([_json_response("search_aapl.json")])),
        registry=registry,
    )
    with pytest.raises(InstrumentMappingError):
        adapter.resolve_instrument(instrument)

    command = BrokerCommand.create(
        intent_hash="a" * 64,
        operation="submit_order",
        broker="etoro",
        environment="demo",
        broker_payload={"instrumentId": 1001},
    )
    with pytest.raises(BrokerWriteDisabled):
        adapter.submit_order(None, command)
    with pytest.raises(BrokerWriteDisabled):
        adapter.close_position(broker_position_id="position-1", command=command)
    with pytest.raises(BrokerWriteDisabled):
        adapter.partial_close_position(
            broker_position_id="position-1", units=1, command=command
        )
    with pytest.raises(BrokerWriteDisabled):
        adapter.cancel_order(broker_order_id="order-1", command=command)
