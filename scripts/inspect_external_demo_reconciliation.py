"""Inspect original external Demo evidence; economic application is unsupported."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents.fill_agent.external_reconciliation import inspect_external_reconciliation
from execution.domain import canonical_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("external_inspection_arguments_invalid")


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        result = inspect_external_reconciliation(runtime=args.runtime, manifest=args.manifest,
                                                   now=datetime.now(timezone.utc))
    except Exception:
        # Untrusted source data, paths and account identifiers never leak in an
        # exception. Library callers receive fixed validation codes for tests.
        print(canonical_json({"status": "EXTERNAL_RECONCILIATION_INVALID_EVIDENCE",
            "apply_supported": False, "activation_ready": False, "migration_not_applied": True}))
        return 2
    print(canonical_json(result))
    return 1  # Inspection succeeded, but migration/activation remains blocked.


if __name__ == "__main__":
    raise SystemExit(main())
