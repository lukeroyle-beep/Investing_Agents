from __future__ import annotations

import subprocess
import sys
from typing import Iterable


PIPELINE_STEPS: list[tuple[str, str]] = [
    ("Universe Agent", "agents.universe_agent.universe_agent"),
    ("Signal Agent", "agents.signal_agent.signal_agent"),
    ("Macro Agent", "agents.macro_agent.macro_agent"),
    ("News Agent", "agents.news_agent.news_agent"),
    ("Risk Agent", "agents.risk_agent.risk_agent"),
    ("Portfolio Agent", "agents.portfolio_agent.portfolio_agent"),
    ("Advisory Agent", "agents.advisory_agent.advisory_agent"),
    ("Fill Agent", "agents.fill_agent.fill_agent"),
    ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
    ("Position Tracking Agent", "agents.position_tracking_agent.position_tracking_agent"),
    ("Lifecycle Integrity Agent", "agents.lifecycle_integrity_agent.lifecycle_integrity_agent"),
    ("Exit Agent", "agents.exit_agent.exit_agent"),
    ("Portfolio Equity Agent", "agents.portfolio_equity_agent.portfolio_equity_agent"),
    ("Journal Agent", "agents.journal_agent.journal_agent"),
]


def run_module(label: str, module_path: str) -> None:
    print(f"\n=== Running {label} ===\n")

    result = subprocess.run(
        [sys.executable, "-m", module_path],
        capture_output=True,
        text=True,
    )

    if result.stdout:
        print(result.stdout)

    if result.returncode != 0:
        if result.stderr:
            print(result.stderr)
        raise RuntimeError(f"{label} failed.")


def run_pipeline(steps: Iterable[tuple[str, str]]) -> None:
    for label, module_path in steps:
        run_module(label, module_path)


def main() -> None:
    run_pipeline(PIPELINE_STEPS)
    print("\nPipeline completed successfully.")


if __name__ == "__main__":
    main()