"""Internal external-origin lifecycle mechanics; live source admission is closed.

No supported cash-event/opening-debit source contract has been qualified. The
public entry point therefore rejects every input before opening any runtime
file. Private transaction mechanics are exercised with synthetic qualified
bundles in tests, not exposed through a CLI, flag, or planner-report token.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import wraps
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from brokers.etoro.account_evidence import _exact_sum, _difference
from brokers.etoro.pilot_account import PilotAccount, build_pilot_account
from brokers.etoro.schemas import _pnl_timestamp, exact_datetime_epoch_seconds
from execution.domain import canonical_json, payload_hash
from risk.kill_switch import KillSwitch


SCHEMA = "external-demo-lifecycle-v1"
EXTERNAL_PREFIX = "external:"


class ExternalLifecycleError(ValueError):
    """Fixed codes only; private identities never appear in error messages."""


@dataclass(frozen=True)
class _QualifiedLifecycle:
    """Private result shape for a future qualified source adapter.

    This type is not an admission token. No public API accepts it as proof.
    A future adapter must derive every projection from authenticated source
    bytes, complete cash-event coverage and original opening-debit evidence.
    """
    account: PilotAccount
    prior_account_hash: str
    closures: tuple[dict, ...]
    openings: tuple[dict, ...]
    cash_events: tuple[dict, ...]
    sources: tuple[dict, ...]
    cash_period_source: str


def _qualify_sources(*, runtime, sources):
    # Do not replace this with a caller flag, a balancing total, a planner's
    # report, or a synthetic document schema. No production success route.
    raise ExternalLifecycleError("external_cash_and_opening_debit_source_contract_unqualified")


def import_external_lifecycle(*, runtime, sources, **options):
    """Unavailable application boundary: rejects before any file/DB access."""
    _qualify_sources(runtime=runtime, sources=sources)
    # A reviewed source adapter and public application contract are separate
    # work. Even replacing the qualifier alone cannot activate these mechanics.
    raise ExternalLifecycleError("external_public_application_not_enabled")


def _number(value, *, nonnegative=False):
    if isinstance(value, (bool, float)) or value is None:
        raise ExternalLifecycleError("external_exact_decimal_required")
    try:
        result = Decimal(value)
    except (ValueError, TypeError, ArithmeticError):
        raise ExternalLifecycleError("external_exact_decimal_required") from None
    if (not result.is_finite() or len(result.as_tuple().digits) > 100
            or abs(result.as_tuple().exponent) > 100 or nonnegative and result < 0):
        raise ExternalLifecycleError("external_bounded_exact_decimal_required")
    return result


def _id(value):
    number = _number(value)
    if number < 1 or number > 9223372036854775807 or number != number.to_integral_value():
        raise ExternalLifecycleError("external_positive_int64_identity_required")
    return str(int(number))


def _sha(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ExternalLifecycleError("external_sha256_required")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ExternalLifecycleError("external_source_text_required")
    return value


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ExternalLifecycleError("external_record_shape_invalid")


def _time(value):
    try:
        return _pnl_timestamp(value)[1]
    except (ValueError, TypeError):
        raise ExternalLifecycleError("external_exact_source_time_required") from None


def _clean(value):
    # The execution journal already owns strict nested secret rejection. A
    # local import avoids circular module imports during ledger startup.
    from agents.journal_agent.execution_journal import _clean as journal_clean
    result = journal_clean(value)
    def pairs(items):
        out = {}
        for key, child in items:
            if key in out:
                raise ExternalLifecycleError("external_duplicate_source_json_key")
            out[key] = child
        return out
    def visit(item):
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str):
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", item):
                _number(item)
            elif item.lstrip().startswith(("{", "[")):
                try:
                    nested = json.loads(item, parse_float=Decimal, object_pairs_hook=pairs)
                except json.JSONDecodeError:
                    return
                visit(nested)
        elif isinstance(item, (int, Decimal)) and not isinstance(item, bool):
            _number(item)
    visit(result)
    return result


def _exact_context(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Bounded values have <=100 digits and exponent magnitude <=100.
        # 512 digits also covers their timestamp arithmetic and bounded sums.
        with localcontext() as context:
            context.prec = 512
            return function(*args, **kwargs)
    return run


def _account(data):
    sources = data["raw_sources"]
    from agents.fill_agent.pilot_ledger import _time as moment
    rebuilt = build_pilot_account(sources["profile"], sources["first"], sources["pnl"],
        sources["last"], data["account_id"], moment(data["capture_completed_at"]))
    if _clean(rebuilt) != data:
        raise ExternalLifecycleError("external_account_source_projection_mismatch")
    return rebuilt


def _source_map(sources):
    if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
        raise ExternalLifecycleError("external_retained_sources_required")
    result = {}
    for source in sources:
        _shape(source, "reference sha256 body")
        reference = _text(source["reference"])
        if reference in result or not isinstance(source["body"], str) or len(source["body"].encode()) > 1048576:
            raise ExternalLifecycleError("external_source_identity_or_size_invalid")
        if hashlib.sha256(source["body"].encode()).hexdigest() != _sha(source["sha256"]):
            raise ExternalLifecycleError("external_source_bytes_changed")
        result[reference] = source
    return result


def _identity(record):
    return payload_hash({"account_id": record["account_id"], "inception_hash": record["inception_hash"],
        "closures": sorted(row["position_id"] for row in record["closures"]),
        "openings": sorted((row["order_id"], row["position_id"]) for row in record["openings"]),
        "cash_events": sorted(row["event_id"] for row in record["cash_events"])})


@_exact_context
def validate_record(record, *, inception, prior_account, retired, attributed):
    """Rebuild accounting effects on every ledger replay and journal readback.

    This validates the mechanics of an already admitted record, never source
    qualification. Raw source integrity and source-adapter admission are
    separate obligations; neither balances nor this function grant admission.
    """
    from agents.fill_agent.pilot_ledger import _same_holdings
    _clean(record)
    _shape(record, "schema event_id ledger_path account_id inception_hash prior_account_hash post_account closures openings cash_events sources cash_period_source reviewed_commit review_reference qualification_credit")
    if (record["schema"] != SCHEMA or record["qualification_credit"] is not False
            or record["account_id"] != inception["account_id"]
            or record["inception_hash"] != payload_hash(inception)
            or record["prior_account_hash"] != payload_hash(prior_account)
            or re.fullmatch(r"[a-f0-9]{40}", record["reviewed_commit"]) is None):
        raise ExternalLifecycleError("external_original_binding_invalid")
    _text(record["review_reference"])
    path = Path(record["ledger_path"])
    if not path.is_absolute() or path.resolve() != path:
        raise ExternalLifecycleError("external_original_ledger_path_required")
    sources = _source_map(record["sources"])
    if record["cash_period_source"] not in sources:
        raise ExternalLifecycleError("external_complete_cash_period_source_required")
    prior, post = _account(prior_account), _account(record["post_account"])
    if post.account_id != prior.account_id or post.capture_started_at < prior.capture_completed_at:
        raise ExternalLifecycleError("external_account_interval_invalid")
    before = {p.position_id: p for p in prior.positions}
    after = {p.position_id: p for p in post.positions}
    baseline = {p["position_id"]: p for p in inception["account"]["positions"] if p["position_id"] not in retired}
    if set(before) != set(baseline) | set(attributed):
        raise ExternalLifecycleError("external_prior_composition_mismatch")
    start = exact_datetime_epoch_seconds(prior.capture_completed_at)
    end = exact_datetime_epoch_seconds(post.capture_started_at)
    close_ids, open_ids, orders, cash_ids = set(), set(), set(), set()
    proceeds, debit, nontrade, positions = [], [], [], []
    for field in ("closures", "openings", "cash_events"):
        if not isinstance(record[field], list) or len(record[field]) > 1000:
            raise ExternalLifecycleError("external_bounded_event_list_required")
    def provenance(row):
        if (row["source_reference"] not in sources or row["account_currency"] != "USD"
                or not start <= _time(row["occurred_at_source"]) <= end):
            raise ExternalLifecycleError("external_event_source_currency_or_time_invalid")
    for row in record["closures"]:
        _shape(row, "position_id original_position cash_proceeds account_currency occurred_at_source source_reference")
        key = _id(row["position_id"])
        if key in close_ids or key not in baseline or key in attributed or row["original_position"] != baseline[key]:
            raise ExternalLifecycleError("external_closure_not_exact_unretired_baseline")
        provenance(row)
        if not _same_holdings(row["original_position"], _clean(before[key])):
            raise ExternalLifecycleError("external_prior_baseline_economics_changed")
        close_ids.add(key)
        proceeds.append(_number(row["cash_proceeds"], nonnegative=True))
    for row in record["openings"]:
        _shape(row, "position_id order_id account_currency occurred_at_source source_reference cash_debit quote_currency quote_price order_type time_in_force")
        key, order = _id(row["position_id"]), _id(row["order_id"])
        if key in open_ids or key in before or key in retired or key not in after or order in orders:
            raise ExternalLifecycleError("external_duplicate_or_existing_opening_identity")
        provenance(row)
        for field in ("quote_currency", "order_type", "time_in_force"):
            _text(row[field])
        if _number(row["quote_price"]) <= 0:
            raise ExternalLifecycleError("external_quote_provenance_invalid")
        position = after[key]
        if (not position.is_buy or position.leverage != 1 or position.settlement_type != 1
                or position.carrying_fees != 0):
            raise ExternalLifecycleError("external_opening_scope_or_carrying_cash_unqualified")
        # Account principal and quote currency remain different quantities. In
        # particular, GBX price * units is never treated as a USD cash debit.
        cost = _exact_sum((position.principal, position.fees, position.taxes))
        if position.principal <= 0 or _number(row["cash_debit"], nonnegative=True) != cost:
            raise ExternalLifecycleError("external_opening_cash_debit_mismatch")
        debit.append(cost)
        open_ids.add(key)
        orders.add(order)
        positions.append({"position_id": key, "instrument_id": position.instrument_id,
            "external_event_id": record["event_id"], "origin": "external",
            "capital": cost, "opening_fees": position.fees, "opening_taxes": position.taxes,
            "position_at_open": _clean(position), "order_id": order,
            "occurred_at_source": row["occurred_at_source"],
            "quote_currency": row["quote_currency"], "quote_price": row["quote_price"],
            "order_type": row["order_type"], "time_in_force": row["time_in_force"]})
    for row in record["cash_events"]:
        _shape(row, "event_id amount account_currency occurred_at_source source_reference kind")
        key = _text(row["event_id"])
        if key in cash_ids or row["kind"] not in {"deposit", "withdrawal"}:
            raise ExternalLifecycleError("external_cash_event_identity_or_kind_invalid")
        provenance(row)
        amount = _number(row["amount"])
        if amount == 0 or row["kind"] == "deposit" and amount < 0 or row["kind"] == "withdrawal" and amount > 0:
            raise ExternalLifecycleError("external_cash_event_sign_invalid")
        cash_ids.add(key)
        nontrade.append(amount)
    if not (close_ids or open_ids or cash_ids):
        raise ExternalLifecycleError("external_empty_lifecycle_event")
    if record["event_id"] != _identity(record):
        raise ExternalLifecycleError("external_lifecycle_identity_changed")
    if set(after) != (set(before) - close_ids) | open_ids:
        raise ExternalLifecycleError("external_post_composition_mismatch")
    for key in set(before) - close_ids:
        if not _same_holdings(_clean(before[key]), _clean(after[key])):
            raise ExternalLifecycleError("external_unchanged_holdings_changed")
    expected_cash = _difference(_exact_sum((prior.cash, *proceeds, *nontrade)), _exact_sum(debit))
    if expected_cash != post.cash:
        raise ExternalLifecycleError("external_cash_residual_nonzero")
    combined = {**attributed, **{p["position_id"]: p for p in positions}}
    pnl = _exact_sum(_difference(after[key].pnl, _number(p["opening_fees"]), _number(p["opening_taxes"]))
                     for key, p in combined.items())
    return {"retired": close_ids, "positions": _clean(positions), "pilot_pnl": pnl,
            "cash_event_ids": cash_ids, "order_ids": orders, "source_account": record["post_account"]}


def _record(ledger, state, proof, *, reviewed_commit, review_reference):
    if type(proof) is not _QualifiedLifecycle or type(proof.account) is not PilotAccount:
        raise ExternalLifecycleError("external_private_qualified_bundle_required")
    record = _clean({"schema": SCHEMA, "event_id": "pending", "ledger_path": str(ledger.path.resolve()),
        "account_id": state["account_id"], "inception_hash": payload_hash(state["inception"]),
        "prior_account_hash": proof.prior_account_hash, "post_account": proof.account,
        "closures": proof.closures, "openings": proof.openings, "cash_events": proof.cash_events,
        "sources": proof.sources, "cash_period_source": proof.cash_period_source,
        "reviewed_commit": reviewed_commit, "review_reference": review_reference, "qualification_credit": False})
    record["event_id"] = _identity(record)
    return record


@contextmanager
def _original_lock(ledger, switch):
    root = ledger.path.resolve().parent
    if (type(switch) is not KillSwitch or switch.state_path.resolve() != root / "kill-switch.json"
            or switch.audit_path.resolve() != root / "kill-switch.jsonl"):
        raise ExternalLifecycleError("external_original_kill_switch_required")
    status = switch.status()
    if not status.integrity_valid or not status.engaged:
        raise ExternalLifecycleError("external_existing_engaged_switch_required")
    # Existing operator lock only; this cannot initialize a replacement runtime.
    fd = os.open(root / "operator.lock", os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "a+b") as handle:
        info = os.fstat(handle.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ExternalLifecycleError("external_original_private_lock_required")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExternalLifecycleError("external_operator_active") from None
        yield status
        if switch.status() != status:
            raise ExternalLifecycleError("external_switch_changed_during_import")


@_exact_context
def _apply_qualified(ledger, proof, *, expected_plan_hash, reviewed_commit, review_reference, kill_switch, now):
    """Private transaction engine; unavailable to production source admission."""
    _sha(expected_plan_hash)
    from agents.fill_agent.pilot_ledger import _time as moment
    with _original_lock(ledger, kill_switch) as switch_before:
        with closing(ledger._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = ledger._state(db)
            record = _record(ledger, state, proof, reviewed_commit=reviewed_commit, review_reference=review_reference)
            if payload_hash(record) != expected_plan_hash:
                raise ExternalLifecycleError("external_exact_reviewed_plan_required")
            previous = state["_external_imports"].get(record["event_id"])
            if previous is not None:
                if previous != record:
                    raise ExternalLifecycleError("external_identity_conflict_never_reapply")
                return record
            if state["unresolved"]:
                raise ExternalLifecycleError("external_pending_outcome_or_journal_blocks_import")
            proof.account.validate_fresh(now)
            validate_record(record, inception=state["inception"], prior_account=state["_account_checkpoint"],
                retired=state["_retired_baseline"], attributed={p["position_id"]: p for p in state["positions"]})
            ledger._append(db, "external_lifecycle", None, record)
            # Replay the committed shape before the transaction can become
            # durable. Positions/capital/P&L and journal obligation are atomic.
            result = ledger._state(db)
            if result["_external_imports"].get(record["event_id"]) != record:
                raise ExternalLifecycleError("external_transaction_readback_failed")
            if result["pilot_pnl"] <= -25 and not result["loss_stopped"]:
                ledger._append(db, "loss_stop", None, {"observed_at": moment(now),
                    "pilot_pnl": result["pilot_pnl"], "reason": "external_import_cumulative_loss_stop"})
            if result["committed_capital"] > 500 or result["position_count"] > 5:
                ledger._append(db, "hold", None, {"observed_at": moment(now),
                    "reason": "external_import_preserved_historical_risk_breach"})
            if kill_switch.status() != switch_before:
                raise ExternalLifecycleError("external_switch_changed_before_commit")
    return ledger.external_lifecycle_receipt(record["event_id"])["record"]


def recover_external_journal(ledger, journal, *, event_id, now):
    """Only reconstruct a durably committed event; never import or resend."""
    from agents.journal_agent.execution_journal import ExecutionJournal
    if type(journal) is not ExecutionJournal or journal.ledger.path.resolve() != ledger.path.resolve():
        raise ExternalLifecycleError("external_same_typed_journal_required")
    terminal = journal.record_external_import(event_id)
    ledger._mark_external_journal_complete(event_id, journal=journal, now=now)
    return terminal
