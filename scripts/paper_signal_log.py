"""Print the paper signal log and the horizon summary.

Read only. This command fetches daily closes, including the Yahoo FX closes
used for GBP scoring, and does not place orders.

    uv run --frozen python scripts/paper_signal_log.py
    uv run --frozen python scripts/paper_signal_log.py --markdown runtime/logs/paper_signal_report.md
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.paper_signal_log import (  # noqa: E402
    PaperSignalLogError,
    evaluate_log,
    format_markdown,
    format_report,
    load_config,
)


def _as_of(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise PaperSignalLogError("--as-of must include an explicit UTC offset")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, help="JSONL signal log. Defaults to the configured path.")
    parser.add_argument("--config", type=Path, help="Paper signal configuration YAML.")
    parser.add_argument(
        "--as-of",
        help="Evaluation timestamp with an explicit offset, for example 2026-11-22T00:00:00+00:00.",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        help="Also write the same report as Markdown to this path.",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.log is not None:
            log_path = args.log.expanduser()
            if not log_path.is_absolute():
                log_path = ROOT / log_path
            config = replace(config, log_path=log_path)
        records, evaluation = evaluate_log(
            config,
            as_of=_as_of(args.as_of) if args.as_of else None,
        )
        report = format_report(records, evaluation)
    except PaperSignalLogError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(report, end="" if report.endswith("\n") else "\n")
    if args.markdown is not None:
        destination = args.markdown.expanduser()
        if not destination.is_absolute():
            destination = ROOT / destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(format_markdown(records, evaluation), encoding="utf-8")
        print(f"Wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
