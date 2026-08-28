from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from execution.domain import Approval, Environment, OrderIntent, RiskDecision, RiskOutcome, _aware_utc
from risk.approval import ApprovalAuthority, ApprovalError
from risk.kill_switch import KillSwitchStatus
from risk.pretrade import RiskLimits


class SubmissionBlockedError(RuntimeError):
    """Final write gate rejected stale, incomplete, or mismatched evidence."""


@dataclass(frozen=True, slots=True)
class SubmissionAuthorization:
    intent_hash: str
    risk_decision_id: str
    approval_id: str
    environment: Environment
    authorized_at: datetime


@dataclass(frozen=True, slots=True)
class GovernanceWritePolicy:
    execution_mode: str
    allow_broker_api: bool
    allow_order_submission: bool
    manual_signoff_required: bool

    @classmethod
    def load(cls, path: Path | str) -> "GovernanceWritePolicy":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise SubmissionBlockedError(
                "governance configuration is missing or malformed"
            ) from exc
        if not isinstance(raw, dict):
            raise SubmissionBlockedError("governance configuration must be a mapping")
        required = {
            "execution_mode",
            "allow_broker_api",
            "allow_order_submission",
            "manual_signoff_required",
        }
        if missing := sorted(required - set(raw)):
            raise SubmissionBlockedError(f"governance configuration missing: {missing}")
        return cls(
            execution_mode=str(raw["execution_mode"]).strip().lower(),
            allow_broker_api=raw["allow_broker_api"] is True,
            allow_order_submission=raw["allow_order_submission"] is True,
            manual_signoff_required=raw["manual_signoff_required"] is True,
        )

    @property
    def demo_writes_allowed(self) -> bool:
        return (
            self.execution_mode == "demo_manual"
            and self.allow_broker_api
            and self.allow_order_submission
            and self.manual_signoff_required
        )


class SubmissionGate:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self.approvals = ApprovalAuthority(limits)

    def authorize(
        self,
        *,
        intent: OrderIntent,
        decision: RiskDecision,
        approval: Approval,
        kill_switch: KillSwitchStatus,
        regular_session_open: bool,
        broker_writes_enabled: bool,
        governance: GovernanceWritePolicy,
        now: datetime,
    ) -> SubmissionAuthorization:
        moment = _aware_utc(now, "now")
        if not broker_writes_enabled:
            raise SubmissionBlockedError("broker writes are disabled by configuration")
        if not governance.demo_writes_allowed:
            raise SubmissionBlockedError("governance does not authorize Demo writes")
        if intent.environment != self.limits.environment:
            raise SubmissionBlockedError("intent environment is not authorized")
        if kill_switch.environment != intent.environment:
            raise SubmissionBlockedError("kill-switch environment mismatch")
        if not kill_switch.writes_allowed:
            raise SubmissionBlockedError("kill switch is engaged, missing, or corrupt")
        if not regular_session_open:
            raise SubmissionBlockedError("regular trading session is closed")
        if decision.intent_hash != intent.intent_hash:
            raise SubmissionBlockedError("risk decision does not bind to the intent")
        if decision.outcome != RiskOutcome.ACCEPTED or not all(
            check.passed for check in decision.checks
        ):
            raise SubmissionBlockedError("current independent risk decision is not accepted")
        decision_age = (moment - decision.decided_at).total_seconds()
        quote_age = (moment - decision.quote_observed_at).total_seconds()
        if not 0 <= decision_age <= self.limits.account_max_age_seconds:
            raise SubmissionBlockedError("risk/account evidence is stale at submission")
        if not 0 <= quote_age <= self.limits.quote_max_age_seconds:
            raise SubmissionBlockedError("broker quote is stale at submission")
        try:
            self.approvals.verify(approval=approval, intent=intent, now=moment)
        except ApprovalError as exc:
            raise SubmissionBlockedError(str(exc)) from exc
        limits = dict(approval.limits)
        if limits.get("risk_decision_id") != str(decision.decision_id):
            raise SubmissionBlockedError(
                "approval does not bind to the current risk decision"
            )
        return SubmissionAuthorization(
            intent_hash=intent.intent_hash,
            risk_decision_id=str(decision.decision_id),
            approval_id=str(approval.approval_id),
            environment=intent.environment,
            authorized_at=moment,
        )
