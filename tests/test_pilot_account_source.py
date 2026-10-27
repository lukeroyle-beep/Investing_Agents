"""Synthetic MCP receipts only; no private broker response is a test fixture."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from brokers.etoro.pilot_account import build_pilot_account
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS


NOW = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
D = Decimal


def receipt(path, payload, start, end):
    wrapper = {"statusCode": 200, "isSuccess": True, "bodyTruncated": False,
               "body": json.dumps(payload)}
    return {"tool": "etoro_execute_read", "arguments": {"path": path},
            "request_started_at": (NOW + timedelta(seconds=start)).isoformat(),
            "response_received_at": (NOW + timedelta(seconds=end)).isoformat(),
            "result": {"content": [{"type": "text", "text": json.dumps(wrapper)}]}}


def sources():
    row = {"positionID": 500, "CID": 123, "instrumentID": 1001,
        "mirrorID": 0, "units": "2", "amount": "100", "isBuy": True,
        "leverage": "1", "settlementTypeID": 0,
        "totalFees": "-0.1", "totalExternalFees": "0.5", "totalExternalTaxes": "0.2"}
    body = {"positions": [row], "credit": "900", "bonusCredit": "0", "mirrors": [],
            **{name: [] for name in DEMO_PENDING_COLLECTIONS}}
    pnl = deepcopy(body)
    pnl.update(accountCurrencyId=1, unrealizedPnL="2")
    pnl["positions"][0]["unrealizedPnL"] = {
        "pnL": "2", "pnlAssetCurrency": "2", "exposureInAccountCurrency": "102",
        "exposureInAssetCurrency": "102", "marginInAccountCurrency": "100",
        "marginInAssetCurrency": "100", "marginCurrencyId": 1, "assetCurrencyId": 1,
        "closeRate": "51", "closeConversionRate": "1",
        "timestamp": (NOW - timedelta(seconds=2)).isoformat()}
    return [receipt("/api/v1/me", {"demoCid": 123, "scopes": [
            "etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]}, -6, -5),
        receipt("/api/v1/trading/info/demo/portfolio", {"clientPortfolio": body}, -4, -3),
        receipt("/api/v1/trading/info/demo/pnl", {"clientPortfolio": pnl}, -2, -1),
        receipt("/api/v1/trading/info/demo/portfolio", {"clientPortfolio": body}, -1, 0)]


def change(items, index, edit):
    wrapper = json.loads(items[index]["result"]["content"][0]["text"])
    payload = json.loads(wrapper["body"])
    edit(payload)
    wrapper["body"] = json.dumps(payload)
    items[index]["result"]["content"][0]["text"] = json.dumps(wrapper)


def build(items=None, now=NOW):
    return build_pilot_account(*(items or sources()), expected_account_id="123", now=now)


def test_pilot_source_preserves_raw_receipts_unknown_history_and_fee_components():
    items = sources()
    account = build(items)
    assert account.account_id == "123"
    assert account.cash == D(900) and account.equity == D(1002)
    assert account.positions[0].principal == D(100)
    assert account.positions[0].pnl == D(2)
    assert account.positions[0].fees == D("0.5")
    assert account.positions[0].taxes == D("0.2")
    assert account.positions[0].carrying_fees == D("-0.1")
    assert account.raw_sources["pnl"] == items[2]
    assert account.qualification_credit is False
    assert account.historical_daily_loss is None and account.historical_drawdown is None
    assert not hasattr(account, "observed_at")
    assert account.deadline == NOW + timedelta(seconds=24)
    # Mutating caller-owned receipts does not silently alter a retained source.
    items[2]["tool"] = "changed"
    account.validate_fresh(NOW)


@pytest.mark.parametrize("age", [24.000001, -0.000001])
def test_original_request_bounds_are_not_refreshed_by_reassessment(age):
    account = build()
    account.validate_fresh(NOW + timedelta(seconds=24))
    with pytest.raises(ValueError, match="stale_or_future"):
        account.validate_fresh(NOW + timedelta(seconds=age))


def test_tampered_raw_evidence_fails_even_with_fresh_clocks():
    account = build()
    account.raw_sources["first"]["tool"] = "modified"
    with pytest.raises(ValueError, match="source_hash"):
        account.validate_fresh(NOW)


def test_correct_source_hash_cannot_bind_modified_cash_or_positions():
    account = build()
    changed = replace(account, cash=D(901), equity=D(1003))
    with pytest.raises(ValueError, match="projection_mismatch"):
        changed.validate_fresh(NOW)
    changed_position = replace(account.positions[0], instrument_id=2002)
    changed = replace(account, positions=(changed_position,))
    with pytest.raises(ValueError, match="projection_mismatch"):
        changed.validate_fresh(NOW)


@pytest.mark.parametrize("index", [1, 2, 3])
def test_wrong_account_in_any_bracket_read_cannot_be_hidden(index):
    items = sources()
    change(items, index, lambda v: v["clientPortfolio"]["positions"][0].update(CID=999))
    with pytest.raises(ValueError, match="wrong_demo_account"):
        build(items)


@pytest.mark.parametrize("edit", [lambda v: v.update(demoCid=999),
    lambda v: v.update(scopes=["etoro-public:trade.real:read", "etoro-public:trade.real:write"])])
def test_connected_demo_identity_and_granted_permissions_are_required(edit):
    items = sources()
    change(items, 0, edit)
    with pytest.raises(ValueError, match="identity_or_permissions"):
        build(items)


@pytest.mark.parametrize("name", DEMO_PENDING_COLLECTIONS)
def test_every_pending_family_must_be_explicit_and_empty(name):
    items = sources()
    change(items, 3, lambda v: v["clientPortfolio"].pop(name))
    with pytest.raises(ValueError):
        build(items)
    items = sources()
    change(items, 3, lambda v: v["clientPortfolio"].update({name: [{"id": 10}]}))
    with pytest.raises(ValueError, match="pending_order"):
        build(items)


@pytest.mark.parametrize("edit", [lambda v: v.update(mirrors=[{}]),
    lambda v: v.update(positions=[]), lambda v: v.update(futureOrders=[]),
    lambda v: v["positions"].append(deepcopy(v["positions"][0]))])
def test_incomplete_or_unsupported_account_coverage_is_never_dropped(edit):
    items = sources()
    change(items, 1, lambda v: edit(v["clientPortfolio"]))
    with pytest.raises(ValueError):
        build(items)


@pytest.mark.parametrize("field,value", [("units", "3"), ("amount", "101"),
    ("totalFees", "-0.2"), ("totalExternalFees", "0.6"), ("totalExternalTaxes", "0.3")])
def test_bracket_binds_units_principal_and_costs(field, value):
    items = sources()
    change(items, 3, lambda v: v["clientPortfolio"]["positions"][0].update({field: value}))
    with pytest.raises(ValueError, match="changed_during_capture"):
        build(items)


@pytest.mark.parametrize("field,value", [("marginCurrencyId", 2), ("assetCurrencyId", None),
    ("pnlAssetCurrency", "2.1"), ("marginInAssetCurrency", "99"),
    ("exposureInAssetCurrency", "101"), ("closeConversionRate", "1.1"),
    ("exposureInAccountCurrency", "NaN"), ("marginInAccountCurrency", "-1")])
def test_currency_and_exact_account_money_must_be_consistent(field, value):
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"]["positions"][0]["unrealizedPnL"].update({field: value}))
    with pytest.raises(ValueError):
        build(items)


def test_non_usd_account_currency_is_rejected():
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"].update(accountCurrencyId=2))
    with pytest.raises(ValueError, match="usd_account"):
        build(items)


def test_margin_and_exposure_never_substitute_for_allocated_principal():
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(
        marginInAccountCurrency="50", marginInAssetCurrency="50"))
    account = build(items)
    assert account.positions[0].principal == D(100)
    assert account.equity == D(1002)


@pytest.mark.parametrize("exposure", ["1", "101.99", "102.00000000000000000000000000001"])
def test_long_one_exposure_cannot_understate_or_conflict_with_principal_plus_pnl(exposure):
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(
        exposureInAccountCurrency=exposure, exposureInAssetCurrency=exposure))
    with pytest.raises(ValueError, match="exposure_principal_pnl_mismatch"):
        build(items)


def test_non_usd_asset_still_reconciles_explicit_usd_exposure():
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(
        assetCurrencyId=666, pnlAssetCurrency="200", exposureInAssetCurrency="10200",
        marginInAssetCurrency="10000", closeConversionRate="0.01", exposureInAccountCurrency="1"))
    with pytest.raises(ValueError, match="exposure_principal_pnl_mismatch"):
        build(items)


@pytest.mark.parametrize("stamp", ["2026-10-26T14:00:00", "2026-10-26T14:00:00-00:00",
    "2026-10-26T13:59:29.9999999Z", "2026-10-26T14:00:00.0000001Z"])
def test_stale_future_and_timezone_unknown_marks_are_rejected(stamp):
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(timestamp=stamp))
    with pytest.raises(ValueError):
        build(items)


def test_seventh_source_timestamp_digit_is_retained_without_rounding_up_expiry():
    items = sources()
    stamp = "2026-10-26T13:59:50.1234567Z"
    change(items, 2, lambda v: v["clientPortfolio"]["positions"][0]["unrealizedPnL"].update(timestamp=stamp))
    account = build(items)
    assert account.positions[0].source_timestamp == stamp
    assert account.deadline == datetime(2026, 10, 26, 14, 0, 20, 123456, tzinfo=timezone.utc)


def test_aggregate_rounding_has_a_fixed_one_cent_per_position_bound():
    items = sources()
    change(items, 2, lambda v: v["clientPortfolio"].update(unrealizedPnL="2.01"))
    assert build(items).equity == D(1002)
    change(items, 2, lambda v: v["clientPortfolio"].update(unrealizedPnL="2.0100001"))
    with pytest.raises(ValueError, match="aggregate_pnl"):
        build(items)


def test_request_interval_and_capture_interval_are_bounded_separately():
    items = sources()
    items[0]["request_started_at"] = (NOW - timedelta(seconds=16)).isoformat()
    with pytest.raises(ValueError, match="request_duration"):
        build(items)
    items = sources()
    for index in (0, 1):
        for field in ("request_started_at", "response_received_at"):
            items[index][field] = (datetime.fromisoformat(items[index][field]) - timedelta(seconds=12)).isoformat()
    with pytest.raises(ValueError, match="capture_duration"):
        build(items)


@pytest.mark.parametrize("edit", [lambda r: r["arguments"].update(path="/api/v1/trading/info/real/portfolio"),
    lambda r: r["arguments"].update(instrumentId=1001), lambda r: r.update(tool="unverified_tool")])
def test_only_unfiltered_supported_demo_read_receipts_are_admitted(edit):
    items = sources()
    edit(items[1])
    with pytest.raises(ValueError, match="supported_read"):
        build(items)


@pytest.mark.parametrize("field,value", [("statusCode", 500), ("bodyTruncated", True), ("isSuccess", False)])
def test_failed_or_truncated_responses_are_rejected(field, value):
    items = sources()
    wrapper = json.loads(items[2]["result"]["content"][0]["text"])
    wrapper[field] = value
    items[2]["result"]["content"][0]["text"] = json.dumps(wrapper)
    with pytest.raises(ValueError):
        build(items)


def test_decoded_object_without_original_mcp_body_is_not_raw_evidence():
    items = sources()
    wrapper = json.loads(items[2]["result"]["content"][0]["text"])
    items[2]["result"] = json.loads(wrapper["body"])
    with pytest.raises(ValueError, match="raw_mcp"):
        build(items)


def test_legacy_short_leverage_or_cfd_remains_in_whole_account_evidence():
    items = sources()
    for index in (1, 2, 3):
        change(items, index, lambda v: v["clientPortfolio"]["positions"][0].update(isBuy=False, leverage="2"))
    account = build(items)
    assert not account.positions[0].is_buy
    assert account.positions[0].leverage == D(2)
    assert account.positions[0].settlement_type == 0
    # Legacy inclusion is accounting evidence, not new-order eligibility.
    assert account.qualification_credit is False
