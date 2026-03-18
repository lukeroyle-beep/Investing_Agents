from __future__ import annotations

import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


PIPELINE_STEPS = [
    ("Universe Agent", "agents.universe_agent.universe_agent"),
    ("Signal Agent", "agents.signal_agent.signal_agent"),
    ("Macro Agent", "agents.macro_agent.macro_agent"),
    ("News Agent", "agents.news_agent.news_agent"),
    ("Risk Agent", "agents.risk_agent.risk_agent"),
    ("Portfolio Agent", "agents.portfolio_agent.portfolio_agent"),
    ("Advisory Agent", "agents.advisory_agent.advisory_agent"),
    ("Fill Agent", "agents.fill_agent.fill_agent"),
    ("Position Tracking Agent", "agents.position_tracking_agent.position_tracking_agent"),
    ("Exit Agent", "agents.exit_agent.exit_agent"),
    ("Portfolio Equity Agent", "agents.portfolio_equity_agent.portfolio_equity_agent"),
    ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
    ("Journal Agent", "agents.journal_agent.journal_agent"),
]


def run_module_step(step_name: str, module_name: str) -> None:
    print()
    print(f"=== Running {step_name} ===")
    print()

    result = subprocess.run(
        [sys.executable, "-m", module_name],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
    )

    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")

    if result.returncode != 0:
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")
        raise RuntimeError(
            f"{step_name} failed.\n"
            f"Module: {module_name}\n"
            f"Return code: {result.returncode}"
        )

    if result.stderr:
        print(result.stderr, end="" if result.stderr.endswith("\n") else "\n")


def main() -> None:
    for step_name, module_name in PIPELINE_STEPS:
        run_module_step(step_name, module_name)

    print()
    print("Pipeline finished successfully.")


if __name__ == "__main__":
    main()