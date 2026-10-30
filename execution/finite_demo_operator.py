"""Finite direct Demo orders sharing the original economic ledger.

Authority is an immutable, explicitly issued mandate, never a simulated human
click. Each candidate is independently screened and journalled. A prior unknown
outcome, unfinished economic reconciliation or journal blocks the next claim.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from brokers.etoro.account_evidence import _exact_sum as total, _difference as subtract
from brokers.etoro.autonomous_api import build_order_payload
from brokers.etoro.batch_reads import validate_market_evidence
from brokers.etoro.connector import instant, number
from execution.autonomous_pilot import AutonomousPilotOperator
from execution.batch_assessment import BatchOrderAssessment, validated_instrument_spec
from execution.domain import (Approval, CommandState, Environment, RiskCheck, RiskDecision,
                              RiskOutcome, canonical_json, immutable_pairs, payload_hash)
from execution.finite_demo_mandate import FiniteDemoMandate, FiniteOrderCandidate, POLICY
from execution.operator import OperatorEvidenceError
from execution.pilot_policy import multiply, preview_costs
from execution.usage_reserve import (
    PilotUsageAdmission, MAX_USAGE_AGE_SECONDS, MINIMUM_FLOOR_PERCENT,
    MINIMUM_RECOVERY_RESERVE_PERCENT,
)
from risk.approval import ApprovalAuthority
from risk.submission_gate import SubmissionBlockedError


class FiniteDemoOperator(AutonomousPilotOperator):
    source_kind = "finite_demo_v2"
    terminal_lookup_states = AutonomousPilotOperator.terminal_lookup_states | {"partially_filled_terminal"}
    filled_lookup_states = AutonomousPilotOperator.filled_lookup_states | {"partially_filled_terminal"}

    def __init__(self, *, journal, reviewed_commit, **kwargs):
        super().__init__(**kwargs)
        self.journal, self.reviewed_commit = journal, reviewed_commit

    @staticmethod
    def _fixture_source(source_kind):
        if source_kind != FiniteDemoOperator.source_kind:
            raise OperatorEvidenceError("finite_demo_provenance_required")

    @staticmethod
    def _fixture_adapter(adapter):
        from brokers.etoro.batch_api import BatchDemoAdapter
        if not isinstance(adapter, BatchDemoAdapter) or adapter.environment != Environment.DEMO:
            raise OperatorEvidenceError("finite_direct_demo_adapter_required")

    def _admission(self, *, mandate, candidate, account, now):
        if type(mandate) is not FiniteDemoMandate:
            raise SubmissionBlockedError("typed_finite_mandate_required")
        state = mandate.validate_binding(ledger=self.ledger, now=now, reviewed_commit=self.reviewed_commit)
        spec = mandate.validate_candidate(candidate)
        account.validate_fresh(now)
        # PilotAccount's base policy allows 30s. The finite worker retains the
        # narrower 15s gate across the original profile, capture and P&L clocks.
        if now >= account.deadline - timedelta(seconds=15):
            raise SubmissionBlockedError("finite_account_evidence_older_than_15_seconds")
        if account.account_id != state["account_id"]:
            raise SubmissionBlockedError("finite_same_demo_account_required")
        self.catalog.validate()
        item = self.catalog.items.get(candidate.instrument_id)
        if (item is None or (item.symbol, item.asset_type, item.issuer, item.sector) !=
                (spec["symbol"], spec["asset_type"], spec["issuer"], spec["sector"])):
            raise SubmissionBlockedError("finite_reviewed_catalog_scope_mismatch")
        issuers, _ = self.catalog.exposures(account)
        scope = mandate.scope
        commitment = total((candidate.amount, candidate.expected_costs))
        attempts = [row for row in state["submissions"] if row.get("mandate_hash") == mandate.mandate_hash]
        attempted = total(total((number(row["amount"], "amount"),
                                 number(row["expected_costs"], "expected_costs"))) for row in attempts)
        day = now.astimezone(ZoneInfo("Europe/London")).date().isoformat()
        checks = {
            "order_cap": commitment <= Decimal(scope["per_order_cap"]),
            "capital_cap": total((state["committed_capital"], commitment)) <= Decimal(scope["total_commitment"]),
            "attempt_commitment": total((attempted, commitment)) <= Decimal(scope["total_commitment"]),
            "position_cap": state["position_count"] < scope["max_positions"],
            "cash_buffer": subtract(account.cash, commitment) >= multiply(account.equity, Decimal(scope["min_cash_fraction"])),
            "issuer_cap": total((issuers.get(item.issuer, Decimal(0)), commitment)) <= multiply(account.equity, Decimal(".05")),
            "loss_stop": not state["loss_stopped"] and state["pilot_pnl"] > -Decimal(scope["loss_stop"]),
            "reconciled": not state["unresolved"],
            "submission_budget": len(attempts) < scope["max_submissions"],
            "daily_submission": sum(row["london_day"] == day for row in state["submissions"]) < scope["max_per_day"],
        }
        result = {"policy": POLICY, "eligible": all(checks.values()), "checks": checks,
            "catalog_hash": self.catalog.source_hash, "mandate_hash": mandate.mandate_hash,
            "opening_capital_usd": commitment, "legacy_breaches": self.catalog.legacy_breaches(account),
            "qualification_credit": False}
        if not result["eligible"]:
            raise SubmissionBlockedError("finite_admission_rejected:" + ",".join(k for k, v in checks.items() if not v))
        return result, spec

    def prepare(self, *, intent, account, quote, assessment, mandate, market_state, now):
        from brokers.etoro.batch_api import BatchDemoAdapter
        BatchDemoAdapter._scope(intent)
        self.journal.ensure_ready()
        if type(assessment) is not BatchOrderAssessment:
            raise SubmissionBlockedError("typed_batch_assessment_required")
        assessment.validate(now)
        if quote != assessment.evidence["quote"] or assessment.evidence.get("allow_unreported_buffer"):
            raise SubmissionBlockedError("finite_exact_quote_and_reported_buffer_required")
        self.ledger.refresh_marks(account, now)
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        mapping = self.coordinator.registry.broker_mapping(intent.internal_instrument_id,
            broker="etoro", environment=Environment.DEMO)
        payload = build_order_payload(mapping.broker_instrument_id, intent.sizing_value)
        if assessment.evidence["payload_hash"] != payload_hash(payload):
            raise SubmissionBlockedError("assessment_order_payload_mismatch")
        upfront, reserve = preview_costs(assessment.preview)
        candidate = FiniteOrderCandidate("preparation", mapping.broker_instrument_id,
                                        intent.sizing_value, reserve, intent.intent_hash)
        check, spec = self._admission(mandate=mandate, candidate=candidate, account=account, now=now)
        validated_instrument_spec(spec, instrument)
        if mapping.exact_match_symbol != spec["symbol"]:
            raise SubmissionBlockedError("finite_exact_instrument_mapping_required")
        validate_market_evidence(quote=quote, market_state=market_state, instrument=instrument,
                                 instrument_spec=spec, now=now)
        risk = RiskDecision(uuid4(), intent.intent_hash, account.source_hash, quote.quote_id,
            quote.observed_at, (("policy", POLICY),),
            tuple(RiskCheck(k, v, str(v), "true") for k, v in check["checks"].items()),
            RiskOutcome.ACCEPTED, (), now)
        self.coordinator.prepare_intent(intent)
        command = self.coordinator.record_risk_decision(intent=intent, decision=risk,
            broker_payload=payload, broker="etoro")
        self.evidence.save_preparation(command.command_id, {"source_kind": self.source_kind,
            "prepared_at": now, "command_id": command.command_id, "intent": intent,
            "instrument": instrument, "mapping": mapping, "account": account, "quote": quote,
            "decision": risk, "broker_payload": payload, "pilot_admission": check,
            "expected_cost_reserve": reserve, "expected_cash_cost": upfront,
            "assessment": assessment.evidence, "assessment_hash": assessment.evidence_hash,
            "assessment_deadline": assessment.deadline, "market_state": market_state,
            "mandate_hash": mandate.mandate_hash, "instrument_spec": spec,
            "classification_evidence": self.catalog.evidence, "qualification_credit": False})
        self.journal.record_intent(command.command_id, mandate_hash=mandate.mandate_hash,
            evidence_refs={"assessment": assessment.evidence_hash, "market": market_state["evidence_hash"],
                           "catalog": self.catalog.source_hash})
        return command

    def submit_under_mandate(self, command_id, *, adapter, mandate, account, quote,
                             assessment, market_state, kill_switch, now, usage):
        self._fixture_adapter(adapter)
        self.journal.ensure_ready()
        proof, command, intent, decision = self._bound(command_id)
        if command.state != CommandState.AWAITING_APPROVAL:
            raise SubmissionBlockedError("fresh_unclaimed_finite_command_required")
        if type(assessment) is not BatchOrderAssessment:
            raise SubmissionBlockedError("typed_batch_assessment_required")
        assessment.validate(now)
        if (assessment.evidence_hash != proof["assessment_hash"]
                or assessment.evidence.get("allow_unreported_buffer")
                or account.source_hash != proof["account"]["source_hash"]
                or quote != assessment.evidence["quote"] or quote.quote_id != decision.quote_id
                or mandate.mandate_hash != proof["mandate_hash"]
                or self.catalog.source_hash != proof["pilot_admission"]["catalog_hash"]):
            raise SubmissionBlockedError("finite_exact_dynamic_evidence_changed")
        if not self.coordinator.config.broker_writes_enabled:
            raise SubmissionBlockedError("finite_writes_disabled")
        if (type(usage) is not PilotUsageAdmission or usage.floor_percent < MINIMUM_FLOOR_PERCENT
                or usage.recovery_reserve_percent < MINIMUM_RECOVERY_RESERVE_PERCENT
                or usage.validate_for_submission(now)):
            raise SubmissionBlockedError("finite_fresh_usage_and_recovery_reserve_required")
        if account.account_id != adapter.expected_account_id:
            raise SubmissionBlockedError("finite_same_direct_demo_account_required")
        self.ledger.refresh_marks(account, now)
        reserve = number(proof["expected_cost_reserve"], "expected_cost_reserve")
        mapping = self.coordinator.registry.broker_mapping(intent.internal_instrument_id,
            broker="etoro", environment=Environment.DEMO)
        instrument = self.coordinator.registry.get(intent.internal_instrument_id)
        candidate = FiniteOrderCandidate(str(command_id), mapping.broker_instrument_id,
                                        intent.sizing_value, reserve, intent.intent_hash)
        check, spec = self._admission(mandate=mandate, candidate=candidate, account=account, now=now)
        validate_market_evidence(quote=quote, market_state=market_state, instrument=instrument,
                                 instrument_spec=spec, now=now, require_handoff=True)
        status = mandate.validate(ledger=self.ledger, kill_switch=kill_switch, now=now,
                                 reviewed_commit=self.reviewed_commit)
        next_day = (now.astimezone(ZoneInfo("Europe/London")) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        deadline = min(next_day, intent.expires_at, mandate.expires_at, account.deadline - timedelta(seconds=15),
            assessment.deadline, quote.observed_at + timedelta(seconds=30),
            instant(market_state["received_at"], "market_time") + timedelta(seconds=15),
            instant(market_state["quote_received_at"], "quote_receipt") + timedelta(seconds=5),
            usage.observed_at + timedelta(seconds=MAX_USAGE_AGE_SECONDS),
            decision.decided_at + timedelta(seconds=self.limits.approval_max_age_seconds))
        if deadline <= now:
            raise SubmissionBlockedError("finite_evidence_expired")
        approval = Approval(approval_id=uuid4(), intent_hash=intent.intent_hash,
            approver="finite-mandate:" + mandate.mandate_id, environment=Environment.DEMO,
            issued_at=now, expires_at=deadline, limits=immutable_pairs({
                "authorization_kind": "finite_mandate", "mandate_hash": mandate.mandate_hash,
                "mandate_id": mandate.mandate_id, "authorization_source_reference": mandate.source_reference,
                "risk_decision_id": str(decision.decision_id), "account_snapshot_id": account.source_hash,
                "quote_id": quote.quote_id, "policy": POLICY, "assessment_hash": assessment.evidence_hash,
                "kill_switch_revision": str(status.revision), "reviewed_commit": self.reviewed_commit,
                "order_payload_hash": command.payload_hash, "usage_source_sha256": usage.source_sha256}))
        ApprovalAuthority.verify(approval=approval, intent=intent, now=now)
        adapter.bind(command, intent=intent, instrument_id=mapping.broker_instrument_id,
            authorization_hash=mandate.mandate_hash, expires_at=deadline,
            evidence={"assessment_hash": assessment.evidence_hash, "mandate_id": mandate.mandate_id,
                      "quote": quote, "market_state": market_state, "instrument": instrument,
                      "instrument_spec": spec})
        self.coordinator.record_approval(command=command, approval=approval, now=now)
        self.evidence.save_approval(command_id, approval)
        command = self.coordinator.store.get_command(command_id)
        self.evidence.append_observation(command_id, {"source_kind": self.source_kind,
            "pre_submit_account": account, "market_state": market_state, "quote": quote,
            "usage": usage, "pilot_admission": check, "observed_at": now,
            "mandate_hash": mandate.mandate_hash, "qualification_credit": False})
        adapter.bindings.authorize(command_id, {"authorized_at": now, "valid_until": deadline,
            "approval_id": str(approval.approval_id), "authorization_hash": mandate.mandate_hash,
            "account_snapshot_id": account.source_hash, "risk_decision_id": str(decision.decision_id),
            "quote_id": quote.quote_id})
        mandate.validate(ledger=self.ledger, kill_switch=kill_switch, now=now, reviewed_commit=self.reviewed_commit)
        self.ledger.reserve_under_mandate(mandate, candidate, account, now)
        self.journal.record_dispatch(command_id)
        pending = self.coordinator.store.authorize_submission(command, intent, decision, approval,
            authorized_at=now, valid_until=deadline)
        result = self.coordinator.submit_pending_command(command=pending, intent=intent, adapter=adapter, now=now)
        self.ledger.record_outcome(str(command_id), {"state": str(result.state),
            "order_id": result.broker_order_id, "reference_id": result.broker_reference_id})
        self.journal.record_outcome(command_id)
        return result

    def reconcile_finite(self, command_id, *, adapter, pre_account, post_account, now):
        self._fixture_adapter(adapter)
        proof, command, intent, _ = self._bound(command_id)
        if command.state == CommandState.SUBMISSION_PENDING:
            if not any(x.outcome == "send_started" and x.request_id == command.logical_request_id
                       for x in command.attempt_history):
                raise SubmissionBlockedError("claimed_without_send_attempt_manual_recovery_required")
            command = self.coordinator.store.transition_command(command_id, CommandState.UNKNOWN,
                details={"reason": "interrupted_direct_dispatch_lookup_only"},
                broker_reference_id=str(command.logical_request_id))
        adapter.verify_demo_identity()
        result = adapter.reconcile_command(command)
        self._validate_lookup(proof, command, intent, result, now)
        self.evidence.append_observation(command_id, {"source_kind": self.source_kind, "observed_at": now,
            "result": result, "executions": result.executions, "qualification_credit": False})
        if not result.reconciled:
            self.ledger.record_outcome(str(command_id), {"lookup_status": result.lifecycle_state, "terminal": False})
            self.journal.record_outcome(command_id)
            raise SubmissionBlockedError("finite_pending_or_unknown_lookup_only")
        if result.lifecycle_state in {"filled", "partially_filled_terminal"}:
            self.ledger.reconcile_open(str(command_id), result.executions, pre_account, post_account, now,
                terminal_partial=result.lifecycle_state == "partially_filled_terminal", terminal_evidence=result)
            target = CommandState.PARTIALLY_FILLED if result.lifecycle_state == "partially_filled_terminal" else CommandState.FILLED
        elif result.lifecycle_state in {"rejected", "cancelled"}:
            self.ledger.reconcile_no_fill(str(command_id), pre_account, post_account, now, terminal_evidence=result)
            target = CommandState.REJECTED if result.lifecycle_state == "rejected" else CommandState.CANCELLED
        else:
            raise SubmissionBlockedError("finite_verified_terminal_outcome_required")
        if command.state == CommandState.UNKNOWN:
            command = self.coordinator.store.transition_command(command_id, CommandState.RECONCILING,
                details={"source_kind": self.source_kind})
        if command.state not in {target, CommandState.RECONCILED}:
            # ACKNOWLEDGED -> CANCELLED is intentionally not a generic transition;
            # the confirmed terminal cancellation passes through its explicit state.
            if target == CommandState.CANCELLED and command.state == CommandState.ACKNOWLEDGED:
                command = self.coordinator.store.transition_command(command_id, CommandState.CANCEL_PENDING)
            command = self.coordinator.store.transition_command(command_id, target,
                details={"source_kind": self.source_kind, "broker_terminal_state": result.lifecycle_state},
                broker_order_id=result.broker_order_id)
        if command.state != CommandState.RECONCILED:
            self.coordinator.store.transition_command(command_id, CommandState.RECONCILED,
                details={"economic_reconciliation_verified": True, "qualification_credit": False,
                         "post_account_source_hash": post_account.source_hash})
        self.journal.record_outcome(command_id)
        self.journal.record_reconciliation(command_id)
        terminal = self.journal.terminal(command_id)
        if terminal is None:
            raise SubmissionBlockedError("finite_durable_terminal_journal_required")
        self.ledger.mark_journal_complete(str(command_id), journal_reference=payload_hash(terminal), now=now)
        return self.ledger.snapshot()

    @staticmethod
    def recover_committed(command_id, *, ledger, store, evidence, journal, now):
        """Complete a committed economic result without broker I/O or new send.

        Recovery uses the exact retained terminal observation and original
        account captures. It never replaces that receipt with a later lookup.
        """
        journal.ensure_ready()
        receipt = ledger.reconciliation_receipt(str(command_id))
        claim, reconciliation = receipt["claim"], receipt["reconciliation"]
        command = store.get_command(command_id)
        proof = evidence.read_preparation(command_id)
        intent = store.get_intent(command.intent_hash) if command else None
        terminal = reconciliation.get("terminal_evidence")
        if (command is None or intent is None or command.environment != Environment.DEMO
                or command.broker != "etoro" or proof.get("source_kind") != FiniteDemoOperator.source_kind
                or proof.get("command_id") != str(command_id)
                or claim.get("mandate_policy") != POLICY
                or claim.get("command_id") != str(command_id)
                or claim.get("intent_hash") != command.intent_hash
                or claim.get("mandate_hash") != proof.get("mandate_hash")
                or canonical_json(intent) != canonical_json(proof.get("intent"))
                or command.payload_hash != payload_hash(proof.get("broker_payload"))
                or receipt["reconciliation_hash"] != payload_hash(reconciliation)
                or not isinstance(terminal, dict)
                or terminal.get("command_id") != str(command_id) or terminal.get("reconciled") is not True
                or not terminal.get("broker_order_id")
                or command.broker_order_id not in {None, terminal["broker_order_id"]}
                or terminal.get("executions") != reconciliation.get("executions")):
            raise OperatorEvidenceError("finite_retained_economic_binding_required")
        details = dict(terminal.get("details", []))
        lifecycle = terminal.get("lifecycle_state")
        statuses = {"filled": {"3"}, "partially_filled_terminal": {"9", "10"},
                    "cancelled": {"7"}, "rejected": {"4", "8"}}
        if (lifecycle not in statuses or details.get("status_id") not in statuses[lifecycle]
                or details.get("remainder_terminal") != "true"
                or details.get("connector_broker_reference_id") != str(command.logical_request_id)
                or not any(canonical_json(row.get("result")) == canonical_json(terminal)
                           for row in evidence.read_observations(command_id))
                or not any(a.outcome == "send_started" and a.request_id == command.logical_request_id
                           for a in command.attempt_history)):
            raise OperatorEvidenceError("finite_original_terminal_observation_required")
        # The journal independently rechecks source accounts and economics before
        # the economic journal gate is released, including after another crash.
        target = {"filled": CommandState.FILLED, "partially_filled_terminal": CommandState.PARTIALLY_FILLED,
                  "cancelled": CommandState.CANCELLED, "rejected": CommandState.REJECTED}[lifecycle]
        if command.state == CommandState.SUBMISSION_PENDING:
            command = store.transition_command(command_id, CommandState.UNKNOWN,
                details={"reason": "retained_economic_commit_recovery"},
                broker_reference_id=str(command.logical_request_id))
        if command.state == CommandState.UNKNOWN:
            command = store.transition_command(command_id, CommandState.RECONCILING)
        if command.state not in {target, CommandState.RECONCILED}:
            if target == CommandState.CANCELLED and command.state == CommandState.ACKNOWLEDGED:
                command = store.transition_command(command_id, CommandState.CANCEL_PENDING)
            command = store.transition_command(command_id, target, broker_order_id=terminal["broker_order_id"],
                details={"source_kind": FiniteDemoOperator.source_kind, "retained_economic_receipt": receipt["reconciliation_hash"]})
        if command.state != CommandState.RECONCILED:
            store.transition_command(command_id, CommandState.RECONCILED,
                details={"economic_reconciliation_verified": True, "qualification_credit": False,
                         "post_account_source_hash": reconciliation["post_account"]["source_hash"]})
        final = journal.reconstruct(command_id, mandate_hash=claim["mandate_hash"],
            evidence_refs={"assessment": proof["assessment_hash"], "market": proof["market_state"]["evidence_hash"],
                           "catalog": proof["pilot_admission"]["catalog_hash"]})
        if final is None:
            raise SubmissionBlockedError("finite_durable_terminal_journal_required")
        return ledger.mark_journal_complete(str(command_id), journal_reference=payload_hash(final), now=now)
