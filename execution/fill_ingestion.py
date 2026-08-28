from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from brokers.base import BrokerExecution
from execution.domain import Environment
from shared.io_utils import write_csv


BROKER_FILL_COLUMNS = [
    "fill_id",
    "ticker",
    "side",
    "action",
    "quantity",
    "fill_price",
    "fees",
    "fill_timestamp",
    "broker",
    "environment",
    "broker_execution_id",
    "broker_order_id",
    "broker_position_id",
    "broker_reference_id",
    "broker_instrument_id",
    "broker_rate_id",
    "broker_fee",
    "broker_tax",
    "currency",
]


class BrokerFillIngestionError(RuntimeError):
    pass


def stage_broker_executions(
    path: Path | str,
    *,
    executions: Sequence[BrokerExecution],
    ticker_by_instrument_id: Mapping[int, str],
    broker: str,
) -> pd.DataFrame:
    """Append broker-confirmed execution evidence without mutating economics."""
    destination = Path(path)
    if destination.exists() and destination.stat().st_size:
        existing = pd.read_csv(destination, keep_default_na=False)
        if list(existing.columns) != BROKER_FILL_COLUMNS:
            raise BrokerFillIngestionError("broker fill staging schema is invalid")
    else:
        existing = pd.DataFrame(columns=BROKER_FILL_COLUMNS)
    rows: list[dict[str, object]] = []
    seen = set(existing["fill_id"].astype(str))
    for execution in executions:
        if execution.environment != Environment.DEMO:
            raise BrokerFillIngestionError("only Demo broker executions may be staged")
        ticker = str(
            ticker_by_instrument_id.get(execution.broker_instrument_id, "")
        ).strip().upper()
        if not ticker:
            raise BrokerFillIngestionError(
                "broker execution lacks an exact internal instrument mapping"
            )
        fill_id = (
            f"{str(broker).strip().lower()}:{execution.environment}:"
            f"{execution.broker_execution_id}"
        )
        if fill_id in seen:
            continue
        rows.append(
            {
                "fill_id": fill_id,
                "ticker": ticker,
                "side": execution.side,
                "action": execution.action,
                "quantity": str(execution.units),
                "fill_price": str(execution.price),
                "fees": str(execution.fees + execution.taxes),
                "fill_timestamp": execution.executed_at.isoformat(),
                "broker": str(broker).strip().lower(),
                "environment": str(execution.environment),
                "broker_execution_id": execution.broker_execution_id,
                "broker_order_id": execution.broker_order_id,
                "broker_position_id": execution.broker_position_id,
                "broker_reference_id": execution.broker_reference_id,
                "broker_instrument_id": execution.broker_instrument_id,
                "broker_rate_id": execution.price_rate_id,
                "broker_fee": str(execution.fees),
                "broker_tax": str(execution.taxes),
                "currency": execution.currency,
            }
        )
        seen.add(fill_id)
    output = pd.concat(
        [existing, pd.DataFrame(rows, columns=BROKER_FILL_COLUMNS)], ignore_index=True
    )
    if output["fill_id"].astype(str).duplicated().any():
        raise BrokerFillIngestionError("broker fill identity collision")
    write_csv(output, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return output
