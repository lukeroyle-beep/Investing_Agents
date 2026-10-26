"""Durable attribution and reconciliation for the explicitly approved Demo pilot.

This is a subset ledger of broker position identifiers, not a broker account.
The Fill Agent owns economic reconciliation. A claim is irreversible: a lost
response can only be resolved from broker lookup and complete account evidence.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from brokers.base import BrokerExecution
from brokers.etoro.account_evidence import _exact_sum, _difference
from brokers.etoro.pilot_account import PilotAccount
from execution.domain import Environment, _aware_utc, canonical_json, payload_hash


class PilotLedgerError(ValueError):
    pass


ZERO = Decimal("0")
LONDON = ZoneInfo("Europe/London")
SCHEMA = "demo-pilot-v1"


def _money(value, name, *, nonnegative=False):
    if isinstance(value, (float, bool)) or value is None:
        raise PilotLedgerError(f"{name}_exact_money_required")
    try:
        value = Decimal(value)
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise PilotLedgerError(f"{name}_exact_money_required") from exc
    if not value.is_finite() or (nonnegative and value < 0):
        raise PilotLedgerError(f"{name}_invalid")
    return value


def _cash_reserve_met(cash, equity):
    # Integer cross-products avoid rounding at exactly the 20% boundary.
    cash_n, cash_d = cash.as_integer_ratio()
    equity_n, equity_d = equity.as_integer_ratio()
    return cash_n * 5 * equity_d >= equity_n * cash_d


def _clean(value):
    """Reject float evidence and exclude secret-bearing fields before storage."""
    value = json.loads(canonical_json(value))
    def visit(item):
        if isinstance(item, float):
            raise PilotLedgerError("exact_decimal_evidence_required")
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()
                    if not any(secret in key.lower().replace("_", "").replace("-", "")
                               for secret in ("token", "authorization", "apikey", "userkey", "credential", "secret"))}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item
    return visit(value)


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(value, "pilot_time")


def _account(account, expected_account_id, now):
    if not isinstance(account, PilotAccount):
        raise PilotLedgerError("typed_pilot_account_required")
    if str(account.account_id) != str(expected_account_id):
        raise PilotLedgerError("pilot_wrong_account")
    account.validate_fresh(now)
    data = _clean(account)
    if not data.get("source_hash"):
        raise PilotLedgerError("pilot_account_provenance_missing")
    cash = _money(data["cash"], "cash", nonnegative=True)
    equity = _money(data["equity"], "equity", nonnegative=True)
    if cash > equity or equity <= 0:
        raise PilotLedgerError("pilot_cash_equity_inconsistent")
    positions = data["positions"]
    if len({str(p["position_id"]) for p in positions}) != len(positions):
        raise PilotLedgerError("pilot_duplicate_position_identity")
    return data


def _positions(account):
    return {str(p["position_id"]): p for p in account["positions"]}


def _same_holdings(before, after, *, carrying=True):
    fields = ("instrument_id", "units", "principal", "fees", "taxes", "is_buy", "leverage", "settlement_type")
    if carrying:
        fields += ("carrying_fees",)
    monetary = {"units", "principal", "fees", "taxes", "leverage", "carrying_fees"}
    return all((_money(before[k], k) == _money(after[k], k) if k in monetary else before[k] == after[k])
               for k in fields)


class PilotLedger:
    """Explicit initialization, immutable inception, append-only observations."""

    @classmethod
    def create(cls, path, account, legacy_breaches, now):
        moment = _time(now)
        data = _account(account, account.account_id, moment)
        inception = {"policy": SCHEMA, "account_id": str(account.account_id), "inception": moment,
                     "account": data, "legacy_breaches": _clean(legacy_breaches),
                     "pilot_positions_at_inception": [], "pilot_realised_pnl_at_inception": ZERO,
                     "historical_whole_account_daily_loss": None,
                     "historical_whole_account_drawdown": None,
                     "qualification_credit": False}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise PilotLedgerError("pilot_inception_already_exists_never_reset") from exc
        os.close(descriptor)
        # An interrupted initialization leaves an unusable file. Never delete
        # it automatically, since that would silently permit a new inception.
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE inception (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE submissions (command_id TEXT PRIMARY KEY, london_day TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE events (sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, command_id TEXT, payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TRIGGER inception_no_update BEFORE UPDATE ON inception BEGIN SELECT RAISE(ABORT,'immutable inception'); END;
                CREATE TRIGGER inception_no_delete BEFORE DELETE ON inception BEGIN SELECT RAISE(ABORT,'immutable inception'); END;
                CREATE TRIGGER submissions_no_update BEFORE UPDATE ON submissions BEGIN SELECT RAISE(ABORT,'immutable submission'); END;
                CREATE TRIGGER submissions_no_delete BEFORE DELETE ON submissions BEGIN SELECT RAISE(ABORT,'immutable submission'); END;
                CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
                CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
            """)
            db.execute("INSERT INTO inception VALUES (1,?,?)", (canonical_json(inception), payload_hash(inception)))
        return cls(path, expected_account_id=str(account.account_id))

    def __init__(self, path, expected_account_id):
        self.path = Path(path)
        self.expected_account_id = str(expected_account_id)
        if not self.path.is_file():
            raise PilotLedgerError("pilot_inception_missing_explicit_creation_required")
        with closing(self._connect()) as db:
            try:
                self._state(db)
            except sqlite3.DatabaseError as exc:
                raise PilotLedgerError("pilot_ledger_invalid_never_reinitialize") from exc

    def _connect(self):
        try:
            db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
            db.execute("PRAGMA synchronous=FULL")
            return db
        except sqlite3.DatabaseError as exc:
            raise PilotLedgerError("pilot_ledger_missing_or_unavailable") from exc

    @staticmethod
    def _decode(row):
        if row is None:
            raise PilotLedgerError("pilot_ledger_missing_record")
        data = json.loads(row[0])
        if payload_hash(data) != row[1]:
            raise PilotLedgerError("pilot_ledger_integrity_failure")
        return data

    @staticmethod
    def _append(db, kind, command_id, data):
        clean = _clean(data)
        db.execute("INSERT INTO events(kind,command_id,payload,digest) VALUES (?,?,?,?)",
                   (kind, str(command_id) if command_id is not None else None,
                    canonical_json(clean), payload_hash(clean)))

    def _state(self, db):
        inception = self._decode(db.execute("SELECT payload,digest FROM inception WHERE singleton=1").fetchone())
        if inception["policy"] != SCHEMA or inception["account_id"] != self.expected_account_id:
            raise PilotLedgerError("pilot_wrong_account_or_policy")
        submissions = {}
        for command_id, day, encoded, digest in db.execute("SELECT command_id,london_day,payload,digest FROM submissions ORDER BY rowid"):
            value = self._decode((encoded, digest))
            if value["command_id"] != command_id or value["london_day"] != day:
                raise PilotLedgerError("pilot_submission_integrity_failure")
            submissions[command_id] = {**value, "reconciled": False}
        positions, executions, reconciliations, holds = {}, {}, {}, []
        pilot_pnl, loss_stopped = ZERO, False
        for kind, command_id, encoded, digest in db.execute("SELECT kind,command_id,payload,digest FROM events ORDER BY sequence"):
            event = self._decode((encoded, digest))
            if kind == "reconciled_open":
                if command_id not in submissions or command_id in reconciliations:
                    raise PilotLedgerError("pilot_reconciliation_integrity_failure")
                submissions[command_id]["reconciled"] = True
                reconciliations[command_id] = event
                for p in event["positions"]:
                    if p["position_id"] in positions:
                        raise PilotLedgerError("pilot_duplicate_position_attribution")
                    positions[p["position_id"]] = p
                for execution in event["executions"]:
                    key = execution["broker_execution_id"]
                    if key in executions:
                        raise PilotLedgerError("pilot_duplicate_execution_attribution")
                    executions[key] = execution
            elif kind == "mark":
                pilot_pnl = _money(event["pilot_pnl"], "pilot_pnl")
            elif kind == "loss_stop":
                loss_stopped = True
            elif kind == "hold":
                holds.append(event["reason"])
            elif kind != "outcome":
                raise PilotLedgerError("pilot_unknown_event")
        pending = tuple(s for s in submissions.values() if not s["reconciled"])
        capital = _exact_sum(_money(p["capital"], "capital", nonnegative=True) for p in positions.values())
        capital = _exact_sum((capital, *(_exact_sum((_money(s["amount"], "amount"), _money(s["expected_costs"], "expected_costs"))) for s in pending)))
        return {"account_id": inception["account_id"], "inception": inception,
                "loss_stopped": loss_stopped, "pilot_pnl": pilot_pnl,
                "committed_capital": capital, "position_count": len(positions) + len(pending),
                "unresolved": bool(pending or holds), "holds": tuple(holds),
                "positions": tuple(positions.values()), "submissions": tuple(submissions.values()),
                "_executions": executions, "_reconciliations": reconciliations}

    @staticmethod
    def _public(state):
        return {key: value for key, value in state.items() if not key.startswith("_")}

    def snapshot(self):
        with closing(self._connect()) as db:
            return self._public(self._state(db))

    def _mark(self, db, state, data, now):
        current = _positions(data)
        baseline = _positions(state["inception"]["account"])
        pilot = {p["position_id"]: p for p in state["positions"]}
        if set(current) != set(baseline) | set(pilot):
            raise PilotLedgerError("pilot_missing_or_unattributed_position_closed_evidence_required")
        for key, p in baseline.items():
            if not _same_holdings(p, current[key]):
                raise PilotLedgerError("pilot_legacy_holdings_changed")
        pnl = ZERO
        for key, p in pilot.items():
            if not _same_holdings(p["position_at_open"], current[key], carrying=False):
                raise PilotLedgerError("pilot_position_changed_closed_evidence_required")
            if _money(current[key]["carrying_fees"], "carrying_fees") != _money(p["position_at_open"]["carrying_fees"], "carrying_fees"):
                raise PilotLedgerError("pilot_carrying_fee_cash_movement_evidence_required")
            # Asset value is principal + broker pnl. Actual opening cash debit
            # was proven as principal + external fees/taxes, so net attribution
            # subtracts those external costs exactly once. Carrying fees require
            # a separately evidenced cash-flow convention when they change.
            pnl = _exact_sum((pnl, _difference(_money(current[key]["pnl"], "pnl"),
                    _money(p["opening_fees"], "opening_fees"),
                    _money(p["opening_taxes"], "opening_taxes"))))
        self._append(db, "mark", None, {"observed_at": now, "account": data,
                     "pilot_pnl": pnl, "realised_pnl": ZERO,
                     "pnl_basis": "verified_opening_cash_flows_plus_position_asset_values"})
        if pnl <= Decimal("-25") and not state["loss_stopped"]:
            self._append(db, "loss_stop", None, {"observed_at": now, "pilot_pnl": pnl,
                                               "reason": "cumulative_pilot_loss_at_least_25"})
        return pnl

    def refresh_marks(self, account, now):
        moment = _time(now)
        data = _account(account, self.expected_account_id, moment)
        error = None
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            if any(not s["reconciled"] for s in state["submissions"]):
                raise PilotLedgerError("pilot_unresolved_submission_reconcile_before_marks")
            try:
                self._mark(db, state, data, moment)
            except PilotLedgerError as exc:
                self._append(db, "hold", None, {"reason": str(exc), "observed_at": moment, "account": data})
                error = exc
        if error:
            raise error
        return self.snapshot()

    def reserve_and_claim(self, command_id, amount, expected_costs, account, now, *, intent_hash=None, instrument_id=None):
        moment = _time(now)
        data = _account(account, self.expected_account_id, moment)
        amount, costs = _money(amount, "amount", nonnegative=True), _money(expected_costs, "expected_costs", nonnegative=True)
        if amount <= 0 or _exact_sum((amount, costs)) > 100:
            raise PilotLedgerError("pilot_order_cap_100_including_costs")
        command_id = str(command_id)
        if not command_id:
            raise PilotLedgerError("pilot_command_id_required")
        day = moment.astimezone(LONDON).date().isoformat()
        error = None
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            if any(s["command_id"] == command_id for s in state["submissions"]):
                raise PilotLedgerError("pilot_duplicate_command_lookup_only")
            if state["unresolved"]:
                raise PilotLedgerError("pilot_unresolved_reconciliation_lookup_only")
            if any(s["london_day"] == day for s in state["submissions"]):
                raise PilotLedgerError("pilot_one_opening_submission_per_london_day")
            try:
                self._mark(db, state, data, moment)
            except PilotLedgerError as exc:
                self._append(db, "hold", None, {"reason": str(exc), "observed_at": moment})
                error = exc
            state = self._state(db)
            if error is None and state["loss_stopped"]:
                error = PilotLedgerError("pilot_cumulative_loss_stop_latched")
            if error is None:
                if _exact_sum((state["committed_capital"], amount, costs)) > 500:
                    raise PilotLedgerError("pilot_combined_capital_cap_500")
                if state["position_count"] >= 5:
                    raise PilotLedgerError("pilot_maximum_five_positions")
                if not _cash_reserve_met(_difference(_money(data["cash"], "cash"), amount, costs), _money(data["equity"], "equity")):
                    raise PilotLedgerError("pilot_cash_reserve_below_20_percent")
                claim = {"command_id": command_id, "london_day": day, "send_started_at": moment,
                         "amount": amount, "expected_costs": costs, "pre_account": data,
                         "intent_hash": intent_hash, "instrument_id": instrument_id,
                         "recovery": "lookup_only_no_resubmission"}
                db.execute("INSERT INTO submissions VALUES (?,?,?,?)",
                           (command_id, day, canonical_json(claim), payload_hash(claim)))
        if error:
            raise error
        return next(s for s in self.snapshot()["submissions"] if s["command_id"] == command_id)

    def record_outcome(self, command_id, response):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            if str(command_id) not in {s["command_id"] for s in state["submissions"]}:
                raise PilotLedgerError("pilot_submission_claim_missing")
            self._append(db, "outcome", command_id, {"response": response})
        return self.snapshot()

    def reconcile_open(self, command_id, executions, pre_account, post_account, now):
        moment = _time(now)
        # The pre-submit capture is expected to age during execution/recovery;
        # its original clocks and bytes must match the durably claimed capture.
        if not isinstance(pre_account, PilotAccount):
            raise PilotLedgerError("typed_pilot_account_required")
        pre_account.validate_fresh(pre_account.capture_completed_at)
        pre = _clean(pre_account)
        post = _account(post_account, self.expected_account_id, moment)
        command_id = str(command_id)
        error = None
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db)
            claims = [s for s in state["submissions"] if s["command_id"] == command_id]
            if len(claims) != 1:
                raise PilotLedgerError("pilot_submission_claim_missing")
            claim = claims[0]
            if payload_hash(pre) != payload_hash(claim["pre_account"]):
                raise PilotLedgerError("pilot_reconciliation_pre_capture_mismatch")
            if _time(post["capture_started_at"]) < _time(claim["send_started_at"]):
                raise PilotLedgerError("pilot_post_capture_precedes_submission")
            unique = {}
            for execution in executions:
                if not isinstance(execution, BrokerExecution):
                    raise PilotLedgerError("pilot_broker_execution_evidence_required")
                raw = _clean(execution)
                key = str(execution.broker_execution_id)
                if key in unique and unique[key] != raw:
                    raise PilotLedgerError("pilot_conflicting_duplicate_execution")
                unique[key] = raw
            values = tuple(unique.values())
            if not values:
                raise PilotLedgerError("pilot_confirmed_opening_executions_required")
            previous = state["_reconciliations"].get(command_id)
            if previous:
                if payload_hash(previous["executions"]) != payload_hash(values):
                    raise PilotLedgerError("pilot_reconciled_execution_changed")
                return self._public(state)
            before, after = _positions(pre), _positions(post)
            grouped = defaultdict(list)
            order_ids, references = set(), set()
            for e in values:
                if (e["environment"] != str(Environment.DEMO) or e["currency"] != "USD"
                        or e["side"] != "long" or e["action"] != "buy"
                        or not e["executed_at_source"] or not e["broker_execution_id"]
                        or not e["broker_order_id"] or not e["broker_reference_id"]
                        or not e["broker_position_id"] or not e["price_rate_id"]
                        or _money(e["units"], "units") <= 0 or _money(e["price"], "price") <= 0
                        or _money(e["executed_amount"], "executed_amount") <= 0
                        or _money(e["fees"], "fees") < 0 or _money(e["taxes"], "taxes") < 0):
                    raise PilotLedgerError("pilot_execution_scope_or_economics_invalid")
                if _time(e["executed_at"]) < _time(claim["send_started_at"]) or _time(e["executed_at"]) > _time(post["capture_completed_at"]):
                    raise PilotLedgerError("pilot_execution_outside_submission_capture")
                if e["broker_execution_id"] in state["_executions"]:
                    raise PilotLedgerError("pilot_execution_already_attributed")
                if claim["instrument_id"] is not None and e["broker_instrument_id"] != claim["instrument_id"]:
                    raise PilotLedgerError("pilot_execution_instrument_mismatch")
                grouped[e["broker_position_id"]].append(e)
                order_ids.add(e["broker_order_id"])
                references.add(e["broker_reference_id"])
            if len(order_ids) != 1 or len(references) != 1:
                raise PilotLedgerError("pilot_one_broker_order_required")
            if set(after) != set(before) | set(grouped) or set(before) & set(grouped):
                raise PilotLedgerError("pilot_whole_account_position_set_mismatch")
            for key, p in before.items():
                if not _same_holdings(p, after[key]):
                    raise PilotLedgerError("pilot_whole_account_existing_holding_changed")
            principal = _exact_sum(_money(e["executed_amount"], "executed_amount") for e in values)
            if principal != _money(claim["amount"], "amount"):
                raise PilotLedgerError("pilot_terminal_full_requested_principal_required")
            costs = _exact_sum(_exact_sum((_money(e["fees"], "fees"), _money(e["taxes"], "taxes"))) for e in values)
            if _difference(_money(pre["cash"], "cash"), principal, costs, _money(post["cash"], "cash")) != ZERO:
                raise PilotLedgerError("pilot_actual_cash_reconciliation_failed")
            records = []
            for key, fills in grouped.items():
                p = after[key]
                invested = _exact_sum(_money(e["executed_amount"], "executed_amount") for e in fills)
                fees = _exact_sum(_money(e["fees"], "fees") for e in fills)
                taxes = _exact_sum(_money(e["taxes"], "taxes") for e in fills)
                if (not p["is_buy"] or _money(p["leverage"], "leverage") != 1 or p["settlement_type"] != 1
                        or any(p["instrument_id"] != e["broker_instrument_id"] for e in fills)
                        or _money(p["units"], "units") != _exact_sum(_money(e["units"], "units") for e in fills)
                        or _money(p["principal"], "principal") != invested
                        or _money(p["fees"], "fees") != fees
                        or _money(p["taxes"], "taxes") != taxes):
                    raise PilotLedgerError("pilot_new_position_or_actual_costs_mismatch")
                if _money(p["carrying_fees"], "carrying_fees") != ZERO:
                    raise PilotLedgerError("pilot_initial_carrying_fee_cash_movement_evidence_required")
                records.append({"position_id": key, "instrument_id": p["instrument_id"],
                                "command_id": command_id, "capital": _exact_sum((invested, fees, taxes)),
                                "opening_fees": fees, "opening_taxes": taxes, "position_at_open": p})
            self._append(db, "reconciled_open", command_id, {"observed_at": moment,
                         "pre_account": pre, "post_account": post,
                         "executions": values, "positions": records})
            state = self._state(db)
            breaches = []
            if _exact_sum((principal, costs)) > 100:
                breaches.append("pilot_actual_order_cap_exceeded")
            if state["committed_capital"] > 500:
                breaches.append("pilot_actual_combined_capital_cap_exceeded")
            if state["position_count"] > 5:
                breaches.append("pilot_actual_position_count_exceeded")
            if not _cash_reserve_met(_money(post["cash"], "cash"), _money(post["equity"], "equity")):
                breaches.append("pilot_actual_cash_reserve_breached")
            for reason in breaches:
                self._append(db, "hold", command_id, {"reason": reason, "observed_at": moment})
            self._mark(db, state, post, moment)
            if breaches:
                error = PilotLedgerError(";".join(breaches))
        if error:
            raise error
        return self.snapshot()
