import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def run_step(step_name, script_path):
    print(f"\n=== Running {step_name} ===")
    print(f"Script: {script_path}")

    result = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=PROJECT_ROOT,
        text=True
    )

    if result.returncode != 0:
        print(f"\n{step_name} failed with exit code {result.returncode}.")
        sys.exit(result.returncode)

    print(f"\n=== {step_name} completed successfully ===")


def main():

    steps = [
        ("Universe Agent", PROJECT_ROOT / "agents" / "universe_agent" / "universe_agent.py"),
        ("Macro Agent", PROJECT_ROOT / "agents" / "macro_agent" / "macro_agent.py"),
        ("Signal Agent", PROJECT_ROOT / "agents" / "signal_agent" / "signal_agent.py"),
        ("Risk Agent", PROJECT_ROOT / "agents" / "risk_agent" / "risk_agent.py"),
        ("Journal Agent", PROJECT_ROOT / "agents" / "journal_agent" / "journal_agent.py"),
        ("News Agent", PROJECT_ROOT / "agents" / "news_agent" / "news_agent.py"),
        ("Portfolio Agent", PROJECT_ROOT / "agents" / "portfolio_agent" / "portfolio_agent.py"),
        ("Execution Agent", PROJECT_ROOT / "agents" / "execution_agent" / "execution_agent.py"),
    ]

    for step_name, script_path in steps:

        if not script_path.exists():
            print(f"Missing script: {script_path}")
            sys.exit(1)

        run_step(step_name, script_path)

    print("\nPipeline finished successfully.")
    print("Key output files:")
    print(" - data/final_shortlist.csv")
    print(" - data/trade_journal.csv")
    print(" - data/news_flags.csv")
    print(" - data/portfolio_orders.csv")
    print(" - data/execution_orders.csv")


if __name__ == "__main__":
    main()