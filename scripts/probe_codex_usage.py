"""Probe the genuine host quota bridge without loading any trading runtime.

The host must answer the emitted usage request with the complete fresh result
of the Codex app get_usage_limits tool. No percentage argument, cached file,
broker call, credential load, mandate or activation path exists here.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_demo_pilot import Host, emit


@contextmanager
def private_stdin():
    """Prevent a terminal from echoing the private, complete tool response."""
    if not sys.stdin.isatty():
        yield
        return
    import termios
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    hidden = list(original)
    # Canonical terminals truncate long JSON lines (often at 4096 bytes).
    # Read bytewise so a complete multi-bucket tool response survives intact.
    hidden[3] &= ~(termios.ECHO | termios.ICANON)
    hidden[6] = list(original[6])
    hidden[6][termios.VMIN] = 1
    hidden[6][termios.VTIME] = 0
    termios.tcsetattr(descriptor, termios.TCSANOW, hidden)
    try:
        yield
    finally:
        termios.tcsetattr(descriptor, termios.TCSANOW, original)


class UsageOnlyHost(Host):
    """The JSON-lines host supplies quota evidence, never a broker operation."""
    def call(self, tool, arguments):
        if tool != "usage" or arguments != {}:
            raise ValueError("finite_host_usage_only")
        with private_stdin():
            return super().call(tool, arguments)


def probe():
    # A fresh probe-only directory is not an execution runtime. The original
    # ledger, inception, kill switch and credential paths are never opened.
    root = Path(tempfile.mkdtemp(prefix="investing-usage-probe-"))
    admission = UsageOnlyHost(root).usage()
    blockers = admission.validate_for_submission(datetime.now(timezone.utc))
    summary = {"event": "usage_probe_complete", "broker_requests": 0,
        "activation": False, "observed_at": admission.observed_at,
        "window_count": len(admission.windows),
        "minimum_remaining_percent": admission.minimum_remaining_percent,
        "required_submission_percent": admission.required_submission_percent,
        "blockers": blockers, "source_sha256": admission.source_sha256,
        "private_evidence_directory": admission.source_reference}
    emit(summary)
    return 2 if blockers else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("invalid_usage_probe_arguments")


def main(argv=None):
    try:
        _Parser(description=__doc__).parse_args(argv)
        return probe()
    except Exception as exc:
        # Upstream errors may contain account details; expose only their type.
        emit({"event": "stopped", "reason": "usage_probe_held",
              "error_type": type(exc).__name__, "broker_requests": 0})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
