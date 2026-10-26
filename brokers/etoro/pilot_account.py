"""Supported MCP portfolio evidence for the separate ``demo-pilot-v1`` policy.

Cash has no broker timestamp. Its original request/receipt interval bounds
admission under this pilot contract only; it never becomes a broker observation
time or qualifies the existing strict account-performance contracts.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import json

from brokers.etoro.account_evidence import _exact_sum
from brokers.etoro.connector import instant, number, positive_id, response_object
from brokers.etoro.schemas import (
    DEMO_PENDING_COLLECTIONS, EtoroDetailedPortfolio, EtoroPnlPortfolio,
    _pnl_timestamp, exact_datetime_epoch_seconds,
)
from execution.domain import _aware_utc, payload_hash


MODEL_VERSION = "demo-pilot-v1"
MAX_CAPTURE_SECONDS = 15
MAX_REQUEST_SECONDS = 10
MAX_AGE_SECONDS = 30
_PROFILE = "/api/v1/me"
_DETAIL = "/api/v1/trading/info/demo/portfolio"
_PNL = "/api/v1/trading/info/demo/pnl"


@dataclass(frozen=True, slots=True)
class PilotPosition:
    position_id: str
    instrument_id: int
    units: Decimal
    principal: Decimal
    pnl: Decimal
    exposure: Decimal
    fees: Decimal
    taxes: Decimal
    carrying_fees: Decimal
    is_buy: bool
    leverage: Decimal
    settlement_type: int
    source_timestamp: str

    def __post_init__(self):
        if positive_id(self.position_id, "position_id") != self.position_id:
            raise ValueError("pilot_position_identity_invalid")
        if type(self.instrument_id) is not int or self.instrument_id <= 0:
            raise ValueError("pilot_instrument_identity_invalid")
        for name in ("units", "principal", "pnl", "exposure", "fees", "taxes", "carrying_fees", "leverage"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("pilot_exact_finite_money_required")
        if (self.units <= 0 or self.principal < 0 or self.exposure <= 0
                or self.fees < 0 or self.taxes < 0 or self.leverage <= 0
                or type(self.is_buy) is not bool or type(self.settlement_type) is not int
                or self.settlement_type not in {0, 1}):
            raise ValueError("pilot_position_values_invalid")
        _pnl_timestamp(self.source_timestamp)


@dataclass(frozen=True, slots=True)
class PilotAccount:
    account_id: str
    cash: Decimal
    equity: Decimal
    positions: tuple[PilotPosition, ...]
    capture_started_at: datetime
    capture_completed_at: datetime
    source_hash: str
    raw_sources: dict
    qualification_credit: bool = field(default=False, init=False)
    historical_daily_loss: None = field(default=None, init=False)
    historical_drawdown: None = field(default=None, init=False)

    def __post_init__(self):
        if positive_id(self.account_id, "account_id") != self.account_id:
            raise ValueError("pilot_account_identity_invalid")
        for name in ("cash", "equity"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("pilot_exact_finite_money_required")
        if self.cash < 0 or self.equity <= 0:
            raise ValueError("pilot_account_balance_invalid")
        if (not isinstance(self.positions, tuple) or not self.positions
                or not all(isinstance(p, PilotPosition) for p in self.positions)
                or len({p.position_id for p in self.positions}) != len(self.positions)):
            raise ValueError("pilot_position_coverage_invalid")
        if self.equity != _exact_sum((self.cash, *(p.principal for p in self.positions), *(p.pnl for p in self.positions))):
            raise ValueError("pilot_equity_identity_mismatch")
        for name in ("capture_started_at", "capture_completed_at"):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        if not 0 <= (self.capture_completed_at - self.capture_started_at).total_seconds() <= MAX_CAPTURE_SECONDS:
            raise ValueError("pilot_capture_duration_exceeded")
        if not isinstance(self.raw_sources, dict) or payload_hash(self.raw_sources) != self.source_hash:
            raise ValueError("pilot_raw_source_hash_mismatch")
        object.__setattr__(self, "raw_sources", deepcopy(self.raw_sources))

    @property
    def deadline(self) -> datetime:
        """Conservative expiry, never a claimed broker cash timestamp."""
        source_deadlines = [exact_datetime_epoch_seconds(self.capture_started_at) + MAX_AGE_SECONDS]
        profile_start = instant(self.raw_sources["profile"]["request_started_at"], "profile_request_time")
        source_deadlines.append(exact_datetime_epoch_seconds(profile_start) + MAX_AGE_SECONDS)
        source_deadlines.extend(_pnl_timestamp(p.source_timestamp)[1] + MAX_AGE_SECONDS for p in self.positions)
        micros = int((min(source_deadlines) * 1000000).to_integral_value(rounding=ROUND_FLOOR))
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=micros)

    def _validate_current_time(self, now: datetime) -> None:
        moment = _aware_utc(now, "now")
        if payload_hash(self.raw_sources) != self.source_hash:
            raise ValueError("pilot_raw_source_hash_mismatch")
        if moment < self.capture_completed_at or moment > self.deadline:
            raise ValueError("pilot_account_stale_or_future")
        current = exact_datetime_epoch_seconds(moment)
        for position in self.positions:
            age = current - _pnl_timestamp(position.source_timestamp)[1]
            if not Decimal(0) <= age <= MAX_AGE_SECONDS:
                raise ValueError("pilot_position_mark_stale_or_future")

    def validate_fresh(self, now: datetime) -> None:
        """Revalidate source binding as well as the original clocks."""
        self._validate_current_time(now)
        rebuilt = build_pilot_account(self.raw_sources["profile"], self.raw_sources["first"],
            self.raw_sources["pnl"], self.raw_sources["last"], self.account_id, now)
        if self != rebuilt:
            raise ValueError("pilot_account_projection_mismatch")


def _receipt(receipt, path):
    if (not isinstance(receipt, dict) or receipt.get("tool") != "etoro_execute_read"
            or receipt.get("arguments") != {"path": path}):
        raise ValueError("pilot_exact_supported_read_required")
    start = instant(receipt.get("request_started_at"), "request_started_at")
    end = instant(receipt.get("response_received_at"), "response_received_at")
    if not 0 <= (end - start).total_seconds() <= MAX_REQUEST_SECONDS:
        raise ValueError("pilot_request_duration_exceeded")
    result = receipt.get("result")
    if (not isinstance(result, dict) or result.get("isError")
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1
            or result["content"][0].get("type") != "text"
            or not isinstance(result["content"][0].get("text"), str)):
        raise ValueError("pilot_raw_mcp_response_required")
    envelope = json.loads(result["content"][0]["text"], parse_float=Decimal)
    if (not isinstance(envelope, dict) or envelope.get("statusCode") != 200
            or envelope.get("isSuccess") is not True or envelope.get("bodyTruncated") is not False
            or not isinstance(envelope.get("body"), str)):
        raise ValueError("pilot_successful_complete_raw_body_required")
    raw = response_object(result)
    return raw, start, end


def _structure(raw, expected):
    parsed = EtoroDetailedPortfolio.parse(raw)
    if parsed.mirror_count:
        raise ValueError("pilot_mirror_valuation_unsupported")
    if any(count for _, count in parsed.pending_collection_counts):
        raise ValueError("pilot_nonempty_pending_order_valuation_unsupported")
    if tuple(name for name, _ in parsed.pending_collection_counts) != DEMO_PENDING_COLLECTIONS:
        raise ValueError("pilot_pending_coverage_incomplete")
    if parsed.bonus_credit:
        raise ValueError("pilot_bonus_credit_unsupported")
    if not parsed.positions:
        raise ValueError("pilot_empty_account_identity_uncorroborated")
    for position in parsed.positions:
        if position.customer_id != expected:
            raise ValueError("pilot_wrong_demo_account")
        if position.mirror_id:
            raise ValueError("pilot_mirror_valuation_unsupported")
        if position.settlement_type_id not in {0, 1}:
            raise ValueError("pilot_legacy_settlement_valuation_unsupported")
    rows = raw["clientPortfolio"]["positions"]
    # Money/units/identity and explicit accrued costs must stay stable across
    # all three reads. Price marks may move; no source marks are re-dated.
    values = {}
    for p, row in zip(parsed.positions, rows, strict=True):
        costs = tuple(number(row.get(name), name) for name in ("totalFees", "totalExternalFees", "totalExternalTaxes"))
        if costs[1] < 0 or costs[2] < 0:
            raise ValueError("pilot_external_cost_invalid")
        values[p.position_id] = (p, costs)
    return parsed, values


def build_pilot_account(profile_receipt, first_receipt, pnl_receipt, last_receipt,
                        expected_account_id, now) -> PilotAccount:
    """Validate complete raw receipts without requests or economic writes.

    Account-currency P&L, exposure and margin are provided directly by the P&L
    route. Asset currency IDs are retained and validated, but are never guessed
    to be currency codes or used to reinterpret account-currency money.
    """
    expected = positive_id(expected_account_id, "expected_account_id")
    profile, profile_start, profile_end = _receipt(profile_receipt, _PROFILE)
    first, started, first_end = _receipt(first_receipt, _DETAIL)
    pnl, pnl_start, pnl_end = _receipt(pnl_receipt, _PNL)
    last, last_start, completed = _receipt(last_receipt, _DETAIL)
    if not profile_start <= profile_end <= started <= first_end <= pnl_start <= pnl_end <= last_start <= completed:
        raise ValueError("pilot_receipt_sequence_invalid")
    if (completed - started).total_seconds() > MAX_CAPTURE_SECONDS:
        raise ValueError("pilot_capture_duration_exceeded")
    scopes = profile.get("scopes")
    if (positive_id(profile.get("demoCid"), "profile_demo_cid") != expected
            or not isinstance(scopes, list)
            or not {"etoro-public:trade.demo:read", "etoro-public:trade.demo:write"} <= set(scopes)):
        raise ValueError("pilot_demo_identity_or_permissions_unverified")
    first_parsed, first_positions = _structure(first, expected)
    pnl_parsed, pnl_positions = _structure(pnl, expected)
    last_parsed, last_positions = _structure(last, expected)
    if (first_positions != pnl_positions or first_positions != last_positions
            or first_parsed.credit != pnl_parsed.credit or first_parsed.credit != last_parsed.credit):
        raise ValueError("pilot_account_changed_during_capture")
    valuations = EtoroPnlPortfolio.parse(pnl)
    if valuations.account_currency_id != 1:
        raise ValueError("pilot_usd_account_currency_required")
    by_id = {v.position_id: v for v in valuations.valuations}
    if len(by_id) != len(valuations.valuations) or set(by_id) != set(first_positions):
        raise ValueError("pilot_valuation_coverage_incomplete")
    positions = []
    completion_epoch = exact_datetime_epoch_seconds(completed)
    for position_id, (position, _) in sorted(first_positions.items()):
        v = by_id[position_id]
        if v.margin_currency_id != 1:
            raise ValueError("pilot_usd_margin_currency_required")
        if (v.exposure_account_currency <= 0 or v.exposure_asset_currency <= 0
                or v.margin_account_currency < 0 or v.margin_asset_currency < 0):
            raise ValueError("pilot_currency_money_values_invalid")
        if v.asset_currency_id == 1 and (v.pnl_asset_currency != v.pnl
                or v.exposure_asset_currency != v.exposure_account_currency
                or v.margin_asset_currency != v.margin_account_currency
                or v.close_conversion_rate != 1):
            raise ValueError("pilot_usd_currency_values_inconsistent")
        # Long x1 market value must reconcile to allocated principal plus the
        # broker mark. Otherwise a smaller fabricated exposure could evade
        # issuer limits while the same row reports a larger asset value.
        # This identity is not applied to other legacy leverage/short shapes.
        if (position.is_buy and position.leverage == 1
                and v.exposure_account_currency != _exact_sum((position.amount, v.pnl))):
            raise ValueError("pilot_long_one_exposure_principal_pnl_mismatch")
        if not Decimal(0) <= completion_epoch - v.timestamp_epoch_seconds <= MAX_AGE_SECONDS:
            raise ValueError("pilot_position_mark_stale_or_future")
        # Detailed ``amount`` is allocated account-currency principal. Margin
        # is collateral and exposure is market value; neither replaces it.
        positions.append(PilotPosition(position_id, position.instrument_id, position.units,
            position.amount, v.pnl, v.exposure_account_currency,
            v.total_external_fees, v.total_external_taxes, v.total_fees,
            position.is_buy, position.leverage, position.settlement_type_id, v.timestamp))
    total_pnl = _exact_sum(p.pnl for p in positions)
    difference = _exact_sum((total_pnl, valuations.total_pnl.copy_negate())).copy_abs()
    if difference > Decimal("0.01") * len(positions):
        raise ValueError("pilot_aggregate_pnl_inconsistent")
    cash = first_parsed.credit
    equity = _exact_sum((cash, *(p.principal for p in positions), total_pnl))
    sources = {"profile": profile_receipt, "first": first_receipt, "pnl": pnl_receipt, "last": last_receipt}
    account = PilotAccount(expected, cash, equity, tuple(positions), started, completed, payload_hash(sources), sources)
    account._validate_current_time(now)
    return account
