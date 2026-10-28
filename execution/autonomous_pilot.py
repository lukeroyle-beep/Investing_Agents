"""One standing Demo mandate, with existing pilot risk and economic ownership."""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4

from brokers.etoro.autonomous_api import DirectDemoAdapter, build_order_payload
from brokers.etoro.connector import instant, number
from execution.domain import CommandState, Environment, RiskCheck, RiskDecision, RiskOutcome, payload_hash
from execution.operator import DemoOperator, OperatorEvidenceError
from execution.pilot_operator import DemoPilotOperator
from execution.pilot_policy import POLICY, evaluate_pilot, preview_costs
from execution.pilot_mandate import PilotUsageWaiver, PilotBufferAmendment
from execution.usage_reserve import PilotUsageAdmission, MAX_USAGE_AGE_SECONDS
from risk.submission_gate import SubmissionBlockedError


class AutonomousPilotOperator(DemoPilotOperator):
    source_kind = "broker_demo_standing_pilot_v1"

    @staticmethod
    def _fixture_source(source_kind):
        if source_kind != AutonomousPilotOperator.source_kind:
            raise OperatorEvidenceError("standing_pilot_provenance_required")

    @staticmethod
    def _fixture_adapter(adapter):
        if (not isinstance(adapter, DirectDemoAdapter) or adapter.environment != Environment.DEMO
                or adapter.broker_name != "etoro"):
            raise OperatorEvidenceError("direct_demo_adapter_required")

    def prepare(self, *, intent, account, quote, assessment, now):
        DirectDemoAdapter._scope(intent)
        assessment.validate(now)
        if quote != assessment.evidence["quote"]:
            raise SubmissionBlockedError("assessment_exact_quote_mismatch")
        self.ledger.refresh_marks(account, now)
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        mapping = self.coordinator.registry.broker_mapping(intent.internal_instrument_id,
            broker="etoro", environment=Environment.DEMO)
        item = self.catalog.items.get(mapping.broker_instrument_id)
        if item is None or item.symbol != instrument.canonical_symbol or item.asset_type != instrument.asset_type:
            raise SubmissionBlockedError("pilot_registry_catalog_mismatch")
        payload = build_order_payload(mapping.broker_instrument_id, intent.sizing_value)
        if assessment.evidence["payload_hash"] != payload_hash(payload):
            raise SubmissionBlockedError("assessment_order_payload_mismatch")
        upfront, reserve = preview_costs(assessment.preview)
        check = evaluate_pilot(account=account, ledger=self.ledger.snapshot(), catalog=self.catalog,
            instrument_id=mapping.broker_instrument_id, amount=intent.sizing_value,
            expected_costs=reserve, now=now)
        if not check["eligible"]:
            raise SubmissionBlockedError("pilot_admission_rejected")
        if (quote.internal_instrument_id != intent.internal_instrument_id
                or not 0 <= (now-quote.observed_at).total_seconds() <= 30 or not quote.regular_session_open):
            raise SubmissionBlockedError("fresh_open_session_quote_required")
        risk = RiskDecision(uuid4(), intent.intent_hash, account.source_hash, quote.quote_id,
            quote.observed_at, (("policy", POLICY),),
            tuple(RiskCheck(k, v, str(v), "true") for k, v in check["checks"].items()),
            RiskOutcome.ACCEPTED, (), now)
        self.coordinator.prepare_intent(intent)
        command = self.coordinator.record_risk_decision(intent=intent, decision=risk,
            broker_payload=payload, broker="etoro")
        self.evidence.save_preparation(command.command_id, {"source_kind":self.source_kind,
            "prepared_at":now, "command_id":command.command_id, "intent":intent,
            "instrument":instrument, "mapping":mapping, "account":account, "quote":quote,
            "decision":risk, "broker_payload":payload, "pilot_admission":check,
            "expected_cost_reserve":reserve, "expected_cash_cost":upfront,
            "assessment":assessment.evidence, "assessment_hash":assessment.evidence_hash,
            "assessment_deadline":assessment.deadline,
            "classification_evidence":self.catalog.evidence, "qualification_credit":False})
        return command

    def submit_under_mandate(self, command_id, *, adapter, mandate, account, quote,
                             assessment, market_state, kill_switch, now, usage,
                             usage_amendment=None, usage_waiver=None, buffer_amendment=None):
        """Authorize from the human's standing mandate, never a fabricated click."""
        self._fixture_adapter(adapter)
        proof, command, intent, decision = self._bound(command_id)
        if command.state != CommandState.AWAITING_APPROVAL:
            raise SubmissionBlockedError("fresh_unclaimed_command_required")
        assessment.validate(now)
        if assessment.evidence.get("allow_unreported_buffer"):
            if type(buffer_amendment) is not PilotBufferAmendment:
                raise SubmissionBlockedError("bound_buffer_rule_amendment_required")
            buffer_amendment.validate(mandate=mandate, ledger=self.ledger, now=now)
        elif buffer_amendment is not None:
            raise SubmissionBlockedError("buffer_amendment_assessment_mode_mismatch")
        if (assessment.evidence_hash != proof["assessment_hash"]
                or quote != assessment.evidence["quote"]
                or account.source_hash != proof["account"]["source_hash"]
                or quote.quote_id != decision.quote_id):
            raise SubmissionBlockedError("standing_dynamic_evidence_changed")
        if not self.coordinator.config.broker_writes_enabled:
            raise SubmissionBlockedError("pilot_writes_disabled")
        if usage_waiver is not None:
            if type(usage_waiver) is not PilotUsageWaiver:
                raise SubmissionBlockedError("typed_pilot_usage_waiver_required")
            usage_waiver.validate(mandate=mandate, ledger=self.ledger, now=now)
        elif type(usage) is not PilotUsageAdmission or usage.validate_for_submission(now):
            raise SubmissionBlockedError("fresh_usage_floor_and_recovery_reserve_required")
        if account.account_id != adapter.expected_account_id:
            raise SubmissionBlockedError("same_direct_demo_account_required")
        self.catalog.validate()
        if self.catalog.source_hash != proof["pilot_admission"]["catalog_hash"]:
            raise SubmissionBlockedError("pilot_catalog_changed")
        self.ledger.refresh_marks(account, now)
        reserve = number(proof["expected_cost_reserve"], "expected_cost_reserve")
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        mapping = self.coordinator.registry.broker_mapping(intent.internal_instrument_id,
            broker="etoro", environment=Environment.DEMO)
        check = evaluate_pilot(account=account, ledger=self.ledger.snapshot(), catalog=self.catalog,
            instrument_id=mapping.broker_instrument_id, amount=intent.sizing_value,
            expected_costs=reserve, now=now)
        if not check["eligible"]:
            raise SubmissionBlockedError("refreshed_pilot_policy_rejected")
        if (quote.internal_instrument_id != intent.internal_instrument_id
                or market_state.get("instrument_id") != mapping.broker_instrument_id
                or market_state.get("quote_type") != "realtime" or market_state.get("quote_id") != quote.quote_id
                or not all(market_state.get(x) is True for x in
                    ("isCurrentlyTradable", "isExchangeOpen", "isActiveInPlatform", "isBuyEnabled"))
                or market_state.get("isDelisted") is not False or not quote.regular_session_open
                or not 0 <= (now-instant(market_state.get("received_at"), "market_time")).total_seconds() <= 15
                or not 0 <= (now-quote.observed_at).total_seconds() <= 30):
            raise SubmissionBlockedError("fresh_open_tradable_realtime_market_required")
        approval = mandate.issue_approval(intent=intent, decision=decision, account=account,
            instrument=instrument, mapping=mapping, ledger=self.ledger, kill_switch=kill_switch,
            limits=self.limits, now=now, usage=usage, usage_amendment=usage_amendment,
            usage_waiver=usage_waiver,
            order_payload=build_order_payload(mapping.broker_instrument_id, intent.sizing_value),
            approval_limits={"assessment_hash":assessment.evidence_hash,
                "buffer_amendment_hash":buffer_amendment.amendment_hash if buffer_amendment else "",
                "buffer_amendment_source_reference":buffer_amendment.source_reference if buffer_amendment else ""})
        next_day = (now.astimezone(ZoneInfo("Europe/London"))+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        deadlines = [next_day, intent.expires_at, approval.expires_at, account.deadline,
            assessment.deadline, quote.observed_at+timedelta(seconds=30),
            instant(market_state["received_at"], "market_time")+timedelta(seconds=15)]
        if usage_waiver is None:
            deadlines.append(usage.observed_at+timedelta(seconds=MAX_USAGE_AGE_SECONDS))
        deadline = min(deadlines)
        if deadline <= now:
            raise SubmissionBlockedError("pilot_evidence_expired")
        adapter.bind(command, intent=intent, instrument_id=mapping.broker_instrument_id,
            authorization_hash=mandate.mandate_hash, expires_at=deadline,
            evidence={"assessment_hash":assessment.evidence_hash, "mandate_id":mandate.mandate_id})
        self.coordinator.record_approval(command=command, approval=approval, now=now)
        self.evidence.save_approval(command_id, approval)
        command = self.coordinator.store.get_command(command_id)
        self.evidence.append_observation(command_id, {"source_kind":self.source_kind,
            "pre_submit_account":account, "market_state":market_state, "quote":quote,
            "usage":usage, "pilot_admission":check, "observed_at":now,
            "mandate_hash":mandate.mandate_hash, "qualification_credit":False})
        adapter.bindings.authorize(command_id, {"authorized_at":now, "valid_until":deadline,
            "approval_id":str(approval.approval_id), "authorization_hash":mandate.mandate_hash,
            "account_snapshot_id":account.source_hash, "risk_decision_id":str(decision.decision_id),
            "quote_id":quote.quote_id})
        # Re-read the shared switch and immutable mandate immediately before
        # consuming the sole account/day slot. A crash can only leave a hold.
        mandate.validate(ledger=self.ledger, kill_switch=kill_switch, now=now)
        if usage_waiver is not None:
            usage_waiver.validate(mandate=mandate, ledger=self.ledger, now=now)
        if buffer_amendment is not None:
            buffer_amendment.validate(mandate=mandate, ledger=self.ledger, now=now)
        self.ledger.reserve_and_claim(str(command_id), intent.sizing_value, reserve, account, now,
            intent_hash=intent.intent_hash, instrument_id=mapping.broker_instrument_id)
        pending = self.coordinator.store.authorize_submission(command, intent, decision, approval,
            authorized_at=now, valid_until=deadline)
        result = self.coordinator.submit_pending_command(command=pending, intent=intent, adapter=adapter, now=now)
        self.ledger.record_outcome(str(command_id), {"state":str(result.state),
            "order_id":result.broker_order_id, "reference_id":result.broker_reference_id})
        return result

    def _validate_lookup(self, proof, command, intent, result, now):
        if any(not item.executed_at_source for item in result.executions):
            raise OperatorEvidenceError("original_execution_source_timestamp_required")
        return DemoOperator._validate_lookup(self, proof, command, intent, result, now)

    def reconcile_pilot(self, command_id, **kwargs):
        # A process can die after the durable send claim but before recording
        # its outcome. Never resend; normalize that crash state for lookup.
        _, command, _, _ = self._bound(command_id)
        if command.state == CommandState.SUBMISSION_PENDING:
            if not any(x.outcome == "send_started" and x.request_id == command.logical_request_id
                       for x in command.attempt_history):
                raise SubmissionBlockedError("claimed_without_send_attempt_manual_recovery_required")
            self.coordinator.store.transition_command(command_id, CommandState.UNKNOWN,
                details={"reason":"interrupted_direct_dispatch_lookup_only"},
                broker_reference_id=str(command.logical_request_id))
        return super().reconcile_pilot(command_id, **kwargs)

    def approve(self, *args, **kwargs):
        raise SubmissionBlockedError("use_standing_mandate_authorization")

    def bind_preview(self, *args, **kwargs):
        raise SubmissionBlockedError("direct_api_cost_assessment_required")

    def submit(self, *args, **kwargs):
        raise SubmissionBlockedError("use_submit_under_mandate")
