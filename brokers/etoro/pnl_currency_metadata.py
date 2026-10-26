"""Currency metadata only from the documented, unfiltered Demo breakdown.

The response contains other valuations, but none of its prices, PnL, timestamp
or rateTimestamp can provide or replace PnL-route valuation evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from brokers.etoro.schemas import (
    EtoroDetailedPosition, EtoroSchemaError, _decimal, _exact_integer, _list, _mapping,
)


PNL_CURRENCY_METADATA_QUERY = (
    ("conversionMode", "eToroApp"), ("positionLevel", "Normal"),
    ("orderLevel", "Normal"), ("mirrorLevel", "Details"),
)


@dataclass(frozen=True, slots=True)
class EtoroPositionCurrencyMetadata:
    position_id: str
    instrument_id: int
    asset_currency: str


def validate_demo_customer_id(value: Any) -> str:
    # CID is a required int32 header in the official route, never a URL filter.
    if (not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*", value) is None
            or len(value) > 10 or int(value) > 2147483647):
        raise EtoroSchemaError("currency metadata requires an explicit positive int32 Demo customer ID")
    return value


def validate_currency_metadata_positions(
    expected_demo_customer_id: str, positions: tuple[EtoroDetailedPosition, ...],
) -> dict[str, EtoroDetailedPosition]:
    expected_id = validate_demo_customer_id(expected_demo_customer_id)
    expected = {position.position_id: position for position in positions}
    if len(expected) != len(positions) or any(position.customer_id != expected_id for position in positions):
        raise EtoroSchemaError("currency metadata expected positions have inconsistent identity")
    if any(position.settlement_type_id not in {0, 1} for position in positions):
        raise EtoroSchemaError("currency metadata settlement scope is not supported")
    return expected


def parse_demo_position_currencies(
    value: Any, expected_demo_customer_id: str,
    positions: tuple[EtoroDetailedPosition, ...],
) -> tuple[EtoroPositionCurrencyMetadata, ...]:
    expected = validate_currency_metadata_positions(expected_demo_customer_id, positions)
    raw = _mapping(value, "currency_metadata")
    if raw.get("accountCurrency") != "USD":
        raise EtoroSchemaError("currency metadata account currency must be USD")
    if _list(raw.get("mirrors"), "currency_metadata.mirrors"):
        raise EtoroSchemaError("currency metadata mirrors must be empty")
    if any("order" in str(key).lower() for key in raw):
        raise EtoroSchemaError("unhandled order collection in currency metadata")
    expected_instruments = {position.instrument_id for position in positions}
    seen_instruments: set[int] = set()
    found: dict[str, EtoroPositionCurrencyMetadata] = {}
    for item in _list(raw.get("instruments"), "currency_metadata.instruments"):
        group = _mapping(item, "currency_metadata.instrument")
        instrument_id = _exact_integer(group.get("instrumentId"), "instrumentId", minimum=1)
        if instrument_id in seen_instruments or instrument_id not in expected_instruments:
            raise EtoroSchemaError("currency metadata has duplicate or unexpected instrument")
        seen_instruments.add(instrument_id)
        if any("order" in str(key).lower() and key != "orders" for key in group):
            raise EtoroSchemaError("unhandled order collection in currency metadata")
        if _list(group.get("orders"), "currency_metadata.instrument.orders"):
            raise EtoroSchemaError("currency metadata orders must be empty")
        for item in _list(group.get("positions"), "currency_metadata.instrument.positions"):
            row = _mapping(item, "currency_metadata.position")
            position_id = str(_exact_integer(row.get("positionId"), "positionId", minimum=1))
            source_instrument = _exact_integer(row.get("instrumentId"), "position.instrumentId", minimum=1)
            position = expected.get(position_id)
            if position is None or position_id in found:
                raise EtoroSchemaError("currency metadata has duplicate or unexpected position")
            if source_instrument != instrument_id or position.instrument_id != instrument_id:
                raise EtoroSchemaError("currency metadata position instrument mismatch")
            if (_decimal(row.get("units"), "position.units") != position.units
                    or _decimal(row.get("leverage"), "position.leverage") != position.leverage
                    or row.get("direction") != ("long" if position.is_buy else "short")
                    or row.get("settlementType") != {0: "cfd", 1: "real"}[position.settlement_type_id]):
                raise EtoroSchemaError("currency metadata position state mismatch")
            currency = row.get("assetCurrency")
            if not isinstance(currency, str) or currency not in {"USD", "GBX"}:
                raise EtoroSchemaError("currency metadata asset currency is not supported")
            found[position_id] = EtoroPositionCurrencyMetadata(position_id, instrument_id, currency)
    if seen_instruments != expected_instruments or set(found) != set(expected):
        raise EtoroSchemaError("currency metadata does not exactly cover expected positions")
    return tuple(found[position_id] for position_id in sorted(found))
