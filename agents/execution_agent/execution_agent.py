from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pandas as pd

from execution.coordinator import (
    ExecutionConfig,
    ExecutionCoordinator,
    ExecutionDisabledError,
)
from execution.instruments import InstrumentRegistry
from execution.store import ExecutionStore
from shared.paths import (
    EXECUTION_CONFIG_PATH,
    EXECUTION_STORE_PATH,
    PORTFOLIO_ORDERS_PATH,
)

PORTFOLIO_ORDERS_FILE = PORTFOLIO_ORDERS_PATH
def load_portfolio_orders() -> pd.DataFrame:
    if not os.path.exists(PORTFOLIO_ORDERS_FILE):
        return pd.DataFrame()

    df = pd.read_csv(PORTFOLIO_ORDERS_FILE, dtype=str, keep_default_na=False)

    if df.empty:
        return pd.DataFrame()

    return df


def build_coordinator() -> ExecutionCoordinator:
    store = ExecutionStore(EXECUTION_STORE_PATH)
    return ExecutionCoordinator(
        store=store,
        registry=InstrumentRegistry(store),
        config=ExecutionConfig.load(EXECUTION_CONFIG_PATH),
    )


def main() -> None:
    orders = load_portfolio_orders()
    if orders.empty:
        print("No portfolio orders found. No intents were created.")
        return
    coordinator = build_coordinator()
    try:
        intents = coordinator.import_advisory_orders(
            orders,
            strategy_id="portfolio_agent",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
    except ExecutionDisabledError as exc:
        print(f"Execution remains disabled: {exc}")
        return
    print(
        f"Persisted {len(intents)} immutable advisory intent(s); "
        "no order was submitted."
    )


if __name__ == "__main__":
    main()
