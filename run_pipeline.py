from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def run_step(name: str, module_name: str) -> None:
    print(f"\n=== Running {name} ===\n")

    try:
        result = subprocess.run(
            [sys.executable, "-m", module_name],
            check=True,
            cwd=ROOT,
        )

        if result.returncode == 0:
            print(f"\n{name} finished.\n")

    except subprocess.CalledProcessError as e:
        print(f"\n{name} failed.")
        print(f"Module: {module_name}")
        print(f"Return code: {e.returncode}")
        raise


def main() -> None:
    run_step("Universe Agent", "agents.universe_agent.universe_agent")
    run_step("Macro Agent", "agents.macro_agent.macro_agent")
    run_step("Signal Agent", "agents.signal_agent.signal_agent")
    run_step("Risk Agent", "agents.risk_agent.risk_agent")
    run_step("News Agent", "agents.news_agent.news_agent")
    run_step("Portfolio Agent", "agents.portfolio_agent.portfolio_agent")
    run_step("Advisory Agent", "agents.advisory_agent.advisory_agent")
    run_step("Position Tracking Agent", "agents.position_tracking_agent.position_tracking_agent")
    run_step("Exit Agent", "agents.exit_agent.exit_agent")
    run_step("Journal Agent", "agents.journal_agent.journal_agent")


if __name__ == "__main__":
    main()