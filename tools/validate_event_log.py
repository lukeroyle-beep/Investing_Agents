from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
EVENT_LOG_PATH = DATA_DIR / "event_log.csv"

REQUIRED_COLUMNS = [
    "event_id",
    "run_id",
    "event_time",
    "agent_name",
    "event_type",
    "entity_type",
    "entity_id",
    "ticker",
    "position_id",
    "order_id",
    "severity",
    "message",
    "before_json",
    "after_json",
    "metadata_json",
]


def fail(message: str) -> None:
    print(f"EVENT LOG VALIDATION FAILED: {message}")
    sys.exit(1)


def main() -> None:
    if not EVENT_LOG_PATH.exists():
        fail(f"Missing event log file: {EVENT_LOG_PATH}")

    with EVENT_LOG_PATH.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        if reader.fieldnames is None:
            fail("Event log has no header row.")

        missing = [col for col in REQUIRED_COLUMNS if col not in reader.fieldnames]
        if missing:
            fail(f"Missing required columns: {missing}")

        seen_event_ids = set()
        row_count = 0

        for idx, row in enumerate(reader, start=2):
            row_count += 1

            event_id = (row.get("event_id") or "").strip()
            run_id = (row.get("run_id") or "").strip()
            agent_name = (row.get("agent_name") or "").strip()
            event_type = (row.get("event_type") or "").strip()
            entity_type = (row.get("entity_type") or "").strip()
            entity_id = (row.get("entity_id") or "").strip()
            severity = (row.get("severity") or "").strip()

            if not event_id:
                fail(f"Row {idx}: event_id is blank.")
            if event_id in seen_event_ids:
                fail(f"Row {idx}: duplicate event_id found: {event_id}")
            seen_event_ids.add(event_id)

            for field_name, value in [
                ("run_id", run_id),
                ("agent_name", agent_name),
                ("event_type", event_type),
                ("entity_type", entity_type),
                ("entity_id", entity_id),
                ("severity", severity),
            ]:
                if not value:
                    fail(f"Row {idx}: {field_name} is blank.")

            for json_field in ["before_json", "after_json", "metadata_json"]:
                raw = (row.get(json_field) or "").strip()
                if raw:
                    try:
                        json.loads(raw)
                    except json.JSONDecodeError as exc:
                        fail(f"Row {idx}: invalid JSON in {json_field}: {exc}")

    print("EVENT LOG VALIDATION PASSED")
    print(f"Rows checked: {row_count}")


if __name__ == "__main__":
    main()