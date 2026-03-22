from __future__ import annotations

import csv
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
EVENT_LOG_PATH = DATA_DIR / "event_log.csv"

EVENT_LOG_COLUMNS = [
    "event_id",
    "run_id",
    "event_time",
    "agent_name",
    "event_type",
    "entity_type",
    "entity_id",
    "ticker",
    "position_id",
    "order_id",
    "severity",
    "message",
    "before_json",
    "after_json",
    "metadata_json",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_for_json(value: Any) -> Any:
    """
    Make values JSON-serialisable and deterministic where possible.
    """
    if value is None:
        return None

    if isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {str(k): _normalise_for_json(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_normalise_for_json(v) for v in value]

    return str(value)


def _json_dumps(value: Optional[Dict[str, Any]]) -> str:
    if value is None:
        return ""
    return json.dumps(
        _normalise_for_json(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_event_log_exists() -> None:
    _ensure_data_dir()
    if not EVENT_LOG_PATH.exists():
        with EVENT_LOG_PATH.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=EVENT_LOG_COLUMNS)
            writer.writeheader()


def _build_event_id(run_id: str, agent_name: str, event_type: str, entity_id: str) -> str:
    base = f"{run_id}|{agent_name}|{event_type}|{entity_id}|{uuid.uuid4().hex}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16].upper()
    return f"EVT_{digest}"


def append_event(
    *,
    run_id: str,
    agent_name: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    ticker: str = "",
    position_id: str = "",
    order_id: str = "",
    severity: str = "info",
    message: str = "",
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Append one immutable event row to the event log.
    """
    _ensure_event_log_exists()

    event_id = _build_event_id(
        run_id=run_id,
        agent_name=agent_name,
        event_type=event_type,
        entity_id=entity_id,
    )

    row = {
        "event_id": event_id,
        "run_id": run_id,
        "event_time": _utc_now_iso(),
        "agent_name": agent_name,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "ticker": ticker,
        "position_id": position_id,
        "order_id": order_id,
        "severity": severity,
        "message": message,
        "before_json": _json_dumps(before_state),
        "after_json": _json_dumps(after_state),
        "metadata_json": _json_dumps(metadata),
    }

    with EVENT_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVENT_LOG_COLUMNS)
        writer.writerow(row)

    return event_id


def append_validation_event(
    *,
    run_id: str,
    agent_name: str,
    passed: bool,
    message: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    return append_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="validation_passed" if passed else "validation_failed",
        entity_type="system",
        entity_id="portfolio_state",
        severity="info" if passed else "error",
        message=message,
        metadata=metadata or {},
    )


def append_state_change_event(
    *,
    run_id: str,
    agent_name: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    before_state: Dict[str, Any],
    after_state: Dict[str, Any],
    ticker: str = "",
    position_id: str = "",
    order_id: str = "",
    severity: str = "info",
    message: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    return append_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        ticker=ticker,
        position_id=position_id,
        order_id=order_id,
        severity=severity,
        message=message,
        before_state=before_state,
        after_state=after_state,
        metadata=metadata or {},
    )