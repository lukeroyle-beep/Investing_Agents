from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.paths import RUNTIME_DIR
from shared.runtime_archive import restore_runtime_backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore a verified runtime backup.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME_DIR)
    args = parser.parse_args()
    pre_restore_backup = restore_runtime_backup(args.archive, runtime_dir=args.runtime_dir)
    print(f"Runtime restored from: {args.archive}")
    print(f"Pre-restore recovery backup: {pre_restore_backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
