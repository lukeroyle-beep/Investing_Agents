"""Demo order guard. Profiles, prices, and ids in this file are example data.

Nothing here contacts eToro or places an order. The HTTP boundary is a fake
submit callable.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from brokers.base import BrokerExecution
from brokers.etoro.endpoints import SUBMIT_DEMO_ORDER_V3
from execution.demo_orders import (
    DEMO_ORDER_ALLOWLIST,
    DemoBrokerReceipt,
    DemoOrderAttempt,
    DemoOrderBlocked,
    DemoOrderConfig,
    DemoOrderLimits,
    TradeLog,
    confirm_demo_account,
    demo_execution_shape,
    demo_order_endpoint,
    place_demo_order,
)
from execution.domain import Environment
from risk.kill_switch import RESET_ACKNOWLEDGEMENT, KillSwitch
from risk.pretrade import RiskLimits
from shared.paths import EXECUTION_CONFIG_PATH, RISK_CONFIG_PATH, config_path


# Monday 23 Nov 2026, 10:00 America/New_York, inside the reviewed Nasdaq session.
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
ASK = Decimal("90")
SECRET = "example-header-value-not-a-credential"
REAL_WRITE_ENDPOINTS = (
    "/trading/execution/real",
    "/trading/execution/orders",
    "/trading/execution/market-open-orders",
    "/trading/execution/market-close-orders",
)


def _config(*, enabled: bool) -> DemoOrderConfig:
    return DemoOrderConfig(
        enabled=enabled,
        environment=Environment.DEMO,
        api_key_environment_variable="ETORO_DEMO_WRITE_API_KEY",
        user_key_environment_variable="ETORO_DEMO_WRITE_USER_KEY",
    )


def _limits() -> DemoOrderLimits:
    return DemoOrderLimits.load()


def _risk() -> RiskLimits:
    return RiskLimits.load(RISK_CONFIG_PATH)


def _switch(tmp_path, *, open_for_writes: bool) -> KillSwitch:
    switch = KillSwitch(
        state_path=tmp_path / "kill.json",
        audit_path=tmp_path / "kill.jsonl",
    )
    switch.initialize_engaged(operator_id="owner", reason="start engaged", now=NOW)
    if open_for_writes:
        switch.reset(
            operator_id="owner",
            reason="demo test",
            acknowledgement=RESET_ACKNOWLEDGEMENT,
            now=NOW,
        )
    return switch


def _preview(ask: Decimal, fee: Decimal = Decimal("1")) -> dict:
    return {
        "confirmation": {
            "costs": [{"type": "transactionFee", "value": fee, "currency": "USD"}],
            "totalUpfrontCost": fee,
            "estimatedUnits": Decimal("1"),
            "quote": {"ask": ask, "bid": ask - Decimal("0.01")},
        }
    }


def _attempt(**overrides) -> DemoOrderAttempt:
    values = dict(
        instrument_id=1142,
        symbol="SBUX",
        side="buy",
        amount=Decimal("10"),
        expected_account_id="123",
        preview_ask=ASK,
        fresh_ask=ASK,
        fee_preview=_preview(ASK),
        equity=Decimal("10000"),
        open_positions=0,
        instrument_exposure=Decimal("0"),
        daily_loss_fraction=Decimal("0"),
    )
    values.update(overrides)
    return DemoOrderAttempt(**values)


def _profile(**overrides) -> dict:
    # Example /api/v1/me shape. realCid can appear beside a demo key; scopes decide.
    profile = {
        "demoCid": 123,
        "realCid": 987,
        "scopes": [
            "etoro-public:trade.demo:read",
            "etoro-public:trade.demo:write",
        ],
    }
    profile.update(overrides)
    return profile


class _Boundary:
    def __init__(self, profile=..., receipt=None, error=None):
        self.profile = _profile() if profile is ... else profile
        self.receipt = receipt or DemoBrokerReceipt("order-1", "ref-1")
        self.error = error
        self.reads = 0
        self.calls = []

    def read(self):
        self.reads += 1
        if isinstance(self.profile, Exception):
            raise self.profile
        return self.profile

    def submit(self, path, body):
        self.calls.append((path, dict(body)))
        if self.error is not None:
            raise self.error
        return self.receipt


def _place(tmp_path, attempt, boundary, *, enabled=True, now=NOW, switch=None, **kwargs):
    if switch is None:
        switch_dir = tmp_path / f"switch-{uuid4().hex}"
        switch_dir.mkdir()
        switch = _switch(switch_dir, open_for_writes=True)
    return place_demo_order(
        config=_config(enabled=enabled),
        limits=_limits(),
        risk_limits=_risk(),
        attempt=attempt,
        profile_reader=boundary.read,
        submit=boundary.submit,
        kill_switch=switch,
        trade_log=kwargs.pop("trade_log", TradeLog(tmp_path / "trades.jsonl")),
        now=now,
        **kwargs,
    )


def test_shipped_defaults_are_off_and_limits_are_the_proposal():
    config = DemoOrderConfig.load()
    limits = DemoOrderLimits.load()
    assert config.enabled is False
    assert config.environment == Environment.DEMO
    assert (config.api_key_environment_variable, config.user_key_environment_variable) == (
        "ETORO_DEMO_WRITE_API_KEY",
        "ETORO_DEMO_WRITE_USER_KEY",
    )
    assert limits.max_position_equity_fraction == Decimal("0.01")
    assert limits.max_position_notional_usd == Decimal("100")
    assert limits.max_open_positions == 3
    assert limits.max_daily_loss_fraction == Decimal("0.005")
    execution = yaml.safe_load(EXECUTION_CONFIG_PATH.read_text(encoding="utf-8"))
    governance = yaml.safe_load(config_path("governance.yaml").read_text(encoding="utf-8"))
    assert execution["broker_writes_enabled"] is False
    assert execution["intent_import_enabled"] is False
    assert governance["execution_mode"] == "advisory_only"
    assert governance["allow_order_submission"] is False


def test_disabled_flag_logs_and_does_not_touch_the_broker(tmp_path):
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(), boundary, enabled=False)
    assert result.sent is False
    assert result.reason == "demo_order_placement_disabled"
    assert boundary.reads == 0
    assert boundary.calls == []
    record = TradeLog(tmp_path / "trades.jsonl").read()[0]
    assert record["outcome"] == "rejected_guard"
    assert record["timestamp_utc"].endswith("+00:00")
    assert record["instrument"] == "SBUX"
    assert record["side"] == "buy"


def test_allowlist_is_only_the_pinned_demo_order_route():
    assert DEMO_ORDER_ALLOWLIST == {("POST", "/api/v3/trading/execution/demo/orders")}
    assert demo_order_endpoint("POST", SUBMIT_DEMO_ORDER_V3.path) is True
    assert demo_execution_shape(SUBMIT_DEMO_ORDER_V3.path) is True
    assert demo_order_endpoint("GET", SUBMIT_DEMO_ORDER_V3.path) is False
    assert demo_order_endpoint("POST", "https://public-api.etoro.com" + SUBMIT_DEMO_ORDER_V3.path) is False


@pytest.mark.parametrize("path", [
    "/api/v3/trading/execution/orders",
    "/api/v2/trading/execution/orders",
    "/api/v2/trading/execution/real/orders",
    "/api/v1/trading/execution/market-open-orders/by-amount",
    "/api/v1/trading/execution/market-open-orders/by-units",
    "/api/v1/trading/execution/market-close-orders/positions/1",
    "/api/v1/trading/execution/demo/market-close-orders/positions/1",
    "/api/v2/trading/execution/demo/orders",
    "/unknown",
])
def test_non_allowlisted_endpoints_are_not_sent(tmp_path, path):
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(path=path), boundary)
    assert result.sent is False
    assert result.reason == "endpoint_not_allowlisted"
    assert boundary.reads == 0
    assert boundary.calls == []
    assert demo_execution_shape(path) is False or path != SUBMIT_DEMO_ORDER_V3.path


def test_real_route_stays_blocked_even_if_added_to_the_allowlist(tmp_path, monkeypatch):
    real_path = "/api/v3/trading/execution/orders"
    monkeypatch.setattr(
        "execution.demo_orders.DEMO_ORDER_ALLOWLIST",
        frozenset({("POST", real_path)}),
    )
    assert demo_order_endpoint("POST", real_path) is False
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(path=real_path), boundary)
    assert result.sent is False
    assert boundary.calls == []


@pytest.mark.parametrize("profile", [
    None,
    {},
    {"realCid": 987, "scopes": ["etoro-public:trade.real:write"]},
    {"demoCid": 123, "scopes": ["etoro-public:trade.real:write"]},
    {"demoCid": 123, "scopes": ["etoro-public:trade.demo:read"]},
    {"demoCid": 123, "scopes": ["etoro-public:trade.demo:write"], "accountType": "real"},
    {"demoCid": 999, "scopes": ["etoro-public:trade.demo:write"]},
    {"demoCid": 123, "scopes": []},
])
def test_guard_refuses_real_or_unknown_accounts(tmp_path, profile):
    boundary = _Boundary(profile=profile)
    result = _place(tmp_path, _attempt(), boundary)
    assert result.sent is False
    assert result.outcome == "rejected_guard"
    assert boundary.calls == []
    assert boundary.reads == 1


def test_broker_read_failure_is_logged_without_the_exception_text(tmp_path, caplog):
    boundary = _Boundary(profile=RuntimeError(f"x-api-key: {SECRET}"))
    with caplog.at_level(logging.WARNING):
        result = _place(tmp_path, _attempt(), boundary)
    assert result.reason == "broker_unavailable"
    assert boundary.calls == []
    blob = caplog.text + (tmp_path / "trades.jsonl").read_text(encoding="utf-8")
    assert SECRET not in blob
    assert "x-api-key" not in blob


def test_confirmed_demo_profile_can_include_an_unused_real_customer_id():
    assert confirm_demo_account(_profile(), "123") == "123"


@pytest.mark.parametrize("overrides,reason", [
    ({"side": "sell"}, "long_only"),
    ({"amount": 10.0}, "order_evidence_unconfirmed"),
    ({"fresh_ask": None}, "order_evidence_unconfirmed"),
    ({"fee_preview": None}, "fee_model_unconfirmed"),
    ({"daily_loss_fraction": None}, "daily_loss_unconfirmed"),
    ({"open_positions": None}, "open_positions_unconfirmed"),
])
def test_missing_or_inexact_evidence_fails_closed(tmp_path, overrides, reason):
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(**overrides), boundary)
    assert result.sent is False
    assert result.reason == reason
    assert boundary.calls == []


@pytest.mark.parametrize("now,reason", [
    (datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc), "market_closed"),
    (datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc), "market_closed"),
    (datetime(2026, 11, 23, 21, 30, tzinfo=timezone.utc), "market_closed"),
    (datetime(2026, 11, 23, 15, 0), "timezone_unconfirmed"),
    (datetime(2027, 11, 23, 15, 0, tzinfo=timezone.utc), "market_calendar_unconfirmed"),
])
def test_closed_or_unconfirmed_session_sends_nothing(tmp_path, now, reason):
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(), boundary, now=now)
    assert result.reason == reason
    assert boundary.reads == 0
    assert boundary.calls == []


def test_kill_switch_blocks_before_any_broker_call(tmp_path):
    boundary = _Boundary()
    engaged = _switch(tmp_path, open_for_writes=False)
    result = _place(tmp_path, _attempt(), boundary, switch=engaged)
    assert result.reason == "kill_switch_blocked"
    assert boundary.calls == []
    missing = KillSwitch(state_path=tmp_path / "absent.json", audit_path=tmp_path / "absent.jsonl")
    result = _place(tmp_path, _attempt(), _Boundary(), switch=missing)
    assert result.reason == "kill_switch_blocked"


def test_price_band_uses_the_configured_reapproval_band(tmp_path):
    risk = _risk()
    moved = ASK + (ASK * risk.reapproval_price_band) + Decimal("0.01")
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(fresh_ask=moved), boundary)
    assert result.reason == "price_band"
    assert result.outcome == "rejected_limits"
    assert boundary.calls == []
    inside = _place(tmp_path, _attempt(fresh_ask=ASK), _Boundary())
    assert inside.sent is True


def test_position_limits_include_fees_fraction_absolute_cap_and_count(tmp_path):
    limits = _limits()
    boundary = _Boundary()
    over_fraction = _place(
        tmp_path,
        _attempt(equity=Decimal("1000"), amount=Decimal("50"), instrument_exposure=Decimal("0")),
        boundary,
    )
    assert over_fraction.reason == "position_size"
    over_absolute = _place(
        tmp_path,
        _attempt(equity=Decimal("1000000"), amount=limits.max_position_notional_usd),
        _Boundary(),
    )
    assert over_absolute.reason == "position_size"
    fee_tips_the_cap = _place(
        tmp_path,
        _attempt(
            equity=Decimal("100000"),
            instrument_exposure=Decimal("90"),
            amount=Decimal("10"),
            open_positions=1,
        ),
        _Boundary(),
    )
    assert fee_tips_the_cap.reason == "position_size"
    at_cap_without_a_fee = _place(
        tmp_path,
        _attempt(
            equity=Decimal("100000"),
            instrument_exposure=Decimal("90"),
            amount=Decimal("10"),
            open_positions=1,
            fee_preview=_preview(ASK, Decimal("0")),
        ),
        _Boundary(),
    )
    assert at_cap_without_a_fee.sent is True
    too_many = _place(
        tmp_path,
        _attempt(open_positions=limits.max_open_positions, instrument_exposure=Decimal("0")),
        _Boundary(),
    )
    assert too_many.reason == "open_position_count"
    adding_to_an_existing_name = _place(
        tmp_path,
        _attempt(open_positions=limits.max_open_positions, instrument_exposure=Decimal("10")),
        _Boundary(),
    )
    assert adding_to_an_existing_name.sent is True
    assert boundary.calls == []


def test_daily_loss_engages_the_kill_switch_for_the_rest_of_the_new_york_day(tmp_path):
    switch = _switch(tmp_path, open_for_writes=True)
    log = TradeLog(tmp_path / "trades.jsonl")
    first = _Boundary()
    halted = _place(
        tmp_path,
        _attempt(daily_loss_fraction=Decimal("0.005")),
        first,
        switch=switch,
        trade_log=log,
    )
    assert halted.reason == "daily_loss_halt"
    assert first.calls == []
    assert switch.status().writes_allowed is False
    assert "2026-11-23" in log.halted_dates()
    switch.reset(
        operator_id="owner",
        reason="owner reset same day",
        acknowledgement=RESET_ACKNOWLEDGEMENT,
        now=NOW,
    )
    same_day = _Boundary()
    blocked = _place(
        tmp_path,
        _attempt(daily_loss_fraction=Decimal("0")),
        same_day,
        switch=switch,
        trade_log=log,
    )
    assert blocked.reason == "daily_loss_halt"
    assert same_day.calls == []
    later = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
    nxt = _Boundary()
    sent = _place(
        tmp_path,
        _attempt(daily_loss_fraction=Decimal("0")),
        nxt,
        switch=switch,
        trade_log=log,
        now=later,
    )
    assert sent.sent is True
    assert nxt.calls[0][0] == SUBMIT_DEMO_ORDER_V3.path


def test_broker_error_is_logged_and_not_retried(tmp_path, caplog):
    boundary = _Boundary(error=RuntimeError(f"x-user-key: {SECRET}"))
    with caplog.at_level(logging.WARNING):
        result = _place(tmp_path, _attempt(), boundary)
    assert result.outcome == "broker_error"
    assert result.sent is False
    assert len(boundary.calls) == 1
    blob = caplog.text + (tmp_path / "trades.jsonl").read_text(encoding="utf-8")
    assert SECRET not in blob
    assert "x-user-key" not in blob
    record = TradeLog(tmp_path / "trades.jsonl").read()[-1]
    assert record["outcome"] == "broker_error"
    assert record["reason"] == "broker_unavailable"


def test_accepted_order_and_fill_are_logged_and_only_staged_for_the_fill_agent(tmp_path):
    execution = BrokerExecution(
        broker_execution_id="ex-1",
        broker_order_id="order-1",
        broker_instrument_id=1142,
        broker_position_id="pos-1",
        broker_reference_id="ref-1",
        environment=Environment.DEMO,
        side="long",
        action="buy",
        units=Decimal("0.1"),
        price=Decimal("90"),
        executed_at=NOW,
        price_rate_id="rate-1",
        fees=Decimal("1"),
        taxes=Decimal("0"),
        currency="USD",
        executed_amount=Decimal("10"),
    )
    boundary = _Boundary(receipt=DemoBrokerReceipt("order-1", "ref-1", (execution,)))
    fills = tmp_path / "broker_fills.csv"
    result = _place(
        tmp_path,
        _attempt(),
        boundary,
        staged_fills_path=fills,
        ticker_by_instrument_id={1142: "SBUX"},
    )
    assert result.sent is True
    assert result.order_id == "order-1"
    assert result.reference_id == "ref-1"
    assert boundary.calls[0][0] == "/api/v3/trading/execution/demo/orders"
    body = boundary.calls[0][1]
    assert isinstance(body["amount"], Decimal)
    assert body["leverage"] == 1
    assert body["transaction"] == "buy"
    staged = fills.read_text(encoding="utf-8")
    assert "etoro:demo:ex-1" in staged
    assert "SBUX" in staged
    assert not (tmp_path / "portfolio_state.csv").exists()
    assert not (tmp_path / "cash_state.csv").exists()
    outcomes = [row["outcome"] for row in TradeLog(tmp_path / "trades.jsonl").read()]
    assert outcomes == ["accepted", "fill"]
    fill = TradeLog(tmp_path / "trades.jsonl").read()[1]
    assert fill["instrument"] == "SBUX:1142"
    assert fill["side"] == "buy"
    assert fill["size"] == "0.1"
    assert fill["price"] == "90"
    assert fill["broker_order_id"] == "order-1"
    assert fill["broker_reference_id"] == "ref-1"
    assert fill["broker_execution_id"] == "ex-1"
    assert SECRET not in staged


def test_real_or_short_fill_is_not_staged(tmp_path):
    execution = replace(
        BrokerExecution(
            broker_execution_id="ex-real",
            broker_order_id="order-1",
            broker_instrument_id=1142,
            broker_position_id="pos-1",
            broker_reference_id="ref-1",
            environment=Environment.REAL,
            side="long",
            action="buy",
            units=Decimal("1"),
            price=Decimal("90"),
            executed_at=NOW,
            price_rate_id="rate-1",
            fees=Decimal("0"),
            taxes=Decimal("0"),
            currency="USD",
        ),
    )
    boundary = _Boundary(receipt=DemoBrokerReceipt("order-1", "ref-1", (execution,)))
    fills = tmp_path / "broker_fills.csv"
    result = _place(
        tmp_path,
        _attempt(),
        boundary,
        staged_fills_path=fills,
        ticker_by_instrument_id={1142: "SBUX"},
    )
    assert result.sent is True
    assert not fills.exists()
    assert TradeLog(tmp_path / "trades.jsonl").read()[-1]["reason"] == "fill_not_demo_long"


def test_trade_log_is_append_only_and_refuses_secrets(tmp_path):
    path = tmp_path / "trades.jsonl"
    log = TradeLog(path)
    _place(tmp_path, _attempt(side="sell"), _Boundary(), trade_log=log)
    first = path.read_text(encoding="utf-8")
    _place(tmp_path, _attempt(side="sell"), _Boundary(), trade_log=log)
    text = path.read_text(encoding="utf-8")
    assert text.startswith(first)
    assert len(text.splitlines()) == 2
    with pytest.raises(DemoOrderBlocked, match="secret"):
        log.append({"x-api-key": SECRET, "outcome": "accepted"})
    with pytest.raises(DemoOrderBlocked, match="secret"):
        log.append({"reason": f"x-user-key: {SECRET}", "outcome": "accepted"})
    assert path.read_text(encoding="utf-8") == text


def test_corrupt_trade_log_blocks_the_order(tmp_path):
    path = tmp_path / "trades.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    boundary = _Boundary()
    result = _place(tmp_path, _attempt(), boundary, trade_log=TradeLog(path))
    assert result.reason == "trade_log_unreadable"
    assert boundary.calls == []


def test_production_sources_contain_no_real_account_write_endpoint():
    root = Path(__file__).resolve().parents[1]
    skip = {
        ".git", ".venv", ".venv-test", "venv", "tests", "__pycache__",
        "runtime", "data", ".pytest_cache", "node_modules",
    }
    hits = []
    for path in root.rglob("*"):
        if not path.is_file() or any(part in skip for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".yaml", ".yml", ".md", ".toml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for marker in REAL_WRITE_ENDPOINTS:
            if marker in text:
                hits.append(f"{path.relative_to(root)} contains {marker}")
    assert hits == []


def test_order_path_does_not_read_credentials_or_write_economics():
    source = Path(place_demo_order.__code__.co_filename).read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source
    assert "open_long_position" not in source
    assert "close_long_position" not in source
    assert "portfolio_state" not in source
    assert "write_cash_balance" not in source
    for marker in REAL_WRITE_ENDPOINTS:
        assert marker not in source.lower()
