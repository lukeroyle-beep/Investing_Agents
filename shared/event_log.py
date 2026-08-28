from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pandas as pd

from shared.io_utils import append_csv_file, write_csv_file
from shared.paths import DATA_DIR, data_path
from shared.sqlite_sidecar import append_event_log_row
from shared.schema_registry import get_file_schema

EVENT_LOG_PATH = data_path("event_log.csv")
EVENT_LOG_SCHEMA = get_file_schema("event_log.csv")
EVENT_LOG_COLUMNS = EVENT_LOG_SCHEMA.canonical_column_order
TARGET_EVENT_TYPES = {
    "artifact_written",
    "fill_processed",
    "position_opened",
    "position_reduced",
    "position_closed",
    "cash_adjusted",
    "exit_decision_generated",
    "equity_snapshot_recorded",
    "validation_passed",
    "validation_failed",
    "run_started",
    "run_completed",
    "run_failed",
}


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


def _merge_metadata(base: Optional[Dict[str, Any]], extra: Optional[Dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if base:
        merged.update(base)
    if extra:
        merged.update(extra)
    return merged


def _build_metadata_envelope(
    *,
    entity_type: str,
    entity_id: str,
    ticker: str = "",
    position_id: str = "",
    order_id: str = "",
    details: Optional[Dict[str, Any]] = None,
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema_version": "1.0",
        "entity": {
            "entity_type": entity_type,
            "entity_id": entity_id,
        },
        "details": details or {},
    }

    refs = {}
    if str(ticker).strip():
        refs["ticker"] = str(ticker).strip()
    if str(position_id).strip():
        refs["position_id"] = str(position_id).strip()
    if str(order_id).strip():
        refs["order_id"] = str(order_id).strip()
    if refs:
        envelope["refs"] = refs

    if before_state is not None:
        envelope["before_state"] = before_state
    if after_state is not None:
        envelope["after_state"] = after_state

    return envelope


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def ensure_event_log_exists() -> None:
    _ensure_data_dir()
    if not EVENT_LOG_PATH.exists():
        write_csv_file(
            pd.DataFrame(columns=EVENT_LOG_COLUMNS),
            EVENT_LOG_PATH,
            producer="Shared Event Log",
        )


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
    ensure_event_log_exists()

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

    append_csv_file(
        pd.DataFrame([row], columns=EVENT_LOG_COLUMNS),
        EVENT_LOG_PATH,
        producer="Shared Event Log",
    )

    append_event_log_row(row)
    return event_id


def append_standard_event(
    *,
    run_id: str,
    agent_name: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    severity: str = "info",
    message: str = "",
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    position_id: str = "",
    order_id: str = "",
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    if event_type not in TARGET_EVENT_TYPES:
        raise ValueError(f"Unsupported event taxonomy value: {event_type}")

    metadata_envelope = _build_metadata_envelope(
        entity_type=entity_type,
        entity_id=entity_id,
        ticker=ticker,
        position_id=position_id,
        order_id=order_id,
        details=_merge_metadata(details, metadata),
        before_state=before_state,
        after_state=after_state,
    )

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
        metadata=metadata_envelope,
    )


def append_validation_event(
    *,
    run_id: str,
    agent_name: str,
    passed: bool,
    message: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="validation_passed" if passed else "validation_failed",
        entity_type="system",
        entity_id="portfolio_state",
        severity="info" if passed else "error",
        message=message,
        details=metadata or {},
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
    return append_standard_event(
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
        details=metadata or {},
    )


def append_fill_processed_event(
    *,
    run_id: str,
    agent_name: str,
    fill_id: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    position_id: str = "",
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="fill_processed",
        entity_type="fill",
        entity_id=fill_id,
        severity=severity,
        message=message,
        details=details,
        ticker=ticker,
        position_id=position_id,
    )


def append_artifact_written_event(
    *,
    run_id: str,
    agent_name: str,
    entity_type: str,
    entity_id: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    position_id: str = "",
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="artifact_written",
        entity_type=entity_type,
        entity_id=entity_id,
        severity=severity,
        message=message,
        details=details,
        ticker=ticker,
        position_id=position_id,
    )


def append_position_opened_event(
    *,
    run_id: str,
    agent_name: str,
    position_id: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="position_opened",
        entity_type="position",
        entity_id=position_id,
        severity=severity,
        message=message,
        details=details,
        ticker=ticker,
        position_id=position_id,
        before_state=before_state,
        after_state=after_state,
    )


def append_position_reduced_event(
    *,
    run_id: str,
    agent_name: str,
    position_id: str,
    ticker: str,
    message: str,
    before_state: Dict[str, Any],
    after_state: Dict[str, Any],
    details: Optional[Dict[str, Any]] = None,
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="position_reduced",
        entity_type="position",
        entity_id=position_id,
        ticker=ticker,
        position_id=position_id,
        message=message,
        before_state=before_state,
        after_state=after_state,
        details=details,
    )


def append_position_closed_event(
    *,
    run_id: str,
    agent_name: str,
    position_id: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="position_closed",
        entity_type="position",
        entity_id=position_id,
        severity=severity,
        message=message,
        details=details,
        ticker=ticker,
        position_id=position_id,
        before_state=before_state,
        after_state=after_state,
    )


def append_cash_adjusted_event(
    *,
    run_id: str,
    agent_name: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    position_id: str = "",
    before_state: Optional[Dict[str, Any]] = None,
    after_state: Optional[Dict[str, Any]] = None,
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="cash_adjusted",
        entity_type="cash",
        entity_id="cash_state",
        severity=severity,
        message=message,
        details=details,
        ticker=ticker,
        position_id=position_id,
        before_state=before_state,
        after_state=after_state,
    )


def append_exit_decision_generated_event(
    *,
    run_id: str,
    agent_name: str,
    position_id: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    ticker: str = "",
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="exit_decision_generated",
        entity_type="position",
        entity_id=position_id,
        severity=severity,
        message=message,
        details=details,
        ticker=ticker,
        position_id=position_id,
    )


def append_equity_snapshot_recorded_event(
    *,
    run_id: str,
    agent_name: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    severity: str = "info",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type="equity_snapshot_recorded",
        entity_type="portfolio",
        entity_id="portfolio_equity",
        severity=severity,
        message=message,
        details=details,
    )


def append_run_lifecycle_event(
    *,
    run_id: str,
    event_type: str,
    message: str,
    details: Optional[Dict[str, Any]] = None,
    severity: str = "info",
    agent_name: str = "Pipeline Runner",
) -> str:
    return append_standard_event(
        run_id=run_id,
        agent_name=agent_name,
        event_type=event_type,
        entity_type="run",
        entity_id=run_id,
        severity=severity,
        message=message,
        details=details,
    )
