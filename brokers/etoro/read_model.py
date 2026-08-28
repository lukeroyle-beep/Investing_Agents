from __future__ import annotations

import os
import tempfile
from pathlib import Path

from brokers.base import BrokerAccountSnapshot
from execution.domain import canonical_json, payload_hash


FORBIDDEN_ECONOMIC_FILENAMES = {
    "portfolio_state.csv",
    "cash_state.csv",
    "cash_ledger.csv",
    "trade_fills.csv",
    "processed_fills.csv",
}


def build_reconciliation_read_model(snapshot: BrokerAccountSnapshot) -> dict[str, object]:
    positions = [
        {
            "broker_position_id": item.broker_position_id,
            "broker_instrument_id": item.broker_instrument_id,
            "units": str(item.units),
            "current_exposure": str(item.current_exposure),
            "average_leverage": str(item.average_leverage),
            "pnl": str(item.pnl),
            "currency": item.currency,
        }
        for item in snapshot.positions
    ]
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "authority": "broker_reconciliation_read_only",
        "broker": "etoro",
        "environment": str(snapshot.environment),
        "snapshot_id": snapshot.snapshot_id,
        "observed_at": snapshot.observed_at.isoformat(),
        "currency": snapshot.currency,
        "equity": str(snapshot.equity),
        "available_cash": str(snapshot.available_cash),
        "balance": str(snapshot.balance),
        "frozen_cash": str(snapshot.frozen_cash),
        "current_pnl": str(snapshot.current_pnl),
        "used_margin": str(snapshot.used_margin),
        "positions": positions,
        "pending_orders_complete": snapshot.pending_orders_complete,
        "executions_complete": snapshot.executions_complete,
    }
    payload["payload_sha256"] = payload_hash(payload)
    return payload


def write_reconciliation_read_model(
    snapshot: BrokerAccountSnapshot,
    path: Path | str,
) -> Path:
    destination = Path(path)
    if destination.name in FORBIDDEN_ECONOMIC_FILENAMES:
        raise ValueError("broker read model cannot target an economic-state artifact")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = build_reconciliation_read_model(snapshot)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination
