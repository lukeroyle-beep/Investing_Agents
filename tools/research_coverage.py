"""Report configured research coverage without downloading data or trading."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.backtesting_agent.backtesting_agent import load_backtest_config
from agents.universe_agent.universe_agent import load_assets
from shared.research_universe import selected_research_universe


def coverage_report():
    assets = load_assets()
    backtest = load_backtest_config()
    selection = selected_research_universe()
    scanner = {asset["ticker"] for asset in assets}
    history = set(backtest["tickers"])
    return {
        "evidence_kind": "configured_membership_only",
        "manifest_version": selection.version if selection else None,
        "manifest_checksum": selection.checksum if selection else None,
        "scanner_count": len(scanner), "backtest_strategy_count": len(history),
        "scanner_asset_classes": dict(sorted(Counter(asset["asset_class"] for asset in assets).items())),
        "scanner_only": sorted(scanner - history), "backtest_only": sorted(history - scanner),
        "shared_tickers": sorted(scanner & history),
        "membership_aligned": scanner == history,
        "benchmark_tickers": backtest["benchmark_tickers"],
        "whole_market": False, "historical_data_validated": False,
        "broker_execution_permission": False, "return_target_pct": None,
    }


if __name__ == "__main__":
    print(json.dumps(coverage_report(), sort_keys=True, indent=2))
