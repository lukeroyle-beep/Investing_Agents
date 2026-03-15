import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent

PIPELINE_STEPS = [
    ("Universe Agent", BASE_DIR / "agents" / "universe_agent" / "universe_agent.py"),
    ("Macro Agent", BASE_DIR / "agents" / "macro_agent" / "macro_agent.py"),
    ("Signal Agent", BASE_DIR / "agents" / "signal_agent" / "signal_agent.py"),
    ("Risk Agent", BASE_DIR / "agents" / "risk_agent" / "risk_agent.py"),
    ("Journal Agent", BASE_DIR / "agents" / "journal_agent" / "journal_agent.py"),
    ("News Agent", BASE_DIR / "agents" / "news_agent" / "news_agent.py"),
    ("Position Tracking Agent", BASE_DIR / "agents" / "position_tracking_agent" / "position_agent.py"),
    ("Portfolio Agent", BASE_DIR / "agents" / "portfolio_agent" / "portfolio_agent.py"),
    ("Execution Agent", BASE_DIR / "agents" / "execution_agent" / "execution_agent.py"),
]


def run_step(step_name: str, script_path: Path) -> None:
    print(f"\n=== Running {step_name} ===")
    result = subprocess.run([sys.executable, str(script_path)], cwd=BASE_DIR)

    if result.returncode != 0:
        raise RuntimeError(f"{step_name} failed with exit code {result.returncode}")


def main() -> None:
    for step_name, script_path in PIPELINE_STEPS:
        run_step(step_name, script_path)

    print("\nPipeline completed successfully.")


if __name__ == "__main__":
    main()