"""LaunchAgent entry point. Exits immediately unless the host switch is on."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scheduler.host_schedule import HostScheduleConfigError, load_host_schedule, run_once
from shared.paths import SCHEDULER_CONFIG_PATH


def main() -> int:
    try:
        config = load_host_schedule(SCHEDULER_CONFIG_PATH)
    except HostScheduleConfigError as exc:
        print(f"host_schedule outcome=config_error exit=1 detail={exc}", file=sys.stderr)
        return 1
    return run_once(config, now=datetime.now(UTC))


if __name__ == "__main__":
    raise SystemExit(main())
