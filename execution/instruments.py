from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from execution.domain import (
    DomainValidationError,
    Environment,
    _aware_utc,
    _uuid,
    payload_hash,
)


class InstrumentRegistryError(RuntimeError):
    """Base error for instrument identity and mapping failures."""


class InstrumentCollisionError(InstrumentRegistryError):
    """Raised when a broker or canonical identity maps to two instruments."""


class InstrumentNotFoundError(InstrumentRegistryError):
    """Raised when an immutable internal instrument cannot be resolved."""


class AmbiguousInstrumentError(InstrumentRegistryError):
    """Raised when symbol-only lookup could identify more than one instrument."""


class InstrumentMappingError(InstrumentRegistryError):
    """Raised when a broker mapping is not an exact identity match."""


@dataclass(frozen=True, slots=True)
class Instrument:
    internal_instrument_id: UUID
    canonical_symbol: str
    exchange: str
    asset_type: str
    currency: str
    sector: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "internal_instrument_id",
            _uuid(self.internal_instrument_id, "internal_instrument_id"),
        )
        object.__setattr__(
            self, "canonical_symbol", str(self.canonical_symbol).strip().upper()
        )
        object.__setattr__(self, "exchange", str(self.exchange).strip().upper())
        object.__setattr__(self, "asset_type", str(self.asset_type).strip().lower())
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        object.__setattr__(self, "sector", str(self.sector).strip().lower() or "unknown")
        for name in (
            "canonical_symbol",
            "exchange",
            "asset_type",
            "currency",
        ):
            if not getattr(self, name):
                raise DomainValidationError(f"{name} must not be blank")

    @classmethod
    def create(
        cls,
        *,
        canonical_symbol: str,
        exchange: str,
        asset_type: str,
        currency: str,
        sector: str = "unknown",
        internal_instrument_id: UUID | str | None = None,
    ) -> "Instrument":
        return cls(
            internal_instrument_id=_uuid(
                internal_instrument_id or uuid4(), "internal_instrument_id"
            ),
            canonical_symbol=canonical_symbol,
            exchange=exchange,
            asset_type=asset_type,
            currency=currency,
            sector=sector,
        )


@dataclass(frozen=True, slots=True)
class BrokerInstrumentMapping:
    internal_instrument_id: UUID
    broker: str
    environment: Environment
    broker_instrument_id: int
    exact_match_symbol: str
    resolved_at: datetime
    metadata_checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "internal_instrument_id",
            _uuid(self.internal_instrument_id, "internal_instrument_id"),
        )
        object.__setattr__(self, "broker", str(self.broker).strip().lower())
        object.__setattr__(self, "environment", Environment(self.environment))
        object.__setattr__(
            self, "exact_match_symbol", str(self.exact_match_symbol).strip().upper()
        )
        object.__setattr__(
            self, "resolved_at", _aware_utc(self.resolved_at, "resolved_at")
        )
        if self.broker_instrument_id <= 0:
            raise DomainValidationError("broker_instrument_id must be positive")
        if not self.broker or not self.exact_match_symbol:
            raise DomainValidationError("broker mapping identity must not be blank")
        if len(self.metadata_checksum) != 64:
            raise DomainValidationError("metadata_checksum must be a sha256 hex digest")
        try:
            int(self.metadata_checksum, 16)
        except ValueError as exc:
            raise DomainValidationError(
                "metadata_checksum must be a sha256 hex digest"
            ) from exc

    @classmethod
    def from_resolution(
        cls,
        *,
        instrument: Instrument,
        broker: str,
        environment: Environment | str,
        broker_instrument_id: int,
        exact_match_symbol: str,
        metadata: Mapping[str, Any],
        resolved_at: datetime | None = None,
    ) -> "BrokerInstrumentMapping":
        exact_symbol = str(exact_match_symbol).strip().upper()
        if exact_symbol != instrument.canonical_symbol:
            raise InstrumentMappingError(
                "broker symbol is not an exact match for the canonical symbol"
            )
        return cls(
            internal_instrument_id=instrument.internal_instrument_id,
            broker=broker,
            environment=Environment(environment),
            broker_instrument_id=int(broker_instrument_id),
            exact_match_symbol=exact_symbol,
            resolved_at=resolved_at or datetime.now(timezone.utc),
            metadata_checksum=payload_hash(metadata),
        )


class InstrumentRegistry:
    """Identity-safe facade over the execution store's registry tables."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def register(self, instrument: Instrument) -> Instrument:
        return self._store.register_instrument(instrument)

    def get(self, internal_instrument_id: UUID | str) -> Instrument:
        instrument = self._store.get_instrument(internal_instrument_id)
        if instrument is None:
            raise InstrumentNotFoundError(str(internal_instrument_id))
        return instrument

    def resolve_canonical(self, *, symbol: str, exchange: str) -> Instrument:
        instrument = self._store.find_instrument(symbol=symbol, exchange=exchange)
        if instrument is None:
            raise InstrumentNotFoundError(f"{symbol}@{exchange}")
        return instrument

    def resolve_symbol_only(self, symbol: str) -> Instrument:
        matches = self._store.find_instruments_by_symbol(symbol)
        if not matches:
            raise InstrumentNotFoundError(symbol)
        if len(matches) != 1:
            raise AmbiguousInstrumentError(
                f"symbol-only lookup is ambiguous for {str(symbol).upper()}"
            )
        return matches[0]

    def rename_symbol(
        self,
        internal_instrument_id: UUID | str,
        *,
        new_symbol: str,
        effective_at: datetime,
    ) -> Instrument:
        return self._store.rename_instrument_symbol(
            internal_instrument_id,
            new_symbol=new_symbol,
            effective_at=effective_at,
        )

    def record_mapping(self, mapping: BrokerInstrumentMapping) -> None:
        instrument = self.get(mapping.internal_instrument_id)
        if mapping.exact_match_symbol != instrument.canonical_symbol:
            raise InstrumentMappingError(
                "broker mapping does not match the current canonical symbol"
            )
        self._store.save_broker_mapping(mapping)

    def broker_mapping(
        self,
        internal_instrument_id: UUID | str,
        *,
        broker: str,
        environment: Environment | str,
    ) -> BrokerInstrumentMapping:
        mapping = self._store.get_broker_mapping(
            internal_instrument_id,
            broker=broker,
            environment=environment,
        )
        if mapping is None:
            raise InstrumentNotFoundError(
                f"no {broker}/{environment} mapping for {internal_instrument_id}"
            )
        return mapping
