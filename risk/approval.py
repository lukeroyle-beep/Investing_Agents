from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping
from uuid import uuid4

from execution.domain import (
    Approval,
    DomainValidationError,
    OrderIntent,
    RiskDecision,
    RiskOutcome,
    _aware_utc,
    immutable_pairs,
)
from risk.pretrade import RiskLimits


class ApprovalError(RuntimeError):
    """Approval is absent, stale, mismatched, or not explicitly human-issued."""


class ApprovalAuthority:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def issue(
        self,
        *,
        intent: OrderIntent,
        decision: RiskDecision,
        approver: str,
        now: datetime,
        human_confirmed: bool,
        approval_limits: Mapping[str, str] | None = None,
    ) -> Approval:
        moment = _aware_utc(now, "now")
        if not human_confirmed:
            raise ApprovalError("explicit human confirmation is required per order")
        if not str(approver).strip():
            raise ApprovalError("approver identity is required")
        if decision.intent_hash != intent.intent_hash:
            raise ApprovalError("risk decision does not bind to the current intent")
        if decision.outcome != RiskOutcome.ACCEPTED:
            raise ApprovalError("risk-rejected intent cannot be approved")
        if moment < decision.decided_at:
            raise ApprovalError("approval time precedes the risk decision")
        expires_at = min(
            intent.expires_at,
            moment + timedelta(seconds=self.limits.approval_max_age_seconds),
        )
        if expires_at <= moment:
            raise ApprovalError("intent has already expired")
        default_limits = {
            "risk_decision_id": str(decision.decision_id),
            "account_snapshot_id": decision.account_snapshot_id,
            "quote_id": decision.quote_id,
        }
        if approval_limits:
            default_limits.update(
                {str(key): str(value) for key, value in approval_limits.items()}
            )
        return Approval(
            approval_id=uuid4(),
            intent_hash=intent.intent_hash,
            approver=str(approver).strip(),
            environment=intent.environment,
            limits=immutable_pairs(default_limits),
            issued_at=moment,
            expires_at=expires_at,
        )

    @staticmethod
    def verify(
        *,
        approval: Approval,
        intent: OrderIntent,
        now: datetime,
    ) -> None:
        moment = _aware_utc(now, "now")
        if intent.compute_hash() != intent.intent_hash:
            raise ApprovalError("intent payload integrity check failed")
        if approval.intent_hash != intent.intent_hash:
            raise ApprovalError("intent change invalidated the approval")
        if approval.environment != intent.environment:
            raise ApprovalError("approval environment mismatch")
        if moment < approval.issued_at or moment >= approval.expires_at:
            raise ApprovalError("approval is stale or not yet valid")
        if moment >= intent.expires_at:
            raise ApprovalError("intent expired after approval")
