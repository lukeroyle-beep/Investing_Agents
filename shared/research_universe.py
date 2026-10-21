"""One opt-in research universe for scanner and historical replay, never execution."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from execution.domain import payload_hash
from shared.paths import project_path


SELECTION_VARIABLE = "INVESTING_RESEARCH_UNIVERSE"


@dataclass(frozen=True)
class ResearchUniverse:
    version: str
    tickers: tuple[str, ...]
    checksum: str


def selected_research_universe() -> ResearchUniverse | None:
    """An invalid explicit selection is an error, never a legacy fallback."""
    selection = os.environ.get(SELECTION_VARIABLE)
    if selection is None:
        return None
    if not selection.strip():
        raise ValueError("research universe selection must not be blank")
    path = Path(selection)
    if not path.is_absolute():
        path = project_path(selection)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "version", "objective", "tickers"}:
        raise ValueError("research universe manifest is incomplete or unsupported")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported research universe schema")
    if manifest["objective"] != "virtual_functionality":
        raise ValueError("research universe objective must remain virtual functionality")
    version, tickers = manifest["version"], manifest["tickers"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("research universe version is required")
    if not isinstance(tickers, list) or not tickers or any(
        not isinstance(ticker, str) or not ticker or ticker != ticker.strip().upper()
        for ticker in tickers
    ) or len(set(tickers)) != len(tickers):
        raise ValueError("research universe tickers must be unique and normalized")
    with project_path("data_sources", "stock_universe.csv").open(newline="", encoding="utf-8") as handle:
        source = list(csv.DictReader(handle))
    rows = []
    for ticker in tickers:
        matches = [row for row in source if row["ticker"] == ticker]
        if len(matches) != 1:
            raise ValueError("research ticker must resolve exactly once in the source")
        row = matches[0]
        if row["asset_class"] != "equity" or row["currency"] != "USD" or row["region"] != "US":
            raise ValueError("initial research pilot supports US dollar US equities only")
        rows.append(row)
    return ResearchUniverse(version, tuple(tickers), payload_hash({"manifest": manifest, "source_rows": rows}))
