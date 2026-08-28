from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.run_finalizer import validate_finalization_record


def _latest_run_id(state_dir: Path) -> str:
    path = state_dir / "run_history.csv"
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    if frame.empty or "run_id" not in frame.columns:
        raise RuntimeError("run_history.csv has no run rows")
    return str(frame.iloc[-1]["run_id"]).strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a run finalization record and all manifest artifacts."
    )
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    runtime_dir = args.runtime_dir.expanduser().resolve()
    state_dir = runtime_dir / "state"
    run_id = str(args.run_id).strip() or _latest_run_id(state_dir)
    result = validate_finalization_record(
        runtime_dir / "runs" / run_id / "run_finalization.json",
        state_dir=state_dir,
    )
    print(f"ARTIFACT VALIDATION PASSED: run_id={result.run_id}")
    print(f"Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
