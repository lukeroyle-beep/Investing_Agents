"""The Demo kill switch opens only after the operator types the acknowledgement."""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from risk.kill_switch import RESET_ACKNOWLEDGEMENT, KillSwitch, KillSwitchError
from scripts import run_demo_pilot
from tests.test_pilot_ledger import NOW


def _switch(tmp_path):
    return KillSwitch(state_path=tmp_path / "kill-switch.json", audit_path=tmp_path / "kill-switch.jsonl")


def _type(monkeypatch, text):
    monkeypatch.setattr(run_demo_pilot.sys, "stdin", io.StringIO(text))


def test_missing_input_refuses_before_any_switch_write(tmp_path, monkeypatch):
    switch = _switch(tmp_path)
    _type(monkeypatch, "")
    with pytest.raises(KillSwitchError, match="not supplied"):
        run_demo_pilot.arm_new_pilot_switch(switch, operator_id="Luke", reason="approved order", now=NOW)
    assert not switch.state_path.exists()
    assert not switch.audit_path.exists()
    assert not switch.status().writes_allowed


def test_wrong_phrase_refuses_before_any_switch_write(tmp_path, monkeypatch):
    switch = _switch(tmp_path)
    _type(monkeypatch, "please resume\n")
    with pytest.raises(KillSwitchError, match="did not match"):
        run_demo_pilot.arm_new_pilot_switch(switch, operator_id="Luke", reason="approved order", now=NOW)
    assert not switch.state_path.exists()
    assert not switch.status().writes_allowed


def test_correct_typed_phrase_resets_and_records_the_operator(tmp_path, monkeypatch):
    switch = _switch(tmp_path)
    moment = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
    _type(monkeypatch, RESET_ACKNOWLEDGEMENT + "\r\n")
    status = run_demo_pilot.arm_new_pilot_switch(
        switch, operator_id="Luke", reason="exact order approved", now=moment,
    )
    assert status.writes_allowed and status.integrity_valid
    assert status.reason == "exact order approved"
    reloaded = KillSwitch(state_path=switch.state_path, audit_path=switch.audit_path).status()
    assert reloaded == status
    events = [json.loads(line) for line in switch.audit_path.read_text().splitlines() if line.strip()]
    assert [event["action"] for event in events] == ["initialize_engaged", "reset"]
    assert events[0]["previous_hash"] == ""
    assert events[1]["previous_hash"] == events[0]["event_hash"]
    assert events[0]["operator_id"] == events[1]["operator_id"] == "Luke"
    assert events[1]["timestamp"] == moment.isoformat()
    assert events[1]["resulting_engaged"] is False


def test_arm_rejects_the_imported_constant_as_an_argument(tmp_path):
    switch = _switch(tmp_path)
    with pytest.raises(TypeError):
        run_demo_pilot.arm_new_pilot_switch(
            switch, operator_id="Luke", reason="approved order", now=NOW,
            acknowledgement=RESET_ACKNOWLEDGEMENT,
        )
    assert not switch.state_path.exists()
