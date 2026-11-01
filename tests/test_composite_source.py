"""Synthetic retained-source admission; no private receipts or live accounts."""
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from agents.fill_agent import composite_source as adapter
from agents.fill_agent import external_lifecycle as lifecycle
from agents.fill_agent.external_reconciliation import ExternalReconciliationError
from agents.fill_agent.pilot_ledger import PilotLedger
from execution.domain import canonical_json, payload_hash
from execution.operator import OperatorEvidenceStore
from execution.store import ExecutionStore
from tests.test_external_reconciliation_admission import bundle, direct_account, save, bytes_under
from tests.test_pilot_ledger import Account, Position, NOW

D = Decimal
COMMIT = "b" * 40
OPEN_TIME = (NOW + timedelta(seconds=2)).isoformat()
SYMBOLS = ["ETH", "RR.L", "SYN3", "SYN4", "SYN5", "SYN6", "SYN7", "SYN8"]
TOOLTIP = ("The end value is calculated by adding the start value, money in, subtracting "
           "the money out and total fees, and including the PNL and dividends.")


def register(f, name, value, *, raw=False, suffix=".json"):
    path = f.root / (name + suffix)
    save(path, value, raw=raw)
    data = path.read_bytes()
    f.manifest["files"][name] = {"path": path.relative_to(f.root).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    return name


def stored(f, name):
    return json.loads((f.root / f.manifest["files"][name]["path"]).read_bytes())


def rewrite(f, name, value):
    suffix = (f.root / f.manifest["files"][name]["path"]).suffix
    register(f, name, value, suffix=suffix)


def register_bundle(f, name, route, query, value):
    group = bundle(f.root, name, route, query, value)
    names = {}
    for part, ref in group.items():
        key = name + "-" + part
        raw = (f.root / ref["path"]).read_bytes()
        f.manifest["files"][key] = {**ref, "size_bytes": len(raw)}
        names[part] = key
    return names


def mcp(name, body, *, start=2.5, end=3):
    return {"request": {"name": name, "arguments": {"account": "demo"}},
        "started_at": (NOW + timedelta(seconds=start)).isoformat(),
        "received_at": (NOW + timedelta(seconds=end)).isoformat(),
        "response": {"isError": False, "resultType": "complete", "content": [
            {"type": "text", "text": canonical_json({"account": "demo", "statusCode": 200,
                "accountCurrency": "USD", **body})}]}}


def change_mcp(f, name, edit):
    receipt = stored(f, name)
    value = json.loads(receipt["response"]["content"][0]["text"])
    edit(value)
    receipt["response"]["content"][0]["text"] = canonical_json(value)
    rewrite(f, name, receipt)


def connector_store(f, execution):
    path = f.runtime / "connector.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS previews(command_id TEXT PRIMARY KEY,payload TEXT,digest TEXT);
            CREATE TABLE IF NOT EXISTS dispatches(command_id TEXT PRIMARY KEY,claimed_at TEXT);
            CREATE TABLE IF NOT EXISTS outcomes(sequence INTEGER PRIMARY KEY,command_id TEXT,payload TEXT,digest TEXT);
            DELETE FROM previews; DELETE FROM dispatches; DELETE FROM outcomes;
        """)
        command = execution["command_id"]
        db.execute("INSERT INTO previews VALUES (?,?,?)", (command,
            canonical_json(execution["preview"]), payload_hash(execution["preview"])))
        db.execute("INSERT INTO dispatches VALUES (?,?)", (command, OPEN_TIME))
        for index, row in enumerate(execution["outcomes"], 1):
            db.execute("INSERT INTO outcomes VALUES (?,?,?,?)", (index, command, canonical_json(row), payload_hash(row)))
    path.chmod(0o600)
    raw = path.read_bytes()
    f.manifest["original_runtime"]["files"]["connector.sqlite3"] = {
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


def execution_sources(f, execution):
    register(f, "execution", execution)
    register(f, "pre-send", execution["outcomes"][0])
    register(f, "placed", execution["outcomes"][1])
    connector_store(f, execution)


def account_sources(f, name, account):
    directory = f.root / name
    direct_account(directory, account)
    index = json.loads((directory / "account.json").read_bytes())
    for key in ("first", "pnl", "last"):
        envelope = index[key]
        body = json.loads(envelope["result"]["body"])
        body["clientPortfolio"]["positions"][0].update(orderID=500, openDateTime=OPEN_TIME,
            openRate="888.8", orderType=17)
        raw = canonical_json(body).encode()
        save(directory / (key + ".bin"), raw, raw=True)
        envelope["result"]["body"] = raw.decode()
        envelope["raw_response"].update(body_sha256=hashlib.sha256(raw).hexdigest(), body_bytes=len(raw))
    return {"original_index": register(f, name + "-original", index),
            "relocated_index": register(f, name + "-relocated", index)}


def edit_account_rows(f, name, edit):
    index = stored(f, name + "-original")
    for key in ("first", "pnl", "last"):
        envelope = index[key]
        body = json.loads(envelope["result"]["body"])
        edit(body["clientPortfolio"]["positions"][0])
        raw = canonical_json(body).encode()
        save(f.root / name / (key + ".bin"), raw, raw=True)
        envelope["result"]["body"] = raw.decode()
        envelope["raw_response"].update(body_sha256=hashlib.sha256(raw).hexdigest(), body_bytes=len(raw))
    rewrite(f, name + "-original", index)
    rewrite(f, name + "-relocated", index)


@pytest.fixture
def composite(tmp_path):
    runtime, root = tmp_path / "runtime", tmp_path / "evidence"
    runtime.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    legacy = tuple(Position(position_id=str(i), instrument_id=9+i) for i in range(1, 9))
    initial = Account(positions=legacy)
    ledger = PilotLedger.create(runtime / "pilot.sqlite3", initial, [], NOW)
    ExecutionStore(runtime / "execution.sqlite3")
    OperatorEvidenceStore(runtime / "operator.sqlite3")
    for path in runtime.glob("*.sqlite3"):
        path.chmod(0o600)
    inception = ledger.snapshot()["inception"]
    save(runtime / "pilot-inception.json", {"policy": "demo-pilot-v1", "account_id": "123",
        "inception_hash": payload_hash(inception)})
    manifest = {"schema": adapter.SCHEMA, "original_runtime": {"path": str(runtime),
        "inception_hash": payload_hash(inception), "files": {}}, "files": {}, "groups": {}}
    f = SimpleNamespace(runtime=runtime, root=root, ledger=ledger, manifest=manifest,
        path=root / "manifest.json", now=NOW + timedelta(days=1))
    position = Position(position_id="101", instrument_id=42, units=D("0.712345"),
        principal=D("12.34"), pnl=D("0.27"), exposure=D("12.61"))
    accounts = {name: account_sources(f, name, Account(cash=D("18014.25"), positions=(position,),
        now=NOW + timedelta(seconds=offset))) for name, offset in (("oct29", 10), ("nov01", 20))}
    history = [{"positionId": i, "instrumentId": 9+i, "isBuy": True, "leverage": 1,
        "units": "10", "investment": "1000", "netProfit": "8", "fees": "0", "openRate": "100", "closeRate": "100.8",
        "closeTimestamp": (NOW + timedelta(seconds=4)).isoformat()} for i in range(1, 9)]
    history_group = register_bundle(f, "history", "/api/v1/trading/info/trade/demo/history",
        {"minDate": NOW.date().isoformat(), "page": 1, "pageSize": 100}, history)
    before = {"phase": "approved_pre_send_baseline", "at": (NOW+timedelta(seconds=1)).isoformat(),
        "positions_source_time": (NOW+timedelta(seconds=1)).isoformat(),
        "summary_totals": {"balance": "10000", "frozenCash": "0"},
        "positions": [{"position_id": str(i), "instrument_id": 9+i, "direction": "long",
            "units": "10", "invested": "1000", "leverage": "1"} for i in range(1, 9)],
        "eligibility": {"instrumentId": 42, "leverageConfigs": [
            {"direction": "long", "settlementType": "real", "leverageValues": [1]}]}}
    fill_position = {"positionId": 101, "units": "0.712345", "investedAmount": "12.34",
        "avgPrice": "888.8", "executionTime": OPEN_TIME,
        "fees": "0", "taxes": "0", "marketSpread": "0", "markup": "0"}
    placed = {"phase": "place_response", "at": (NOW+timedelta(seconds=2.1)).isoformat(),
        "response": {"account": "demo", "statusCode": 200, "outcome": "partiallyFilled",
            "orderId": 500, "totalCosts": "0", "positions": [fill_position]}}
    execution = {"command_id": "synthetic-original-ioc", "dispatch_count": 1,
        "preview": {"account_id": "123", "instrument_id": 42}, "outcomes": [before, placed]}
    execution_sources(f, execution)
    holdings = [{"market": {"instrumentId": 9+i, "symbol": SYMBOLS[i-1]}} for i in range(1, 9)]
    summary = mcp("get-my-portfolio-summary", {"pendingOrders": [], "holdings": holdings,
        "totals": {"balance": "9987.66", "availableCash": "9987.66", "frozenCash": "0"}})
    post_positions = [{"positionId": i, "market": {"instrumentId": 9+i}, "direction": "long",
        "invested": "1000", "units": "10", "leverage": "1"} for i in range(1, 9)]
    post_positions.append({"positionId": 101, "market": {"instrumentId": 42}, "direction": "long",
        "invested": "12.34", "units": "0.712345", "leverage": "1", "openRate": "888.8"})
    positions = mcp("get-my-positions-and-orders", {"mirrors": [],
        "timestamp": (NOW+timedelta(seconds=2.5)).isoformat(),
        "direct": {"orders": [], "positionCount": 9, "positions": post_positions}})
    register(f, "post-summary", summary)
    register(f, "post-positions", positions)
    lookup = {"accountId": 123, "orderId": 500, "action": "open", "transaction": "buy",
        "type": "limitIOC", "orderCurrency": "usd", "totalCosts": "0",
        "asset": {"instrumentId": 42, "currency": "GBX", "side": "long", "leverage": 1,
            "settlementType": "REAL"}, "status": {"id": 9, "name": "CanceledPartiallyFilled"},
        "positionExecutions": [{"positionId": 101, "state": "open", "remainingUnits": "0.712345",
            "investedAmountCurrency": "0.71", "addedFunds": "0", "marginAccountCurrency": "12.34",
            "initialExposureAccountCurrency": str(D("888.8") * D("0.712345") * D("0.01949")),
            "openingData": {**{k: v for k, v in fill_position.items() if k not in {"positionId", "investedAmount"}},
                "orderId": 500, "avgConversionRate": "0.01949"}}]}
    lookup_group = register_bundle(f, "lookup", "/api/v2/trading/info/demo/orders:lookup", {"orderId": "500"}, lookup)
    screenshots = {name: register(f, name, ("synthetic-image-only-"+name).encode(), raw=True, suffix=".png")
                   for name in ("history", "tooltip", "eth_fees", "rrl_fees")}
    observation = {"schema": "composite-broker-ui-observations-v1", "image_capture_time": None,
        "header": {"account_mode": "Virtual Portfolio / Demo", "currency_symbol": "$", "closed_count": 8,
            "tooltip_verbatim": TOOLTIP, "source": "tooltip", "per_symbol_pnl_source": "history",
            "per_symbol_pnl": {symbol: "8" for symbol in SYMBOLS}, "pnl": "64", "money_in": "0", "money_out": "0",
            "start_value": "18000", "end_value": "18026.59", "period_start": "2026-10-01", "period_end": "2026-10-31"},
        "coverage_scope": {"all_individual_cash_movements_enumerated": False, "exact_fee_posting_times_known": False,
            "gross_fees_and_dividends_separately_known": False, "gross_fees": None, "gross_dividends": None},
        "market_column": {"column_heading_visible": False, "signed_amounts": {
            "SYN3": "-10", "SYN4": "-9", "SYN5": "-6.07", "SYN6": "0", "SYN7": "0", "SYN8": "0"}},
        "explicit_fee_details": {symbol: {"label": "Total Fees", "fee_posting_time": None,
            "displayed_timezone": None, "units": "10", "investment": "1000", "profit_loss": "8",
            "end_value": "1008", "opening_price": "100", "closing_price": "100.8",
            "source": "eth_fees" if symbol == "ETH" else "rrl_fees",
            "signed_amount": "-12.34" if symbol == "ETH" else "0"} for symbol in ("ETH", "RR.L")}}
    register(f, "extraction", observation)
    register(f, "history-contract", {"synthetic_contract": "Monetary amounts are in account currency; netProfit excludes separately reported fees."})
    f.manifest["groups"] = {"accounts": accounts, "history": history_group, "hsbc": {
        "execution": "execution", "original_pre_send": "pre-send", "original_place_response": "placed",
        "post_summary": "post-summary", "post_positions": "post-positions", "later_lookup": lookup_group},
        "screenshots": screenshots, "extraction": "extraction", "history_contract": "history-contract"}
    save(f.path, f.manifest)
    return f


def plan(f):
    save(f.path, f.manifest)
    return adapter.build_composite_plan(runtime=f.runtime, manifest=f.path, reviewed_commit=COMMIT,
        review_reference="synthetic independently reviewed extraction", now=f.now)


def test_synthetic_original_sources_derive_net_period_without_gross_claims(composite):
    before = bytes_under(composite.runtime)
    proof, record, report = plan(composite)
    assert report["closure_count"] == 8 and report["opening_count"] == 1
    assert D(report["gross_closure_proceeds"]) == D("8064")
    assert D(report["opening_cash_debit"]) == D("12.34")
    assert D(report["period_net_cash_effect"]) == D("-37.41")
    assert D(report["post_cash"]) == D("18014.25") and D(report["cash_residual"]) == 0
    assert D(report["pilot_pnl"]) == D("0.27") and report["legacy_profit_credited_to_pilot"] is False
    assert report["cash_event_coverage"] == "net_effects_only"
    assert report["gross_fees"] is report["gross_dividends"] is report["individual_posting_times"] is None
    assert report["historical_daily_performance_qualified"] is False and report["current_account_fresh"] is False
    assert not report["trading_ready"] and not report["applied"]
    assert proof.cash_events == () and record["cash_events"] == []
    assert proof.period_reconciliation["effect_after_observation"] == (NOW+timedelta(seconds=3)).isoformat()
    assert proof.period_reconciliation["effect_by_observation"] == (NOW+timedelta(seconds=10)).isoformat()
    assert report["lookup_adjudication"]["principal"]["conflicting"] is True
    assert report["lookup_adjudication"]["principal"]["margin_substituted"] is False
    assert bytes_under(composite.runtime) == before


@pytest.mark.parametrize("source", ["history-body", "eth_fees", "nov01-original"])
def test_mutated_retained_source_bytes_are_rejected(composite, source):
    path = composite.root / composite.manifest["files"][source]["path"]
    path.write_bytes(path.read_bytes()+b" ")
    with pytest.raises((ExternalReconciliationError, lifecycle.ExternalLifecycleError), match="hash_mismatch"):
        plan(composite)


@pytest.mark.parametrize("field,value", [("start_value", "18001"), ("end_value", "18027.59"),
    ("pnl", "63"), ("money_in", "1"), ("closed_count", 7)])
def test_balanced_cash_does_not_override_conflicting_broker_header(composite, field, value):
    ui = stored(composite, "extraction")
    ui["header"][field] = value
    rewrite(composite, "extraction", ui)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="ui_"):
        plan(composite)


@pytest.mark.parametrize("field,value", [("gross_fees", "37.41"), ("gross_dividends", "0"),
    ("all_individual_cash_movements_enumerated", True), ("exact_fee_posting_times_known", True)])
def test_gross_fee_or_exact_timestamp_claims_cannot_be_inferred(composite, field, value):
    ui = stored(composite, "extraction")
    ui["coverage_scope"][field] = value
    rewrite(composite, "extraction", ui)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="gross_coverage"):
        plan(composite)


def test_cash_debit_must_independently_equal_original_fill(composite):
    change_mcp(composite, "post-summary", lambda value: value["totals"].update(balance="9987.65", availableCash="9987.65"))
    with pytest.raises(lifecycle.ExternalLifecycleError, match="cash_debit"):
        plan(composite)


def test_original_fill_mismatch_is_not_resolved_from_later_margin(composite):
    execution = stored(composite, "execution")
    execution["outcomes"][1]["response"]["positions"][0]["investedAmount"] = "12.35"
    execution_sources(composite, execution)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="fill_identity_principal"):
        plan(composite)


def test_changed_connector_export_cannot_replace_original_consumed_outcome(composite):
    execution = stored(composite, "execution")
    execution["outcomes"][1]["response"]["positions"][0]["avgPrice"] = "888.9"
    rewrite(composite, "execution", execution)
    rewrite(composite, "placed", execution["outcomes"][1])
    with pytest.raises(lifecycle.ExternalLifecycleError, match="connector_phase_binding"):
        plan(composite)


@pytest.mark.parametrize("missing", ["pendingOrders", "orders", "positionCount"])
def test_missing_pending_or_count_fields_never_mean_zero(composite, missing):
    name = "post-summary" if missing == "pendingOrders" else "post-positions"
    change_mcp(composite, name, lambda value: (value if missing == "pendingOrders" else value["direct"]).pop(missing))
    with pytest.raises(lifecycle.ExternalLifecycleError, match="pending_or_mirrored|position_count"):
        plan(composite)


def test_postopening_observation_must_precede_first_closure(composite):
    receipt = stored(composite, "post-summary")
    receipt["received_at"] = (NOW+timedelta(seconds=4)).isoformat()
    rewrite(composite, "post-summary", receipt)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="temporal_bracket"):
        plan(composite)


@pytest.mark.parametrize("field,value", [("period_start", "2026-10-24"),
    ("period_end", "2026-10-22"), ("period_start", "2026-09-01")])
def test_reporting_period_must_cover_both_account_observations_within_month(composite, field, value):
    ui = stored(composite, "extraction")
    ui["header"][field] = value
    rewrite(composite, "extraction", ui)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="reporting_period"):
        plan(composite)


def test_independent_visible_net_values_must_reconcile(composite):
    ui = stored(composite, "extraction")
    ui["market_column"]["signed_amounts"]["SYN3"] = "-9.99"
    rewrite(composite, "extraction", ui)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="visible_net_values_disagree"):
        plan(composite)


@pytest.mark.parametrize("field,value", [("orderID", 501), ("orderType", 18),
    ("openRate", "888.9"), ("openDateTime", (NOW+timedelta(seconds=1)).isoformat())])
def test_detailed_account_opening_fields_must_match_original_fill(composite, field, value):
    edit_account_rows(composite, "nov01", lambda row: row.update({field: value}))
    with pytest.raises(lifecycle.ExternalLifecycleError, match="detailed_original_opening_binding"):
        plan(composite)


def test_relocated_account_receipt_cannot_change_original_source_clock(composite):
    index = stored(composite, "nov01-relocated")
    index["profile"]["request_started_at"] = (NOW+timedelta(seconds=19)).isoformat()
    rewrite(composite, "nov01-relocated", index)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="relocated_receipt_changed"):
        plan(composite)


def test_connector_store_bytes_must_match_original_snapshot(composite):
    with closing(sqlite3.connect(composite.runtime / "connector.sqlite3")) as db, db:
        db.execute("UPDATE dispatches SET claimed_at=?", ((NOW+timedelta(seconds=1)).isoformat(),))
    with pytest.raises(lifecycle.ExternalLifecycleError, match="original_connector_bytes_changed"):
        plan(composite)


@pytest.mark.parametrize("kind", ["missing_dispatch", "corrupt_outcome_digest"])
def test_consumed_original_connector_claim_and_digests_cannot_be_inferred(composite, kind):
    path = composite.runtime / "connector.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        if kind == "missing_dispatch":
            db.execute("DELETE FROM dispatches")
        else:
            db.execute("UPDATE outcomes SET digest=?", ("0"*64,))
    raw = path.read_bytes()
    composite.manifest["original_runtime"]["files"]["connector.sqlite3"].update(
        sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    with pytest.raises(lifecycle.ExternalLifecycleError, match="consumed_dispatch|outcome_digest"):
        plan(composite)


@pytest.mark.parametrize("field,value", [("fee_posting_time", OPEN_TIME), ("displayed_timezone", "UTC"),
    ("opening_price", "99.9"), ("closing_price", "100.7"), ("units", "9.9")])
def test_fee_details_preserve_unknown_posting_time_and_exact_trade_identity(composite, field, value):
    ui = stored(composite, "extraction")
    ui["explicit_fee_details"]["ETH"][field] = value
    rewrite(composite, "extraction", ui)
    with pytest.raises(lifecycle.ExternalLifecycleError, match="explicit_fee_detail_mismatch"):
        plan(composite)


def test_pure_planning_never_opens_original_database_path(composite, monkeypatch):
    original = sqlite3.connect
    def memory_only(path, *args, **kwargs):
        assert path == ":memory:", "original source database opened during read-only planning"
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sqlite3, "connect", memory_only)
    assert plan(composite)[2]["original_bytes_unchanged"] is True
