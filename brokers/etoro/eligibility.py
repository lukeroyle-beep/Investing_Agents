"""Demo instrument configuration, not market-open, account-risk or order approval.

Pinned to getInstrumentEligibilityDemo, official API v1.398.0, 2026-10-21.
https://api-portal.etoro.com/api-reference/trading--demo/check-instrument-trading-eligibility
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from brokers.etoro.schemas import EtoroSchemaError, _decimal, _exact_integer, _list, _mapping, _text
from execution.domain import payload_hash


def validated_ids(values) -> tuple[int, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 100:
        raise ValueError("eligibility requires one to 100 distinct instrument IDs")
    if any(type(value) is not int or value <= 0 for value in values) or len(set(values)) != len(values):
        raise ValueError("eligibility instrument IDs must be distinct positive integers")
    return tuple(sorted(values))


def _boolean(raw, field):
    value = raw.get(field)
    if type(value) is not bool:
        raise EtoroSchemaError(f"{field} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class EtoroInstrumentEligibility:
    instrument_id: int
    symbol: str
    allow_open_position: bool
    allowed_quantity_type: str
    trade_unit_type: str
    minimum_exposure: Decimal
    maximum_units: Decimal
    real_long_one_available: bool
    real_long_one_minimum_amount: Decimal | None

    @classmethod
    def parse(cls, value):
        raw = _mapping(value, "eligibility")
        quantity = _text(raw.get("allowedOrderQuantityType"), "allowedOrderQuantityType")
        units = _text(raw.get("tradeUnitType"), "tradeUnitType")
        if quantity not in {"unitsOnly", "amountOnly", "all"} or units not in {"units", "contracts"}:
            raise EtoroSchemaError("unsupported eligibility sizing configuration")
        minimum = _decimal(raw.get("minPositionExposure"), "minPositionExposure")
        maximum = _decimal(raw.get("maxUnitsPerOrder"), "maxUnitsPerOrder")
        if minimum < 0 or maximum <= 0:
            raise EtoroSchemaError("eligibility position limits are invalid")
        matches = []
        for item in _list(raw.get("leverageConfigs"), "leverageConfigs"):
            config = _mapping(item, "leverageConfig")
            settlement = _text(config.get("settlementType"), "settlementType")
            direction = _text(config.get("direction"), "direction")
            if settlement not in {"real", "cfd", "realFutures", "marginTrade"} or direction not in {"long", "short"}:
                raise EtoroSchemaError("unsupported leverage configuration")
            leverage = _list(config.get("leverageValues"), "leverageValues")
            if not leverage or any(type(v) is not int or v <= 0 for v in leverage) or len(set(leverage)) != len(leverage):
                raise EtoroSchemaError("leverage values must be distinct positive integers")
            potential = _boolean(config, "isPotential")
            amount = _decimal(config.get("minPositionAmount"), "minPositionAmount")
            if amount < 0:
                raise EtoroSchemaError("minimum position amount cannot be negative")
            if settlement == "real" and direction == "long" and 1 in leverage:
                matches.append((potential, amount))
        if len(matches) > 1:
            raise EtoroSchemaError("ambiguous real long leverage-one configuration")
        available = bool(matches) and matches[0][0] is False
        return cls(
            instrument_id=_exact_integer(raw.get("instrumentId"), "instrumentId", minimum=1),
            symbol=_text(raw.get("symbol"), "symbol").upper(),
            allow_open_position=_boolean(raw, "allowOpenPosition"),
            allowed_quantity_type=quantity, trade_unit_type=units,
            minimum_exposure=minimum, maximum_units=maximum,
            real_long_one_available=available,
            real_long_one_minimum_amount=matches[0][1] if available else None,
        )


@dataclass(frozen=True, slots=True)
class EtoroEligibilityResponse:
    evidence_id: str
    currency: str
    items: tuple[EtoroInstrumentEligibility, ...]

    @classmethod
    def parse(cls, value: Any, requested_ids: tuple[int, ...]):
        requested = validated_ids(requested_ids)
        raw = _mapping(value, "eligibilityResponse")
        currency = _text(raw.get("currency"), "currency").upper()
        if currency != "USD":
            raise EtoroSchemaError("eligibility response currency must be USD")
        missing_ids = _list(raw.get("notFoundInstrumentIds"), "notFoundInstrumentIds")
        missing_symbols = _list(raw.get("notFoundSymbols"), "notFoundSymbols")
        if missing_ids or missing_symbols:
            raise EtoroSchemaError("eligibility contains unresolved instruments")
        items = tuple(EtoroInstrumentEligibility.parse(item) for item in _list(raw.get("eligibilities"), "eligibilities"))
        ids = [item.instrument_id for item in items]
        if len(set(ids)) != len(ids) or set(ids) != set(requested):
            raise EtoroSchemaError("eligibility does not exactly cover requested instruments")
        return cls(payload_hash(raw), currency, tuple(sorted(items, key=lambda item: item.instrument_id)))
