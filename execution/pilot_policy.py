"""Explicit, additive Demo integration policy. Strict risk policy is untouched."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo
from types import MappingProxyType
from copy import deepcopy

from brokers.etoro.connector import number, response_object
from brokers.etoro.pilot_account import PilotAccount
from brokers.etoro.account_evidence import _exact_sum as total, _difference as subtract
from execution.domain import payload_hash

D = Decimal
POLICY = "demo-pilot-v1"


def multiply(left, right):
    with localcontext() as context:
        context.prec = max(50, len(left.as_tuple().digits) + len(right.as_tuple().digits) + 2)
        return left * right


class PilotPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class PilotClassification:
    instrument_id: int
    symbol: str
    issuer: str
    sector: str
    asset_type: str
    source_reference: str


class PilotCatalog:
    """Reviewed issuer/sector grouping bound to complete broker metadata.

    Issuer grouping is an explicit review input, never inferred from ticker
    prefixes. Multiple share classes must have the same issuer key.
    """
    def __init__(self, classifications, metadata):
        raw = response_object(metadata)
        rows = raw.get("results")
        if not isinstance(rows, list) or raw.get("pagination", {}).get("hasNext") is not False:
            raise PilotPolicyError("complete_instrument_metadata_required")
        self.items = {x.instrument_id: x for x in classifications}
        if len(self.items) != len(classifications) or len({x.get("instrumentId") for x in rows}) != len(rows):
            raise PilotPolicyError("duplicate_instrument_metadata")
        if set(self.items) != {x.get("instrumentId") for x in rows}:
            raise PilotPolicyError("classification_metadata_coverage_mismatch")
        types = {"Stocks": "equity", "ETF": "etf", "Crypto": "crypto"}
        for row in rows:
            item = self.items[row["instrumentId"]]
            if (item.symbol != row.get("symbol") or item.asset_type != types.get(row.get("type"))
                    or not all((item.issuer, item.sector, item.source_reference))):
                raise PilotPolicyError("reviewed_instrument_classification_required")
        self.evidence = deepcopy({"classifications": classifications, "metadata": raw})
        self.source_hash = payload_hash(self.evidence)
        self.items = MappingProxyType(self.items)

    def validate(self):
        if self.source_hash != payload_hash(self.evidence) or tuple(self.items.values()) != self.evidence["classifications"]:
            raise PilotPolicyError("classification_evidence_changed")

    def exposures(self, account):
        self.validate()
        issuers, sectors = {}, {}
        for p in account.positions:
            if p.instrument_id not in self.items:
                raise PilotPolicyError("whole_account_classification_incomplete")
            item = self.items[p.instrument_id]
            issuers[item.issuer] = total((issuers.get(item.issuer, D(0)), p.exposure.copy_abs()))
            sectors[item.sector] = total((sectors.get(item.sector, D(0)), p.exposure.copy_abs()))
        return issuers, sectors

    def legacy_breaches(self, account):
        issuers, sectors = self.exposures(account)
        gross = total(p.exposure.copy_abs() for p in account.positions)
        net = total(p.exposure if p.is_buy else p.exposure.copy_negate() for p in account.positions)
        rows = []
        for name, observed, limit in [("gross", gross, multiply(account.equity, D('.5'))),
                                      ("absolute_net", net.copy_abs(), multiply(account.equity, D('.5'))),
                                      *[("issuer:"+k, v, multiply(account.equity, D('.05'))) for k,v in issuers.items()],
                                      *[("sector:"+k, v, multiply(account.equity, D('.2'))) for k,v in sectors.items()]]:
            if observed > limit:
                rows.append({"kind": name, "observed_usd": observed, "limit_usd": limit})
        for p in account.positions:
            if self.items[p.instrument_id].asset_type not in {"equity", "etf"} or p.settlement_type != 1 or p.leverage != 1 or not p.is_buy:
                rows.append({"kind": "legacy_instrument_outside_new_entry_scope", "position_id": p.position_id,
                             "asset_type": self.items[p.instrument_id].asset_type, "settlement_type": p.settlement_type,
                             "leverage": p.leverage, "is_buy": p.is_buy})
        return tuple(rows)


def preview_costs(preview):
    """Cash charge and conservative opening-cost reserve remain distinct.

    Bid/ask spread is embedded in the purchased asset's value. Reserve it for
    the pilot caps, but never debit it a second time during cash reconciliation.
    """
    raw = response_object(preview)
    c = raw.get("confirmation", {})
    costs = c.get("costs")
    if not isinstance(costs, list) or not costs:
        raise PilotPolicyError("explicit_opening_cost_breakdown_required")
    seen, cash_cost = set(), D(0)
    for item in costs:
        kind = item.get("type", item.get("costType"))
        if kind in seen or kind not in {"transactionFee", "markup", "marketSpread", "sdrt", "overnightFee", "overWeekendFee"}:
            raise PilotPolicyError("cost_component_unknown_or_duplicate")
        seen.add(kind)
        value = number(item.get("value"), "cost")
        if item.get("currency", "").upper() != "USD" or value < 0:
            raise PilotPolicyError("usd_nonnegative_cost_required")
        if kind not in {"overnightFee", "overWeekendFee", "marketSpread"}:
            cash_cost = total((cash_cost, value))
        elif kind in {"overnightFee", "overWeekendFee"} and value != 0:
            raise PilotPolicyError("recurring_cost_requires_separate_evidence")
    upfront = number(c.get("totalUpfrontCost"), "upfront_cost")
    if upfront != cash_cost:
        raise PilotPolicyError("upfront_cost_breakdown_mismatch")
    quote = c.get("quote", {})
    ask, bid = number(quote.get("ask"), "ask"), number(quote.get("bid"), "bid")
    units = number(c.get("estimatedUnits"), "estimated_units")
    if ask < bid or bid <= 0 or units <= 0:
        raise PilotPolicyError("invalid_preview_quote")
    spread = multiply(units, subtract(ask, bid))
    explicit = total(number(x["value"], "spread") for x in costs if x.get("type", x.get("costType")) == "marketSpread")
    return upfront, total((upfront, max(spread, explicit)))


def evaluate_pilot(*, account, ledger, catalog, instrument_id, amount, expected_costs, now):
    if not isinstance(account, PilotAccount):
        raise PilotPolicyError("typed_pilot_account_required")
    account.validate_fresh(now)
    if account.account_id != ledger["account_id"]:
        raise PilotPolicyError("pilot_account_mismatch")
    amount, expected_costs = number(amount, "amount"), number(expected_costs, "expected_costs")
    if amount <= 0 or expected_costs < 0:
        raise PilotPolicyError("positive_principal_nonnegative_cost_required")
    candidate = catalog.items.get(instrument_id)
    if candidate is None or candidate.asset_type not in {"equity", "etf"}:
        raise PilotPolicyError("eligible_equity_or_etf_required")
    issuers, _ = catalog.exposures(account)
    commitment = total((amount, expected_costs))
    checks = {
        "order_cap": commitment <= 100,
        "capital_cap": total((ledger["committed_capital"], commitment)) <= 500,
        "position_cap": ledger["position_count"] < 5,
        "cash_buffer": subtract(account.cash, commitment) >= multiply(account.equity, D('.20')),
        "issuer_cap": total((issuers.get(candidate.issuer, D(0)), commitment)) <= multiply(account.equity, D('.05')),
        "loss_stop": not ledger["loss_stopped"] and ledger["pilot_pnl"] > -25,
        "reconciled": not ledger["unresolved"],
    }
    day = now.astimezone(ZoneInfo("Europe/London")).date().isoformat()
    checks["daily_submission"] = not any(x.get("london_day") == day for x in
        (*ledger["submissions"], *ledger.get("external_openings", ())))
    return {"policy": POLICY, "eligible": all(checks.values()), "checks": checks,
            "opening_capital_usd": commitment, "cash_after_commitment_usd": subtract(account.cash,commitment),
            "legacy_breaches": catalog.legacy_breaches(account), "catalog_hash": catalog.source_hash,
            "historical_daily_loss": None, "historical_drawdown": None, "qualification_credit": False}
