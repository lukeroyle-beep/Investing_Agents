from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
import json
from copy import deepcopy

import pytest

from agents.fill_agent.pilot_ledger import PilotLedger, PilotLedgerError
from brokers.base import BrokerExecution
from brokers.etoro.account_evidence import _exact_sum
from brokers.etoro.pilot_account import build_pilot_account
from brokers.etoro.schemas import DEMO_PENDING_COLLECTIONS
from execution.domain import Environment, payload_hash

D = Decimal
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Position:
    position_id: str = "1"
    instrument_id: int = 9
    units: Decimal = D("10")
    principal: Decimal = D("1000")
    pnl: Decimal = D("0")
    exposure: Decimal = D("1000")
    fees: Decimal = D("0")
    taxes: Decimal = D("0")
    carrying_fees: Decimal = D("0")
    is_buy: bool = True
    leverage: Decimal = D("1")
    settlement_type: int = 1
    source_timestamp: str = "2026-10-23T12:00:00Z"


def Account(*, account_id="123", cash=D("10000"), positions=(Position(),), now=NOW):
    # Every account uses real supported-source parsing, including projection,
    # identity, coverage, currency and immutable raw-evidence checks.
    rows = []
    for p in positions:
        rows.append({"positionID": int(p.position_id), "CID": int(account_id),
            "instrumentID": p.instrument_id, "mirrorID": 0, "units": str(p.units),
            "amount": str(p.principal), "isBuy": p.is_buy, "leverage": str(p.leverage),
            "settlementTypeID": p.settlement_type, "totalFees": str(p.carrying_fees),
            "totalExternalFees": str(p.fees), "totalExternalTaxes": str(p.taxes)})
    body = {"positions": rows, "credit": str(cash), "bonusCredit": "0", "mirrors": [],
            **{name: [] for name in DEMO_PENDING_COLLECTIONS}}
    pnl = deepcopy(body)
    pnl.update(accountCurrencyId=1, unrealizedPnL=str(_exact_sum(p.pnl for p in positions)))
    for row, p in zip(pnl["positions"], positions):
        row["unrealizedPnL"] = {"pnL": str(p.pnl), "pnlAssetCurrency": str(p.pnl),
            "exposureInAccountCurrency": str(p.exposure), "exposureInAssetCurrency": str(p.exposure),
            "marginInAccountCurrency": str(p.principal), "marginInAssetCurrency": str(p.principal),
            "marginCurrencyId": 1, "assetCurrencyId": 1,
            "closeRate": "1", "closeConversionRate": "1", "timestamp": now.isoformat()}
    def receipt(path, payload):
        wrapper = {"statusCode": 200, "isSuccess": True, "bodyTruncated": False,
                   "body": json.dumps(payload)}
        return {"tool": "etoro_execute_read", "arguments": {"path": path},
                "request_started_at": now.isoformat(), "response_received_at": now.isoformat(),
                "result": {"content": [{"type": "text", "text": json.dumps(wrapper)}]}}
    profile = receipt("/api/v1/me", {"demoCid": int(account_id), "scopes": [
        "etoro-public:trade.demo:read", "etoro-public:trade.demo:write"]})
    first = receipt("/api/v1/trading/info/demo/portfolio", {"clientPortfolio": body})
    mark = receipt("/api/v1/trading/info/demo/pnl", {"clientPortfolio": pnl})
    return build_pilot_account(profile, first, mark, deepcopy(first), account_id, now)


def recapture(account, now, **kwargs):
    kwargs.pop("equity", None)  # Equity is derived from the synthetic broker evidence.
    positions = kwargs.get("positions", account.positions)
    previous = {p.position_id: p for p in account.positions}
    # Tests changing principal/P&L must still describe a coherent broker mark;
    # the ledger then tests the intended legacy, fill or loss mismatch.
    positions = tuple(replace(p, exposure=_exact_sum((p.principal, p.pnl)))
        if p.position_id in previous and p.is_buy and p.leverage == 1
        and (p.principal != previous[p.position_id].principal or p.pnl != previous[p.position_id].pnl)
        else p for p in positions)
    return Account(account_id=kwargs.get("account_id", account.account_id),
        cash=kwargs.get("cash", account.cash), positions=positions, now=now)


def updated(account, **kwargs):
    return recapture(account, account.capture_completed_at, **kwargs)


def execution(position_id="100", amount="99", fees="1", now=NOW, instrument_id=1):
    return BrokerExecution(broker_execution_id="fill-" + position_id,
        broker_order_id="order-" + position_id, broker_instrument_id=instrument_id,
        broker_position_id=position_id, broker_reference_id="reference-" + position_id,
        environment=Environment.DEMO, side="long", action="buy", units=D("1"), price=D(amount),
        executed_at=now, price_rate_id="rate-1", fees=D(fees), taxes=D("0"),
        currency="USD", executed_amount=D(amount), executed_at_source=now.isoformat())


def post_capture(pre, fill, now):
    p = Position(position_id=fill.broker_position_id, instrument_id=fill.broker_instrument_id,
                 units=fill.units, principal=fill.executed_amount, exposure=fill.executed_amount,
                 fees=fill.fees, taxes=fill.taxes, source_timestamp=now.isoformat())
    return recapture(pre, now, cash=pre.cash-fill.executed_amount-fill.fees-fill.taxes,
                     equity=pre.equity-fill.fees-fill.taxes, positions=pre.positions+(p,))


@pytest.fixture
def ledger(tmp_path):
    return PilotLedger.create(tmp_path / "pilot.sqlite3", Account(),
                              [{"issuer": "legacy", "breach": "preexisting"}], NOW)


def fill_order(ledger, account=Account(), day=NOW, command="first", amount="99", fees="1", position_id="100"):
    account = recapture(account, day)
    ledger.reserve_and_claim(command, D(amount), D(fees), account, day, instrument_id=1)
    fill = execution(position_id, amount, fees, day + timedelta(seconds=1))
    post = post_capture(account, fill, day + timedelta(seconds=2))
    ledger.reconcile_open(command, (fill,), account, post, day + timedelta(seconds=2))
    return post, fill, account


def test_explicit_creation_preserves_inception_and_legacy(ledger):
    saved = ledger.snapshot()
    assert saved["inception"]["legacy_breaches"] == [{"issuer": "legacy", "breach": "preexisting"}]
    assert saved["inception"]["account"]["positions"][0]["position_id"] == "1"
    assert saved["inception"]["historical_whole_account_daily_loss"] is None
    assert saved["inception"]["historical_whole_account_drawdown"] is None
    assert saved["inception"]["qualification_credit"] is False
    assert saved["pilot_pnl"] == 0  # no pilot trades since this verified inception
    with pytest.raises(PilotLedgerError, match="never_reset"):
        PilotLedger.create(ledger.path, Account(), [], NOW + timedelta(seconds=1))
    assert PilotLedger(ledger.path, "123").snapshot() == saved


def test_reopen_never_creates_and_wrong_account_rejected(tmp_path, ledger):
    with pytest.raises(PilotLedgerError, match="inception_missing"):
        PilotLedger(tmp_path / "missing.sqlite3", "123")
    assert not (tmp_path / "missing.sqlite3").exists()
    with pytest.raises(PilotLedgerError, match="wrong_account"):
        PilotLedger(ledger.path, "999")
    with pytest.raises(PilotLedgerError, match="wrong_account"):
        ledger.reserve_and_claim("x", D("1"), D("0"), Account(account_id="999"), NOW)


@pytest.mark.parametrize("amount,cost", [("100", "0.01"), ("99", "2"), ("0", "0"), ("-1", "0")])
def test_order_cap_includes_expected_costs(ledger, amount, cost):
    with pytest.raises(PilotLedgerError):
        ledger.reserve_and_claim("x", D(amount), D(cost), Account(), NOW)
    assert not ledger.snapshot()["submissions"]


def test_stale_account_and_float_money_rejected(ledger):
    with pytest.raises(ValueError, match="stale"):
        ledger.reserve_and_claim("x", D("99"), D("1"), Account(), NOW + timedelta(seconds=31))
    with pytest.raises(PilotLedgerError, match="exact_money"):
        ledger.reserve_and_claim("x", 99.0, D("1"), Account(), NOW)
    assert not ledger.snapshot()["submissions"]


def test_available_cash_reserve_includes_costs(ledger):
    low = updated(Account(), cash=D("374.99"))
    with pytest.raises(PilotLedgerError, match="cash_reserve"):
        ledger.reserve_and_claim("x", D("99"), D("1"), low, NOW)
    edge = updated(Account(), cash=D("375"))
    ledger.reserve_and_claim("x", D("99"), D("1"), edge, NOW)
    assert ledger.snapshot()["committed_capital"] == 100


def test_claim_survives_restart_and_unknown_outcome_never_releases(ledger):
    ledger.reserve_and_claim("x", D("99"), D("1"), Account(), NOW)
    ledger.record_outcome("x", {"status": "rejected", "token": "do-not-persist", "nested": {"api_key": "never"}})
    assert b"do-not-persist" not in ledger.path.read_bytes()
    restarted = PilotLedger(ledger.path, "123")
    assert restarted.snapshot()["unresolved"]
    assert restarted.snapshot()["committed_capital"] == 100
    with pytest.raises(PilotLedgerError, match="duplicate_command_lookup_only"):
        restarted.reserve_and_claim("x", D("99"), D("1"), Account(), NOW)
    tomorrow = NOW + timedelta(days=1)
    with pytest.raises(PilotLedgerError, match="unresolved"):
        restarted.reserve_and_claim("other", D("99"), D("1"), recapture(Account(), tomorrow), tomorrow)


def test_concurrent_claim_has_exactly_one_durable_winner(ledger):
    def claim(command):
        try:
            PilotLedger(ledger.path, "123").reserve_and_claim(command, D("99"), D("1"), Account(), NOW)
            return True
        except PilotLedgerError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ("one", "two"))) == [False, True]
    assert len(ledger.snapshot()["submissions"]) == 1


def test_first_fill_reconciles_cash_actual_costs_and_preserves_legacy(ledger):
    post, fill, pre = fill_order(ledger)
    s = ledger.snapshot()
    assert not s["unresolved"]
    assert s["position_count"] == 1
    assert s["committed_capital"] == 100
    assert s["pilot_pnl"] == -1
    assert s["positions"][0]["position_id"] == "100"
    assert replace(post.positions[0], source_timestamp=pre.positions[0].source_timestamp) == pre.positions[0]
    assert ledger.reconcile_open("first", (fill, fill), pre, post, NOW + timedelta(seconds=3)) == s
    with pytest.raises(PilotLedgerError, match="one_opening_submission"):
        ledger.reserve_and_claim("second", D("99"), D("1"), post, NOW + timedelta(seconds=3))


def test_london_day_boundary_not_utc_day(tmp_path):
    before_midnight = datetime(2026, 10, 23, 22, 59, 50, tzinfo=timezone.utc)
    a = recapture(Account(), before_midnight)
    l = PilotLedger.create(tmp_path / "london.sqlite3", a, [], before_midnight)
    post, _, _ = fill_order(l, a, before_midnight)
    after_midnight = datetime(2026, 10, 23, 23, 0, 1, tzinfo=timezone.utc)
    l.reserve_and_claim("new-london-day", D("99"), D("1"), recapture(post, after_midnight), after_midnight)
    assert [s["london_day"] for s in l.snapshot()["submissions"]] == ["2026-10-23", "2026-10-24"]


def test_five_positions_and_combined_cap_survive_restarts(ledger):
    account = Account()
    for i in range(5):
        account, _, _ = fill_order(ledger, account, NOW + timedelta(days=i), command=str(i), position_id=str(i+100))
        ledger = PilotLedger(ledger.path, "123")
    assert ledger.snapshot()["committed_capital"] == 500
    assert ledger.snapshot()["position_count"] == 5
    day = NOW + timedelta(days=5)
    with pytest.raises(PilotLedgerError, match="combined_capital_cap"):
        ledger.reserve_and_claim("six", D("1"), D("0"), recapture(account, day), day)


def test_position_count_cap_independent_of_capital(ledger):
    account = Account()
    for i in range(5):
        account, _, _ = fill_order(ledger, account, NOW + timedelta(days=i), command=str(i), amount="1", fees="0", position_id=str(i+100))
    day = NOW + timedelta(days=5)
    with pytest.raises(PilotLedgerError, match="maximum_five_positions"):
        ledger.reserve_and_claim("six", D("1"), D("0"), recapture(account, day), day)


@pytest.mark.parametrize("change,reason", [
    ({"cash": D("9900.02")}, "cash_reconciliation"),
    ({"positions": (Position(units=D("11")),)}, "position_set"),
])
def test_wrong_cash_or_missing_position_keeps_claim_unresolved(ledger, change, reason):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = updated(post_capture(pre, fill, NOW + timedelta(seconds=2)), **change)
    with pytest.raises(PilotLedgerError, match=reason):
        ledger.reconcile_open("x", (fill,), pre, post, NOW + timedelta(seconds=2))
    assert PilotLedger(ledger.path, "123").snapshot()["unresolved"]


def test_changed_legacy_or_partial_fill_cannot_reconcile(ledger):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = post_capture(pre, fill, NOW + timedelta(seconds=2))
    changed = updated(post, positions=(replace(post.positions[0], principal=D("999")), post.positions[1]))
    with pytest.raises(PilotLedgerError, match="existing_holding_changed"):
        ledger.reconcile_open("x", (fill,), pre, changed, NOW + timedelta(seconds=2))
    partial = replace(fill, executed_amount=D("98"))
    with pytest.raises(PilotLedgerError, match="terminal_full_requested_principal"):
        ledger.reconcile_open("x", (partial,), pre, post, NOW + timedelta(seconds=2))
    assert ledger.snapshot()["unresolved"]


def test_actual_cost_overrun_recorded_but_latches_hold(ledger):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(fees="2", now=NOW + timedelta(seconds=1))
    post = post_capture(pre, fill, NOW + timedelta(seconds=2))
    with pytest.raises(PilotLedgerError, match="actual_order_cap_exceeded"):
        ledger.reconcile_open("x", (fill,), pre, post, NOW + timedelta(seconds=2))
    s = PilotLedger(ledger.path, "123").snapshot()
    assert s["committed_capital"] == 101
    assert s["position_count"] == 1
    assert s["unresolved"]
    assert s["submissions"][0]["reconciled"]
    assert s["pilot_pnl"] == -2


def test_loss_latch_uses_prospective_costs_survives_recovery_restart(ledger):
    post, _, _ = fill_order(ledger)
    when = NOW + timedelta(days=1)
    loss = recapture(post, when, positions=(post.positions[0], replace(post.positions[1], pnl=D("-24"))))
    with pytest.raises(PilotLedgerError, match="loss_stop_latched"):
        ledger.reserve_and_claim("next", D("99"), D("1"), loss, when)
    restarted = PilotLedger(ledger.path, "123")
    assert restarted.snapshot()["pilot_pnl"] == -25
    assert restarted.snapshot()["loss_stopped"]
    recovered = recapture(post, when + timedelta(seconds=1))
    restarted.refresh_marks(recovered, when + timedelta(seconds=1))
    assert restarted.snapshot()["loss_stopped"]
    with pytest.raises(PilotLedgerError, match="loss_stop_latched"):
        restarted.reserve_and_claim("next", D("99"), D("1"), recovered, when + timedelta(seconds=1))


def test_missing_pilot_position_never_fabricates_zero_realised_loss(ledger):
    post, _, _ = fill_order(ledger)
    now = NOW + timedelta(seconds=10)
    closed_without_evidence = recapture(post, now, positions=(post.positions[0],))
    with pytest.raises(PilotLedgerError, match="closed_evidence_required"):
        ledger.refresh_marks(closed_without_evidence, now)
    s = PilotLedger(ledger.path, "123").snapshot()
    assert s["unresolved"]
    assert s["pilot_pnl"] == -1
    assert s["position_count"] == 1


def test_carrying_fee_change_requires_cash_evidence_without_double_subtraction(ledger):
    post, _, _ = fill_order(ledger)
    now = NOW + timedelta(seconds=10)
    changed = recapture(post, now, positions=(post.positions[0], replace(post.positions[1], carrying_fees=D("2"))))
    with pytest.raises(PilotLedgerError, match="carrying_fee_cash_movement"):
        ledger.refresh_marks(changed, now)
    assert ledger.snapshot()["pilot_pnl"] == -1
    assert ledger.snapshot()["unresolved"]


def test_database_prevents_mutating_inception_or_claims(ledger):
    ledger.reserve_and_claim("x", D("99"), D("1"), Account(), NOW)
    with sqlite3.connect(ledger.path) as db:
        for statement in ("DELETE FROM inception", "DELETE FROM submissions", "DELETE FROM events", "UPDATE inception SET digest='altered'"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute(statement)


def test_actual_supported_account_projection_is_accepted_and_immutable(tmp_path):
    from tests.test_pilot_account_source import build, NOW as source_now
    account = build()
    ledger = PilotLedger.create(tmp_path / "source.sqlite3", account, [{"type": "legacy_cfd"}], source_now)
    ledger.reserve_and_claim("source-command", D("99"), D("1"), account, source_now, instrument_id=1002)
    saved = ledger.snapshot()["inception"]["account"]
    assert saved["source_hash"] == account.source_hash
    assert saved["positions"][0]["principal"] == "100"
    assert saved["positions"][0]["carrying_fees"] == "-0.1"
    assert saved["raw_sources"]["pnl"] == account.raw_sources["pnl"]


@pytest.mark.parametrize("field,value", [
    ("environment", Environment.REAL), ("currency", "GBP"),
    ("side", "short"), ("action", "sell"), ("executed_amount", None),
    ("executed_at_source", None), ("units", D("0")),
    ("broker_instrument_id", 2),
])
def test_execution_wrong_scope_and_unknown_economics_never_release_claim(ledger, field, value):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW, instrument_id=1)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = post_capture(pre, fill, NOW + timedelta(seconds=2))
    invalid = replace(fill, **{field: value})
    with pytest.raises(PilotLedgerError):
        ledger.reconcile_open("x", (invalid,), pre, post, NOW + timedelta(seconds=2))
    assert ledger.snapshot()["unresolved"]
    assert not ledger.snapshot()["positions"]


def test_inconsistent_opening_fees_and_duplicate_execution_conflicts_block(ledger):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = post_capture(pre, fill, NOW + timedelta(seconds=2))
    changed = updated(post, positions=(post.positions[0], replace(post.positions[1], fees=D("0"))))
    with pytest.raises(PilotLedgerError, match="actual_costs_mismatch"):
        ledger.reconcile_open("x", (fill,), pre, changed, NOW + timedelta(seconds=2))
    with pytest.raises(PilotLedgerError, match="conflicting_duplicate_execution"):
        ledger.reconcile_open("x", (fill, replace(fill, fees=D("2"))), pre, post, NOW + timedelta(seconds=2))
    assert ledger.snapshot()["unresolved"]


def test_lookup_after_restart_uses_original_pre_capture_without_redating_it(ledger):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(now=NOW + timedelta(seconds=1))
    later = NOW + timedelta(hours=2)
    post = post_capture(pre, fill, later)
    restarted = PilotLedger(ledger.path, "123")
    result = restarted.reconcile_open("x", (fill,), pre, post, later)
    assert result["submissions"][0]["pre_account"]["capture_started_at"] == "2026-10-23T12:00:00Z"
    assert not result["unresolved"]
    assert result["pilot_pnl"] == -1


def test_tiny_cost_cannot_round_away_order_cap(ledger):
    with pytest.raises(PilotLedgerError, match="order_cap"):
        ledger.reserve_and_claim("tiny", D("100"), D("1E-40"), Account(), NOW)
    assert not ledger.snapshot()["submissions"]


def test_tiny_cash_shortfall_cannot_round_up_to_twenty_percent(ledger):
    tiny_shortfall = Account(cash=D("374.9999999999999999999999999999999999999999"))
    with pytest.raises(PilotLedgerError, match="cash_reserve"):
        ledger.reserve_and_claim("tiny", D("100"), D("0"), tiny_shortfall, NOW)
    assert not ledger.snapshot()["submissions"]


def test_arbitrary_validator_dataclass_cannot_authorize_a_pilot(tmp_path, ledger):
    @dataclass
    class FakeAccount:
        account_id: str = "123"
        def validate_fresh(self, now):
            pass
    fake = FakeAccount()
    with pytest.raises(PilotLedgerError, match="typed_pilot_account"):
        PilotLedger.create(tmp_path / "fake.sqlite3", fake, [], NOW)
    with pytest.raises(PilotLedgerError, match="typed_pilot_account"):
        ledger.reserve_and_claim("fake", D("99"), D("1"), fake, NOW)
    with pytest.raises(PilotLedgerError, match="typed_pilot_account"):
        ledger.refresh_marks(fake, NOW)
    assert not (tmp_path / "fake.sqlite3").exists()


def test_mutated_real_projection_does_not_preserve_eligibility(ledger):
    original = Account()
    # Preserve the raw source hash while altering the projected economic fields.
    altered = replace(original, cash=D("10001"), equity=D("11001"))
    with pytest.raises(ValueError, match="projection_mismatch"):
        ledger.reserve_and_claim("fake", D("99"), D("1"), altered, NOW)
    assert not ledger.snapshot()["submissions"]


@pytest.mark.parametrize("field,value", [("principal", D("99.0001")), ("fees", D("1.0001")), ("taxes", D("0.0001"))])
def test_subcent_position_cost_or_principal_difference_cannot_hide_cap_or_loss(ledger, field, value):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = post_capture(pre, fill, NOW + timedelta(seconds=2))
    changed = updated(post, positions=tuple(replace(p, **{field: value}) if p.position_id == "100" else p for p in post.positions))
    with pytest.raises(PilotLedgerError, match="actual_costs_mismatch"):
        ledger.reconcile_open("x", (fill,), pre, changed, NOW + timedelta(seconds=2))
    assert ledger.snapshot()["unresolved"]


def test_one_cent_missing_cash_charge_is_not_reconciled_by_tolerance(ledger):
    pre = Account()
    ledger.reserve_and_claim("x", D("99"), D("1"), pre, NOW)
    fill = execution(now=NOW + timedelta(seconds=1))
    post = post_capture(pre, fill, NOW + timedelta(seconds=2))
    wrong = updated(post, cash=D("9900.01"))
    with pytest.raises(PilotLedgerError, match="actual_cash_reconciliation_failed"):
        ledger.reconcile_open("x", (fill,), pre, wrong, NOW + timedelta(seconds=2))
    assert ledger.snapshot()["unresolved"]
