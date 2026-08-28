from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.runtime_bootstrap import bootstrap_runtime


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a synthetic, schema-current runtime baseline.")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    args = parser.parse_args()
    result = bootstrap_runtime(args.runtime_dir)
    print(f"Bootstrapped runtime: {result.runtime_dir}")
    print(f"Baseline run: {result.run_id}")
    print(f"Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
