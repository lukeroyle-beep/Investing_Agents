"""Synthetic-only history planning/archive timings; no timing acceptance gates.

Run this same script before and after a repair, with the corresponding checkout's
frozen Python environment. It only creates disposable synthetic files.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from agents.journal_agent import history_migration as migration


def benchmark(rows, repeats):
    records = [{"ticker": "SYNTH", "name": "Synthetic", "market_regime": "neutral",
        "adjusted_setup_score": "82.5", "adjusted_setup_status": "candidate",
        "risk_decision": "approved", "risk_notes": "synthetic", "run_id": f"RUN_{i}",
        "journaled_at": "2026-01-01T10:00:00+00:00", "review_status": "open",
        "user_action": "", "outcome": "", "notes": ""} for i in range(rows)]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(records[0]), lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(records)
    with tempfile.TemporaryDirectory(prefix="synthetic-history-benchmark-") as scratch:
        source = Path(scratch).resolve() / "trade_journal.csv"
        source.write_bytes(output.getvalue().encode())
        planning = []
        for _ in range(repeats):
            start = time.perf_counter()
            plan, operational = migration._plan(source, source.read_bytes())
            planning.append(time.perf_counter() - start)
        # Build the completed synthetic archive from the already measured plan.
        archive = migration._history(source) / plan["source_sha256"]
        archive.mkdir(parents=True)
        prepared = {"schema": migration.SCHEMA, "source_path": str(source),
            "source_sha256": plan["source_sha256"],
            "plan_sha256": migration._hash(migration._json(plan)),
            "reviewed_commit": "a" * 40, "review_reference": "synthetic benchmark"}
        artifacts = {"original.csv": source.read_bytes(), "operational.csv": operational,
            "plan.json": migration._json(plan), "prepared.json": migration._json(prepared),
            "completed.json": migration._json({"schema": migration.SCHEMA,
                "prepared_sha256": migration._hash(migration._json(prepared)),
                "operational_sha256": plan["operational_sha256"], "status": "applied"})}
        for name, raw in artifacts.items():
            (archive / name).write_bytes(raw)
        source.write_bytes(operational)
        recurring = []
        for _ in range(repeats):
            start = time.perf_counter()
            migration.validate_journal_history(source)
            recurring.append(time.perf_counter() - start)
    return {"synthetic_only": True, "keyed_rows": rows, "repeats": repeats,
        "python": platform.python_version(), "pandas": pd.__version__, "platform": platform.platform(),
        "planning_seconds": planning, "planning_median_seconds": statistics.median(planning),
        "recurring_archive_validation_seconds": recurring,
        "recurring_archive_validation_median_seconds": statistics.median(recurring)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.rows < 1 or args.repeats < 1:
        parser.error("rows and repeats must be positive")
    print(json.dumps(benchmark(args.rows, args.repeats), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
