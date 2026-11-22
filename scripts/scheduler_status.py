"""Show LaunchAgent load state, the host switch, and the last recorded run."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scheduler.host_schedule import (
    HostScheduleConfigError,
    collect_status,
    format_status,
    load_host_schedule,
)
from shared.paths import SCHEDULER_CONFIG_PATH


def main() -> int:
    try:
        config = load_host_schedule(SCHEDULER_CONFIG_PATH)
        report = collect_status(config, now=datetime.now(UTC))
    except HostScheduleConfigError as exc:
        print(f"scheduler status failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(format_status(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
