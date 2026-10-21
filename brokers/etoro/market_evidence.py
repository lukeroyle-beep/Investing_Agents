"""Bind pilot identity, broker configuration and scheduled hours without trading."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from brokers.base import BrokerDataUnavailable
from brokers.etoro.eligibility import EtoroInstrumentEligibility, validated_ids
from execution.domain import Environment, _aware_utc, payload_hash
from execution.instruments import Instrument
from execution.market_session import PilotSessionEvidence, assess_pilot_session


@dataclass(frozen=True, slots=True)
class EtoroPilotMarketEvidence:
    evidence_id: str
    broker_instrument_id: int
    security_metadata_checksum: str
    exchange_metadata_checksum: str
    eligibility_response_checksum: str
    eligibility: EtoroInstrumentEligibility
    session: PilotSessionEvidence
    capture_started_at: datetime
    capture_completed_at: datetime
    supported_pilot_configuration: bool
    submission_available: bool = field(default=False, init=False)
    qualification_credit: bool = field(default=False, init=False)
    limitations: tuple[str, ...] = (
        "configuration_receipt_time_is_not_broker_snapshot_time",
        "account_identity_and_risk_not_bound",
        "fresh_quote_and_market_halts_not_checked",
        "order_size_not_checked_against_broker_minimum_or_maximum",
        "live_operator_not_bound",
    )


def capture_pilot_market_evidence(client, instrument: Instrument, *,
                                  broker_instrument_id: int, now=None):
    """Capture diagnostics only. `now` freezes time for offline tests; live
    callers must omit it so duration and session-at-completion are measured.
    """
    if client.environment != Environment.DEMO or not client.read_only:
        raise BrokerDataUnavailable("pilot_market_capture_requires_demo_read_only_client")
    identifiers = validated_ids((broker_instrument_id,))
    started = _aware_utc(now or datetime.now(timezone.utc), "capture_started_at")
    preliminary = assess_pilot_session(instrument, now=started)
    if preliminary.opens_at is None and any(reason not in {"weekend", "full_holiday"} for reason in preliminary.reasons):
        raise BrokerDataUnavailable("unsupported_pilot_calendar_or_instrument")
    securities = client.get_security_metadata(identifiers)
    if len(securities) != 1:
        raise BrokerDataUnavailable("pilot_security_identity_mismatch")
    security = securities[0]
    if security.instrument_id != broker_instrument_id or security.symbol != instrument.canonical_symbol or security.instrument_type != "Stocks":
        raise BrokerDataUnavailable("pilot_security_identity_mismatch")
    exchanges = client.get_exchange_names((security.exchange_id,))
    if exchanges != {security.exchange_id: instrument.exchange}:
        raise BrokerDataUnavailable("pilot_exchange_identity_mismatch")
    response = client.get_demo_eligibility(identifiers)
    if len(response.items) != 1 or response.items[0].instrument_id != broker_instrument_id or response.items[0].symbol != instrument.canonical_symbol:
        raise BrokerDataUnavailable("pilot_eligibility_identity_mismatch")
    completed = _aware_utc(now or datetime.now(timezone.utc), "capture_completed_at")
    if not 0 <= (completed - started).total_seconds() <= 15:
        raise BrokerDataUnavailable("pilot_capture_timing_invalid")
    # Assess at completion so a read that crosses the close does not retain an
    # earlier open indication. No frozen caller boolean is accepted here.
    session = assess_pilot_session(instrument, now=completed)
    eligibility = response.items[0]
    supported = (eligibility.allow_open_position and eligibility.real_long_one_available
                 and eligibility.allowed_quantity_type in {"all", "amountOnly"}
                 and eligibility.trade_unit_type == "units")
    security_hash, exchange_hash = payload_hash(securities), payload_hash(exchanges)
    identity = payload_hash({
        "instrument": instrument, "broker_instrument_id": broker_instrument_id,
        "security": security_hash, "exchange": exchange_hash,
        "eligibility": response.evidence_id, "session": session,
        "capture_started_at": started, "capture_completed_at": completed,
    })
    return EtoroPilotMarketEvidence(identity, broker_instrument_id, security_hash,
        exchange_hash, response.evidence_id, eligibility, session, started, completed, supported)
