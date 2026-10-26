"""Human-approved demo-pilot-v1 using the existing once-only connector path."""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo
from uuid import uuid4

from brokers.etoro.connector import instant, number, response_object, _sanitized
from execution.connector_operator import ConnectorDemoOperator
from execution.domain import CommandState, Environment, RiskCheck, RiskDecision, RiskOutcome, canonical_json, payload_hash
from execution.pilot_policy import POLICY, PilotPolicyError, evaluate_pilot, preview_costs
from execution.usage_reserve import UsageAdmission, MAX_USAGE_AGE_SECONDS
from risk.approval import ApprovalAuthority, ApprovalError
from risk.submission_gate import SubmissionBlockedError


class DemoPilotOperator(ConnectorDemoOperator):
    source_kind = "broker_demo_pilot_v1"

    def __init__(self, *, ledger, catalog, **kwargs):
        super().__init__(**kwargs)
        self.ledger, self.catalog = ledger, catalog

    @staticmethod
    def _fixture_source(source_kind):
        if source_kind != "broker_demo_pilot_v1":
            raise PilotPolicyError("pilot_connector_provenance_required")

    def prepare(self, *, intent, account, quote, preview, now):
        # Validate before creating any approval-bearing command.
        from brokers.etoro.connector import EtoroConnectorAdapter
        EtoroConnectorAdapter._scope(intent)
        self.ledger.refresh_marks(account, now)
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        mapping = self.coordinator.registry.broker_mapping(intent.internal_instrument_id,
            broker="etoro", environment=Environment.DEMO)
        item = self.catalog.items.get(mapping.broker_instrument_id)
        if item is None or item.symbol != instrument.canonical_symbol or item.asset_type != instrument.asset_type:
            raise PilotPolicyError("pilot_registry_catalog_mismatch")
        upfront, reserve = preview_costs(preview)
        decision = evaluate_pilot(account=account, ledger=self.ledger.snapshot(), catalog=self.catalog,
            instrument_id=mapping.broker_instrument_id, amount=intent.sizing_value, expected_costs=reserve, now=now)
        if not decision["eligible"]:
            raise PilotPolicyError("pilot_admission_rejected:" + ",".join(k for k,v in decision["checks"].items() if not v))
        if (quote.internal_instrument_id != intent.internal_instrument_id
                or not 0 <= (now-quote.observed_at).total_seconds() <= 30 or not quote.regular_session_open):
            raise PilotPolicyError("fresh_open_session_quote_required")
        risk = RiskDecision(uuid4(), intent.intent_hash, account.source_hash, quote.quote_id,
            quote.observed_at, (("policy", POLICY),),
            tuple(RiskCheck(k, v, str(v), "true") for k,v in decision["checks"].items()),
            RiskOutcome.ACCEPTED, (), now)
        payload = {"action":"open", "transaction":"buy", "instrumentId":mapping.broker_instrument_id,
            "settlementType":"real", "orderType":"mkt", "leverage":1,
            "amount":str(intent.sizing_value), "orderCurrency":"usd"}
        self.coordinator.prepare_intent(intent)
        command = self.coordinator.record_risk_decision(intent=intent, decision=risk,
            broker_payload=payload, broker="etoro")
        self.evidence.save_preparation(command.command_id, {"source_kind":self.source_kind,
            "prepared_at":now, "command_id":command.command_id, "intent":intent,
            "instrument":instrument, "mapping":mapping, "account":account, "quote":quote,
            "decision":risk, "broker_payload":payload, "pilot_admission":decision,
            "expected_cost_reserve":reserve, "expected_cash_cost":upfront,
            "preview_confirmation_hash":payload_hash({"confirmation":_sanitized(response_object(preview).get("confirmation", {})),
                "warnings":_sanitized(response_object(preview).get("warnings", []))}),
            "classification_evidence":self.catalog.evidence, "qualification_credit":False})
        return command

    def bind_preview(self, command_id, *, adapter, preview, eligibility, now):
        proof, _, _, _ = self._bound(command_id)
        raw = response_object(preview)
        actual = payload_hash({"confirmation":_sanitized(raw.get("confirmation", {})),
                               "warnings":_sanitized(raw.get("warnings", []))})
        if actual != proof["preview_confirmation_hash"]:
            raise PilotPolicyError("pilot_prepared_preview_changed")
        return super().bind_preview(command_id, adapter=adapter, preview=preview, eligibility=eligibility, now=now)

    def submit(self, command_id, *, adapter, account, quote, market_state,
               kill_switch, governance, now, usage=None, regular_session_open=None):
        self._fixture_adapter(adapter)
        proof, command, intent, decision = self._bound(command_id)
        bound = adapter.validate_binding(command, intent, now)
        if bound["confirmation_hash"] != proof["preview_confirmation_hash"]:
            raise SubmissionBlockedError("pilot_bound_preview_changed")
        self.catalog.validate()
        if self.catalog.source_hash != proof["pilot_admission"]["catalog_hash"]:
            raise SubmissionBlockedError("pilot_catalog_changed_new_preparation_required")
        approval = self.coordinator.store.get_command_approval(command_id)
        if (approval is None or canonical_json(approval) != canonical_json(self.evidence.read_approval(command_id))
                or dict(approval.limits).get("connector_token_hash") != bound["token_hash"]
                or dict(approval.limits).get("connector_confirmation_hash") != bound["confirmation_hash"]):
            raise ApprovalError("persisted_exact_connector_approval_required")
        ApprovalAuthority.verify(approval=approval, intent=intent, now=now)
        if (command.state != CommandState.APPROVED or not self.coordinator.config.broker_writes_enabled
                or not governance.demo_writes_allowed or not kill_switch.writes_allowed
                or kill_switch.environment != Environment.DEMO):
            raise SubmissionBlockedError("pilot_submission_governance_or_lifecycle_blocked")
        if not isinstance(usage, UsageAdmission) or usage.validate_for_submission(now):
            raise SubmissionBlockedError("fresh_usage_floor_and_recovery_reserve_required")
        if account.account_id != adapter.expected_account_id or account.account_id != proof["account"]["account_id"]:
            raise SubmissionBlockedError("fresh_same_demo_account_required")
        # Re-run the source parser, account caps, persisted P&L and issuer check.
        self.ledger.refresh_marks(account, now)
        reserve = number(proof["expected_cost_reserve"], "expected_cost_reserve")
        check = evaluate_pilot(account=account, ledger=self.ledger.snapshot(), catalog=self.catalog,
            instrument_id=bound["instrument_id"], amount=intent.sizing_value, expected_costs=reserve, now=now)
        if not check["eligible"]:
            raise SubmissionBlockedError("refreshed_pilot_policy_rejected")
        if (quote.internal_instrument_id != intent.internal_instrument_id
                or market_state.get("instrument_id") != bound["instrument_id"]
                or market_state.get("quote_type") != "realtime" or market_state.get("quote_id") != quote.quote_id
                or not all(market_state.get(x) is True for x in
                    ("isCurrentlyTradable","isExchangeOpen","isActiveInPlatform","isBuyEnabled"))
                or market_state.get("isDelisted") is not False or not quote.regular_session_open
                or not 0 <= (now-instant(market_state.get("received_at"), "market_time")).total_seconds() <= 15
                or not 0 <= (now-quote.observed_at).total_seconds() <= 30):
            raise SubmissionBlockedError("fresh_open_tradable_realtime_market_required")
        # Bound all freshness clocks; the adapter rechecks after its identity read.
        next_day = (now.astimezone(ZoneInfo("Europe/London"))+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        deadline = min(next_day, intent.expires_at, approval.expires_at, instant(bound["expires_at"], "preview_expiry"),
            account.deadline, quote.observed_at+timedelta(seconds=30),
            instant(market_state["received_at"], "market_time")+timedelta(seconds=15),
            usage.observed_at+timedelta(seconds=MAX_USAGE_AGE_SECONDS))
        if deadline <= now:
            raise SubmissionBlockedError("pilot_evidence_expired")
        self.evidence.append_observation(command_id, {"source_kind":self.source_kind,
            "pre_submit_account":account, "market_state":market_state, "quote":quote,
            "usage":usage, "pilot_admission":check, "observed_at":now, "qualification_credit":False})
        adapter.bindings.authorize(command_id, {"authorized_at":now, "valid_until":deadline,
            "approval_id":str(approval.approval_id), "account_snapshot_id":account.source_hash,
            "risk_decision_id":str(decision.decision_id), "quote_id":quote.quote_id})
        # Account/day claim precedes the existing per-command claim. Any crash
        # between them leaves a conservative durable recovery obligation.
        self.ledger.reserve_and_claim(str(command_id), intent.sizing_value, reserve, account, now,
            intent_hash=intent.intent_hash, instrument_id=bound["instrument_id"])
        pending = self.coordinator.store.authorize_submission(command, intent, decision, approval,
            authorized_at=now, valid_until=deadline)
        result = self.coordinator.submit_pending_command(command=pending, intent=intent, adapter=adapter, now=now)
        self.ledger.record_outcome(str(command_id), {"state":str(result.state),
            "order_id":result.broker_order_id, "reference_id":result.broker_reference_id})
        return result

    def reconcile_pilot(self, command_id, *, adapter, pre_account, post_account, now):
        """Lookup is the only recovery route. A terminal status cannot release a hold."""
        proof, command, intent, _ = self._bound(command_id)
        self._fixture_adapter(adapter)
        adapter.verify_demo_identity()
        result = adapter.reconcile_command(command)
        self._validate_lookup(proof, command, intent, result, now)
        self.evidence.append_observation(command_id, {"source_kind":self.source_kind,
            "observed_at":now, "result":result, "qualification_credit":False})
        if not result.reconciled or result.lifecycle_state != "filled" or not result.executions:
            self.ledger.record_outcome(str(command_id), {"lookup_status":result.lifecycle_state, "terminal":result.reconciled})
            raise SubmissionBlockedError("confirmed_full_fill_and_whole_account_reconciliation_required")
        snapshot = self.ledger.reconcile_open(str(command_id), result.executions, pre_account, post_account, now)
        # Economic commit first. A crash afterwards is an idempotent ledger
        # replay plus the unfinished operational transition, never another send.
        if command.state == CommandState.UNKNOWN:
            command = self.coordinator.store.transition_command(command_id, CommandState.RECONCILING,
                details={"source_kind":self.source_kind})
        if command.state not in {CommandState.FILLED, CommandState.RECONCILED}:
            command = self.coordinator.store.transition_command(command_id, CommandState.FILLED,
                details={"source_kind":self.source_kind}, broker_order_id=result.broker_order_id)
        if command.state != CommandState.RECONCILED:
            self.coordinator.store.transition_command(command_id, CommandState.RECONCILED,
                details={"economic_reconciliation_verified":True, "qualification_credit":False,
                         "post_account_source_hash":post_account.source_hash})
        return snapshot
