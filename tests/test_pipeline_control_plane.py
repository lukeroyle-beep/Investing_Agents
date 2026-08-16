from __future__ import annotations

import run_pipeline
from agents.fill_agent import fill_agent
from agents.position_tracking_agent import position_tracking_agent
from shared.run_context import RUN_ID_ENV_VAR


def test_state_mutating_agents_use_canonical_pipeline_run_id(monkeypatch) -> None:
    canonical_run_id = "RUN_CANONICAL_CONTEXT"
    monkeypatch.setenv(RUN_ID_ENV_VAR, canonical_run_id)

    assert fill_agent.current_run_id() == canonical_run_id
    assert position_tracking_agent.current_run_id() == canonical_run_id


def test_pipeline_steps_preserve_dependency_order() -> None:
    labels = [label for label, _ in run_pipeline.PIPELINE_STEPS]

    assert labels == [
        "Universe Agent",
        "Macro Agent",
        "Signal Agent",
        "Risk Agent",
        "News Agent",
        "Portfolio Agent",
        "Advisory Agent",
        "Fill Agent",
        "Lifecycle Integrity Agent",
        "Position Tracking Agent",
        "Lifecycle Integrity Agent",
        "Exit Agent",
        "Portfolio Equity Agent",
        "Journal Agent",
    ]
