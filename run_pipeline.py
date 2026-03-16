from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent


def run_step(name: str, script_path: str) -> None:
    print(f"\n=== Running {name} ===\n")

    full_path = ROOT / script_path

    if not full_path.exists():
        raise FileNotFoundError(f"Agent script not found: {full_path}")

    try:
        result = subprocess.run([sys.executable, str(full_path)], check=True)

        if result.returncode == 0:
            print(f"\n{name} finished.\n")

    except subprocess.CalledProcessError as e:
        print(f"\n{name} failed.")
        print(f"Script: {full_path}")
        print(f"Return code: {e.returncode}")
        raise


def main() -> None:
    run_step("Universe Agent", "agents/universe_agent/universe_agent.py")
    run_step("Macro Agent", "agents/macro_agent/macro_agent.py")
    run_step("Signal Agent", "agents/signal_agent/signal_agent.py")
    run_step("Risk Agent", "agents/risk_agent/risk_agent.py")
    run_step("News Agent", "agents/news_agent/news_agent.py")
    run_step("Portfolio Agent", "agents/portfolio_agent/portfolio_agent.py")

    run_step("Advisory Agent", "agents/advisory_agent/advisory_agent.py")

    run_step("Position Tracking Agent", "agents/position_tracking_agent/position_tracking_agent.py")

    run_step("Exit Agent", "agents/exit_agent/exit_agent.py")

    run_step("Journal Agent", "agents/journal_agent/journal_agent.py")


if __name__ == "__main__":
    main()