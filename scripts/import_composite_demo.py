"""Plan, apply or recover one reviewed historical composite Demo reconciliation."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.fill_agent.composite_source import build_composite_plan
from agents.fill_agent.composite_import import apply_composite_import, recover_composite_import
from execution.domain import canonical_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply", "recover"))
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--reviewed-commit", required=True)
    parser.add_argument("--review-reference")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-plan-sha256")
    parser.add_argument("--event-id")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    try:
        if args.mode == "plan":
            _, _, report = build_composite_plan(runtime=args.runtime, manifest=args.manifest,
                reviewed_commit=args.reviewed_commit, review_reference=args.review_reference, now=now)
        elif args.mode == "apply":
            report = apply_composite_import(runtime=args.runtime, manifest=args.manifest,
                expected_manifest_sha256=args.expected_manifest_sha256,
                expected_plan_sha256=args.expected_plan_sha256, reviewed_commit=args.reviewed_commit,
                review_reference=args.review_reference, now=now)
        else:
            report = recover_composite_import(runtime=args.runtime, event_id=args.event_id,
                expected_plan_sha256=args.expected_plan_sha256, reviewed_commit=args.reviewed_commit, now=now)
    except Exception:
        # Source data and local exception text can contain private identifiers.
        print(canonical_json({"status": "COMPOSITE_IMPORT_STOPPED", "trading_ready": False,
                              "inspect_existing_economic_event_before_any_retry": True}))
        return 2
    print(canonical_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
