"""Synthetic contracts only: no live account, credentials, or broker writes."""
from __future__ import annotations

import json
import sqlite3
import stat
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest

import brokers.etoro.autonomous_api as api
from brokers.etoro.autonomous_api import (DirectBindingStore, DirectDemoAdapter, DirectDemoClient,
    DirectDemoError, DirectDemoHttpTransport, build_order_payload, verify_profile)
from brokers.etoro.capture_transport import RawCaptureResponse
from brokers.etoro.client import EtoroCredentials, HttpRequest
from execution.domain import BrokerCommand, CommandAttempt, CommandState, OrderIntent

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
D = Decimal
PROFILE = {"demoCid": 123, "realCid": 987,
           "scopes": ["etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]}
KEYS = EtoroCredentials("synthetic-dedicated-app-secret", "synthetic-dedicated-user-secret")


def raw(value, status=200, outcome="complete"):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return RawCaptureResponse(status, body, "identity", outcome)


def full_lookup(reference):
    return {"accountId": 123, "orderId": 42, "referenceId": str(reference), "action": "open",
            "transaction": "buy", "type": "mkt", "orderCurrency": "USD", "requestedAmount": "10",
            "asset": {"instrumentId": 1001, "side": "long", "settlementType": "real", "leverage": 1, "currency": "USD"},
            "status": {"id": 3}, "positionExecutions": [{"positionId": 500, "state": "open",
                "investedAmountCurrency": "10", "addedFunds": "0", "remainingUnits": "0.1",
                "openingData": {"orderId": 42, "units": "0.1", "avgPrice": "100", "avgConversionRate": "1",
                    "executionTime": "2026-10-27T09:00:01.0000001+00:00", "priceId": 700, "fees": "0.01", "taxes": "0"}}]}


class SyntheticTransport:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.profile = deepcopy(PROFILE)
        self.post_result = None
        self.lookup = None
        self.identity_delay = 0

    def send(self, request):
        self.calls.append(request)
        if request.url == api.BASE_URL + api.PROFILE_PATH:
            self.clock[0] += timedelta(seconds=self.identity_delay)
            return raw(self.profile)
        if request.method == "POST":
            if isinstance(self.post_result, Exception):
                raise self.post_result
            if self.post_result is not None:
                return self.post_result
            return raw({"orderId": 42, "referenceId": request.headers["x-request-id"], "token": "synthetic-server-token"})
        assert request.url.startswith(api.BASE_URL + api.LOOKUP_PATH + "?")
        return raw(self.lookup)

    @property
    def posts(self):
        return [call for call in self.calls if call.method == "POST"]


def workflow(tmp_path, *, now=NOW):
    clock = [now]
    transport = SyntheticTransport(clock)
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport, clock=lambda: clock[0])
    bindings = DirectBindingStore(tmp_path / "bindings.sqlite3")
    adapter = DirectDemoAdapter(expected_account_id="123", client=client, bindings=bindings, clock=lambda: clock[0], pre_send_guard=lambda: None)
    intent = OrderIntent.create(strategy_id="synthetic", run_id="synthetic-run", internal_instrument_id=uuid4(),
        environment="demo", side="buy", order_type="market", sizing_method="fixed_notional",
        sizing_value="10", currency="USD", expires_at=now + timedelta(minutes=5))
    command = BrokerCommand.create(intent_hash=intent.intent_hash, operation="submit_order", broker="etoro",
                                   environment="demo", broker_payload=build_order_payload(1001, D("10")))
    adapter.bind(command, intent=intent, instrument_id=1001, authorization_hash="synthetic-standing-mandate",
                 expires_at=now + timedelta(seconds=30))
    bindings.authorize(command.command_id, {"authorized_at": now.isoformat(), "valid_until": (now + timedelta(seconds=20)).isoformat()})
    command = replace(command, state=CommandState.SUBMISSION_PENDING, attempt_history=(
        CommandAttempt(1, command.logical_request_id, now, "send_started"),))
    transport.lookup = full_lookup(command.logical_request_id)
    return adapter, command, intent, transport, clock


def test_submit_acceptance_then_full_lookup_preserves_reference_and_exact_source(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    sent = adapter.submit_order(intent, command)
    assert sent.accepted_for_processing and not sent.ambiguous and sent.raw_status == "accepted"
    assert sent.broker_reference_id == str(command.logical_request_id)
    post = transport.posts[0]
    assert post.url == api.BASE_URL + api.SUBMIT_PATH
    assert post.headers["x-request-id"] == str(command.logical_request_id)
    assert json.loads(post.body)["amount"] == 10
    assert b'"amount":10' in post.body and b'"amount":"' not in post.body
    assert b"synthetic-server-token" not in adapter.bindings.path.read_bytes()
    clock[0] += timedelta(seconds=2)
    result = adapter.reconcile_command(replace(command, broker_order_id="42", broker_reference_id=sent.broker_reference_id))
    assert result.reconciled and result.lifecycle_state == "filled"
    execution, = result.executions
    assert execution.executed_amount == D(10) and execution.fees == D(".01")
    assert execution.executed_at_source == "2026-10-27T09:00:01.0000001+00:00"
    assert execution.broker_reference_id == str(command.logical_request_id)
    assert transport.calls[-1].url.endswith("?orderId=42")
    assert len(transport.posts) == 1


@pytest.mark.parametrize("failure", [
    TimeoutError("synthetic-dedicated-user-secret"), raw(b"invalid JSON"), raw({}, 429), raw({}, 500),
    raw({}, 302), raw({}, outcome="partial"), raw({"orderId": 42, "referenceId": str(uuid4())}),
])
def test_uncertain_submit_restart_is_lookup_only_with_preserved_actual_reference(tmp_path, failure):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    transport.post_result = failure
    sent = adapter.submit_order(intent, command)
    assert sent.ambiguous and not sent.accepted_for_processing
    assert sent.broker_reference_id == str(command.logical_request_id)
    restarted = DirectDemoAdapter(expected_account_id="123", client=adapter.client,
        bindings=DirectBindingStore(adapter.bindings.path), clock=lambda: clock[0], pre_send_guard=lambda: None)
    with pytest.raises(DirectDemoError, match="already_dispatched_lookup_only"):
        restarted.submit_order(intent, command)
    assert len(transport.posts) == 1
    clock[0] += timedelta(seconds=2)
    lookup = restarted.reconcile_command(replace(command, state=CommandState.UNKNOWN, broker_reference_id=sent.broker_reference_id))
    assert lookup.lifecycle_state == "filled" and lookup.reconciled
    assert transport.calls[-1].url.endswith("?referenceId=" + str(command.logical_request_id))
    assert len(transport.posts) == 1


def test_client_claim_survives_new_instance_even_without_adapter(tmp_path):
    clock = [NOW]
    transport = SyntheticTransport(clock)
    transport.post_result = TimeoutError("secret must not appear")
    kwargs = dict(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport, clock=lambda: clock[0])
    reference = uuid4()
    with pytest.raises(DirectDemoError, match="transport_outcome_unknown") as error:
        DirectDemoClient(**kwargs).submit_order(build_order_payload(1001, D(10)), reference=reference, pre_transport_guard=lambda: None)
    assert "secret must not appear" not in str(error.value)
    with pytest.raises(DirectDemoError, match="already_claimed"):
        DirectDemoClient(**kwargs).submit_order(build_order_payload(1001, D(10)), reference=reference, pre_transport_guard=lambda: None)
    assert len(transport.posts) == 1


def test_raw_response_saved_before_invalid_parse_and_private_modes(tmp_path):
    clock = [NOW]
    transport = SyntheticTransport(clock)
    transport.post_result = raw(b'{"amount":0.123456789012345678901,"amount":4}')
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport, clock=lambda: clock[0])
    reference = uuid4()
    with pytest.raises(DirectDemoError, match="json_invalid"):
        client.submit_order(build_order_payload(1001, D(10)), reference=reference, pre_transport_guard=lambda: None)
    assert (client.receipt_dir / f"submit-{reference}.response.bin").read_bytes() == transport.post_result.body
    assert stat.S_IMODE(client.receipt_dir.stat().st_mode) == 0o700
    for path in client.receipt_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert KEYS.api_key.encode() not in path.read_bytes() and KEYS.user_key.encode() not in path.read_bytes()
        assert b"x-api-key" not in path.read_bytes()


def test_response_parsing_retains_numeric_decimal_precision(tmp_path):
    clock = [NOW]
    transport = SyntheticTransport(clock)
    transport.lookup = {"placeholder": 1}
    transport.send = lambda request: raw(b'{"value":0.123456789012345678901}')
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport, clock=lambda: clock[0])
    assert client.get_profile()["value"] == D("0.123456789012345678901")


@pytest.mark.parametrize("profile", [
    {"demoCid": 999, "scopes": PROFILE["scopes"]},
    {"realCid": 123, "scopes": PROFILE["scopes"]},
    {"demoCid": 123, "scopes": ["etoro-public:trade.demo:write"]},
    {"demoCid": 123, "scopes": ["etoro-public:trade.demo:read"]},
    {"demoCid": 123, "scopes": ["etoro-public:trade.real:read", "etoro-public:trade.real:write"]},
])
def test_wrong_identity_or_missing_demo_read_write_scopes_refuses_send(tmp_path, profile):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    transport.profile = profile
    with pytest.raises(DirectDemoError, match="identity_read_write_scope"):
        adapter.submit_order(intent, command)
    assert not transport.posts


def test_documented_scope_aliases_are_accepted():
    assert verify_profile({"demoCid": 123, "scopes": ["etoro-public:demo:read", "etoro-public:demo:write"]}, "123") == {"demo_cid": "123"}


def test_expired_during_identity_read_refuses_send(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    clock[0] += timedelta(seconds=15)
    transport.identity_delay = 6
    with pytest.raises(DirectDemoError, match="authorization_expired"):
        adapter.submit_order(intent, command)
    assert not transport.posts


def test_london_midnight_during_identity_read_refuses_send(tmp_path):
    now = datetime(2026, 10, 27, 23, 59, 59, tzinfo=timezone.utc)
    adapter, command, intent, transport, _ = workflow(tmp_path, now=now)
    transport.identity_delay = 2
    with pytest.raises(DirectDemoError, match="binding_expired"):
        adapter.submit_order(intent, command)
    assert not transport.posts


def test_durable_coordinator_attempt_required_before_identity_or_send(tmp_path):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    with pytest.raises(DirectDemoError, match="durable_coordinator_send_claim"):
        adapter.submit_order(intent, replace(command, attempt_history=()))
    assert transport.calls == []


def test_binding_is_immutable_and_reference_cannot_be_rebound(tmp_path):
    adapter, command, intent, _, _ = workflow(tmp_path)
    with pytest.raises(DirectDemoError, match="wire_payload_binding_mismatch"):
        adapter.bind(command, intent=intent, instrument_id=1002, authorization_hash="synthetic-standing-mandate")
    other = replace(command, command_id=uuid4())
    with pytest.raises(DirectDemoError, match="reference_already_bound"):
        adapter.bind(other, intent=intent, instrument_id=1001, authorization_hash="synthetic-standing-mandate")
    with pytest.raises(DirectDemoError, match="binding_immutable"):
        adapter.bind(command, intent=intent, instrument_id=1001, authorization_hash="changed-mandate")


def test_corrupt_binding_refuses_send(tmp_path):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    with sqlite3.connect(adapter.bindings.path) as db:
        db.execute("UPDATE bindings SET payload='{}'")
    with pytest.raises(DirectDemoError, match="binding_corrupt"):
        adapter.submit_order(intent, command)
    assert not transport.calls


@pytest.mark.parametrize("path,value", [
    (("accountId",), 999), (("orderId",), 44), (("referenceId",), str(uuid4())),
    (("action",), "close"), (("transaction",), "sellShort"), (("type",), "mit"),
    (("asset", "instrumentId"), 1002), (("asset", "currency"), "HKD"),
    (("asset", "settlementType"), "cfd"), (("asset", "leverage"), 2), (("asset", "side"), "short"),
    (("orderCurrency",), "GBP"), (("requestedAmount",), "11"), (("requestedUnits",), "0.1"),
])
def test_lookup_rejects_wrong_economics_and_identity(tmp_path, path, value):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    target = transport.lookup
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    with pytest.raises((DirectDemoError, ValueError), match="mismatch"):
        adapter.reconcile_command(command)
    assert len(transport.posts) == 1


@pytest.mark.parametrize("status", [1, 2, 5, 6, 9, 10, 11, 12])
def test_pending_and_partial_never_return_economic_execution(tmp_path, status):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup["status"]["id"] = status
    if status not in {5, 9, 10}:
        transport.lookup["positionExecutions"] = []
    result = adapter.reconcile_command(command)
    assert not result.reconciled and not result.executions
    assert len(transport.posts) == 1


@pytest.mark.parametrize("timestamp", [
    "2026-10-27T09:00:01.0000001", "2026-10-27T09:00:01-00:00",
    "2026-10-27T09:00:02.0000001Z", "2026-10-27T08:59:59.9999999Z",
    "2026-10-27T09:00:01.12345678Z",
])
def test_execution_time_requires_exact_known_offset_and_command_window(tmp_path, timestamp):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    transport.lookup["positionExecutions"][0]["openingData"]["executionTime"] = timestamp
    with pytest.raises(DirectDemoError, match="execution_time"):
        adapter.reconcile_command(command)


@pytest.mark.parametrize("change", ["missing_fees", "fx", "negative_tax", "closed", "added_funds", "principal", "duplicate", "missing_positions"])
def test_lookup_rejects_incomplete_or_changed_opening_economics(tmp_path, change):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    position = transport.lookup["positionExecutions"][0]
    if change == "missing_fees": del position["openingData"]["fees"]
    elif change == "fx": position["openingData"]["avgConversionRate"] = ".13"
    elif change == "negative_tax": position["openingData"]["taxes"] = "-.01"
    elif change == "closed": position["state"] = "closed"
    elif change == "added_funds": position["addedFunds"] = "1"
    elif change == "principal": position["investedAmountCurrency"] = "9"
    elif change == "duplicate": transport.lookup["positionExecutions"].append(deepcopy(position))
    elif change == "missing_positions": del transport.lookup["positionExecutions"]
    with pytest.raises(ValueError):
        adapter.reconcile_command(command)
    assert len(transport.posts) == 1


def test_repeated_lookup_idempotent_but_changed_execution_rejected(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    adapter.submit_order(intent, command)
    clock[0] += timedelta(seconds=2)
    first = adapter.reconcile_command(command)
    count = len(adapter.bindings.outcomes(command.command_id))
    assert adapter.reconcile_command(command) == first
    assert len(adapter.bindings.outcomes(command.command_id)) == count
    transport.lookup["positionExecutions"][0]["openingData"]["fees"] = ".02"
    with pytest.raises(DirectDemoError, match="economics_changed"):
        adapter.reconcile_command(command)


@pytest.mark.parametrize("value", [0, -1, 11, float("nan"), float("inf"), True])
def test_transport_timeout_is_finite_and_at_most_ten_seconds(tmp_path, value):
    with pytest.raises(DirectDemoError, match="bounded_timeout"):
        DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", timeout_seconds=value)


def test_client_cannot_send_extended_order_shape(tmp_path):
    transport = SyntheticTransport([NOW])
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport)
    for change in ({"settlementType": "cfd"}, {"amount": 10.0}, {"leverage": 2}, {"action": "close"}, {"symbol": "AAPL"}):
        payload = build_order_payload(1001, D(10)) | change
        with pytest.raises(DirectDemoError):
            client.submit_order(payload, reference=uuid4(), pre_transport_guard=lambda: None)
    assert transport.calls == []


def test_transport_pins_host_and_ignores_environment_proxy_cookies_redirects(monkeypatch):
    calls = []
    monkeypatch.setenv("HTTPS_PROXY", "http://malicious.invalid")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/malicious/ca")
    class Response:
        status_code = 302
        headers = {"Location": "https://malicious.invalid", "Set-Cookie": "secret=value"}
        raw = BytesIO(b'{"redirect":true}')
        def __enter__(self): return self
        def __exit__(self, *args): return False
    # BytesIO.read1 does not accept decode_content; implement the urllib3 shape.
    body = BytesIO(b'{"redirect":true}')
    Response.raw = type("Stream", (), {"read1": lambda self, n, decode_content: body.read(n)})()
    def send(session, request, **kwargs):
        assert session.trust_env is False and session.auth is None
        assert session.proxies == {} and not session.cookies
        assert session.get_adapter(request.url).max_retries.total == 0
        assert kwargs == dict(timeout=10, stream=True, allow_redirects=False, verify=True, proxies={})
        assert "Cookie" not in request.headers and "Authorization" not in request.headers
        calls.append(request)
        return Response()
    monkeypatch.setattr(api._NoRedirectSession, "send", send)
    request = HttpRequest("GET", api.BASE_URL + api.PROFILE_PATH, {"x-request-id": str(uuid4())}, 10, None)
    response = DirectDemoHttpTransport().send(request)
    assert response.status_code == 302 and response.body == b'{"redirect":true}'
    assert len(calls) == 1
    for url in ("http://public-api.etoro.com/api/v1/me", "https://public-api.etoro.com.evil/api/v1/me",
                api.BASE_URL + "/api/v2/trading/execution/real/orders", api.BASE_URL + "/api/v3/trading/execution/demo/orders"):
        with pytest.raises(DirectDemoError, match="transport_boundary"):
            DirectDemoHttpTransport().send(replace(request, url=url))
    assert len(calls) == 1


def test_guard_is_required_and_rechecked_after_identity_read(tmp_path):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    adapter.pre_send_guard = None
    with pytest.raises(DirectDemoError, match="pre_send_guard_required"):
        adapter.submit_order(intent, command)
    assert not transport.posts
    checked = []
    def newly_engaged_kill():
        checked.append(len(transport.calls))
        return False
    adapter.pre_send_guard = newly_engaged_kill
    with pytest.raises(DirectDemoError, match="pre_send_guard_blocked"):
        adapter.submit_order(intent, command)
    assert checked == [2] and not transport.posts


def test_guard_delay_cannot_extend_dispatch_authorization(tmp_path):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    def slow_guard():
        clock[0] += timedelta(seconds=21)
    adapter.pre_send_guard = slow_guard
    with pytest.raises(DirectDemoError, match="authorization_expired"):
        adapter.submit_order(intent, command)
    assert not transport.posts


def test_reference_and_both_durable_claims_exist_before_network_mutation(tmp_path):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    original = transport.send
    observed = []
    def inspect_before_send(request):
        if request.method == "POST":
            reference = str(command.logical_request_id)
            request_record = json.loads((adapter.client.receipt_dir / f"submit-{reference}.request.json").read_bytes())
            assert request_record["request_id"] == request.headers["x-request-id"] == reference
            with sqlite3.connect(adapter.bindings.path) as db:
                rows = db.execute("SELECT command_id,reference_id FROM dispatches").fetchall()
            assert rows == [(str(command.command_id), reference)]
            observed.append(reference)
        return original(request)
    transport.send = inspect_before_send
    assert adapter.submit_order(intent, command).accepted_for_processing
    assert observed == [str(command.logical_request_id)]


def test_failed_presend_receipt_never_reaches_network_and_retains_adapter_claim(tmp_path, monkeypatch):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    original = api.private_json
    def disk_full(path, value):
        if path.name.startswith("submit-") and path.name.endswith(".request.json"):
            raise OSError("synthetic disk failure")
        return original(path, value)
    monkeypatch.setattr(api, "private_json", disk_full)
    assert adapter.submit_order(intent, command).ambiguous
    assert not transport.posts
    monkeypatch.setattr(api, "private_json", original)
    with pytest.raises(DirectDemoError, match="already_dispatched_lookup_only"):
        adapter.submit_order(intent, command)
    assert not transport.posts


@pytest.mark.parametrize("stage", ["claim", "request_fsync", "binding_read", "authorization_read", "final_guard"])
def test_final_wire_guard_rechecks_clock_after_all_persistence_and_guard_work(tmp_path, monkeypatch, stage):
    adapter, command, intent, transport, clock = workflow(tmp_path)
    if stage == "claim":
        original = adapter.bindings.claim
        def delayed_claim(*args):
            original(*args)
            clock[0] += timedelta(seconds=21)
        monkeypatch.setattr(adapter.bindings, "claim", delayed_claim)
    elif stage == "request_fsync":
        original = api.private_json
        def delayed_fsync(path, value):
            original(path, value)
            if path.name.startswith("submit-") and path.name.endswith(".request.json"):
                clock[0] += timedelta(seconds=21)
        monkeypatch.setattr(api, "private_json", delayed_fsync)
    elif stage == "binding_read":
        original = adapter.bindings.read
        reads = []
        def delayed_read(*args):
            result = original(*args)
            reads.append(1)
            if len(reads) == 3:
                clock[0] += timedelta(seconds=21)
            return result
        monkeypatch.setattr(adapter.bindings, "read", delayed_read)
    elif stage == "authorization_read":
        original = adapter.bindings.read_authorization
        def delayed_authorization(*args):
            result = original(*args)
            clock[0] += timedelta(seconds=21)
            return result
        monkeypatch.setattr(adapter.bindings, "read_authorization", delayed_authorization)
    else:
        calls = []
        def delayed_guard():
            calls.append(1)
            if len(calls) == 2:
                clock[0] += timedelta(seconds=21)
        adapter.pre_send_guard = delayed_guard
    result = adapter.submit_order(intent, command)
    assert result.ambiguous and result.broker_reference_id == str(command.logical_request_id)
    assert not transport.posts
    with sqlite3.connect(adapter.bindings.path) as db:
        assert db.execute("SELECT reference_id FROM dispatches").fetchall() == [(str(command.logical_request_id),)]
    receipt = adapter.client.receipt_dir / f"submit-{command.logical_request_id}.request.json"
    assert receipt.exists()
    failure = json.loads(receipt.with_name(f"submit-{command.logical_request_id}.failure.json").read_bytes())
    assert failure["outcome"] == "pre_transport_guard_blocked"


def test_kill_revoked_during_request_fsync_blocks_wire_after_claim(tmp_path, monkeypatch):
    adapter, command, intent, transport, _ = workflow(tmp_path)
    allowed = [True]
    adapter.pre_send_guard = lambda: allowed[0]
    original = api.private_json
    def revoke_after_fsync(path, value):
        original(path, value)
        if path.name.startswith("submit-") and path.name.endswith(".request.json"):
            allowed[0] = False
    monkeypatch.setattr(api, "private_json", revoke_after_fsync)
    assert adapter.submit_order(intent, command).ambiguous
    assert not transport.posts
    allowed[0] = True
    with pytest.raises(DirectDemoError, match="already_dispatched_lookup_only"):
        adapter.submit_order(intent, command)
    assert not transport.posts


def test_london_day_cannot_roll_over_during_request_fsync(tmp_path, monkeypatch):
    now = datetime(2026, 10, 27, 23, 59, 59, tzinfo=timezone.utc)
    adapter, command, intent, transport, clock = workflow(tmp_path, now=now)
    original = api.private_json
    def cross_midnight(path, value):
        original(path, value)
        if path.name.startswith("submit-") and path.name.endswith(".request.json"):
            clock[0] += timedelta(seconds=2)
    monkeypatch.setattr(api, "private_json", cross_midnight)
    assert adapter.submit_order(intent, command).ambiguous
    assert not transport.posts


def test_client_requires_final_guard_even_for_independent_caller(tmp_path):
    transport = SyntheticTransport([NOW])
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport)
    with pytest.raises(DirectDemoError, match="pre_transport_guard_required"):
        client.submit_order(build_order_payload(1001, D(10)), reference=uuid4(), pre_transport_guard=None)
    assert not transport.calls


def test_compressed_response_is_preserved_without_unbounded_decompression(tmp_path):
    import gzip
    clock = [NOW]
    transport = SyntheticTransport(clock)
    body = gzip.compress(b' ' * (9 * 1024 * 1024) + b'{}')
    transport.post_result = RawCaptureResponse(200, body, "gzip", "complete")
    client = DirectDemoClient(credentials=KEYS, receipt_dir=tmp_path / "receipts", transport=transport, clock=lambda: clock[0])
    reference = uuid4()
    with pytest.raises(DirectDemoError, match="encoding_unsupported"):
        client.submit_order(build_order_payload(1001, D(10)), reference=reference, pre_transport_guard=lambda: None)
    assert (client.receipt_dir / f"submit-{reference}.response.bin").read_bytes() == body
    assert len(transport.posts) == 1
