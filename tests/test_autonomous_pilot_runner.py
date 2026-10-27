"""Actual bounded worker wiring with local receipts and a synthetic transport."""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from brokers.etoro import autonomous_api
from brokers.etoro.autonomous_api import DirectDemoClient
from execution.domain import CommandState, payload_hash
from execution.pilot_mandate import PilotUsageAmendment
from execution.usage_reserve import PilotUsageAdmission, UsageAdmission, UsageWindow
from scripts import run_autonomous_demo_pilot as runner
from tests.test_autonomous_api import KEYS, full_lookup
from tests.test_autonomous_pilot import account, f, submit
from tests.test_pilot_account_source import NOW, receipt


def wire_runner(fixture, monkeypatch, *, remaining=D(19), lookup_pending=False):
    calls, events = [], []
    classifications = (
        (1142, "SBUX", "Starbucks", "consumer", "equity"),
        (1002, "MSFT", "Microsoft", "technology", "equity"),
    )
    metadata = {"results": [{"instrumentId": value[0], "symbol": value[1], "type": "Stocks"}
                            for value in classifications], "pagination": {"hasNext": False}}
    args = SimpleNamespace(reviewed_commit="a" * 40, work_floor_percent=D(10),
                           usage_authorization="synthetic current standing authorization")

    def lookup_for(reference):
        result = full_lookup(reference)
        result["asset"]["instrumentId"] = 1142
        result["positionExecutions"][0]["openingData"]["executionTime"] = (NOW + timedelta(seconds=1)).isoformat()
        return result

    original_send = fixture.transport.send

    def send(request):
        if request.method == "POST":
            # The worker creates its own command. Lookup must echo that actual
            # request header, never the fixture's earlier prepared command.
            fixture.transport.lookup = lookup_for(request.headers["x-request-id"])
            response = original_send(request)
            fixture.clock[0] = NOW + timedelta(seconds=2)
            return response
        if lookup_pending and "referenceId=" in request.url:
            reference = parse_qs(urlsplit(request.url).query)["referenceId"][0]
            fixture.transport.lookup = lookup_for(reference)
            fixture.transport.lookup["status"] = {"id": 1}
            fixture.transport.lookup["positionExecutions"] = []
        elif fixture.transport.posts:
            fixture.transport.lookup = lookup_for(fixture.transport.posts[0].headers["x-request-id"])
        return original_send(request)

    monkeypatch.setattr(fixture.transport, "send", send)

    class Host:
        def __init__(self, root):
            assert root == fixture.root
            calls.append("host")

        def usage(self):
            calls.append("usage")
            return UsageAdmission(fixture.clock[0], (UsageWindow("weekly", remaining, True),),
                                  ("weekly",), "synthetic-current-usage", "a" * 64)

        def read(self, path, **arguments):
            calls.append(path)
            if path == "/api/v2/market-data/instruments":
                assert arguments == {"query": {"instrumentsIds": "1142,1002"}}
                elapsed = (fixture.clock[0] - NOW).total_seconds()
                result = receipt(path, metadata, elapsed, elapsed)
                result["arguments"].update(arguments)
                return result
            source = {"/api/v2/trading/info/demo/costs": "cost_receipt",
                      "/api/v2/trading/info/demo/eligibility": "eligibility_receipt"}[path]
            result = deepcopy(fixture.assessed.evidence[source])
            assert json.loads(arguments["body"]) == json.loads(result["arguments"]["body"])
            result["arguments"] = {"path": path, **arguments}
            return result

        def market(self, instrument):
            calls.append("market")
            assert instrument.internal_instrument_id == fixture.quote.internal_instrument_id
            return fixture.quote, {"instrument_id": 1142, "quote_type": "realtime",
                "quote_id": fixture.quote.quote_id, "received_at": fixture.clock[0].isoformat(),
                "isCurrentlyTradable": True, "isExchangeOpen": True, "isActiveInPlatform": True,
                "isBuyEnabled": True, "isDelisted": False}

        def account(self, expected):
            assert expected == "123"
            if fixture.transport.posts:
                calls.append("post_account")
                fixture.clock[0] = NOW + timedelta(seconds=8)
                return account(fixture.clock[0], post=True)
            calls.append("pre_account")
            return account(fixture.clock[0])

    def credentials(path):
        assert path == fixture.root / "demo-api-credentials.json"
        calls.append("dedicated_credentials")
        return KEYS

    def client(**kwargs):
        assert kwargs == {"credentials": KEYS, "receipt_dir": fixture.root / "direct-api-receipts"}
        calls.append("direct_client")
        return DirectDemoClient(**kwargs, transport=fixture.transport, clock=lambda: fixture.clock[0])

    def reviewed(commit):
        assert commit == args.reviewed_commit
        calls.append("reviewed_checkout")

    monkeypatch.setattr(runner, "ReadOnlyHost", Host)
    monkeypatch.setattr(runner, "load_dedicated_credentials", credentials)
    monkeypatch.setattr(runner, "DirectDemoClient", client)
    monkeypatch.setattr(runner, "verify_reviewed_checkout", reviewed)
    monkeypatch.setattr(runner, "NOW", lambda: fixture.clock[0])
    monkeypatch.setattr(runner, "CLASSIFICATIONS", classifications)
    monkeypatch.setattr(runner, "emit", lambda event: events.append(deepcopy(event)))
    return args, calls, events


def test_fresh_runner_cycle_submits_and_reconciles_actual_new_command_once(f, monkeypatch):
    args, calls, events = wire_runner(f, monkeypatch)
    inception = f.ledger.snapshot()["inception"]
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    assert len(state["submissions"]) == state["position_count"] == 1
    assert not state["unresolved"] and state["inception"] == inception
    assert state["pilot_pnl"] == D("-.03")
    command = f.op.coordinator.store.get_command(state["submissions"][0]["command_id"])
    assert command.command_id != f.command.command_id
    assert command.state == CommandState.RECONCILED
    assert len(f.transport.posts) == 1
    assert f.transport.posts[0].headers["x-request-id"] == str(command.logical_request_id)
    assert [event["event"] for event in events] == ["submitted_once", "reconciled"]
    assert events[-1]["standing_mandate_exhausted"] is True
    assert calls.count("usage") == 2 and calls.count("market") == 2
    assert calls.index("pre_account") < calls.index("post_account")
    assert "reviewed_checkout" in calls
    before = list(calls)
    runner.locked_cycle(args, f.root, f.ledger)
    assert calls == before and events[-1]["event"] == "already_completed"
    assert len(f.transport.posts) == 1


def test_claimed_runner_recovery_uses_retained_reference_without_new_preparation(f, monkeypatch):
    f.transport.post_result = TimeoutError("synthetic response lost after send")
    assert submit(f).state == CommandState.UNKNOWN
    assert len(f.transport.posts) == 1 and f.ledger.snapshot()["unresolved"]
    f.clock[0] = NOW + timedelta(seconds=8)
    args, calls, events = wire_runner(f, monkeypatch)
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    assert not state["unresolved"] and state["submissions"][0]["reconciled"]
    assert state["submissions"][0]["command_id"] == str(f.command.command_id)
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.RECONCILED
    assert len(f.transport.posts) == 1
    assert any(request.url.endswith("?referenceId=" + str(f.command.logical_request_id)) for request in f.transport.calls)
    assert calls.count("usage") == 1 and calls.count("post_account") == 1
    assert "market" not in calls and "pre_account" not in calls and "reviewed_checkout" not in calls
    assert "/api/v2/trading/info/demo/costs" not in calls
    assert "/api/v2/trading/info/demo/eligibility" not in calls
    assert [event["event"] for event in events] == ["reconciled"]


def amend_usage(fixture, args):
    args.work_floor_percent = D(5)
    args.usage_authorization = "synthetic current human authorization for five percent"
    return PilotUsageAmendment.create(fixture.root / "pilot-usage-amendment.json",
        mandate=fixture.mandate, ledger=fixture.ledger,
        source_reference=args.usage_authorization, issued_at=NOW)


def test_amended_runner_submits_at_ten_remaining_and_keeps_original_inception(f, monkeypatch):
    args, calls, events = wire_runner(f, monkeypatch, remaining=D(10))
    amendment = amend_usage(f, args)
    original_mandate = f.mandate.path.read_bytes()
    inception = f.ledger.snapshot()["inception"]
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    command_id = state["submissions"][0]["command_id"]
    approval = f.op.coordinator.store.get_command_approval(command_id)
    limits = dict(approval.limits)
    assert limits["usage_work_floor_percent"] == limits["usage_recovery_reserve_percent"] == "5"
    assert limits["usage_amendment_hash"] == amendment.amendment_hash
    assert limits["usage_floor_authorization_reference"] == args.usage_authorization
    assert len(f.transport.posts) == state["position_count"] == 1 and not state["unresolved"]
    assert state["inception"] == inception and f.mandate.path.read_bytes() == original_mandate
    assert calls.count("usage") == 2
    assert [event["event"] for event in events] == ["submitted_once", "reconciled"]


@pytest.mark.parametrize("failure", ["missing", "mismatched_reference"])
def test_lower_floor_authority_is_checked_before_host_or_credentials(f, monkeypatch, failure):
    args, calls, _ = wire_runner(f, monkeypatch, remaining=D(10))
    args.work_floor_percent = D(5)
    if failure == "mismatched_reference":
        amend_usage(f, args)
        args.usage_authorization = "a different human authorization"
    with pytest.raises(ValueError, match="lower_usage_floor"):
        runner.locked_cycle(args, f.root, f.ledger)
    assert calls == [] and f.transport.calls == []
    assert not f.ledger.snapshot()["submissions"]


@pytest.mark.parametrize("remaining", [D(5), None])
def test_amended_runner_stops_at_work_floor_or_unknown_before_credentials(f, monkeypatch, remaining):
    args, calls, _ = wire_runner(f, monkeypatch, remaining=remaining)
    amend_usage(f, args)
    with pytest.raises(ValueError, match="usage_floor_or_unknown_stop"):
        runner.locked_cycle(args, f.root, f.ledger)
    assert calls == ["host", "usage"] and f.transport.calls == []
    assert not f.ledger.snapshot()["submissions"]


def test_amended_lost_response_recovery_uses_reserve_without_another_post(f, monkeypatch):
    args = SimpleNamespace()
    amendment = amend_usage(f, args)
    usage = PilotUsageAdmission(NOW, (UsageWindow("weekly", D(10), True),),
        ("weekly",), "synthetic", "a" * 64, floor_percent=D(5),
        authorization_reference=amendment.source_reference)
    f.transport.post_result = TimeoutError("synthetic response lost after send")
    assert submit(f, usage=usage, usage_amendment=amendment).state == CommandState.UNKNOWN
    assert len(f.transport.posts) == 1 and f.ledger.snapshot()["unresolved"]
    f.clock[0] = NOW + timedelta(seconds=8)
    args, calls, events = wire_runner(f, monkeypatch, remaining=D(7))
    args.work_floor_percent = D(5)
    args.usage_authorization = amendment.source_reference
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    assert len(f.transport.posts) == 1 and len(state["submissions"]) == 1
    assert state["submissions"][0]["command_id"] == str(f.command.command_id)
    assert state["submissions"][0]["reconciled"] and not state["unresolved"]
    assert f.op.coordinator.store.get_command(f.command.command_id).state == CommandState.RECONCILED
    assert any(request.url.endswith("?referenceId=" + str(f.command.logical_request_id))
               for request in f.transport.calls)
    assert calls.count("usage") == 1 and calls.count("post_account") == 1
    assert "market" not in calls and "pre_account" not in calls and "reviewed_checkout" not in calls
    assert "/api/v2/trading/info/demo/costs" not in calls
    assert "/api/v2/trading/info/demo/eligibility" not in calls
    assert [event["event"] for event in events] == ["reconciled"]


@pytest.mark.parametrize("change", ["deleted", "rehashed_replacement"])
def test_amendment_is_rechecked_after_durable_post_receipt_before_transport(f, monkeypatch, change):
    args, _, events = wire_runner(f, monkeypatch, remaining=D(10), lookup_pending=True)
    amendment = amend_usage(f, args)
    original_write = autonomous_api.private_json
    changed = []

    def replace_amendment_after_receipt(path, value):
        result = original_write(path, value)
        if path.name.startswith("submit-") and path.name.endswith(".request.json"):
            changed.append(path)
            if change == "deleted":
                amendment.path.unlink()
            else:
                record = json.loads(amendment.path.read_text())
                record["amendment"]["source_reference"] = "replacement after durable claim"
                record["sha256"] = payload_hash(record["amendment"])
                amendment.path.write_text(json.dumps(record))
        return result

    monkeypatch.setattr(autonomous_api, "private_json", replace_amendment_after_receipt)
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    assert len(changed) == 1 and changed[0].exists()
    assert not f.transport.posts and len(state["submissions"]) == 1 and state["unresolved"]
    command = f.op.coordinator.store.get_command(state["submissions"][0]["command_id"])
    assert command.state == CommandState.UNKNOWN
    failure = json.loads(changed[0].with_name(changed[0].name.replace(".request.json", ".failure.json")).read_text())
    assert failure["outcome"] == "pre_transport_guard_blocked"
    assert [event["event"] for event in events] == ["submitted_once", "lookup_hold"]
