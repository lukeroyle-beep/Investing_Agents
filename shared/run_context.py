from __future__ import annotations

import os
from datetime import datetime, timezone


RUN_ID_ENV_VAR = "TRADING_PIPELINE_RUN_ID"


def generate_run_id(prefix: str = "RUN") -> str:
    """
    Generate a governed pipeline run identifier.

    Example:
    RUN_20260316T141530Z
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{timestamp}"


def set_current_run_id(run_id: str) -> None:
    """
    Persist the current run_id in the process environment so child modules can read it.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string.")
    os.environ[RUN_ID_ENV_VAR] = run_id.strip()


def get_current_run_id() -> str:
    """
    Read the current pipeline run_id from the environment.
    """
    run_id = os.environ.get(RUN_ID_ENV_VAR, "").strip()
    if not run_id:
        raise RuntimeError(
            f"{RUN_ID_ENV_VAR} is not set. "
            "run_pipeline.py must create and set the run_id before agents run."
        )
    return run_id


def get_or_create_run_id() -> str:
    """
    Return the current run_id if already set, otherwise create and set one.
    Useful for standalone agent testing.
    """
    existing = os.environ.get(RUN_ID_ENV_VAR, "").strip()
    if existing:
        return existing

    run_id = generate_run_id()
    set_current_run_id(run_id)
    return run_id