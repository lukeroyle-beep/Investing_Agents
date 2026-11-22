"""S&P 500 point-in-time membership from retrieved, citable extracts.

The built table is valid on as-of dates from 2021-12-20 through 2026-11-22.
A row whose effective_from is 2021-12-20 was already a member on that anchor
date; the extract does not say when it first joined. effective_to is exclusive
and is the published effective date of removal (before the open). delisted_at
is left blank: these sources do not consistently record the last trading day.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from shared.paths import project_path


ROOT = project_path("data_sources", "sp500")
CHANGES_SINCE_2019 = ROOT / "sp500_changes_since_2019.csv"
WIKIPEDIA_CONSTITUENTS = ROOT / "wikipedia_constituents_2026-11-22.csv"
WIKIPEDIA_CHANGES = ROOT / "wikipedia_changes_after_2026-08-18.csv"
SUPPLEMENTAL_CHANGES = ROOT / "supplemental_changes.csv"
ANCHOR_DIR = ROOT / "anchors"
MEMBERSHIP_FILE = ROOT / "point_in_time_membership.csv"

ANCHOR_DATE = date(2021, 12, 20)
SPLICE_DATE = date(2026, 8, 18)
RETRIEVAL_DATE = date(2026, 11, 22)

COLUMNS = [
    "internal_instrument_id",
    "symbol",
    "exchange",
    "effective_from",
    "effective_to",
    "delisted_at",
]


@dataclass(frozen=True)
class MembershipChange:
    effective_date: date
    added: frozenset[str]
    removed: frozenset[str]


def normalize_symbol(value: object) -> str:
    """Yahoo's symbol form used by the rest of this repo. BRK.B becomes BRK-B."""
    text = str(value).strip().upper().replace(".", "-")
    if not text or text == "NAN" or any(character.isspace() for character in text):
        raise ValueError(f"unusable membership symbol: {value!r}")
    return text


def _symbols(value: object) -> frozenset[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return frozenset()
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return frozenset()
    return frozenset(normalize_symbol(part) for part in text.split(",") if part.strip())


def _read_anchor(day: date) -> set[str]:
    path = ANCHOR_DIR / f"{day.isoformat()}.txt"
    symbols = {
        normalize_symbol(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if not symbols:
        raise ValueError(f"membership anchor is empty: {path}")
    return symbols


def load_current_symbols() -> set[str]:
    with WIKIPEDIA_CONSTITUENTS.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    symbols = {normalize_symbol(row["Symbol"]) for row in rows}
    if len(symbols) != len(rows):
        raise ValueError("Wikipedia constituent extract has duplicate symbols")
    return symbols


def _parse_day(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def load_changes() -> list[MembershipChange]:
    """Changes strictly after the anchor date, through the retrieval date."""
    grouped: dict[date, tuple[set[str], set[str]]] = {}

    def add(day: date, added: frozenset[str], removed: frozenset[str]) -> None:
        if day <= ANCHOR_DATE or day > RETRIEVAL_DATE:
            raise ValueError(f"membership change {day.isoformat()} is outside the retained window")
        current_added, current_removed = grouped.setdefault(day, (set(), set()))
        overlap = (current_added | added) & (current_removed | removed)
        if overlap or (added & removed):
            raise ValueError(f"{day.isoformat()} both adds and removes {sorted(overlap or (added & removed))}")
        current_added.update(added)
        current_removed.update(removed)

    with CHANGES_SINCE_2019.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = _parse_day(row["date"])
            if day <= ANCHOR_DATE:
                continue
            if day > SPLICE_DATE:
                raise ValueError(
                    "the fja05680 change file extends past 2026-08-18; "
                    "refresh the Wikipedia splice before using the new rows"
                )
            add(day, _symbols(row["add"]), _symbols(row["remove"]))

    with WIKIPEDIA_CHANGES.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = _parse_day(row["effective_date"])
            if day <= SPLICE_DATE:
                raise ValueError(f"Wikipedia change {day.isoformat()} is on or before the splice")
            add(day, _symbols(row["added_ticker"]), _symbols(row["removed_ticker"]))

    with SUPPLEMENTAL_CHANGES.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            day = _parse_day(row["effective_date"])
            add(day, _symbols(row["added_ticker"]), _symbols(row["removed_ticker"]))

    return [
        MembershipChange(day, frozenset(added), frozenset(removed))
        for day, (added, removed) in sorted(grouped.items())
    ]


def build_membership_frame() -> pd.DataFrame:
    """Walk backward from the retrieved constituent list to the anchor snapshot."""
    current = load_current_symbols()
    changes = load_changes()
    active = set(current)
    ends: dict[str, date | None] = {symbol: None for symbol in active}
    intervals: list[tuple[str, date, date | None]] = []
    splice_checked = False

    for change in sorted(changes, key=lambda item: item.effective_date, reverse=True):
        if change.effective_date <= SPLICE_DATE and not splice_checked:
            _same_members(active, _read_anchor(SPLICE_DATE), SPLICE_DATE)
            splice_checked = True
        for symbol in sorted(change.added):
            if symbol not in active:
                raise ValueError(
                    f"{symbol} is added on {change.effective_date.isoformat()} "
                    "but is not a member after that date"
                )
            intervals.append((symbol, change.effective_date, ends.pop(symbol)))
            active.remove(symbol)
        for symbol in sorted(change.removed):
            if symbol in active:
                raise ValueError(
                    f"{symbol} is removed on {change.effective_date.isoformat()} "
                    "while the later membership still contains it"
                )
            active.add(symbol)
            ends[symbol] = change.effective_date

    if not splice_checked:
        raise ValueError("the splice snapshot was not checked")
    _same_members(active, _read_anchor(ANCHOR_DATE), ANCHOR_DATE)
    for symbol in sorted(active):
        intervals.append((symbol, ANCHOR_DATE, ends[symbol]))

    rows = [
        {
            "internal_instrument_id": f"sp500:{symbol}:{start.isoformat()}",
            "symbol": symbol,
            "exchange": "unspecified",
            "effective_from": start.isoformat(),
            "effective_to": "" if end is None else end.isoformat(),
            "delisted_at": "",
        }
        for symbol, start, end in intervals
    ]
    frame = pd.DataFrame(rows, columns=COLUMNS)
    frame = frame.sort_values(["symbol", "effective_from"]).reset_index(drop=True)
    _assert_intervals(frame, current)
    return frame


def _same_members(actual: set[str], expected: set[str], day: date) -> None:
    if actual == expected:
        return
    raise ValueError(
        f"membership on {day.isoformat()} does not match the stored snapshot: "
        f"only built {sorted(actual - expected)[:8]}, "
        f"only snapshot {sorted(expected - actual)[:8]}"
    )


def _assert_intervals(frame: pd.DataFrame, current: set[str]) -> None:
    open_ended = set(frame.loc[frame["effective_to"] == "", "symbol"])
    if open_ended != current:
        raise ValueError("open membership intervals are not the retrieved constituent list")
    for symbol, group in frame.groupby("symbol", sort=False):
        ordered = list(group.sort_values("effective_from").itertuples(index=False))
        previous_end: date | None = None
        for index, row in enumerate(ordered):
            start = _parse_day(row.effective_from)
            end = _parse_day(row.effective_to) if row.effective_to else None
            if end is not None and end <= start:
                raise ValueError(f"{symbol} has an empty membership interval")
            if index and (previous_end is None or start < previous_end):
                raise ValueError(f"{symbol} has overlapping membership")
            previous_end = end
        if ordered[-1].effective_to and symbol in current:
            raise ValueError(f"{symbol} is in the current list but has no open interval")


def members_on(frame: pd.DataFrame, day: date) -> set[str]:
    """Same exclusive end date as PointInTimeUniverse.members."""
    start = pd.to_datetime(frame["effective_from"]).dt.date
    end = pd.to_datetime(frame["effective_to"], errors="coerce").dt.date
    active = (start <= day) & (end.isna() | (end > day))
    return set(frame.loc[active, "symbol"])
