from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from execution.instruments import Instrument, InstrumentRegistry
from execution.store import ExecutionStore
from shared.paths import EXECUTION_STORE_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Register an immutable internal instrument identity; no broker call is made."
    )
    parser.add_argument("symbol")
    parser.add_argument("exchange")
    parser.add_argument("--asset-type", required=True, choices=("equity", "etf"))
    parser.add_argument("--currency", required=True)
    parser.add_argument("--sector", default="unknown")
    parser.add_argument("--store", type=Path, default=EXECUTION_STORE_PATH)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    store = ExecutionStore(args.store)
    registry = InstrumentRegistry(store)
    instrument = registry.register(
        Instrument.create(
            canonical_symbol=args.symbol,
            exchange=args.exchange,
            asset_type=args.asset_type,
            currency=args.currency,
            sector=args.sector,
        )
    )
    print(f"Internal instrument UUID: {instrument.internal_instrument_id}")
    print(
        "Registered canonical identity: "
        f"{instrument.canonical_symbol}@{instrument.exchange} "
        f"{instrument.asset_type}/{instrument.currency}"
    )
    print("No broker mapping, connection, credential, or order was created.")


if __name__ == "__main__":
    main()
