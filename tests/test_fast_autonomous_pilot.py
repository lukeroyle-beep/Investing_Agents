"""Actual direct-read runner wiring using synthetic raw HTTP responses only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from brokers.etoro.autonomous_api import _json_body
from brokers.etoro.capture_transport import RawCaptureResponse
from brokers.etoro.connector import response_object
from brokers.etoro.pilot_reads import DirectPilotReads
from execution.domain import CommandState
from execution.pilot_mandate import PilotUsageWaiver
from scripts import run_autonomous_demo_pilot as runner
from scripts import run_demo_pilot as manual_runner
from tests.test_autonomous_pilot import account, f
from tests.test_autonomous_pilot_runner import wire_runner
from tests.test_pilot_account_source import NOW


def wire_fast(fixture, monkeypatch, *, authorized=True):
    args, calls, events = wire_runner(fixture, monkeypatch)
    args.direct_reads = True
    args.ignore_usage_limits = authorized
    if authorized:
        waiver = PilotUsageWaiver.create(fixture.root / "pilot-usage-waiver.json", mandate=fixture.mandate,
            ledger=fixture.ledger, source_reference="Synthetic explicit quota waiver", issued_at=NOW)
        args.usage_authorization = waiver.source_reference
    monkeypatch.setattr(manual_runner, "NOW", lambda:fixture.clock[0])
    monkeypatch.setattr(runner, "ReadOnlyHost", lambda *a:pytest.fail("No MCP/JSONL host in direct mode"))

    class Transport:
        def __init__(self):
            self.calls, self.open, self.rate_id, self.stale_rate = [], True, 1142, False
            self.omit_buffer = False

        def send(self, request):
            self.calls.append(request)
            path = urlsplit(request.url).path
            if fixture.transport.posts:
                fixture.clock[0] = NOW+timedelta(seconds=8)
            if path == "/api/v2/market-data/instruments":
                payload = {"results":[{"instrumentId":1142,"symbol":"SBUX","type":"Stocks"},
                    {"instrumentId":1002,"symbol":"MSFT","type":"Stocks"}],"pagination":{"hasNext":False}}
            elif path == "/api/v1/market-data/search":
                query = parse_qs(urlsplit(request.url).query)
                assert query["instrumentId"] == ["1142"] and "internalSymbolFull" not in query
                payload = {"totalItems":1,"items":[{"instrumentId":1142,"internalSymbolFull":"SBUX",
                    "isCurrentlyTradable":True,"isExchangeOpen":self.open,"isActiveInPlatform":True,
                    "isBuyEnabled":True,"isDelisted":False}]}
                # The provider always includes instrumentId. Selecting it in
                # fields reproduces the observed duplicate-key response.
                if "instrumentId" in query["fields"][0].split(","):
                    body = _json_body(payload).replace(b'"instrumentId":1142',
                        b'"instrumentId":1142,"instrumentId":1142')
                    return RawCaptureResponse(200, body)
            elif path == "/api/v2/market-data/rates":
                assert parse_qs(urlsplit(request.url).query) == {"instrumentIds":["1142"]}
                when = fixture.clock[0]-timedelta(seconds=31) if self.stale_rate else fixture.clock[0]
                payload = {"results":[{"instrumentId":self.rate_id,"quoteType":"realtime",
                    "date":when.isoformat(),"bid":100,"ask":100}]}
            elif path in {"/api/v2/trading/info/demo/costs", "/api/v2/trading/info/demo/eligibility"}:
                key = "cost_receipt" if path.endswith("/costs") else "eligibility_receipt"
                original = fixture.assessed.evidence[key]
                assert json.loads(request.body) == json.loads(original["arguments"]["body"])
                payload = response_object(original["result"])
                if path.endswith("/eligibility") and self.omit_buffer:
                    payload["eligibilities"][0].pop("additionalBufferPercent")
            else:
                original = account(fixture.clock[0], post=bool(fixture.transport.posts))
                key = {"/api/v1/me":"profile", "/api/v1/trading/info/demo/portfolio":"first",
                    "/api/v1/trading/info/demo/pnl":"pnl"}[path]
                payload = response_object(original.raw_sources[key]["result"])
            return RawCaptureResponse(200, _json_body(payload))

    transport = Transport()
    def reader(**kwargs):
        assert kwargs["receipt_dir"] == fixture.root / "direct-read-receipts"
        calls.append("direct_reader")
        return DirectPilotReads(**kwargs, transport=transport)
    monkeypatch.setattr(runner, "DirectPilotReads", reader)
    return args, calls, events, transport


def test_fast_direct_cycle_keeps_original_instrument_and_raw_sources(f, monkeypatch):
    args, calls, events, transport = wire_fast(f, monkeypatch)
    inception = deepcopy(f.ledger.snapshot()["inception"])
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    assert len(f.transport.posts) == state["position_count"] == len(state["submissions"]) == 1
    assert not state["unresolved"] and state["inception"] == inception
    assert json.loads(f.transport.posts[0].body)["instrumentId"] == 1142
    assert [event["event"] for event in events] == ["submitted_once", "reconciled"]
    assert "host" not in calls and "usage" not in calls
    assert all(source["tool"] == "etoro_direct_read"
        for source in state["submissions"][0]["pre_account"]["raw_sources"].values())
    assert all("execution" not in urlsplit(request.url).path for request in transport.calls)
    for request in transport.calls:
        if urlsplit(request.url).path == "/api/v1/market-data/search":
            assert parse_qs(urlsplit(request.url).query) == {
                "instrumentId":["1142"], "fields":["internalSymbolFull,isCurrentlyTradable,isExchangeOpen,isActiveInPlatform,isBuyEnabled,isDelisted"],
                "pageNumber":["1"], "pageSize":["100"]}
    command = f.op.coordinator.store.get_command(state["submissions"][0]["command_id"])
    assert command.state == CommandState.RECONCILED
    counts = len(transport.calls), len(f.transport.posts)
    runner.locked_cycle(args, f.root, f.ledger)
    assert (len(transport.calls), len(f.transport.posts)) == counts
    assert events[-1]["event"] == "already_completed"


def test_fast_mode_requires_verified_existing_waiver_before_credentials(f, monkeypatch):
    args, calls, _, transport = wire_fast(f, monkeypatch, authorized=False)
    with pytest.raises(ValueError, match="direct_reads_require_existing_usage_waiver"):
        runner.locked_cycle(args, f.root, f.ledger)
    assert not calls and not transport.calls and not f.transport.calls
    assert not f.ledger.snapshot()["submissions"]


@pytest.mark.parametrize("gate", ["closed", "wrong_rate", "stale_rate"])
def test_fast_market_failures_never_prepare_or_claim(f, monkeypatch, gate):
    args, _, _, transport = wire_fast(f, monkeypatch)
    if gate == "closed":
        transport.open = False
    elif gate == "wrong_rate":
        transport.rate_id = 14323
    else:
        transport.stale_rate = True
    with pytest.raises(ValueError):
        runner.locked_cycle(args, f.root, f.ledger)
    assert not f.transport.posts and not f.ledger.snapshot()["submissions"]
    assert not any(request.method == "POST" for request in transport.calls)


def test_fast_host_refuses_session_instrument_before_any_read(f, monkeypatch):
    _, _, _, transport = wire_fast(f, monkeypatch)
    host = runner.DirectPilotHost(f.root, credentials=f.adapter.client._credentials)
    original = f.op.coordinator.registry.get(f.quote.internal_instrument_id)
    with pytest.raises(ValueError, match="original_standing_instrument_required"):
        host.market(replace(original, canonical_symbol="SBUX.RTH"))
    assert not transport.calls


def test_provider_duplicate_id_is_rejected_without_relaxing_json_parser(f, monkeypatch):
    from brokers.etoro.pilot_reads import PilotReadError
    wire_fast(f, monkeypatch)
    host = runner.DirectPilotHost(f.root, credentials=f.adapter.client._credentials)
    with pytest.raises(PilotReadError, match="direct_read_original_json_object_required"):
        host.read("/api/v1/market-data/search", query={"instrumentId":"1142",
            "fields":"instrumentId,internalSymbolFull", "pageNumber":"1", "pageSize":"100"})
    assert not f.transport.posts and not f.ledger.snapshot()["submissions"]


def test_fast_lost_send_recovery_reuses_reference_without_another_post(f, monkeypatch):
    args, _, events, transport = wire_fast(f, monkeypatch)
    f.transport.post_result = TimeoutError("Synthetic lost response")
    original = f.transport.send
    def delayed_loss(request):
        try:
            return original(request)
        finally:
            if request.method == "POST":
                f.clock[0] = NOW+timedelta(seconds=2)
    monkeypatch.setattr(f.transport, "send", delayed_loss)
    runner.locked_cycle(args, f.root, f.ledger)
    assert len(f.transport.posts) == 1
    assert f.ledger.snapshot()["submissions"][0]["reconciled"]
    assert events[-1]["event"] == "reconciled"
    assert any("referenceId=" in request.url for request in f.transport.calls)
    before = len(transport.calls)
    runner.locked_cycle(args, f.root, f.ledger)
    assert len(f.transport.posts) == 1 and len(transport.calls) == before


def authorize_buffer(fixture, args):
    from execution.pilot_mandate import PilotBufferAmendment
    args.allow_unreported_buffer = True
    args.buffer_authorization = "Synthetic explicit human amendment of the temporary buffer rule"
    return PilotBufferAmendment.create(fixture.root / "pilot-buffer-amendment.json",
        mandate=fixture.mandate, ledger=fixture.ledger, source_reference=args.buffer_authorization, issued_at=NOW)


def test_direct_mode_does_not_imply_permission_to_accept_missing_buffer(f, monkeypatch):
    args, _, _, transport = wire_fast(f, monkeypatch)
    transport.omit_buffer = True
    with pytest.raises(ValueError, match="assessment_additional_cash_buffer_unknown"):
        runner.locked_cycle(args, f.root, f.ledger)
    assert not f.transport.posts and not f.ledger.snapshot()["submissions"]


def test_explicit_bound_buffer_amendment_preserves_unknown_and_allows_one_cycle(f, monkeypatch):
    args, _, events, transport = wire_fast(f, monkeypatch)
    amendment = authorize_buffer(f, args)
    transport.omit_buffer = True
    runner.locked_cycle(args, f.root, f.ledger)
    assert len(f.transport.posts) == 1 and f.ledger.snapshot()["position_count"] == 1
    assert events[-1]["event"] == "reconciled"
    command_id = f.ledger.snapshot()["submissions"][0]["command_id"]
    proof = f.op.evidence.read_preparation(command_id)
    assert proof["assessment"]["additional_buffer"]["status"] == "not_reported"
    assert proof["assessment"]["additional_buffer"]["reported_percent"] is None
    approval = f.op.coordinator.store.get_command_approval(command_id)
    assert dict(approval.limits)["buffer_amendment_hash"] == amendment.amendment_hash


def test_buffer_authorization_mismatch_stops_before_credentials_or_read(f, monkeypatch):
    args, calls, _, transport = wire_fast(f, monkeypatch)
    authorize_buffer(f, args)
    args.buffer_authorization = "different source"
    with pytest.raises(ValueError, match="buffer_amendment_authorization_mismatch"):
        runner.locked_cycle(args, f.root, f.ledger)
    assert not calls and not transport.calls and not f.transport.calls
