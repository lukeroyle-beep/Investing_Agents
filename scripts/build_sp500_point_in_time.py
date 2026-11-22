"""Rebuild data_sources/sp500/point_in_time_membership.csv from the retrieved extracts."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.sp500_membership import MEMBERSHIP_FILE, build_membership_frame


def main() -> None:
    frame = build_membership_frame()
    frame.to_csv(MEMBERSHIP_FILE, index=False)
    print(f"Wrote {len(frame)} membership intervals to {MEMBERSHIP_FILE}")


if __name__ == "__main__":
    main()
