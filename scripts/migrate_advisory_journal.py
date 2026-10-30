"""Dry-run by default; apply only an exact reviewed legacy Journal plan.

Never touches broker/execution state. Output contains counts/hashes, not rows.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.journal_agent.history_migration import apply_migration, inspect_migration
from scripts.run_autonomous_demo_pilot import verify_reviewed_checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-source-sha256")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--reviewed-commit")
    parser.add_argument("--review-reference")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    if args.apply:
        result = apply_migration(args.source, expected_source_sha256=args.expected_source_sha256,
            expected_plan_sha256=args.expected_plan_sha256, reviewed_commit=args.reviewed_commit,
            review_reference=args.review_reference, writers_stopped=args.writers_stopped,
            checkout_verifier=verify_reviewed_checkout)
    else:
        result = inspect_migration(args.source)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
