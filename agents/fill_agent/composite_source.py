"""Retained Demo evidence for bounded historical net-cash reconciliation.

This adapter does not manufacture a cash transaction history. It combines a
broker History display, exact known closures, and independently bracketed
opening evidence. Screenshot transcription is an independently reviewed input,
bound with its original image bytes in the exact plan. Gross fee/dividend
enumeration and historical daily performance remain explicitly unqualified.
"""
from __future__ import annotations

import base64
from contextlib import closing
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from agents.fill_agent import external_reconciliation as retained
from agents.fill_agent import external_lifecycle as lifecycle
from agents.fill_agent.pilot_ledger import PilotLedger
from brokers.etoro.account_evidence import _exact_sum, _difference
from brokers.etoro.pilot_account import build_pilot_account
from brokers.etoro.pilot_reads import read_receipt_envelope
from execution.domain import canonical_json, payload_hash


SCHEMA = "composite-retained-evidence-v1"


def require(condition, code):
    if not condition:
        raise lifecycle.ExternalLifecycleError("composite_" + code)


def _one(rows, predicate, code):
    matches = [row for row in rows if predicate(row)]
    require(len(matches) == 1, code)
    return matches[0]


def _money(value):
    return retained._number(value)


def _mcp(receipt, name):
    require(receipt.get("request", {}).get("name") == name
            and receipt["request"].get("arguments", {}).get("account") == "demo",
            "original_demo_request_required")
    result = receipt.get("response", {})
    require(result.get("isError") is False and result.get("resultType") == "complete",
            "original_connector_response_incomplete")
    content = result.get("content", [])
    row = _one(content, lambda c: c.get("type") == "text", "single_connector_body_required")
    body = retained._json(row["text"])
    require(body.get("account") == "demo" and body.get("statusCode") == 200
            and body.get("accountCurrency") == "USD", "connector_account_currency")
    start, end = retained._instant(receipt["started_at"]), retained._instant(receipt["received_at"])
    require(0 <= (end-start).total_seconds() <= 10, "connector_receipt_interval")
    return body


class Sources:
    def __init__(self, manifest):
        self.path = Path(manifest)
        require(self.path.is_absolute() and self.path.resolve() == self.path, "canonical_manifest_path")
        self.raw = retained._read(self.path)
        self.data = retained._json(self.raw)
        require(self.data.get("schema") == SCHEMA, "manifest_schema")
        require(isinstance(self.data.get("files"), dict)
                and "composite-period-observation" not in self.data["files"], "reserved_source_reference")
        paths = [ref["path"] for ref in self.data["files"].values()]
        require(len(paths) == len(set(paths)), "duplicate_source_paths")
        self.root = self.path.parent
        self.seen = {self.path: self.raw}
        self.bodies = {}
        self.journal = {}

    def name(self, reference):
        if isinstance(reference, str):
            require(reference in self.data["files"], "unknown_source_reference")
            return reference
        require(isinstance(reference, dict) and set(reference) == {"path", "sha256"}, "source_reference_shape")
        names = [name for name, entry in self.data["files"].items()
                 if entry["path"] == reference["path"] and entry["sha256"] == reference["sha256"]]
        require(len(names) == 1, "unambiguous_source_reference")
        return names[0]

    def read(self, name):
        name = self.name(name)
        if name in self.bodies:
            return self.bodies[name]
        ref = self.data["files"][name]
        body = retained._source(self.root, {"path": ref["path"], "sha256": ref["sha256"]}, self.seen)
        require(len(body) == ref["size_bytes"], "source_size_mismatch")
        self.bodies[name] = body
        return body

    def json(self, name):
        return retained._json(self.read(name))

    def keep(self, name, *, selection=None):
        name = self.name(name)
        raw = self.read(name)
        if selection is not None:
            body = canonical_json({"original_sha256": retained._hash(raw), "selected_evidence": selection})
        elif self.data["files"][name]["path"].lower().endswith((".jpg", ".jpeg", ".png")):
            body = canonical_json({"encoding": "base64", "original_sha256": retained._hash(raw),
                                   "bytes": len(raw), "image": base64.b64encode(raw).decode("ascii")})
        else:
            body = raw.decode("utf-8")
        self.journal[name] = {"reference": name, "body": body,
                              "sha256": retained._hash(body.encode())}
        return name

    def unchanged(self):
        for path, raw in self.seen.items():
            require((not path.exists()) if raw is None else retained._read(path) == raw,
                    "source_changed_during_plan")


def _account(sources, group, account_id):
    index = sources.json(group["relocated_index"])
    require(set(index) == {"profile", "first", "pnl", "last"}, "complete_account_bracket")
    original = sources.json(group["original_index"])
    for key, envelope in index.items():
        old = deepcopy(original[key])
        # Relocation may change only the body-file pointer, never source bytes,
        # capture timestamps or any result field.
        old["raw_response"]["body_file"] = envelope["raw_response"]["body_file"]
        require(old == envelope, "relocated_receipt_changed")
        read_receipt_envelope(envelope)
        body = Path(envelope["raw_response"]["body_file"])
        require(body.is_absolute() and body.resolve() == body and body.is_relative_to(sources.root),
                "account_raw_path_escape")
        raw = retained._read(body)
        require(retained._hash(raw) == envelope["raw_response"]["body_sha256"]
                and raw.decode() == envelope["result"]["body"], "account_raw_mismatch")
        retained._json(raw)
        sources.seen[body] = raw
    capture = retained._instant(index["last"]["response_received_at"])
    account = build_pilot_account(index["profile"], index["first"], index["pnl"], index["last"], account_id, capture)
    sources.keep(group["original_index"])
    sources.keep(group["relocated_index"])
    return account


def _original_state(runtime, sources):
    root = Path(runtime)
    require(root.is_absolute() and root.resolve() == root, "canonical_runtime_path")
    initial, count, _ = retained._original(root, sources.seen)
    require(count == 0, "first_composite_import_requires_no_submissions")
    ledger = object.__new__(PilotLedger)
    ledger.path, ledger.expected_account_id = root / "pilot.sqlite3", initial["account_id"]
    with closing(sqlite3.connect(":memory:")) as db:
        db.deserialize(sources.seen[ledger.path])
        db.execute("PRAGMA query_only=ON")
        state = ledger._state(db)
    require(not state["unresolved"] and not state["positions"] and not state["_external_imports"],
            "original_state_not_initial_reconciliation")
    return ledger, state


def _history(sources, bundles, initial, account, now):
    baseline = {p["position_id"]: p for p in initial["account"]["positions"]}
    require(len(baseline) == 8 and len(account.positions) == 1, "bounded_eight_closures_one_opening")
    rows, page_size = {}, None
    for page, bundle in enumerate(bundles, 1):
        refs = {k: {"path": sources.data["files"][sources.name(v)]["path"],
                    "sha256": sources.data["files"][sources.name(v)]["sha256"]} for k, v in bundle.items()}
        request, body, received = retained._response(sources.root, refs, sources.seen, retained.HISTORY_PATH)
        query = request.get("query", {})
        require(set(query) == {"minDate", "page", "pageSize"} and type(query["page"]) is int
                and query["page"] == page and type(query["pageSize"]) is int
                and 1 <= query["pageSize"] <= 200 and (page_size is None or page_size == query["pageSize"]),
                "history_pagination")
        require(date.fromisoformat(query["minDate"]) <= retained._instant(initial["inception"]).date()
                and received <= now and isinstance(body, list) and len(body) <= query["pageSize"],
                "history_period")
        page_size = query["pageSize"]
        for row in body:
            key = retained._id(row.get("positionId"))
            require(key in baseline and key not in rows, "unknown_or_duplicate_closure")
            old = baseline[key]
            require(row.get("instrumentId") == old["instrument_id"] and type(row.get("isBuy")) is bool
                    and row["isBuy"] == old["is_buy"]
                    and all(_money(row[a]) == _money(old[b]) for a, b in
                            (("units", "units"), ("investment", "principal"), ("leverage", "leverage"))),
                    "closure_identity_economics")
            # This source combination qualifies zero API fee rows plus a
            # separate UI net deduction; nonzero API fees need another contract
            # to avoid deducting the same fee twice.
            require(_money(row["fees"]) == 0, "api_fee_overlap_requires_review")
            require(retained._source_time(initial["inception"]) <= retained._source_time(row["closeTimestamp"])
                    <= min(retained._source_time(account.capture_started_at.isoformat()),
                           retained._source_time(received.isoformat())), "closure_time")
            rows[key] = row
        for name in bundle.values():
            sources.keep(name)
    require(set(rows) == set(baseline), "all_eight_baseline_closures_required")
    return rows


def _opening(sources, group, initial, account, first_closed):
    execution = sources.json(group["execution"])
    require(execution.get("dispatch_count") == 1, "original_single_dispatch")
    require(execution["preview"]["account_id"] == initial["account_id"], "original_fill_account")
    before = _one(execution["outcomes"], lambda r: r.get("phase") == "approved_pre_send_baseline", "original_pre_send")
    placed = _one(execution["outcomes"], lambda r: r.get("phase") == "place_response", "original_place_response")
    require(sources.json(group["original_pre_send"]) == before
            and sources.json(group["original_place_response"]) == placed,
            "original_connector_outcomes_disagree")
    # Exports are navigational conveniences. Re-read the consumed original
    # connector store and its row digests; never qualify mutable copies alone.
    connector = Path(sources.data["original_runtime"]["path"]) / "connector.sqlite3"
    declared = sources.data["original_runtime"]["files"]["connector.sqlite3"]
    raw_connector = retained._read(connector)
    require(declared["path"] == str(connector) and retained._hash(raw_connector) == declared["sha256"]
            and len(raw_connector) == declared["size_bytes"], "original_connector_bytes_changed")
    sources.seen[connector] = raw_connector
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(connector) + suffix)
        raw = retained._read(sidecar) if sidecar.exists() else None
        require(not raw, "connector_uncheckpointed_state")
        sources.seen[sidecar] = raw
    with closing(sqlite3.connect(":memory:")) as db:
        db.deserialize(raw_connector)
        db.execute("PRAGMA query_only=ON")
        require(db.execute("PRAGMA quick_check").fetchone() == ("ok",), "connector_integrity")
        command = execution["command_id"]
        require(db.execute("SELECT COUNT(*) FROM dispatches WHERE command_id=?", (command,)).fetchone() == (1,),
                "original_consumed_dispatch_required")
        preview_row = db.execute("SELECT payload,digest FROM previews WHERE command_id=?", (command,)).fetchone()
        require(preview_row is not None and payload_hash(retained._json(preview_row[0])) == preview_row[1]
                and retained._json(preview_row[0]) == execution["preview"], "original_preview_binding")
        outcomes = []
        for payload, digest in db.execute("SELECT payload,digest FROM outcomes WHERE command_id=? ORDER BY sequence", (command,)):
            value = retained._json(payload)
            require(payload_hash(value) == digest, "original_outcome_digest")
            outcomes.append(value)
        require(_one(outcomes, lambda r: r.get("phase") == "approved_pre_send_baseline", "connector_pre_send") == before
                and _one(outcomes, lambda r: r.get("phase") == "place_response", "connector_place") == placed,
                "original_connector_phase_binding")
    fill = placed["response"]
    require(fill.get("account") == "demo" and fill.get("statusCode") == 200
            and fill.get("outcome") in {"filled", "partiallyFilled"} and len(fill.get("positions", [])) == 1,
            "original_fill_terminal_scope")
    p = fill["positions"][0]
    present = account.positions[0]
    key, order = retained._id(p["positionId"]), retained._id(fill["orderId"])
    require(key == present.position_id and key not in {p["position_id"] for p in initial["account"]["positions"]}
            and execution["preview"]["instrument_id"] == present.instrument_id
            and _money(p["units"]) == present.units and _money(p["investedAmount"]) == present.principal,
            "original_fill_identity_principal")
    require(present.is_buy and present.leverage == 1 and present.settlement_type == 1
            and present.fees == present.taxes == present.carrying_fees == 0
            and all(_money(p[k]) == 0 for k in ("fees", "taxes", "marketSpread", "markup"))
            and _money(fill["totalCosts"]) == 0, "opening_costs_or_real_long_scope")
    configs = before["eligibility"]["leverageConfigs"]
    require(before["eligibility"]["instrumentId"] == present.instrument_id
            and any(c["direction"] == "long" and c["settlementType"] == "real"
                    and 1 in c["leverageValues"] for c in configs), "original_real_settlement_required")
    baseline = {p["position_id"]: p for p in initial["account"]["positions"]}
    old_positions = {str(p["position_id"]): p for p in before["positions"]}
    require(len(old_positions) == len(before["positions"]) and set(old_positions) == set(baseline), "opening_before_composition")
    for identity, old in baseline.items():
        row = old_positions[identity]
        require(row["instrument_id"] == old["instrument_id"] and row["direction"] == "long"
                and _money(row["invested"]) == _money(old["principal"])
                and _money(row["units"]) == _money(old["units"])
                and _money(row["leverage"]) == _money(old["leverage"]), "opening_before_economics")
    require(_money(before["summary_totals"]["balance"]) == _money(initial["account"]["cash"])
            and _money(before["summary_totals"]["frozenCash"]) == 0, "preopening_cash_changed")
    post_receipt = sources.json(group["post_summary"])
    summary = _mcp(post_receipt, "get-my-portfolio-summary")
    positions_receipt = sources.json(group["post_positions"])
    positions = _mcp(positions_receipt, "get-my-positions-and-orders")
    require(summary.get("pendingOrders") == [] and positions.get("mirrors") == []
            and positions["direct"].get("orders") == [], "opening_pending_or_mirrored")
    after = {str(r["positionId"]): r for r in positions["direct"]["positions"]}
    require(len(after) == len(positions["direct"]["positions"]) and set(after) == set(baseline) | {key},
            "opening_after_composition")
    require(type(positions["direct"].get("positionCount")) is int
            and positions["direct"]["positionCount"] == len(after), "opening_position_count")
    for identity, old in baseline.items():
        row = after[identity]
        require(row["market"]["instrumentId"] == old["instrument_id"] and row["direction"] == "long"
                and _money(row["invested"]) == _money(old["principal"])
                and _money(row["units"]) == _money(old["units"])
                and _money(row["leverage"]) == _money(old["leverage"]), "opening_after_baseline_changed")
    row = after[key]
    require(row["market"]["instrumentId"] == present.instrument_id and row["direction"] == "long"
            and _money(row["units"]) == present.units and _money(row["invested"]) == present.principal
            and _money(row["leverage"]) == 1 and _money(row["openRate"]) == _money(p["avgPrice"]),
            "independent_opening_readback")
    debit = _difference(_money(before["summary_totals"]["balance"]), _money(summary["totals"]["balance"]))
    require(debit == present.principal and _money(summary["totals"]["frozenCash"]) == 0
            and _money(summary["totals"]["availableCash"]) == _money(summary["totals"]["balance"]),
            "independent_opening_cash_debit")
    execution_time = retained._source_time(p["executionTime"])
    lower = post_receipt["received_at"]
    require(retained._source_time(before["positions_source_time"]) <= execution_time
            <= retained._source_time(positions["timestamp"]) <= retained._source_time(lower)
            < first_closed and retained._instant(placed["at"]) <= retained._instant(post_receipt["started_at"]),
            "opening_temporal_bracket")
    selected = {"dispatch_count": execution["dispatch_count"],
                "account_id": initial["account_id"], "instrument_id": present.instrument_id,
                "pre_send": before, "place_response": placed}
    source = sources.keep(group["execution"], selection=selected)
    sources.keep(group["post_summary"])
    sources.keep(group["post_positions"])
    opening = {"position_id": key, "order_id": order, "account_currency": "USD",
               "occurred_at_source": p["executionTime"], "source_reference": source,
               "cash_debit": str(debit), "quote_currency": "GBX", "quote_price": str(p["avgPrice"]),
               "order_type": "limit", "time_in_force": "IOC"}
    return opening, lower, summary


def _detailed_opening_binding(sources, account_groups, opening):
    for group in account_groups.values():
        index = sources.json(group["relocated_index"])
        for envelope in (index["first"], index["pnl"], index["last"]):
            body = retained._json(envelope["result"]["body"])["clientPortfolio"]
            row = _one(body["positions"], lambda p: retained._id(p["positionID"]) == opening["position_id"],
                       "detailed_single_opening")
            require(retained._id(row["orderID"]) == opening["order_id"]
                    and retained._source_time(row["openDateTime"]) == retained._source_time(opening["occurred_at_source"])
                    and _money(row["openRate"]) == _money(opening["quote_price"])
                    and row["orderType"] == 17, "detailed_original_opening_binding")


def _lookup_conflicts(sources, group, initial, opening, account, now):
    refs = {key: {"path": sources.data["files"][sources.name(ref)]["path"],
                  "sha256": sources.data["files"][sources.name(ref)]["sha256"]}
            for key, ref in group.items()}
    request, lookup, received = retained._response(sources.root, refs, sources.seen, retained.LOOKUP_PATH)
    require(set(request["query"]) == {"orderId"}
            and retained._id(request["query"]["orderId"]) == opening["order_id"]
            and retained._id(lookup["orderId"]) == opening["order_id"]
            and retained._id(lookup["accountId"]) == initial["account_id"] and received <= now,
            "lookup_binding")
    require(lookup["status"]["id"] in {3, 9, 10}
            and lookup["status"]["name"] == {3:"Filled", 9:"CanceledPartiallyFilled", 10:"RejectedPartiallyFilled"}[lookup["status"]["id"]]
            and lookup["action"] == "open" and lookup["transaction"] == "buy"
            and lookup["type"] == "limitIOC" and lookup["asset"]["currency"] == "GBX"
            and lookup["orderCurrency"] in {"usd", "USD"}, "lookup_terminal_scope")
    require(lookup["asset"]["settlementType"] in {"real", "REAL"}, "new_settlement_contradiction")
    require(len(lookup["positionExecutions"]) == 1, "lookup_single_position")
    row = lookup["positionExecutions"][0]
    position = account.positions[0]
    opened = row["openingData"]
    require(retained._id(row["positionId"]) == opening["position_id"] and row["state"] == "open"
            and retained._id(opened["orderId"]) == opening["order_id"]
            and _money(row["remainingUnits"]) == _money(opened["units"]) == position.units
            and lookup["asset"]["instrumentId"] == position.instrument_id
            and lookup["asset"]["side"] == "long" and _money(lookup["asset"]["leverage"]) == 1
            and retained._source_time(opened["executionTime"]) == retained._source_time(opening["occurred_at_source"])
            and _money(opened["avgPrice"]) == _money(opening["quote_price"])
            and all(_money(opened[k]) == 0 for k in ("fees", "taxes", "marketSpread", "markup"))
            and _money(row["addedFunds"]) == 0 and _money(lookup["totalCosts"]) == 0,
            "lookup_identity_or_unexplained_economic_change")
    require(_money(row["marginAccountCurrency"]) == position.principal
            and _money(row["initialExposureAccountCurrency"])
                == _money(opened["avgPrice"]) * position.units * _money(opened["avgConversionRate"]),
            "new_lookup_economic_contradiction")
    for ref in group.values():
        sources.keep(ref)
    return {"principal": {"original_fill_and_independent_cash_debit": str(position.principal),
                          "later_investedAmountCurrency": str(_money(row["investedAmountCurrency"])),
                          "conflicting": _money(row["investedAmountCurrency"]) != position.principal,
                          "selected_basis": "original_fill_independent_debit_and_detailed_account",
                          "margin_substituted": False},
            "settlement": {"original_eligibility": "real", "detailed_account_id": position.settlement_type,
                           "later_lookup_raw": lookup["asset"]["settlementType"],
                           "later_value_used_for_admission": False}}


def _ui(sources, groups, initial, account, history, summary):
    observation = sources.json(groups["extraction"])
    require(observation.get("schema") == "composite-broker-ui-observations-v1", "ui_observation_schema")
    require(observation.get("image_capture_time") is None, "ui_capture_time_not_established")
    header = observation["header"]
    require(header["account_mode"] == "Virtual Portfolio / Demo" and header["currency_symbol"] == "$"
            and type(header["closed_count"]) is int and header["closed_count"] == len(history),
            "ui_account_and_closure_count")
    require(header["tooltip_verbatim"] == "The end value is calculated by adding the start value, money in, subtracting the money out and total fees, and including the PNL and dividends.",
            "ui_net_effect_semantics")
    require(sources.name(header["source"]) == sources.name(groups["screenshots"]["tooltip"])
            and sources.name(header["per_symbol_pnl_source"]) == sources.name(groups["screenshots"]["history"]),
            "ui_header_image_binding")
    for reference in groups["screenshots"].values():
        sources.keep(reference)
    symbol_by_instrument = {r["market"]["instrumentId"]: r["market"]["symbol"] for r in summary["holdings"]}
    require(len(symbol_by_instrument) == len(summary["holdings"]), "duplicate_summary_instrument")
    mapped = {symbol_by_instrument[r["instrumentId"]]: r for r in history.values()}
    require(len(mapped) == len(history) and set(mapped) == set(header["per_symbol_pnl"])
            and all(_money(header["per_symbol_pnl"][symbol]) == _money(row["netProfit"])
                    for symbol, row in mapped.items()), "ui_closure_profit_mismatch")
    require(_money(header["pnl"]) == _exact_sum(_money(r["netProfit"]) for r in history.values())
            and _money(header["money_in"]) == _money(header["money_out"]) == 0,
            "ui_profit_or_external_money_change")
    initial_book = _exact_sum((_money(initial["account"]["cash"]),
                              *(_money(p["principal"]) for p in initial["account"]["positions"])))
    require(_money(header["start_value"]) == initial_book
            and _money(header["end_value"]) == _exact_sum((account.cash, account.positions[0].principal)),
            "ui_book_value_brackets")
    start, end = date.fromisoformat(header["period_start"]), date.fromisoformat(header["period_end"])
    require(start <= retained._instant(initial["inception"]).date() <= account.capture_completed_at.date() <= end
            and (end-start).days <= 31, "ui_reporting_period")
    scope = observation["coverage_scope"]
    require(scope["all_individual_cash_movements_enumerated"] is False
            and scope["exact_fee_posting_times_known"] is False
            and scope["gross_fees_and_dividends_separately_known"] is False
            and scope["gross_fees"] is None and scope["gross_dividends"] is None,
            "ui_overstates_gross_coverage")
    require(observation["market_column"]["column_heading_visible"] is False,
            "obscured_fee_heading_must_remain_unknown")
    require(set(observation["market_column"]["signed_amounts"]) == set(mapped) - {"ETH", "RR.L"},
            "visible_fee_rows_do_not_match_history")
    require(set(observation["explicit_fee_details"]) == {"ETH", "RR.L"}, "explicit_fee_scope")
    for symbol, detail in observation["explicit_fee_details"].items():
        row = mapped[symbol]
        require(detail["label"] == "Total Fees" and detail["fee_posting_time"] is None
                and detail["displayed_timezone"] is None
                and _money(detail["units"]) == _money(row["units"])
                and _money(detail["investment"]) == _money(row["investment"])
                and _money(detail["profit_loss"]) == _money(row["netProfit"])
                and _money(detail["opening_price"]) == _money(row["openRate"])
                and _money(detail["closing_price"]) == _money(row["closeRate"])
                and _money(detail["end_value"]) == _exact_sum((_money(row["investment"]), _money(row["netProfit"]))),
                "explicit_fee_detail_mismatch")
        require(sources.name(detail["source"]) == sources.name(groups["screenshots"]["eth_fees" if symbol == "ETH" else "rrl_fees"]),
                "explicit_fee_image_binding")
    sources.keep(groups["extraction"])
    # Derived from independent broker display semantics, not a plug calculated
    # from the cash residual. The latter is checked separately by ledger replay.
    amount = _difference(_money(header["end_value"]), _money(header["start_value"]), _money(header["pnl"]))
    observed_columns = _exact_sum((_money(d["signed_amount"]) for d in observation["explicit_fee_details"].values()))
    observed_columns = _exact_sum((observed_columns, *(_money(v) for v in observation["market_column"]["signed_amounts"].values())))
    require(observed_columns == amount, "visible_net_values_disagree")
    return amount, header


@lifecycle._exact_context
def build_composite_plan(*, runtime, manifest, reviewed_commit, review_reference, now):
    """Rebuild an exact historical plan without opening original SQLite stores.

    The manifest and image transcription must be independently reviewed at the
    returned digest before application. This is deliberately a narrow adapter
    for eight old closures and one original, zero-cost, real-long IOC opening.
    """
    now = retained._instant(now.isoformat())
    sources = Sources(manifest)
    require(sources.data["original_runtime"]["path"] == str(runtime), "original_runtime_binding")
    ledger, state = _original_state(runtime, sources)
    initial = state["inception"]
    require(sources.data["original_runtime"]["inception_hash"] == payload_hash(initial), "inception_digest")
    groups = sources.data["groups"]
    earliest = _account(sources, groups["accounts"]["oct29"], initial["account_id"])
    latest = _account(sources, groups["accounts"]["nov01"], initial["account_id"])
    require(earliest.capture_completed_at < latest.capture_started_at <= latest.capture_completed_at <= now,
            "post_account_capture_order")
    require(earliest.cash == latest.cash and len(earliest.positions) == len(latest.positions) == 1,
            "post_period_cash_or_composition_changed")
    from agents.fill_agent.pilot_ledger import _same_holdings
    require(_same_holdings(lifecycle._clean(earliest.positions[0]), lifecycle._clean(latest.positions[0])),
            "post_period_opening_economics_changed")
    rows = _history(sources, [groups["history"]], initial, earliest, now)
    opening, lower, summary = _opening(sources, groups["hsbc"], initial, latest,
                                      min(retained._source_time(r["closeTimestamp"]) for r in rows.values()))
    _detailed_opening_binding(sources, groups["accounts"], opening)
    conflicts = _lookup_conflicts(sources, groups["hsbc"]["later_lookup"], initial, opening, latest, now)
    amount, header = _ui(sources, groups, initial, latest, rows, summary)
    # The earliest independent retained cash receipt bounds the net effect.
    # Its receipt time is an observation bound, never a fabricated fee timestamp.
    upper = earliest.capture_completed_at.isoformat()
    aggregate_source = "composite-period-observation"
    require(aggregate_source not in sources.journal, "derived_source_reference_collision")
    observation = {"kind": "derived_composite_period_observation",
                   "source_manifest_sha256": retained._hash(sources.raw),
                   "image_extraction_reference": sources.name(groups["extraction"]),
                   "images": {key: sources.data["files"][sources.name(ref)]["sha256"]
                              for key, ref in groups["screenshots"].items()},
                   "cash_effect_basis": "independent_ui_header_formula_and_retained_cash_brackets",
                   "cash_effect": str(amount), "lookup_adjudication": conflicts,
                   "coverage": "net_cash_effects_only_gross_event_enumeration_unknown",
                   "historical_daily_performance_qualified": False}
    body = canonical_json(observation)
    sources.journal[aggregate_source] = {"reference": aggregate_source, "body": body,
                                         "sha256": retained._hash(body.encode())}
    sources.keep(groups["history_contract"])
    baseline = {p["position_id"]: p for p in initial["account"]["positions"]}
    closures = tuple({"position_id": key, "original_position": baseline[key],
                      "cash_proceeds": str(_exact_sum((_money(row["investment"]), _money(row["netProfit"])))),
                      "account_currency": "USD", "occurred_at_source": row["closeTimestamp"],
                      "source_reference": sources.name(groups["history"]["body"])} for key, row in sorted(rows.items()))
    period = {"kind": "net_fees_less_dividends", "amount": str(amount), "account_currency": "USD",
              "effect_after_observation": lower, "effect_by_observation": upper,
              "reporting_start_date": header["period_start"], "reporting_end_date": header["period_end"],
              "reporting_timezone": None, "gross_fees": None, "gross_dividends": None,
              "enumeration_complete": False, "individual_attribution": "unknown",
              "baseline_position_ids": sorted(baseline), "source_reference": aggregate_source}
    proof = lifecycle._QualifiedLifecycle(latest, payload_hash(state["_account_checkpoint"]), closures,
                (opening,), (), tuple(sources.journal.values()), aggregate_source, period)
    record = lifecycle._record(ledger, state, proof, reviewed_commit=reviewed_commit, review_reference=review_reference)
    effects = lifecycle.validate_record(record, inception=initial, prior_account=state["_account_checkpoint"],
                                        retired=set(), attributed={})
    sources.unchanged()
    report = {"status": "COMPOSITE_NET_RECONCILIATION_PLANNED", "schema": lifecycle.COMPOSITE_SCHEMA,
              "manifest_sha256": retained._hash(sources.raw), "plan_sha256": payload_hash(record),
              "source_count": len(proof.sources), "closure_count": len(closures), "opening_count": 1,
              "gross_closure_proceeds": str(_exact_sum(_money(r["cash_proceeds"]) for r in closures)),
              "opening_cash_debit": opening["cash_debit"], "period_net_cash_effect": str(amount),
              "post_cash": str(latest.cash), "cash_residual": "0", "cash_event_coverage": "net_effects_only",
              "gross_fees": None, "gross_dividends": None, "individual_posting_times": None,
              "net_effect_after_observation": lower, "net_effect_by_observation": upper,
              "pilot_pnl": str(effects["pilot_pnl"]), "legacy_profit_credited_to_pilot": False,
              "historical_daily_performance_qualified": False, "lookup_adjudication": conflicts,
              "current_account_fresh": latest.capture_completed_at <= now <= latest.deadline,
              "trading_ready": False, "original_bytes_unchanged": True, "applied": False}
    return proof, record, report

