from __future__ import annotations

from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

import pandas as pd

from brokers.base import BrokerExecution
from execution.domain import Environment
from execution.accounting import _sum as exact_sum
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
    "executed_amount",
]
LEGACY_BROKER_FILL_COLUMNS = BROKER_FILL_COLUMNS[:-1]


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
        # A broker Decimal must never make a binary-float round trip while a
        # later fill is appended. Keep retained values and identifiers as text.
        existing = pd.read_csv(destination, keep_default_na=False, dtype=str)
        if list(existing.columns) not in (BROKER_FILL_COLUMNS, LEGACY_BROKER_FILL_COLUMNS):
            raise BrokerFillIngestionError("broker fill staging schema is invalid")
    else:
        existing = pd.DataFrame(columns=LEGACY_BROKER_FILL_COLUMNS)
    columns = (BROKER_FILL_COLUMNS if "executed_amount" in existing.columns
               or any(x.executed_amount is not None for x in executions)
               else LEGACY_BROKER_FILL_COLUMNS)
    if "executed_amount" in columns and "executed_amount" not in existing.columns:
        existing["executed_amount"] = ""
    rows: list[dict[str, object]] = []
    existing_by_id = {row["fill_id"]: row for row in existing.to_dict(orient="records")}
    if len(existing_by_id) != len(existing):
        raise BrokerFillIngestionError("broker fill identity collision")
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
        row = {
                "fill_id": fill_id,
                "ticker": ticker,
                "side": execution.side,
                "action": execution.action,
                "quantity": str(execution.units),
                "fill_price": str(execution.price),
                "fees": str(exact_sum((execution.fees, execution.taxes))),
                "fill_timestamp": execution.executed_at_source or execution.executed_at.isoformat(),
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
        if "executed_amount" in columns:
            row["executed_amount"] = "" if execution.executed_amount is None else str(execution.executed_amount)
        # An execution ID is idempotent only when every economic and identity
        # field agrees. A changed cumulative execution cannot silently vanish.
        if fill_id in existing_by_id:
            old = existing_by_id[fill_id]
            numeric = {"quantity", "fill_price", "fees", "broker_fee", "broker_tax", "executed_amount"}
            try:
                same = all(Decimal(str(old[k])) == Decimal(str(v)) if k in numeric and str(v) != "" and str(old[k]) != ""
                           else str(old[k]) == str(v) for k, v in row.items())
            except (InvalidOperation, KeyError):
                same = False
            if not same:
                raise BrokerFillIngestionError("broker fill identity has contradictory values")
            continue
        rows.append(row)
        existing_by_id[fill_id] = row
    output = pd.concat(
        [existing, pd.DataFrame(rows, columns=columns)], ignore_index=True
    )
    if output["fill_id"].astype(str).duplicated().any():
        raise BrokerFillIngestionError("broker fill identity collision")
    write_csv(output, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return output
