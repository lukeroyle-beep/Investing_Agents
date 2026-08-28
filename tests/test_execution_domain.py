from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from execution.coordinator import (
    ExecutionConfig,
    ExecutionCoordinator,
    IntentImportError,
)
from execution.domain import (
    BrokerCommand,
    CommandState,
    InvalidLifecycleTransition,
    OrderIntent,
)
from execution.instruments import (
    AmbiguousInstrumentError,
    BrokerInstrumentMapping,
    Instrument,
    InstrumentCollisionError,
    InstrumentRegistry,
)
from execution.store import DuplicateIntentError, ExecutionStore


NOW = datetime(2026, 8, 28, 10, 15, tzinfo=timezone.utc)


def _intent(instrument: Instrument, **overrides) -> OrderIntent:
    values = {
        "strategy_id": "quality_v1",
        "run_id": "RUN_WP8",
        "internal_instrument_id": instrument.internal_instrument_id,
        "environment": "demo",
        "side": "buy",
        "order_type": "market",
        "sizing_method": "fixed_notional",
        "sizing_value": "100",
        "currency": "USD",
        "expires_at": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return OrderIntent.create(**values)


def _config(*, import_enabled: bool = True, writes: bool = False) -> ExecutionConfig:
    return ExecutionConfig(
        schema_version="1.0",
        intent_import_enabled=import_enabled,
        broker_writes_enabled=writes,
        adapter="offline",
        environment="demo",
        require_internal_instrument_id=True,
        allowed_asset_types=("equity", "etf"),
        allowed_sides=("buy",),
        allowed_leverage=Decimal("1"),
    )


def test_intent_hash_is_immutable_and_detects_duplicate_business_payload():
    instrument = Instrument.create(
        canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity", currency="USD"
    )
    first = _intent(instrument)
    second = _intent(instrument)

    assert first.intent_id != second.intent_id
    assert first.intent_hash == second.intent_hash
    with pytest.raises(FrozenInstanceError):
        first.currency = "GBP"


def test_lifecycle_rejects_invalid_and_cancel_fill_race_transitions():
    command = BrokerCommand.create(
        intent_hash="a" * 64,
        operation="submit_order",
        broker="etoro",
        environment="demo",
        broker_payload={"instrumentId": 1001},
    )
    with pytest.raises(InvalidLifecycleTransition):
        command.transition(CommandState.FILLED)

    command = command.transition("awaiting_approval").transition("approved")
    command = command.transition("submission_pending").transition("acknowledged")
    command = command.transition("partially_filled").transition("cancel_pending")
    assert command.transition("filled").state == CommandState.FILLED


def test_store_survives_restart_and_deduplicates_intents(tmp_path):
    path = tmp_path / "control" / "execution.sqlite3"
    first_store = ExecutionStore(path)
    registry = InstrumentRegistry(first_store)
    instrument = registry.register(
        Instrument.create(
            canonical_symbol="AAPL",
            exchange="NASDAQ",
            asset_type="equity",
            currency="USD",
        )
    )
    intent = _intent(instrument)
    first_store.save_intent(intent)
    command = BrokerCommand.create(
        intent_hash=intent.intent_hash,
        operation="submit_order",
        broker="etoro",
        environment="demo",
        broker_payload={"instrumentId": 1001},
    )
    first_store.save_command(command)
    first_store.transition_command(command.command_id, "awaiting_approval")

    second_store = ExecutionStore(path)
    assert second_store.get_intent(intent.intent_hash) == intent
    assert second_store.get_command(command.command_id).state == CommandState.AWAITING_APPROVAL
    with pytest.raises(DuplicateIntentError):
        second_store.save_intent(_intent(instrument))


def test_registry_prevents_collisions_and_preserves_uuid_through_ticker_change(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    nasdaq = registry.register(
        Instrument.create(
            canonical_symbol="ABC", exchange="NASDAQ", asset_type="equity", currency="USD"
        )
    )
    registry.register(
        Instrument.create(
            canonical_symbol="ABC", exchange="LSE", asset_type="equity", currency="GBP"
        )
    )
    with pytest.raises(AmbiguousInstrumentError):
        registry.resolve_symbol_only("ABC")
    with pytest.raises(InstrumentCollisionError):
        registry.register(
            Instrument.create(
                canonical_symbol="ABC",
                exchange="NASDAQ",
                asset_type="equity",
                currency="USD",
            )
        )

    renamed = registry.rename_symbol(
        nasdaq.internal_instrument_id,
        new_symbol="XYZ",
        effective_at=NOW,
    )
    assert renamed.internal_instrument_id == nasdaq.internal_instrument_id
    assert renamed.canonical_symbol == "XYZ"


def test_broker_mapping_is_exact_and_immutable(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(
        Instrument.create(
            canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity", currency="USD"
        )
    )
    mapping = BrokerInstrumentMapping.from_resolution(
        instrument=instrument,
        broker="etoro",
        environment="demo",
        broker_instrument_id=1001,
        exact_match_symbol="AAPL",
        metadata={"instrumentId": 1001},
        resolved_at=NOW,
    )
    registry.record_mapping(mapping)
    assert registry.broker_mapping(
        instrument.internal_instrument_id, broker="etoro", environment="demo"
    ) == mapping

    changed = BrokerInstrumentMapping.from_resolution(
        instrument=instrument,
        broker="etoro",
        environment="demo",
        broker_instrument_id=1002,
        exact_match_symbol="AAPL",
        metadata={"instrumentId": 1002},
        resolved_at=NOW,
    )
    with pytest.raises(InstrumentCollisionError):
        registry.record_mapping(changed)


def test_coordinator_rejects_ticker_only_rows_and_never_writes_economic_files(tmp_path):
    store = ExecutionStore(tmp_path / "runtime" / "control" / "execution.sqlite3")
    registry = InstrumentRegistry(store)
    instrument = registry.register(
        Instrument.create(
            canonical_symbol="AAPL", exchange="NASDAQ", asset_type="equity", currency="USD"
        )
    )
    coordinator = ExecutionCoordinator(store=store, registry=registry, config=_config())
    ticker_only = pd.DataFrame(
        [{
            "run_id": "RUN_WP8",
            "ticker": "AAPL",
            "direction": "long",
            "asset_type": "equity",
            "currency": "USD",
            "capital_allocated": "100",
        }]
    )
    with pytest.raises(IntentImportError, match="internal_instrument_id"):
        coordinator.import_advisory_orders(
            ticker_only, strategy_id="quality_v1", expires_at=NOW + timedelta(minutes=5)
        )

    enriched = ticker_only.assign(
        internal_instrument_id=str(instrument.internal_instrument_id)
    )
    intents = coordinator.import_advisory_orders(
        enriched, strategy_id="quality_v1", expires_at=NOW + timedelta(minutes=5)
    )
    assert len(intents) == 1
    assert not (tmp_path / "runtime" / "state").exists()
