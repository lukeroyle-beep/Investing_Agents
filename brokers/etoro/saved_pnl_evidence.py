"""Pure replay of the retained, single-metadata-page Demo PnL bracket.

The caller supplies independently checked identity, original collection clocks,
decoded response payloads and the retained canonical evidence digest. Hash
agreement establishes consistency, not broker authenticity or identity provenance.
This module has no file, client, credential, baseline or execution capabilities.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
import re
from typing import Any, Mapping

from brokers.base import BrokerDataUnavailable
from brokers.etoro.account_evidence import (
    MAX_CAPTURE_SECONDS, AccountComponentTimeEvidence, ComponentCashObservation,
    EtoroPnlAccountEvidence, assess_pnl_component_time_evidence, validate_pnl_account_evidence,
    _pnl_expected_identity,
)
from brokers.etoro.pnl_currency_metadata import parse_demo_position_currencies
from brokers.etoro.schemas import (
    EtoroDetailedPortfolio, EtoroPnlPortfolio, EtoroSecuritiesPage,
    exact_datetime_epoch_seconds,
)
from execution.domain import payload_hash


@dataclass(frozen=True, slots=True)
class PnlReplayAssessment:
    """Private evidence assessment; never accepted by risk or baseline APIs."""

    evidence: EtoroPnlAccountEvidence
    assessed_at: datetime
    present_valuation_ages_seconds: tuple[Decimal, ...]
    present_valuation_freshness: tuple[bool, ...]
    source_consistency_verified: bool = field(default=True, init=False)
    risk_input_compatible: bool = field(default=False, init=False)
    baseline_input_compatible: bool = field(default=False, init=False)
    trading_readiness: str = field(default="BLOCKED", init=False)
    qualification_credit: bool = field(default=False, init=False)
    compatibility_blockers: tuple[str, ...] = field(default=(
        "whole_account_observation_time_unavailable",
        "full_daily_pnl_unavailable",
        "peak_equity_unavailable",
        "execution_and_cash_flow_history_not_complete",
        "baseline_invested_amount_contract_not_supported",
    ), init=False)

    @property
    def historical_restricted_capture_accepted(self) -> bool:
        return self.evidence.valuations_fresh

    @property
    def present_valuations_fresh(self) -> bool:
        return bool(self.present_valuation_freshness) and all(self.present_valuation_freshness)

    def assess_component_time(
        self, *, cash_observation: ComponentCashObservation | None = None,
        max_component_skew_seconds: Decimal = Decimal("15"),
    ) -> AccountComponentTimeEvidence:
        """Separate versioned assessment; original replay flags stay unchanged.

        Retains the original bracket and every original valuation timestamp.
        Supplying a recent cash observation cannot refresh stale position or
        structural evidence. This method grants no baseline/history authority.
        """
        return assess_pnl_component_time_evidence(
            self.evidence, assessed_at=self.assessed_at,
            cash_observation=cash_observation,
            max_component_skew_seconds=max_component_skew_seconds,
        )


def assess_saved_pnl_capture(
    *, opening_payload: Any, pnl_payload: Any, securities_payload: Any,
    currency_payload: Any, closing_payload: Any,
    expected_demo_customer_id: str,
    capture_started_at: datetime, capture_completed_at: datetime,
    saved_account_evidence: Mapping[str, Any], expected_evidence_sha256: str,
    assessed_at: datetime, account_max_age_seconds: int = MAX_CAPTURE_SECONDS,
) -> PnlReplayAssessment:
    """Rebuild evidence with original clocks, then assess marks at another time.

    No stored acceptance flag is consulted. Both the saved account object and
    its independently retained canonical digest must match the reconstructed
    evidence. The digest is *not* the SHA-256 of the pretty-printed JSON file.
    Detail/PnL/currency JSON must be decoded with ``parse_float=Decimal``, as
    in the online client: binary floats can lose both precision and scale.
    Only the exact five-response shape with one complete securities page is
    supported; pagination cannot trigger further reads. Schema/data errors
    propagate and no partially accepted result is returned.
    """
    if (not isinstance(expected_evidence_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_evidence_sha256) is None):
        raise BrokerDataUnavailable("saved_account_evidence_hash_required")
    if (not isinstance(assessed_at, datetime) or assessed_at.tzinfo is None
            or assessed_at.utcoffset() is None):
        raise BrokerDataUnavailable("saved_pnl_assessment_time_not_valid")
    assessed_at = assessed_at.astimezone(timezone.utc)
    expected = _pnl_expected_identity(expected_demo_customer_id)

    first = EtoroDetailedPortfolio.parse(opening_payload)
    pnl = EtoroPnlPortfolio.parse(pnl_payload)
    page = EtoroSecuritiesPage.parse(securities_payload)
    if page.has_next:
        raise BrokerDataUnavailable("saved_pnl_security_pagination_not_supported")
    # The online client sorts securities by ID after proving exact coverage.
    # The shared validator below proves coverage and rejects duplicates here.
    securities = tuple(sorted(page.securities, key=lambda item: item.instrument_id))
    currencies = parse_demo_position_currencies(
        currency_payload, expected, first.positions,
    )
    last = EtoroDetailedPortfolio.parse(closing_payload)
    evidence = validate_pnl_account_evidence(
        first=first, pnl=pnl, securities=securities, currencies=currencies,
        last=last, expected_demo_customer_id=expected,
        capture_started_at=capture_started_at, capture_completed_at=capture_completed_at,
        account_max_age_seconds=account_max_age_seconds,
    )
    if assessed_at < evidence.capture_completed_at:
        raise BrokerDataUnavailable("saved_pnl_assessment_time_not_valid")
    if (not isinstance(saved_account_evidence, Mapping)
            or payload_hash(evidence) != expected_evidence_sha256
            or payload_hash(saved_account_evidence) != expected_evidence_sha256):
        raise BrokerDataUnavailable("saved_account_evidence_hash_mismatch")

    epoch = exact_datetime_epoch_seconds(assessed_at)
    ages = tuple(epoch - item.timestamp_epoch_seconds for item in evidence.position_valuations)
    return PnlReplayAssessment(
        evidence=evidence, assessed_at=assessed_at,
        present_valuation_ages_seconds=ages,
        present_valuation_freshness=tuple(
            Decimal("0") <= age <= evidence.valuation_max_age_seconds for age in ages
        ),
    )
