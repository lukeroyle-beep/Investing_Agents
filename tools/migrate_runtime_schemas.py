from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.runtime_schema_migration import migrate_runtime_schemas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate run-control and freshness schemas using a verified backup."
    )
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    args = parser.parse_args()
    result = migrate_runtime_schemas(args.runtime_dir)
    print(f"Migrated runtime: {result.runtime_dir}")
    print(f"Verified backup: {result.backup.archive_path}")
    print(f"Migration report: {result.report_path}")
    print(f"Changed files: {', '.join(result.changed_files) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
