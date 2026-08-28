from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.sqlite_parity import format_parity_report, validate_sqlite_dual_write_parity


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate required CSV/SQLite mirror parity.")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    state_dir = args.runtime_dir.expanduser().resolve() / "state"
    report = validate_sqlite_dual_write_parity(
        run_id=str(args.run_id).strip() or None,
        state_dir=state_dir,
    )
    print(format_parity_report(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
