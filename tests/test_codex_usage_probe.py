"""Synthetic protocol checks; live quota evidence is separately labelled."""
import io
import json
from pathlib import Path

import pytest

from scripts import probe_codex_usage as probe


def response(remaining=68):
    raw = {"accountId": "SYNTHETIC_PRIVATE_ACCOUNT", "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 100 - remaining}, "secondary": None}}}
    return {"content": [{"type": "text", "text": json.dumps(raw)}], "isError": False}


@pytest.mark.parametrize("remaining,code", [(68, 0), (25, 0), (24, 2)])
def test_probe_retains_original_result_privately_and_only_emits_summary(tmp_path, monkeypatch, capsys, remaining, code):
    raw = response(remaining)
    monkeypatch.setattr(probe.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO(json.dumps(raw) + "\n"))
    assert probe.main([]) == code
    output = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE_ACCOUNT" not in output
    request, summary = [json.loads(line) for line in output.splitlines()]
    assert request == {"event": "tool_request", "sequence": 1, "tool": "usage", "arguments": {}}
    assert summary["broker_requests"] == 0 and summary["activation"] is False
    assert summary["minimum_remaining_percent"] == str(remaining)
    assert summary["required_submission_percent"] == "25"
    receipts = list(Path(summary["private_evidence_directory"]).glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["result"] == raw
    assert receipts[0].stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob("*.sqlite3"))


def test_probe_tool_failure_never_replaces_missing_quota(tmp_path, monkeypatch, capsys):
    raw = {"isError": True, "content": [{"type": "text", "text": "SYNTHETIC_PRIVATE_FAILURE"}]}
    monkeypatch.setattr(probe.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    monkeypatch.setattr(probe.sys, "stdin", io.StringIO(json.dumps(raw) + "\n"))
    assert probe.main([]) == 2
    output = capsys.readouterr().out
    assert "SYNTHETIC_PRIVATE_FAILURE" not in output
    assert "usage_probe_complete" not in output
    assert "usage_probe_held" in output


def test_probe_never_echoes_accidental_secret_arguments(capsys):
    assert probe.main(["--api-key", "SYNTHETIC_PRIVATE_SECRET"]) == 2
    assert "SYNTHETIC_PRIVATE_SECRET" not in capsys.readouterr().out


def test_usage_only_host_rejects_broker_request_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.sys, "stdin", None)
    host = probe.UsageOnlyHost(tmp_path)
    with pytest.raises(ValueError, match="finite_host_usage_only"):
        host.call("etoro_execute_read", {})
    assert list(host.directory.iterdir()) == []


def test_private_terminal_input_restores_echo_even_after_exception(monkeypatch):
    import termios
    from types import SimpleNamespace
    original = [0, 0, 0, termios.ECHO | termios.ICANON, 0, 0, [0] * 32]
    changed = []
    monkeypatch.setattr(probe.sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 42))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: original)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, flags: changed.append(flags))
    with pytest.raises(RuntimeError):
        with probe.private_stdin():
            raise RuntimeError("synthetic")
    assert changed[0][3] & termios.ECHO == 0
    assert changed[0][3] & termios.ICANON == 0
    assert changed[1] == original


def test_real_terminal_retains_long_complete_usage_reply_without_echo(tmp_path):
    import errno
    import os
    import pty
    import select
    import subprocess
    import sys
    import time
    master, slave = pty.openpty()
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    process = subprocess.Popen([sys.executable, str(probe.ROOT / "scripts/probe_codex_usage.py")],
        stdin=slave, stdout=slave, stderr=slave, env=env)
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 10
    try:
        while b"\n" not in output:
            assert time.monotonic() < deadline, "probe request did not arrive"
            if select.select([master], [], [], .1)[0]:
                output += os.read(master, 65536)
        raw = response()
        raw["private_padding"] = "SYNTHETIC_PRIVATE_PADDING_" * 600
        pending = memoryview((json.dumps(raw) + "\n").encode())
        while pending:
            pending = pending[os.write(master, pending):]
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                output += data
            elif process.poll() is not None:
                break
        assert process.wait(timeout=1) == 0
        assert b"SYNTHETIC_PRIVATE" not in output
        summary = json.loads(output.decode().splitlines()[-1])
        receipt = next(Path(summary["private_evidence_directory"]).glob("*.json"))
        assert json.loads(receipt.read_text())["result"] == raw
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
