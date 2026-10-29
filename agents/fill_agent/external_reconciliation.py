"""Read-only admission report for external Demo lifecycle reconciliation.

No supported complete cash-event source has been qualified yet. This module
does not import economics, initialize stores or clear holds, even when observed
cash happens to balance. Prospective effects are evidence for the next review.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from contextlib import closing
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from brokers.etoro.pilot_account import build_pilot_account
from brokers.etoro.account_evidence import _exact_sum, _difference
from brokers.etoro.pilot_reads import read_receipt_envelope
from brokers.etoro.schemas import _pnl_timestamp, exact_datetime_epoch_seconds
from execution.domain import canonical_json, payload_hash

SCHEMA = "external-demo-reconciliation-admission-v1"
HISTORY_PATH = "/api/v1/trading/info/trade/demo/history"
LOOKUP_PATH = "/api/v2/trading/info/demo/orders:lookup"
_SHA = re.compile(r"[0-9a-f]{64}")
_BASE = {"cash_event_source_contract_unqualified", "external_import_apply_unimplemented",
         "external_import_journal_phase_unimplemented"}


class ExternalReconciliationError(ValueError):
    """Fixed codes only: private identifiers and bodies never reach stdout."""


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def invalid(_):
        raise ValueError
    def bounded_int(value):
        if len(value) > 100:
            raise ValueError
        return int(value)
    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value):
            _number(value)
        elif isinstance(value, (Decimal, int)) and not isinstance(value, bool):
            _number(value)
    try:
        result = json.loads(raw, parse_float=_number, parse_int=bounded_int,
                            parse_constant=invalid, object_pairs_hook=pairs)
        visit(result)
        return result
    except (ValueError, TypeError, UnicodeError):
        raise ExternalReconciliationError("external_source_json_invalid") from None


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 16 * 1024 * 1024):
                raise ExternalReconciliationError("external_owned_private_source_required")
            return handle.read()
    except OSError:
        raise ExternalReconciliationError("external_source_unavailable") from None


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise ExternalReconciliationError("external_source_timezone_required") from None


def _number(value):
    if isinstance(value, (bool, float)) or value is None:
        raise ExternalReconciliationError("external_exact_money_required")
    try:
        result = Decimal(value)
        if (not result.is_finite() or len(result.as_tuple().digits) > 100
                or abs(result.as_tuple().exponent) > 100):
            raise ValueError
        return result
    except (ValueError, ArithmeticError, TypeError):
        raise ExternalReconciliationError("external_exact_money_required") from None


def _source_time(value):
    try:
        return _pnl_timestamp(value)[1]
    except ValueError:
        raise ExternalReconciliationError("external_source_exact_timezone_time_required") from None


def _id(value):
    result = _number(value)
    if result < 1 or result > 9223372036854775807 or result != result.to_integral_value():
        raise ExternalReconciliationError("external_positive_identity_required")
    return str(int(result))


def _source(root, reference, retained):
    if (not isinstance(reference, dict) or set(reference) != {"path", "sha256"}
            or not isinstance(reference["path"], str)
            or not isinstance(reference["sha256"], str) or not _SHA.fullmatch(reference["sha256"])):
        raise ExternalReconciliationError("external_exact_source_reference_required")
    path = root / reference["path"]
    if Path(reference["path"]).is_absolute() or path.resolve() != path or not path.is_relative_to(root):
        raise ExternalReconciliationError("external_source_path_escape")
    raw = _read(path)
    if _hash(raw) != reference["sha256"]:
        raise ExternalReconciliationError("external_source_hash_mismatch")
    retained[path] = raw
    return raw


def _response(root, bundle, retained, path):
    if not isinstance(bundle, dict) or set(bundle) != {"request", "receipt", "body"}:
        raise ExternalReconciliationError("external_original_response_bundle_required")
    request, receipt = (_json(_source(root, bundle[k], retained)) for k in ("request", "receipt"))
    body = _source(root, bundle["body"], retained)
    if (request.get("method") != "GET" or request.get("path") != path
            or receipt.get("status_code") != 200 or receipt.get("read_outcome") != "complete"
            or receipt.get("content_encoding") != "identity"
            or receipt.get("body_sha256") != _hash(body) or receipt.get("body_bytes") != len(body)):
        raise ExternalReconciliationError("external_response_provenance_invalid")
    start, end = _instant(request["request_started_at"]), _instant(receipt["response_received_at"])
    if not 0 <= (end - start).total_seconds() <= 10:
        raise ExternalReconciliationError("external_response_interval_invalid")
    # History receipts include their original request fields; the direct order
    # client uses a separate response record. Never ignore a contradictory echo.
    for key in ("method", "path", "query", "request_id", "request_started_at"):
        if key in receipt and receipt[key] != request.get(key):
            raise ExternalReconciliationError("external_request_receipt_mismatch")
    return request, _json(body), end


def _original(root, retained):
    marker_path = root / "pilot-inception.json"
    marker_raw = _read(marker_path)
    marker = _json(marker_raw)
    if set(marker) != {"account_id", "inception_hash", "policy"} or marker["policy"] != "demo-pilot-v1":
        raise ExternalReconciliationError("external_original_marker_required")
    retained[marker_path] = marker_raw
    paths = [root / name for name in ("pilot.sqlite3", "execution.sqlite3", "operator.sqlite3")]
    for path in paths:
        retained[path] = _read(path)
        # A moving WAL cannot be represented by a hash of the main DB alone.
        wal = Path(str(path) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise ExternalReconciliationError("external_runtime_wal_requires_stopped_snapshot")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(path) + suffix)
            retained[sidecar] = _read(sidecar) if sidecar.exists() else None
    try:
        # Deserialize the already-read stable bytes: SQLite never opens the
        # original path and cannot create/update its WAL or SHM sidecars.
        with closing(sqlite3.connect(":memory:")) as db:
            db.deserialize(retained[paths[0]])
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError
            rows = db.execute("SELECT payload,digest FROM inception").fetchall()
            if len(rows) != 1:
                raise ValueError
            initial = _json(rows[0][0])
            if payload_hash(initial) != rows[0][1] or rows[0][1] != marker["inception_hash"]:
                raise ValueError
            for table in ("submissions", "events"):
                for payload, digest in db.execute(f"SELECT payload,digest FROM {table}"):
                    if payload_hash(_json(payload)) != digest:
                        raise ValueError
            submissions = db.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
            event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if initial["account_id"] != marker["account_id"]:
                raise ValueError
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise ExternalReconciliationError("external_original_ledger_integrity_failure") from None
    return initial, submissions, event_count


def inspect_external_reconciliation(*, runtime, manifest, now):
    """Rebuild evidence and return a sanitized, permanently non-applying plan.

    Manifest references only original bytes. Neither supplied totals nor a
    verified flag is accepted. Historic account validity is distinct from its
    present freshness. No network, credentials, ledger classes or write locks.
    """
    root, manifest = Path(runtime), Path(manifest)
    if not root.is_absolute() or root.resolve() != root or not manifest.is_absolute() or manifest.resolve() != manifest:
        raise ExternalReconciliationError("external_canonical_absolute_paths_required")
    now = _instant(now.isoformat())
    retained = {manifest: _read(manifest)}
    inputs = _json(retained[manifest])
    if (not isinstance(inputs, dict) or set(inputs) != {"schema", "runtime", "inception_hash",
            "account_receipts", "history", "opening_lookups"} or inputs["schema"] != SCHEMA
            or inputs["runtime"] != str(root)):
        raise ExternalReconciliationError("external_manifest_shape_or_runtime_invalid")
    initial, submissions, events = _original(root, retained)
    if inputs["inception_hash"] != payload_hash(initial):
        raise ExternalReconciliationError("external_manifest_inception_mismatch")
    evidence_root = manifest.parent
    sources = _json(_source(evidence_root, inputs["account_receipts"], retained))
    if not isinstance(sources, dict) or set(sources) != {"profile", "first", "pnl", "last"}:
        raise ExternalReconciliationError("external_complete_account_bracket_required")
    for source in sources.values():
        _json(source["result"]["body"])
        read_receipt_envelope(source)
        raw = source["raw_response"]
        path = Path(raw["body_file"])
        if not path.is_absolute() or path.resolve() != path or not path.is_relative_to(evidence_root):
            raise ExternalReconciliationError("external_account_body_path_escape")
        body = _read(path)
        if _hash(body) != raw["body_sha256"] or body.decode("utf-8") != source["result"]["body"]:
            raise ExternalReconciliationError("external_account_body_mismatch")
        retained[path] = body
    capture = _instant(sources["last"]["response_received_at"])
    account = build_pilot_account(sources["profile"], sources["first"], sources["pnl"],
                                  sources["last"], initial["account_id"], capture)
    blockers = set(_BASE)
    if now < capture or now > account.deadline:
        blockers.add("current_account_refresh_required")
    if submissions:
        blockers.add("existing_submissions_require_separate_lifecycle_review")
    baseline = {p["position_id"]: p for p in initial["account"]["positions"]}
    current = {p.position_id: p for p in account.positions}
    missing, added = set(baseline) - set(current), set(current) - set(baseline)
    unchanged = 0
    for key in set(baseline) & set(current):
        old, present = baseline[key], current[key]
        if (old["instrument_id"] == present.instrument_id and old["is_buy"] == present.is_buy
                and old["settlement_type"] == present.settlement_type
                and all(_number(old[k]) == getattr(present, k) for k in
                        ("units", "principal", "leverage", "fees", "taxes", "carrying_fees"))):
            unchanged += 1
        else:
            blockers.add("baseline_position_economics_changed")
    pages, history = inputs["history"], []
    if not isinstance(pages, list) or not pages or len(pages) > 10:
        raise ExternalReconciliationError("external_bounded_history_required")
    inception_at = _instant(initial["inception"])
    previous_size = None
    for page, bundle in enumerate(pages, 1):
        request, rows, received = _response(evidence_root, bundle, retained, HISTORY_PATH)
        query = request.get("query", {})
        if (set(query) != {"minDate", "page", "pageSize"} or type(query["page"]) is not int or query["page"] != page
                or type(query["pageSize"]) is not int or not 1 <= query["pageSize"] <= 200
                or (previous_size is not None and previous_size != query["pageSize"])):
            raise ExternalReconciliationError("external_history_query_invalid")
        try:
            start = date.fromisoformat(query["minDate"])
        except (ValueError, TypeError):
            raise ExternalReconciliationError("external_history_date_invalid") from None
        if start > inception_at.date() or (received.date()-start).days >= 365 or received > now:
            raise ExternalReconciliationError("external_history_interval_incomplete")
        if not isinstance(rows, list) or len(rows) > query["pageSize"]:
            raise ExternalReconciliationError("external_history_rows_invalid")
        history.extend((row, received) for row in rows)
        previous_size = query["pageSize"]
    closures, seen = {}, set()
    for row, received in history:
        key = _id(row.get("positionId"))
        if key in seen:
            raise ExternalReconciliationError("external_duplicate_history_identity")
        seen.add(key)
        if key not in missing:
            blockers.add("unmatched_history_requires_review")
            continue
        old = baseline[key]
        if (row.get("instrumentId") != old["instrument_id"] or type(row.get("isBuy")) is not bool
                or row["isBuy"] != old["is_buy"] or _number(row["units"]) != _number(old["units"])
                or _number(row["leverage"]) != _number(old["leverage"])
                or _number(row["investment"]) != _number(old["principal"])
                or _number(row["fees"]) < 0):
            blockers.add("closed_position_economics_mismatch")
            continue
        if not (exact_datetime_epoch_seconds(inception_at) <= _source_time(row["closeTimestamp"])
                <= exact_datetime_epoch_seconds(min(capture, received))):
            blockers.add("closed_position_time_outside_interval")
            continue
        closures[key] = row
    if set(closures) != missing:
        blockers.add("missing_baseline_closure_evidence")
    lookups = inputs["opening_lookups"]
    if not isinstance(lookups, list) or len(lookups) > 20:
        raise ExternalReconciliationError("external_bounded_opening_lookups_required")
    observed, conflicts, order_ids = set(), 0, set()
    for bundle in lookups:
        request, lookup, received = _response(evidence_root, bundle, retained, LOOKUP_PATH)
        query = request.get("query", {})
        if set(query) != {"orderId"} or _id(query["orderId"]) != _id(lookup.get("orderId")):
            raise ExternalReconciliationError("external_lookup_original_identity_mismatch")
        order = _id(lookup["orderId"])
        if order in order_ids:
            raise ExternalReconciliationError("external_duplicate_lookup_order")
        order_ids.add(order)
        if _id(lookup.get("accountId")) != initial["account_id"] or received > now:
            raise ExternalReconciliationError("external_lookup_account_or_time_mismatch")
        status = lookup.get("status", {})
        if (status.get("id") not in {3, 9, 10}
                or status.get("name") != {3: "Filled", 9: "CanceledPartiallyFilled", 10: "RejectedPartiallyFilled"}.get(status.get("id"))
                or lookup.get("action") != "open" or lookup.get("transaction") != "buy"
                or str(lookup.get("orderCurrency")).upper() != "USD"):
            blockers.add("external_opening_terminal_scope_unverified")
            continue
        positions = lookup.get("positionExecutions")
        if not isinstance(positions, list) or not positions:
            raise ExternalReconciliationError("external_lookup_positions_required")
        for row in positions:
            key = _id(row.get("positionId"))
            if key in observed:
                raise ExternalReconciliationError("external_duplicate_opening_position")
            if key not in added:
                blockers.add("lookup_position_not_external_candidate")
                continue
            opening, position = row.get("openingData", {}), current[key]
            asset = lookup.get("asset", {})
            if (_id(opening.get("orderId")) != order or row.get("state") != "open"
                    or _number(row.get("remainingUnits")) != position.units
                    or _number(opening.get("units")) != position.units
                    or asset.get("instrumentId") != position.instrument_id
                    or _number(asset.get("leverage")) != position.leverage
                    or asset.get("settlementType") != {0: "cfd", 1: "real"}[position.settlement_type]
                    or asset.get("side") != ("long" if position.is_buy else "short")
                    or not position.is_buy
                    or not (exact_datetime_epoch_seconds(inception_at) <= _source_time(opening.get("executionTime"))
                            <= exact_datetime_epoch_seconds(min(capture, received)))):
                blockers.add("external_opening_identity_units_or_time_mismatch")
                continue
            observed.add(key)
            if _number(row.get("investedAmountCurrency")) != position.principal:
                conflicts += 1
                blockers.add("lookup_invested_amount_conflicts_with_account_principal")
            if (_number(row.get("addedFunds")) != 0 or _number(opening.get("fees")) != position.fees
                    or _number(opening.get("taxes")) != position.taxes):
                blockers.add("external_opening_costs_or_added_funds_unreconciled")
    if observed != added:
        blockers.add("missing_external_opening_lookup")
    if added:
        blockers.add("original_opening_cash_debit_proof_not_admitted")
    carrying = [current[key].carrying_fees for key in added if current[key].carrying_fees != 0]
    if carrying:
        blockers.add("external_carrying_fee_cash_basis_unqualified")
    # Official eToro MCP history contract: account-currency amounts; netProfit
    # excludes fees. This is a known-event bridge, never completeness proof.
    proceeds = _exact_sum(_difference(_exact_sum((_number(r["investment"]), _number(r["netProfit"]))), _number(r["fees"]))
                          for r in closures.values())
    opening_debit = _exact_sum(_exact_sum((current[k].principal, current[k].fees, current[k].taxes)) for k in added)
    predicted = _difference(_exact_sum((_number(initial["account"]["cash"]), proceeds)), opening_debit)
    residual = _difference(account.cash, predicted)
    if residual:
        blockers.add("unexplained_account_cash_difference")
    for path, raw in retained.items():
        if (path.exists() if raw is None else _read(path) != raw):
            raise ExternalReconciliationError("external_source_changed_during_inspection")
    result = {"schema": SCHEMA, "status": "EXTERNAL_RECONCILIATION_BLOCKED", "apply_supported": False,
        "activation_ready": False, "migration_not_applied": True, "broker_requests": 0,
        "original_store_bytes_unchanged": True, "inception_hash": payload_hash(initial),
        "existing_submission_count": submissions, "existing_event_count": events,
        "historical_account_validation": "passed", "account_capture_at": capture.isoformat(),
        "current_account_fresh": now >= capture and now <= account.deadline,
        "baseline_positions": len(baseline), "unchanged_baseline_positions": unchanged,
        "known_baseline_closures_verified": len(closures), "missing_baseline_positions": len(missing),
        "external_position_candidates": len(added), "external_lookup_positions_matched": len(observed),
        "lookup_principal_conflicts": conflicts, "history_page_count": len(pages),
        "history_short_page_is_only_heuristic": True, "cash_event_coverage": "unknown",
        "currency": "USD", "history_amount_basis": "official_etoro_mcp_account_currency_net_profit_excludes_fees",
        "cash_observed": account.cash, "cash_known_event_prediction": predicted, "cash_residual": residual,
        "prospective_external_capital": opening_debit,
        "prospective_external_pnl": _exact_sum(_difference(current[k].pnl, current[k].fees, current[k].taxes) for k in added),
        "nonzero_external_carrying_fee_positions": len(carrying),
        "reported_external_carrying_fees": _exact_sum(carrying),
        "prospective_amounts_and_cash_bridge_exclude_unqualified_carrying_effects": True,
        "external_lookup_match_scope": "account_order_position_instrument_direction_leverage_settlement_units_source_time",
        "legacy_closure_profit_credited_to_pilot": False, "new_submission_claims": 0,
        "required_journal_phase": "external_import_without_synthetic_dispatch",
        "blockers": sorted(blockers), "manifest_sha256": _hash(retained[manifest]),
        "source_bytes_sha256": payload_hash(sorted((str(p), _hash(raw) if raw is not None else None) for p, raw in retained.items()))}
    return json.loads(canonical_json(result))
