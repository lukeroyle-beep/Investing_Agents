from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.runtime_archive import create_runtime_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a checksum-verified runtime backup.")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    parser.add_argument("--label", default="operator")
    args = parser.parse_args()
    result = create_runtime_backup(args.runtime_dir, label=args.label)
    print(f"Backup archive: {result.archive_path}")
    print(f"SHA-256: {result.sha256}")
    print(f"Files: {result.file_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
