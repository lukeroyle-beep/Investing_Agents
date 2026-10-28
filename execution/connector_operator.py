"""Manual Demo connector lifecycle with refreshed risk and exact token approval.

This module stages broker evidence only. Fill Agent retains economic ownership.
The 30-session/20-cycle qualification threshold is intentionally not a gate to
one human-approved manual cycle.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from brokers.etoro.connector import EtoroConnectorAdapter, instant, number
from execution.domain import CommandState, Environment, RiskOutcome, _aware_utc, canonical_json
from execution.operator import DemoOperator, OperatorEvidenceError
from execution.usage_reserve import MAX_USAGE_AGE_SECONDS, UsageAdmission
from risk.approval import ApprovalAuthority, ApprovalError
from risk.submission_gate import SubmissionBlockedError


class ConnectorDemoOperator(DemoOperator):
    broker_name = "etoro"
    source_kind = "broker_demo_connector"

    @staticmethod
    def _fixture_source(source_kind):
        if source_kind != "broker_demo_connector":
            raise OperatorEvidenceError("connector_broker_provenance_required")

    @staticmethod
    def _fixture_adapter(adapter):
        if (not isinstance(adapter, EtoroConnectorAdapter) or adapter.environment != Environment.DEMO
                or adapter.broker_name != "etoro"):
            raise OperatorEvidenceError("supported_demo_connector_adapter_required")

    def prepare(self, *, intent, account, quote, source_kind, now):
        if account.contract_version != "account-risk-v2" or not account.account_id:
            raise OperatorEvidenceError("versioned_live_account_evidence_required")
        self._validate_account_mapping(account)
        return super().prepare(intent=intent, account=account, quote=quote, source_kind=source_kind, now=now)

    def _validate_account_mapping(self, account):
        for position in account.positions:
            instrument = self.coordinator.registry.get(position.internal_instrument_id)
            mapping = self.coordinator.registry.broker_mapping(position.internal_instrument_id,
                broker=self.broker_name, environment=Environment.DEMO)
            if (position.broker_instrument_id != mapping.broker_instrument_id
                    or position.sector != instrument.sector):
                raise OperatorEvidenceError("account_position_mapping_or_sector_mismatch")
        for pending in account.pending_orders:
            instrument = self.coordinator.registry.get(pending.internal_instrument_id)
            self.coordinator.registry.broker_mapping(pending.internal_instrument_id,
                broker=self.broker_name, environment=Environment.DEMO)
            if pending.sector != instrument.sector:
                raise OperatorEvidenceError("account_pending_sector_mismatch")

    def bind_preview(self, command_id, *, adapter, preview, eligibility, now):
        self._fixture_adapter(adapter)
        proof, command, intent, decision = self._bound(command_id)
        if command.state != CommandState.AWAITING_APPROVAL or decision.outcome != RiskOutcome.ACCEPTED:
            raise OperatorEvidenceError("accepted_risk_required_before_trade_preparation")
        if proof["account"]["account_id"] != adapter.expected_account_id:
            raise OperatorEvidenceError("connector_account_identity_mismatch")
        return adapter.bind_preview(command, intent=intent,
            instrument_id=proof["mapping"]["broker_instrument_id"], preview=preview,
            eligibility=eligibility, now=now)

    def approve(self, command_id, *, adapter, confirmation_hash, approver, human_confirmed, now):
        if human_confirmed is not True:
            raise ApprovalError("explicit human confirmation is required per order")
        self._fixture_adapter(adapter)
        _, command, intent, decision = self._bound(command_id)
        bound = adapter.validate_binding(command, intent, now)
        if confirmation_hash != bound["confirmation_hash"]:
            raise ApprovalError("approval_must_confirm_exact_preview_and_warnings")
        if command.state == CommandState.APPROVED:
            approval = self.coordinator.store.get_command_approval(command_id)
            if (approval is None or approval.approver != approver
                    or dict(approval.limits).get("connector_token_hash") != bound["token_hash"]):
                raise ApprovalError("original_connector_approval_required")
            ApprovalAuthority.verify(approval=approval, intent=intent, now=now)
        else:
            approval = ApprovalAuthority(self.limits).issue(
                intent=intent, decision=decision, approver=approver,
                human_confirmed=human_confirmed, now=now,
                approval_limits={"connector_token_hash": bound["token_hash"],
                                 "connector_confirmation_hash": bound["confirmation_hash"],
                                 "connector_preview_expires_at": bound["expires_at"]})
            approval = replace(approval, expires_at=min(approval.expires_at, instant(bound["expires_at"], "preview_expiry")))
            self.coordinator.record_approval(command=command, approval=approval, now=now)
        self.evidence.save_approval(command_id, approval)
        return approval

    def submit(self, command_id, *, adapter, account, quote, market_state,
               kill_switch, governance, now, usage=None, regular_session_open=None):
        self._fixture_adapter(adapter)
        proof, command, intent, original_decision = self._bound(command_id)
        moment = _aware_utc(now, "now")
        bound = adapter.validate_binding(command, intent, moment)
        approval = self.coordinator.store.get_command_approval(command_id)
        if (approval is None or canonical_json(approval) != canonical_json(self.evidence.read_approval(command_id))
                or dict(approval.limits).get("connector_token_hash") != bound["token_hash"]
                or dict(approval.limits).get("connector_confirmation_hash") != bound["confirmation_hash"]):
            raise ApprovalError("persisted_exact_connector_approval_required")
        ApprovalAuthority.verify(approval=approval, intent=intent, now=moment)
        if (command.state != CommandState.APPROVED or not self.coordinator.config.broker_writes_enabled
                or not governance.demo_writes_allowed or not kill_switch.writes_allowed
                or kill_switch.environment != Environment.DEMO):
            raise SubmissionBlockedError("connector_submission_governance_or_lifecycle_blocked")
        # A scoped pilot allowance must never relax the strict connector policy.
        if type(usage) is not UsageAdmission or usage.validate_for_submission(moment):
            raise SubmissionBlockedError("fresh_usage_floor_and_recovery_reserve_required")
        if (account.contract_version != "account-risk-v2" or account.account_id != adapter.expected_account_id
                or account.account_id != proof["account"]["account_id"]):
            raise SubmissionBlockedError("fresh_same_demo_account_required")
        self._validate_account_mapping(account)
        if account.estimated_order_costs < number(bound["confirmation"]["totalUpfrontCost"], "upfront_cost"):
            raise SubmissionBlockedError("risk_costs_do_not_cover_preview")
        # This evidence comes from the official instrument-state/rates routes,
        # independently of a calendar or caller-supplied session boolean.
        if (market_state.get("instrument_id") != bound["instrument_id"]
                or market_state.get("quote_type") != "realtime"
                or market_state.get("quote_id") != quote.quote_id
                or not all(market_state.get(x) is True for x in
                           ("isOpen", "isCurrentlyTradable", "isExchangeOpen", "isActiveInPlatform", "isBuyEnabled"))
                or market_state.get("isDelisted") is not False
                or not quote.regular_session_open
                or not 0 <= (moment - instant(market_state.get("received_at"), "market_state_time")).total_seconds() <= 15):
            raise SubmissionBlockedError("fresh_open_tradable_realtime_market_required")
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        fresh_decision = self.risk.evaluate(intent=intent, instrument=instrument, account=account, quote=quote, now=moment)
        if fresh_decision.outcome != RiskOutcome.ACCEPTED or not all(x.passed for x in fresh_decision.checks):
            raise SubmissionBlockedError("refreshed_account_wide_risk_rejected")
        # Revalidation is supplemental: never replace, re-date, or issue a new
        # human approval for the original exact order/preview.
        self.evidence.append_observation(command_id, {
            "source_kind": self.source_kind, "observed_at": moment,
            "pre_submit_refresh": {"account": account, "quote": quote,
                                   "market": market_state, "decision": fresh_decision, "usage": usage},
            "approval_id": str(approval.approval_id), "qualification_credit": False})
        account_deadline = (account.observed_at + timedelta(seconds=self.limits.account_max_age_seconds)
                            if account.observed_at is not None else
                            account.component_time.freshness_deadline(self.limits.account_max_age_seconds))
        deadline = min(intent.expires_at, approval.expires_at,
                       instant(bound["expires_at"], "preview_expiry"), account_deadline,
                       quote.observed_at + timedelta(seconds=self.limits.quote_max_age_seconds),
                       instant(market_state["received_at"], "market_state_time") + timedelta(seconds=15),
                       usage.observed_at + timedelta(seconds=MAX_USAGE_AGE_SECONDS))
        adapter.bindings.authorize(command_id, {
            "authorized_at": moment, "valid_until": deadline,
            "approval_id": str(approval.approval_id), "account_snapshot_id": account.snapshot_id,
            "risk_decision_id": str(fresh_decision.decision_id), "quote_id": quote.quote_id})
        pending = self.coordinator.store.authorize_submission(
            command, intent, original_decision, approval, authorized_at=moment,
            valid_until=deadline)
        return self.coordinator.submit_pending_command(command=pending, intent=intent, adapter=adapter, now=moment)

    def _validate_lookup(self, proof, command, intent, result, now):
        if any(not item.executed_at_source for item in result.executions):
            raise OperatorEvidenceError("original_connector_execution_source_timestamp_required")
        # An acknowledged connector response may have been durably captured
        # before a crash prevented its coordinator transition. The adapter
        # supplies only its verified stored broker reference, never the local
        # command UUID. This view changes no persisted lifecycle evidence.
        reference = dict(result.details).get("connector_broker_reference_id")
        if result.executions and not reference:
            raise OperatorEvidenceError("verified_connector_reference_required")
        if reference:
            if command.broker_reference_id and command.broker_reference_id != reference:
                raise OperatorEvidenceError("connector_reference_conflict")
            command = replace(command, broker_reference_id=reference)
        return super()._validate_lookup(proof, command, intent, result, now)
