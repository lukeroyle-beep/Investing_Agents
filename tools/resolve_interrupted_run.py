from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.runtime_recovery import resolve_interrupted_run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve an interrupted run fail-closed using its finalization proof."
    )
    parser.add_argument("run_id")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    args = parser.parse_args()
    result = resolve_interrupted_run(args.runtime_dir, args.run_id)
    print(
        f"Resolved {result.run_id}: {result.prior_status} -> "
        f"{result.resolved_status}; finalization_proof={result.used_finalization_proof}"
    )
    print(f"Audit trail: {result.audit_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
