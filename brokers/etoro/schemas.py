from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from execution.domain import payload_hash


class EtoroSchemaError(ValueError):
    """Official response payload is missing or contradicts required fields."""


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EtoroSchemaError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise EtoroSchemaError(f"{field} must be an array")
    return value


def _text(value: Any, field: str) -> str:
    result = str(value).strip() if value is not None else ""
    if not result:
        raise EtoroSchemaError(f"{field} must not be blank")
    return result


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise EtoroSchemaError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise EtoroSchemaError(f"{field} must be an integer") from exc
    if result <= 0:
        raise EtoroSchemaError(f"{field} must be positive")
    return result


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EtoroSchemaError(f"{field} must be numeric") from exc
    if not result.is_finite():
        raise EtoroSchemaError(f"{field} must be finite")
    return result


def _timestamp(value: Any, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise EtoroSchemaError(f"{field} must be an ISO timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise EtoroSchemaError(f"{field} must include a timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class EtoroInstrument:
    instrument_id: int
    symbol: str
    exchange_name: str
    instrument_type: str
    is_delisted: bool
    is_currently_tradable: bool
    metadata: tuple[tuple[str, str], ...]

    @classmethod
    def parse(cls, value: Any) -> "EtoroInstrument":
        raw = _mapping(value, "instrument")
        metadata = tuple(
            sorted(
                (str(key), str(item))
                for key, item in raw.items()
                if key not in {"logo35x35", "logo50x50", "logo150x150"}
            )
        )
        return cls(
            instrument_id=_integer(raw.get("instrumentId"), "instrumentId"),
            symbol=_text(raw.get("internalSymbolFull"), "internalSymbolFull").upper(),
            exchange_name=_text(
                raw.get("internalExchangeName"), "internalExchangeName"
            ).upper(),
            instrument_type=_text(raw.get("instrumentType"), "instrumentType").lower(),
            is_delisted=raw.get("isDelisted") is True,
            is_currently_tradable=raw.get("isCurrentlyTradable") is True,
            metadata=metadata,
        )

    def metadata_mapping(self) -> dict[str, str]:
        return dict(self.metadata)


@dataclass(frozen=True, slots=True)
class EtoroSearchResponse:
    items: tuple[EtoroInstrument, ...]
    total_items: int

    @classmethod
    def parse(cls, value: Any) -> "EtoroSearchResponse":
        raw = _mapping(value, "search_response")
        items = tuple(
            EtoroInstrument.parse(item) for item in _list(raw.get("items"), "items")
        )
        try:
            total_items = int(raw.get("totalItems", len(items)))
        except (TypeError, ValueError) as exc:
            raise EtoroSchemaError("totalItems must be an integer") from exc
        if total_items < len(items) or total_items < 0:
            raise EtoroSchemaError("totalItems contradicts items")
        return cls(items=items, total_items=total_items)


@dataclass(frozen=True, slots=True)
class EtoroMarketRate:
    instrument_id: int
    bid: Decimal
    ask: Decimal
    observed_at: datetime
    price_rate_id: str

    @classmethod
    def parse(cls, value: Any) -> "EtoroMarketRate":
        raw = _mapping(value, "market_rate")
        bid = _decimal(raw.get("bid"), "bid")
        ask = _decimal(raw.get("ask"), "ask")
        if bid <= 0 or ask <= 0 or ask < bid:
            raise EtoroSchemaError("market rate bid/ask is invalid")
        return cls(
            instrument_id=_integer(raw.get("instrumentID"), "instrumentID"),
            bid=bid,
            ask=ask,
            observed_at=_timestamp(raw.get("date"), "date"),
            price_rate_id=_text(raw.get("priceRateID"), "priceRateID"),
        )


@dataclass(frozen=True, slots=True)
class EtoroMarketRatesResponse:
    rates: tuple[EtoroMarketRate, ...]

    @classmethod
    def parse(cls, value: Any) -> "EtoroMarketRatesResponse":
        raw = _mapping(value, "market_rates_response")
        rates = tuple(
            EtoroMarketRate.parse(item) for item in _list(raw.get("rates"), "rates")
        )
        identifiers = [rate.instrument_id for rate in rates]
        if len(identifiers) != len(set(identifiers)):
            raise EtoroSchemaError("market rates contain duplicate instrument IDs")
        return cls(rates=rates)


@dataclass(frozen=True, slots=True)
class EtoroAccountTotals:
    available_cash: Decimal
    frozen_cash: Decimal
    current_pnl: Decimal
    total_value: Decimal
    used_margin: Decimal
    balance: Decimal

    @classmethod
    def parse(cls, value: Any) -> "EtoroAccountTotals":
        raw = _mapping(value, "accountTotals")
        result = cls(
            available_cash=_decimal(raw.get("accountAvailableCash"), "accountAvailableCash"),
            frozen_cash=_decimal(raw.get("accountFrozenCash"), "accountFrozenCash"),
            current_pnl=_decimal(raw.get("accountCurrentPnl"), "accountCurrentPnl"),
            total_value=_decimal(raw.get("accountTotalValue"), "accountTotalValue"),
            used_margin=_decimal(
                raw.get("accountTotalUsedMargin"), "accountTotalUsedMargin"
            ),
            balance=_decimal(raw.get("accountBalance"), "accountBalance"),
        )
        if result.total_value < 0 or result.available_cash < 0:
            raise EtoroSchemaError("account totals contain negative equity or cash")
        return result


@dataclass(frozen=True, slots=True)
class EtoroInstrumentAggregate:
    instrument_id: int
    currency: str
    units: Decimal
    current_exposure: Decimal
    average_leverage: Decimal
    pnl: Decimal

    @classmethod
    def parse(cls, value: Any) -> "EtoroInstrumentAggregate":
        raw = _mapping(value, "instrumentAggregate")
        leverage = _decimal(raw.get("avgLeverage"), "avgLeverage")
        if leverage < 0:
            raise EtoroSchemaError("average leverage must not be negative")
        return cls(
            instrument_id=_integer(raw.get("instrumentId"), "instrumentId"),
            currency=_text(raw.get("assetCurrency"), "assetCurrency").upper(),
            units=_decimal(raw.get("netUnits"), "netUnits"),
            current_exposure=_decimal(
                raw.get("netCurrentExposureAccountCurrency"),
                "netCurrentExposureAccountCurrency",
            ),
            average_leverage=leverage,
            pnl=_decimal(raw.get("accountCurrencyReturn"), "accountCurrencyReturn"),
        )


@dataclass(frozen=True, slots=True)
class EtoroPortfolioSnapshot:
    customer_id: str
    observed_at: datetime
    account_currency: str
    totals: EtoroAccountTotals
    instrument_aggregates: tuple[EtoroInstrumentAggregate, ...]
    mirror_count: int
    snapshot_id: str

    @classmethod
    def parse(cls, value: Any) -> "EtoroPortfolioSnapshot":
        raw = _mapping(value, "portfolio_snapshot")
        aggregates = tuple(
            EtoroInstrumentAggregate.parse(item)
            for item in _list(raw.get("instrumentAggregates"), "instrumentAggregates")
        )
        mirrors = _list(raw.get("mirrors", []), "mirrors")
        identity_payload = {
            "cid": raw.get("cid"),
            "timestamp": raw.get("timestamp"),
            "accountCurrency": raw.get("accountCurrency"),
            "accountTotals": raw.get("accountTotals"),
            "instrumentAggregates": raw.get("instrumentAggregates"),
        }
        return cls(
            customer_id=_text(raw.get("cid"), "cid"),
            observed_at=_timestamp(raw.get("timestamp"), "timestamp"),
            account_currency=_text(
                raw.get("accountCurrency"), "accountCurrency"
            ).upper(),
            totals=EtoroAccountTotals.parse(raw.get("accountTotals")),
            instrument_aggregates=aggregates,
            mirror_count=len(mirrors),
            snapshot_id=payload_hash(identity_payload),
        )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field)


def _non_negative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise EtoroSchemaError(f"{field} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise EtoroSchemaError(f"{field} must be an integer") from exc
    if result < 0:
        raise EtoroSchemaError(f"{field} must not be negative")
    return result


@dataclass(frozen=True, slots=True)
class EtoroAcceptedOrder:
    order_id: str
    reference_id: str
    token: str | None

    @classmethod
    def parse(cls, value: Any) -> "EtoroAcceptedOrder":
        raw = _mapping(value, "accepted_order")
        return cls(
            order_id=str(_integer(raw.get("orderId"), "orderId")),
            reference_id=_text(raw.get("referenceId"), "referenceId"),
            token=_optional_text(raw.get("token")),
        )


@dataclass(frozen=True, slots=True)
class EtoroCloseSubmission:
    order_id: str
    position_id: str
    instrument_id: int
    units_to_deduct: Decimal
    token: str | None

    @classmethod
    def parse(cls, value: Any) -> "EtoroCloseSubmission":
        raw = _mapping(value, "close_submission")
        order = _mapping(raw.get("orderForClose"), "orderForClose")
        return cls(
            order_id=str(_integer(order.get("orderID"), "orderForClose.orderID")),
            position_id=str(
                _integer(order.get("positionID"), "orderForClose.positionID")
            ),
            instrument_id=_integer(
                order.get("instrumentID"), "orderForClose.instrumentID"
            ),
            units_to_deduct=_decimal(
                order.get("unitsToDeduct"), "orderForClose.unitsToDeduct"
            ),
            token=_optional_text(raw.get("token")),
        )


@dataclass(frozen=True, slots=True)
class EtoroOrderStatus:
    status_id: int
    name: str
    error_code: int
    error_message: str | None

    @classmethod
    def parse(cls, value: Any) -> "EtoroOrderStatus":
        raw = _mapping(value, "status")
        status_id = _integer(raw.get("id"), "status.id")
        if status_id not in set(range(1, 13)):
            raise EtoroSchemaError("status.id is outside the pinned lifecycle")
        return cls(
            status_id=status_id,
            name=_text(raw.get("name"), "status.name"),
            error_code=_non_negative_integer(raw.get("errorCode", 0), "status.errorCode"),
            error_message=_optional_text(raw.get("errorMessage")),
        )


@dataclass(frozen=True, slots=True)
class EtoroPositionExecution:
    execution_id: str
    position_id: str
    state: str
    units: Decimal
    price: Decimal
    executed_at: datetime
    price_rate_id: str
    fees: Decimal
    taxes: Decimal

    @classmethod
    def parse(cls, value: Any) -> "EtoroPositionExecution":
        raw = _mapping(value, "positionExecution")
        opening = _mapping(raw.get("openingData"), "positionExecution.openingData")
        identity = {
            "positionId": raw.get("positionId"),
            "orderId": opening.get("orderId"),
            "executionTime": opening.get("executionTime"),
            "units": opening.get("units"),
            "avgPrice": opening.get("avgPrice"),
            "priceId": opening.get("priceId"),
        }
        units = _decimal(opening.get("units"), "openingData.units")
        price = _decimal(opening.get("avgPrice"), "openingData.avgPrice")
        if units <= 0 or price <= 0:
            raise EtoroSchemaError("execution units and price must be positive")
        return cls(
            execution_id=payload_hash(identity),
            position_id=str(_integer(raw.get("positionId"), "positionId")),
            state=_text(raw.get("state"), "positionExecution.state").lower(),
            units=units,
            price=price,
            executed_at=_timestamp(opening.get("executionTime"), "executionTime"),
            price_rate_id=str(_integer(opening.get("priceId"), "priceId")),
            fees=_decimal(opening.get("fees", 0), "openingData.fees"),
            taxes=_decimal(opening.get("taxes", 0), "openingData.taxes"),
        )


@dataclass(frozen=True, slots=True)
class EtoroOrderLookup:
    order_id: str
    action: str
    transaction: str
    order_type: str
    instrument_id: int
    symbol: str
    currency: str
    reference_id: str | None
    status: EtoroOrderStatus
    executions: tuple[EtoroPositionExecution, ...]
    requested_amount: Decimal | None
    requested_units: Decimal | None
    total_costs: Decimal
    requested_at: datetime
    updated_at: datetime

    @classmethod
    def parse(
        cls, value: Any, *, reference_id: str | None = None
    ) -> "EtoroOrderLookup":
        raw = _mapping(value, "order_lookup")
        asset = _mapping(raw.get("asset"), "asset")
        executions = tuple(
            EtoroPositionExecution.parse(item)
            for item in _list(raw.get("positionExecutions", []), "positionExecutions")
        )
        execution_ids = [item.execution_id for item in executions]
        if len(execution_ids) != len(set(execution_ids)):
            raise EtoroSchemaError("order lookup contains duplicate executions")
        return cls(
            order_id=str(_integer(raw.get("orderId"), "orderId")),
            action=_text(raw.get("action"), "action").lower(),
            transaction=_text(raw.get("transaction"), "transaction"),
            order_type=_text(raw.get("type"), "type").lower(),
            instrument_id=_integer(asset.get("instrumentId"), "asset.instrumentId"),
            symbol=_text(asset.get("symbol"), "asset.symbol").upper(),
            currency=_text(asset.get("currency"), "asset.currency").upper(),
            reference_id=_optional_text(reference_id),
            status=EtoroOrderStatus.parse(raw.get("status")),
            executions=executions,
            requested_amount=_optional_decimal(raw.get("requestedAmount"), "requestedAmount"),
            requested_units=_optional_decimal(raw.get("requestedUnits"), "requestedUnits"),
            total_costs=_decimal(raw.get("totalCosts", 0), "totalCosts"),
            requested_at=_timestamp(raw.get("requestTime"), "requestTime"),
            updated_at=_timestamp(raw.get("lastUpdate"), "lastUpdate"),
        )
