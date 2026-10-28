"""The optional buffer mode cannot become authority by changing an assessment."""
from datetime import timedelta
import json

import pytest

from brokers.etoro import autonomous_api
from execution.domain import CommandState, OrderIntent, payload_hash
from execution.pilot_assessment import assess_order
from execution.pilot_mandate import PilotBufferAmendment, PilotMandateError
from tests.test_autonomous_api import full_lookup
from tests.test_autonomous_pilot import f, submit, reconcile, waive_usage
from tests.test_pilot_account_source import NOW


def unreported(f):
    evidence = f.assessed.evidence
    eligible = evidence["eligibility_receipt"]
    envelope = json.loads(eligible["result"]["content"][0]["text"])
    body = json.loads(envelope["body"])
    body["eligibilities"][0].pop("additionalBufferPercent")
    envelope["body"] = json.dumps(body)
    eligible["result"]["content"][0]["text"] = json.dumps(envelope)
    f.assessed = assess_order(cost_receipt=evidence["cost_receipt"], eligibility_receipt=eligible,
        payload=evidence["payload"], quote=f.quote, now=NOW, allow_unreported_buffer=True)
    intent = OrderIntent.create(strategy_id="synthetic-standing", run_id="synthetic-buffer-amendment",
        internal_instrument_id=f.quote.internal_instrument_id, environment="demo", side="buy",
        order_type="market", sizing_method="fixed_notional", sizing_value="10", currency="USD",
        expires_at=NOW+timedelta(minutes=3))
    f.command = f.op.prepare(intent=intent, account=f.pre, quote=f.quote,
        assessment=f.assessed, now=NOW)
    lookup = full_lookup(f.command.logical_request_id)
    lookup["asset"]["instrumentId"] = 1142
    lookup["positionExecutions"][0]["openingData"]["executionTime"] = (NOW+timedelta(seconds=1)).isoformat()
    f.transport.lookup = lookup


def amendment(f):
    return PilotBufferAmendment.create(f.root / "pilot-buffer-amendment.json", mandate=f.mandate,
        ledger=f.ledger, source_reference="Synthetic human authorizes only unknown-buffer admission", issued_at=NOW)


def test_unknown_buffer_without_bound_amendment_cannot_consume_claim(f):
    unreported(f)
    with pytest.raises(RuntimeError, match="bound_buffer_rule_amendment_required"):
        submit(f, usage=None, usage_waiver=waive_usage(f))
    assert not f.ledger.snapshot()["submissions"] and not f.transport.posts


def test_amended_buffer_keeps_original_order_and_reconciles_once(f):
    original = f.mandate.path.read_bytes()
    allowed = amendment(f)
    unreported(f)
    result = submit(f, buffer_amendment=allowed, usage=None, usage_waiver=waive_usage(f))
    assert result.state == CommandState.ACKNOWLEDGED
    approval = f.op.coordinator.store.get_command_approval(f.command.command_id)
    assert dict(approval.limits)["buffer_amendment_hash"] == allowed.amendment_hash
    assert dict(approval.limits)["buffer_amendment_source_reference"] == allowed.source_reference
    assert f.assessed.evidence["additional_buffer"]["status"] == "not_reported"
    assert f.assessed.evidence["additional_buffer"]["reported_percent"] is None
    assert json.loads(f.transport.posts[0].body)["instrumentId"] == 1142
    assert not reconcile(f)["unresolved"] and len(f.transport.posts) == 1
    assert f.mandate.path.read_bytes() == original


def test_changed_buffer_authorization_blocks_before_claim(f):
    allowed = amendment(f)
    unreported(f)
    allowed.path.write_text("{}")
    with pytest.raises(PilotMandateError, match="buffer_amendment_record_shape_invalid"):
        submit(f, buffer_amendment=allowed)
    assert not f.ledger.snapshot()["submissions"] and not f.transport.posts


def test_buffer_amendment_must_match_assessment_mode(f):
    with pytest.raises(RuntimeError, match="buffer_amendment_assessment_mode_mismatch"):
        submit(f, buffer_amendment=amendment(f))
    assert not f.ledger.snapshot()["submissions"] and not f.transport.posts


@pytest.mark.parametrize("change", ["deleted", "rehashed_replacement"])
def test_buffer_authority_is_rechecked_after_durable_request_before_wire(f, monkeypatch, change):
    from scripts import run_autonomous_demo_pilot as runner
    from tests.test_fast_autonomous_pilot import authorize_buffer, wire_fast

    args, _, events, reads = wire_fast(f, monkeypatch)
    allowed = authorize_buffer(f, args)
    reads.omit_buffer = True
    original = autonomous_api.private_json
    claimed_requests = []

    def change_after_receipt(path, value):
        result = original(path, value)
        if path.name.startswith("submit-") and path.name.endswith(".request.json"):
            claimed_requests.append(path)
            if change == "deleted":
                allowed.path.unlink()
            else:
                record = json.loads(allowed.path.read_text())
                record["amendment"]["source_reference"] = "Changed after durable claim"
                record["sha256"] = payload_hash(record["amendment"])
                allowed.path.write_text(json.dumps(record))
            lookup = full_lookup(value["request_id"])
            lookup["asset"]["instrumentId"] = 1142
            lookup["status"] = {"id":1}
            lookup["positionExecutions"] = []
            f.transport.lookup = lookup
        return result

    monkeypatch.setattr(autonomous_api, "private_json", change_after_receipt)
    runner.locked_cycle(args, f.root, f.ledger)
    state = f.ledger.snapshot()
    assert len(claimed_requests) == 1 and claimed_requests[0].exists()
    assert len(state["submissions"]) == 1 and state["unresolved"] and not f.transport.posts
    command = f.op.coordinator.store.get_command(state["submissions"][0]["command_id"])
    assert command.state == CommandState.UNKNOWN
    failure_path = claimed_requests[0].with_name(claimed_requests[0].name.replace(".request.json", ".failure.json"))
    assert json.loads(failure_path.read_text())["outcome"] == "pre_transport_guard_blocked"
    assert [event["event"] for event in events] == ["submitted_once", "lookup_hold"]
    before = len(f.transport.calls)
    with pytest.raises((PilotMandateError, ValueError)):
        runner.locked_cycle(args, f.root, f.ledger)
    assert len(f.transport.calls) == before and not f.transport.posts
    assert len(f.ledger.snapshot()["submissions"]) == 1
