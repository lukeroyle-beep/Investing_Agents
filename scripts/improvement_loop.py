"""Compare pre-registered strategy variants and write a report.

Read only. Market data uses the Yahoo provider already used by the backtester.
Paper and demo logs are local files. Nothing in this command edits strategy
config, opens a pull request, or sends an order.

    uv run --frozen python scripts/improvement_loop.py
    uv run --frozen python scripts/improvement_loop.py --output-dir runtime/reports
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.paper_signal_log import PaperSignalLogError  # noqa: E402
from shared.paths import runtime_path  # noqa: E402
from strategy.domain import StrategyValidationError  # noqa: E402
from strategy.improvement_loop import (  # noqa: E402
    ImprovementLoopError,
    render_markdown,
    run_loop,
    write_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Improvement loop YAML. Defaults to config/improvement_loop.yaml.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Directory for the markdown and JSON report. Defaults to runtime/reports.",
    )
    args = parser.parse_args(argv)
    config_path = args.config
    if config_path is not None and not config_path.is_absolute():
        config_path = ROOT / config_path
    output_dir = args.output_dir or runtime_path("reports")
    if args.output_dir is not None and not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    try:
        report = run_loop(config_path=config_path)
        markdown_path, json_path = write_report(report, output_dir)
    except (ImprovementLoopError, StrategyValidationError, PaperSignalLogError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(render_markdown(report), end="")
    print(f"Wrote {markdown_path}")
    print(f"Wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
