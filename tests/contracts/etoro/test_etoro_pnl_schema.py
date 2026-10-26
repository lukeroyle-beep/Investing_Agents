"""Synthetic PnL source contracts; no account/network acceptance evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from brokers.etoro.schemas import (
    DEMO_PENDING_COLLECTIONS, EtoroDetailedPortfolio, EtoroPnlPortfolio,
    EtoroSchemaError, exact_datetime_epoch_seconds,
)
from tests.contracts.etoro.test_etoro_account_evidence import NOW, _detail


def _pnl():
    body = _detail()
    portfolio = body["clientPortfolio"]
    portfolio.update(accountCurrencyId=1, unrealizedPnL=20)
    position = portfolio["positions"][0]
    position.update(totalFees=2, totalExternalFees=3, totalExternalTaxes=1)
    position["unrealizedPnL"] = {
        "pnL": 20, "pnlAssetCurrency": 20,
        "exposureInAccountCurrency": 1020, "exposureInAssetCurrency": 1020,
        "marginInAccountCurrency": 1000, "marginInAssetCurrency": 1000,
        "marginCurrencyId": 1, "assetCurrencyId": 1,
        "closeRate": 102, "closeConversionRate": 1,
        "timestamp": "2026-10-21T14:30:00.0000000Z",
    }
    return body


def _valuation(body):
    return body["clientPortfolio"]["positions"][0]["unrealizedPnL"]


def test_official_nested_shape_preserves_detail_pnl_and_separate_fee_components():
    body = _pnl()
    original = deepcopy(body)
    parsed = EtoroPnlPortfolio.parse(body)
    detail = EtoroDetailedPortfolio.parse(_detail())
    assert parsed.detailed.positions == detail.positions
    assert parsed.detailed.credit == detail.credit
    assert parsed.detailed.pending_collection_counts == detail.pending_collection_counts
    assert parsed.account_currency_id == 1
    assert parsed.total_pnl == Decimal("20")
    value = parsed.valuations[0]
    assert value.position_id == "7001"
    assert value.pnl == Decimal("20")
    assert value.exposure_account_currency == Decimal("1020")
    assert value.margin_account_currency == Decimal("1000")
    assert (value.total_fees, value.total_external_fees, value.total_external_taxes) == (
        Decimal("2"), Decimal("3"), Decimal("1"),
    )
    assert value.timestamp == "2026-10-21T14:30:00.0000000Z"
    assert value.timestamp_epoch_seconds == exact_datetime_epoch_seconds(NOW)
    assert body == original


@pytest.mark.parametrize("field", list(_valuation(_pnl())))
@pytest.mark.parametrize("missing", [True, False])
def test_every_nested_valuation_field_must_be_explicit_and_non_null(field, missing):
    body = _pnl()
    if missing:
        del _valuation(body)[field]
    else:
        _valuation(body)[field] = None
    with pytest.raises(EtoroSchemaError):
        EtoroPnlPortfolio.parse(body)


@pytest.mark.parametrize("field", ["totalFees", "totalExternalFees", "totalExternalTaxes"])
@pytest.mark.parametrize("invalid", [None, True, "NaN", "Infinity", "not-money"])
def test_fee_components_must_be_explicit_finite_numbers(field, invalid):
    body = _pnl()
    body["clientPortfolio"]["positions"][0][field] = invalid
    with pytest.raises(EtoroSchemaError):
        EtoroPnlPortfolio.parse(body)


def test_refund_does_not_get_dropped_or_applied_as_a_second_pnl_adjustment():
    body = _pnl()
    body["clientPortfolio"]["positions"][0]["totalFees"] = "-2.1234567"
    parsed = EtoroPnlPortfolio.parse(body)
    assert parsed.valuations[0].total_fees == Decimal("-2.1234567")
    assert parsed.valuations[0].pnl == Decimal("20")
    assert parsed.total_pnl == Decimal("20")


@pytest.mark.parametrize("field", DEMO_PENDING_COLLECTIONS)
def test_pnl_keeps_the_strict_explicit_pending_collection_contract(field):
    body = _pnl()
    del body["clientPortfolio"][field]
    with pytest.raises(EtoroSchemaError, match=field):
        EtoroPnlPortfolio.parse(body)


@pytest.mark.parametrize("field", ["clientPortfolio", "positions", "mirrors", "credit", "bonusCredit", "accountCurrencyId", "unrealizedPnL"])
def test_required_portfolio_shape_fields_are_not_defaulted(field):
    body = _pnl()
    del (body if field == "clientPortfolio" else body["clientPortfolio"])[field]
    with pytest.raises(EtoroSchemaError):
        EtoroPnlPortfolio.parse(body)


def test_flattened_example_pnl_fields_cannot_substitute_for_nested_broker_shape():
    body = _pnl()
    position = body["clientPortfolio"]["positions"][0]
    position.update(position.pop("unrealizedPnL"))
    with pytest.raises(EtoroSchemaError, match="position.unrealizedPnL must be an object"):
        EtoroPnlPortfolio.parse(body)


def test_duplicate_positions_fail_even_with_distinct_valuation_timestamps():
    body = _pnl()
    duplicate = deepcopy(body["clientPortfolio"]["positions"][0])
    duplicate["unrealizedPnL"]["timestamp"] = "2026-10-21T14:30:00.0000001Z"
    body["clientPortfolio"]["positions"].append(duplicate)
    with pytest.raises(EtoroSchemaError, match="duplicate position identities"):
        EtoroPnlPortfolio.parse(body)


@pytest.mark.parametrize("value", [True, 0, -1, "1.2", "NaN"])
@pytest.mark.parametrize("field", ["assetCurrencyId", "marginCurrencyId"])
def test_currency_ids_are_exact_positive_integers(field, value):
    body = _pnl()
    _valuation(body)[field] = value
    with pytest.raises(EtoroSchemaError):
        EtoroPnlPortfolio.parse(body)


@pytest.mark.parametrize("field", ["closeRate", "closeConversionRate"])
@pytest.mark.parametrize("value", [0, -1, True, "NaN", "Infinity"])
def test_close_rates_must_be_positive_finite_numbers(field, value):
    body = _pnl()
    _valuation(body)[field] = value
    with pytest.raises(EtoroSchemaError):
        EtoroPnlPortfolio.parse(body)


@pytest.mark.parametrize("timestamp", [
    "2026-10-21T14:30:00Z", "2026-10-21T14:30:00.1234567Z",
    "2026-10-21T15:30:00.1234567+01:00", "2026-10-21T10:30:00.1234567-04:00",
    "2026-10-22T04:30:00.1234567+14:00", "2026-10-21T14:30:00.1234567+00:00",
])
def test_explicit_offsets_keep_original_text_and_all_seven_fractional_digits(timestamp):
    body = _pnl()
    _valuation(body)["timestamp"] = timestamp
    parsed = EtoroPnlPortfolio.parse(body).valuations[0]
    fraction = Decimal("0.1234567") if ".1234567" in timestamp else Decimal(0)
    assert parsed.timestamp == timestamp
    assert parsed.timestamp_epoch_seconds == exact_datetime_epoch_seconds(NOW) + fraction


def test_seventh_digit_changes_both_comparison_and_source_identity():
    one = _pnl()
    two = _pnl()
    _valuation(one)["timestamp"] = "2026-10-21T14:30:00.1234567Z"
    _valuation(two)["timestamp"] = "2026-10-21T14:30:00.1234568Z"
    first, second = EtoroPnlPortfolio.parse(one), EtoroPnlPortfolio.parse(two)
    assert second.valuations[0].timestamp_epoch_seconds - first.valuations[0].timestamp_epoch_seconds == Decimal("0.0000001")
    assert first.snapshot_id != second.snapshot_id
    assert first.detailed.snapshot_id != second.detailed.snapshot_id


@pytest.mark.parametrize("timestamp", [
    None, 123, "", "invalid", "2026-10-21T14:30:00", "2026-10-21T14:30:00.1234567",
    "2026-10-21", "2026-10-21 14:30:00Z", "2026-10-21T14:30:00 UTC",
    "2026-10-21T14:30:00-00:00", "2026-10-21T14:30:00.12345678Z",
    "2026-10-21T14:30:00.Z", "2026-02-30T14:30:00Z", "2026-10-21T24:30:00Z",
    "2026-10-21T14:30:60Z", "2026-10-21T14:30:00+24:00", "2026-10-21T14:30:00+00:60",
    "2026-10-21T14:30:00+0000", "2026-10-21T14:30:00+00:00Z",
    " 2026-10-21T14:30:00Z", "2026-10-21T14:30:00Z ",
])
def test_ambiguous_malformed_or_unsupported_precision_timestamps_fail(timestamp):
    body = _pnl()
    _valuation(body)["timestamp"] = timestamp
    with pytest.raises(EtoroSchemaError, match="position.unrealizedPnL.timestamp"):
        EtoroPnlPortfolio.parse(body)


def test_clock_comparison_keeps_microseconds_without_binary_float_rounding():
    instant = datetime(2026, 10, 21, 14, 30, 0, 123456, tzinfo=timezone.utc)
    assert exact_datetime_epoch_seconds(instant) - exact_datetime_epoch_seconds(NOW) == Decimal("0.123456")
    assert exact_datetime_epoch_seconds(instant.astimezone(timezone(timedelta(hours=5, minutes=30)))) == exact_datetime_epoch_seconds(instant)
    assert exact_datetime_epoch_seconds(datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)) == Decimal("-0.000001")
    with pytest.raises(EtoroSchemaError, match="timezone"):
        exact_datetime_epoch_seconds(instant.replace(tzinfo=None))
