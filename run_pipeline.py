from __future__ import annotations

import runpy
import traceback
from datetime import datetime, timezone

from shared.run_context import generate_run_id, set_current_run_id


AGENT_STEPS = [
    ("Universe Agent", "agents.universe_agent.universe_agent"),
    ("Macro Agent", "agents.macro_agent.macro_agent"),
    ("Signal Agent", "agents.signal_agent.signal_agent"),
    ("Risk Agent", "agents.risk_agent.risk_agent"),
    ("News Agent", "agents.news_agent.news_agent"),
    ("Portfolio Agent", "agents.portfolio_agent.portfolio_agent"),
    ("Advisory Agent", "agents.advisory_agent.advisory_agent"),
    ("Fill Agent", "agents.fill_agent.fill_agent"),
    ("Position Tracking Agent", "agents.position_tracking_agent.position_tracking_agent"),
    ("Exit Agent", "agents.exit_agent.exit_agent"),
    ("Journal Agent", "agents.journal_agent.journal_agent"),
]


def run_agent(agent_name: str, module_path: str) -> None:
    print(f"\n=== Running {agent_name} ===\n")
    runpy.run_module(module_path, run_name="__main__")


def main() -> None:
    run_id = generate_run_id()
    set_current_run_id(run_id)

    started_at = datetime.now(timezone.utc).isoformat()

    print("=== Trading Pipeline Started ===")
    print(f"Run ID: {run_id}")
    print(f"Started at: {started_at}")

    completed_agents: list[str] = []

    try:
        for agent_name, module_path in AGENT_STEPS:
            run_agent(agent_name, module_path)
            completed_agents.append(agent_name)

        finished_at = datetime.now(timezone.utc).isoformat()

        print("\n=== Trading Pipeline Finished Successfully ===")
        print(f"Run ID: {run_id}")
        print(f"Finished at: {finished_at}")
        print(f"Completed agents: {len(completed_agents)}")

    except Exception as exc:
        failed_at = datetime.now(timezone.utc).isoformat()

        print("\n=== Trading Pipeline Failed ===")
        print(f"Run ID: {run_id}")
        print(f"Failed at: {failed_at}")
        print(f"Completed agents before failure: {len(completed_agents)}")
        print(f"Error: {exc}")
        print("\nTraceback:")
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()