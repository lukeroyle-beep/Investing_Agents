"""Fill-owned boundary between exact broker staging and legacy economics.

Canonical economic CSV schemas currently coerce money/units to binary floats,
SQLite mirrors use REAL, and absent cash initializes a synthetic USD100,000.
The versioned broker_runtime module provides exact full-account economics in
its own new canonical CSV runtime. A genuine broker fill cannot enter this
legacy writer. Preseeded cash/processed markers do not select the v2 schema.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Mapping


LEGACY_BROKER_ECONOMIC_APPLICATION_AVAILABLE = False
BLOCK_REASON = "legacy_fill_requires_versioned_broker_runtime"


class LiveBrokerEconomicApplicationBlocked(RuntimeError):
    """Broker staging is retained; canonical economic state is untouched."""

    reason_code = BLOCK_REASON

    def __init__(self):
        super().__init__(BLOCK_REASON)


def assert_legacy_fill_row_admissible(row: Mapping, *, broker_input: bool = False) -> None:
    """Allow legacy manual/fake rows, never convert actual broker economics."""
    broker = str(row.get("broker", "")).strip().casefold()
    fill_id = str(row.get("fill_id", "")).strip().casefold()
    environment = str(row.get("environment", "")).strip().casefold()
    # Check both identity channels so moving an eToro row into manual_fills or
    # deleting its broker column cannot route it into the synthetic writer.
    if (broker not in {"", "fake"} or fill_id.startswith("etoro:")
            or (broker_input and broker != "fake")
            or (broker and environment != "demo")):
        raise LiveBrokerEconomicApplicationBlocked()


def assert_legacy_fill_inputs_admissible(*, manual_fills_path: str | Path,
                                        broker_fills_path: str | Path) -> None:
    """Read original strings before any bootstrap, event or economic write.

    This performs no numeric conversion, initialization, directory creation or
    marker update. It is also fail-closed on ambiguous broker CSV structure.
    """
    for candidate in (manual_fills_path, broker_fills_path):
        if (Path(candidate).parent / "broker_runtime_manifest.json").exists():
            raise LiveBrokerEconomicApplicationBlocked()
    for candidate, broker_input in ((manual_fills_path, False), (broker_fills_path, True)):
        path = Path(candidate)
        if not path.exists() or path.stat().st_size == 0:
            continue
        try:
            with path.open("r", encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                if reader.fieldnames is not None and len(set(reader.fieldnames)) != len(reader.fieldnames):
                    raise LiveBrokerEconomicApplicationBlocked()
                for row in reader:
                    if None in row:
                        raise LiveBrokerEconomicApplicationBlocked()
                    assert_legacy_fill_row_admissible(row, broker_input=broker_input)
        except (csv.Error, UnicodeError):
            raise LiveBrokerEconomicApplicationBlocked() from None
