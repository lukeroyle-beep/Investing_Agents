from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agents.exit_agent import exit_agent
from agents.fill_agent import fill_agent
from agents.portfolio_equity_agent import portfolio_equity_agent
from agents.position_tracking_agent import position_tracking_agent
from shared import paths
from tools import validate_event_log


def test_default_runtime_layout_is_untracked_runtime_tree() -> None:
    assert paths.RUNTIME_DIR == paths.PROJECT_ROOT / "runtime"
    assert paths.DATA_DIR == paths.RUNTIME_DIR / "state"
    assert paths.RUNTIME_RUNS_DIR == paths.RUNTIME_DIR / "runs"
    assert paths.RUNTIME_CONTROL_DIR == paths.RUNTIME_DIR / "control"
    assert paths.RUNTIME_CACHE_DIR == paths.RUNTIME_DIR / "cache"
    assert paths.RUNTIME_LOGS_DIR == paths.RUNTIME_DIR / "logs"
    assert paths.RUNTIME_BACKUPS_DIR == paths.RUNTIME_DIR / "backups"


def test_legacy_path_consumers_use_runtime_state_by_default() -> None:
    assert Path(fill_agent.STATE_PATH) == paths.data_path("portfolio_state.csv")
    assert Path(fill_agent.PROCESSED_FILLS_PATH) == paths.data_path("processed_fills.csv")
    assert Path(position_tracking_agent.STATE_PATH) == paths.data_path("portfolio_state.csv")
    assert Path(position_tracking_agent.MONITOR_PATH) == paths.data_path("portfolio_monitor.csv")
    assert Path(position_tracking_agent.ALERTS_PATH) == paths.data_path("position_alerts.csv")
    assert Path(exit_agent.STATE_PATH) == paths.data_path("portfolio_state.csv")
    assert Path(exit_agent.MONITOR_PATH) == paths.data_path("portfolio_monitor.csv")
    assert Path(exit_agent.EXIT_ADVICE_PATH) == paths.data_path("exit_advice.csv")
    assert Path(portfolio_equity_agent.STATE_PATH) == paths.data_path("portfolio_state.csv")
    assert Path(portfolio_equity_agent.MONITOR_PATH) == paths.data_path("portfolio_monitor.csv")
    assert Path(portfolio_equity_agent.CASH_STATE_PATH) == paths.data_path("cash_state.csv")
    assert validate_event_log.EVENT_LOG_PATH == paths.data_path("event_log.csv")


def test_runtime_root_can_be_overridden_for_isolated_operation(tmp_path: Path) -> None:
    runtime_root = tmp_path / "isolated-runtime"
    environment = os.environ.copy()
    environment[paths.RUNTIME_DIR_ENV_VAR] = str(runtime_root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from shared.paths import RUNTIME_DIR, DATA_DIR; "
                "print(json.dumps({'runtime': str(RUNTIME_DIR), 'state': str(DATA_DIR)}))"
            ),
        ],
        cwd=paths.PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    resolved = json.loads(result.stdout)
    assert resolved == {
        "runtime": str(runtime_root),
        "state": str(runtime_root / "state"),
    }


@pytest.mark.parametrize("run_id", ["", "../escape", "nested/run", "has space"])
def test_run_path_rejects_unsafe_run_identifiers(run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id must contain only"):
        paths.run_path(run_id)


def test_run_path_accepts_canonical_run_identifier() -> None:
    assert paths.run_path("RUN_20260828T120000Z", "manifest.json") == (
        paths.RUNTIME_RUNS_DIR / "RUN_20260828T120000Z" / "manifest.json"
    )
