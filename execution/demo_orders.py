"""Fail-closed eToro demo order placement. Real-money routes are not defined.

An order is handed to the injected sender only when every check holds: the
demo flag in ``config/demo_orders.yaml`` is on, the method and path are the
single allowlisted demo order route, and a runtime ``/api/v1/me`` profile
confirms a demo write account. Anything missing, malformed, or non-demo is
logged and not sent.

The allowlisted route is the pinned demo submit already used by the demo
execution client:

    POST /api/v3/trading/execution/demo/orders

eToro's public reference places virtual trading under the ``demo`` path
segment (api-portal.etoro.com). This module never builds another execution
route. ``settlementType`` ``real`` on the body is eToro's unleveraged share
settlement on that demo route, not a real-money account.

Fills are staged for the Fill Agent. This module does not write positions,
cash, or P&L.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping

import yaml

from brokers.etoro.connector import positive_id
from brokers.etoro.endpoints import SUBMIT_DEMO_ORDER_V3
from brokers.etoro.execution_client import EtoroDemoExecutionClient
from brokers.etoro.redaction import redact_text
from execution.accounting import _sum as exact_sum
from execution.domain import DomainValidationError, Environment, _aware_utc, decimal_text
from execution.fill_ingestion import stage_broker_executions
from execution.market_session import DEFAULT_CALENDAR_PATH, _NEW_YORK, _validate_calendar
from execution.pilot_policy import multiply, preview_costs, price_move_exceeds_band
from risk.kill_switch import KillSwitch, KillSwitchError
from risk.pretrade import RiskLimits


logger = logging.getLogger("execution.demo_orders")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "demo_orders.yaml"
DEMO_ORDER_ALLOWLIST = frozenset({
    (SUBMIT_DEMO_ORDER_V3.method, SUBMIT_DEMO_ORDER_V3.path),
})
DEMO_WRITE_SCOPES = frozenset({
    "etoro-public:demo:write",
    "etoro-public:trade.demo:write",
})
_SECRET_KEY = (
    "api_key", "user_key", "authorization", "x-api-key", "x-user-key",
    "credential", "secret", "password", "headers",
)
_ACCOUNT_TYPE_KEYS = ("accountType", "account_type", "environment")


class DemoOrderBlocked(RuntimeError):
    """Fail-closed configuration or log failure. The message is a reason code."""


@dataclass(frozen=True, slots=True)
class DemoOrderConfig:
    enabled: bool
    environment: Environment
    api_key_environment_variable: str
    user_key_environment_variable: str

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "DemoOrderConfig":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            names = raw["credential_environment_variables"]
            config = cls(
                enabled=raw["demo_order_placement_enabled"] is True,
                environment=Environment(str(raw["environment"]).strip().lower()),
                api_key_environment_variable=str(names["api_key"]),
                user_key_environment_variable=str(names["user_key"]),
            )
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise DemoOrderBlocked("demo_order_config_invalid") from exc
        if config.environment != Environment.DEMO:
            raise DemoOrderBlocked("demo_order_environment_refused")
        if (config.api_key_environment_variable, config.user_key_environment_variable) != (
            "ETORO_DEMO_WRITE_API_KEY",
            "ETORO_DEMO_WRITE_USER_KEY",
        ):
            raise DemoOrderBlocked("demo_order_credential_names_refused")
        return config


@dataclass(frozen=True, slots=True)
class DemoOrderLimits:
    """Proposed caps. They are inclusive position maximums, not targets."""

    max_position_equity_fraction: Decimal
    max_position_notional_usd: Decimal
    max_open_positions: int
    max_daily_loss_fraction: Decimal

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "DemoOrderLimits":
        try:
            raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
            limits = cls(
                max_position_equity_fraction=Decimal(str(raw["max_position_equity_fraction"])),
                max_position_notional_usd=Decimal(str(raw["max_position_notional_usd"])),
                max_open_positions=int(raw["max_open_positions"]),
                max_daily_loss_fraction=Decimal(str(raw["max_daily_loss_fraction"])),
            )
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise DemoOrderBlocked("demo_order_limits_invalid") from exc
        fractions = (limits.max_position_equity_fraction, limits.max_daily_loss_fraction)
        if any(not value.is_finite() or value <= 0 or value > 1 for value in fractions):
            raise DemoOrderBlocked("demo_order_limits_invalid")
        if (not limits.max_position_notional_usd.is_finite()
                or limits.max_position_notional_usd <= 0
                or isinstance(raw["max_open_positions"], bool)
                or limits.max_open_positions < 1):
            raise DemoOrderBlocked("demo_order_limits_invalid")
        return limits


@dataclass(frozen=True, slots=True)
class DemoOrderAttempt:
    instrument_id: int
    symbol: str
    side: str
    amount: object
    expected_account_id: str
    preview_ask: object = None
    fresh_ask: object = None
    fee_preview: Mapping | None = None
    equity: object = None
    open_positions: int | None = None
    instrument_exposure: object = None
    daily_loss_fraction: object = None
    method: str = "POST"
    path: str = SUBMIT_DEMO_ORDER_V3.path


@dataclass(frozen=True, slots=True)
class DemoBrokerReceipt:
    """Example-shaped broker acknowledgement. Executions are fills, not 202s."""

    order_id: str
    reference_id: str
    executions: tuple = ()


@dataclass(frozen=True, slots=True)
class DemoOrderResult:
    sent: bool
    outcome: str
    reason: str
    order_id: str = ""
    reference_id: str = ""


class TradeLog:
    """Append-only JSONL of attempts and fills. Secret-shaped records are refused."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass

    def read(self) -> tuple[dict, ...]:
        if not self.path.exists():
            return ()
        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DemoOrderBlocked("trade_log_unreadable") from exc
                if not isinstance(record, dict):
                    raise DemoOrderBlocked(f"trade_log_unreadable:{line_number}")
                records.append(record)
        return tuple(records)

    def halted_dates(self) -> frozenset[str]:
        return frozenset(
            str(record.get("ny_date"))
            for record in self.read()
            if record.get("outcome") == "daily_loss_halt" and record.get("ny_date")
        )

    def append(self, record: Mapping) -> None:
        if any(str(key).lower().replace("-", "_") in _SECRET_KEY or str(key).lower() in _SECRET_KEY
               for key in record):
            raise DemoOrderBlocked("trade_log_refused_secret")
        encoded = json.dumps(dict(record), sort_keys=True, separators=(",", ":"))
        if redact_text(encoded) != encoded or "\n" in encoded:
            raise DemoOrderBlocked("trade_log_refused_secret")
        line = encoded + "\n"
        descriptor = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass


def demo_execution_shape(path: str) -> bool:
    """True when ``path`` is the demo order route and not a real-account route.

    Real v2/v3 order routes omit the ``demo`` segment that follows
    ``execution``. Membership in the allowlist is not enough on its own:
    a real route added there still fails this shape check.
    """
    if not isinstance(path, str):
        return False
    parts = [part for part in path.split("/") if part]
    try:
        execution_at = parts.index("execution")
    except ValueError:
        return False
    return (
        execution_at + 2 < len(parts)
        and parts[execution_at + 1] == "demo"
        and parts[execution_at + 2:] == ["orders"]
        and "real" not in parts
    )


def demo_order_endpoint(method: str, path: str) -> bool:
    """True only for the allowlisted demo order route, checked by path shape too."""
    if not isinstance(method, str) or not isinstance(path, str):
        return False
    return (method, path) in DEMO_ORDER_ALLOWLIST and demo_execution_shape(path)


def confirm_demo_account(profile: object, expected_account_id: str) -> str:
    """Return the demo customer id, or raise when the profile is not demo."""
    if not isinstance(profile, Mapping):
        raise DemoOrderBlocked("demo_account_unconfirmed")
    try:
        demo_cid = positive_id(profile.get("demoCid"), "demo_cid")
        expected = positive_id(expected_account_id, "expected_account_id")
    except Exception as exc:
        raise DemoOrderBlocked("demo_account_unconfirmed") from exc
    if demo_cid != expected:
        raise DemoOrderBlocked("demo_account_mismatch")
    scopes = profile.get("scopes")
    if not isinstance(scopes, list) or not scopes or not all(isinstance(item, str) for item in scopes):
        raise DemoOrderBlocked("demo_account_unconfirmed")
    lowered = {item.strip().lower() for item in scopes}
    if any(":real:" in item or item.endswith(":real") for item in lowered):
        raise DemoOrderBlocked("real_account_refused")
    if not lowered & {item.lower() for item in DEMO_WRITE_SCOPES}:
        raise DemoOrderBlocked("demo_write_scope_unconfirmed")
    for key in _ACCOUNT_TYPE_KEYS:
        if key in profile and str(profile[key]).strip().lower() != "demo":
            raise DemoOrderBlocked("real_account_refused")
    return demo_cid


def _exact_money(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, Decimal):
        raise DemoOrderBlocked(f"{name}_not_exact_decimal")
    if not value.is_finite():
        raise DemoOrderBlocked(f"{name}_not_exact_decimal")
    return value


def regular_session_reason(now: datetime) -> str | None:
    """None when the reviewed Nasdaq 2026 regular session is open.

    The symbol allowlist on the pilot session helper does not include the
    demo order instrument, so the clock comes from the same calendar file
    and checksum without that symbol gate. An unreadable calendar, a date
    outside 2026, or a closed session fails closed.
    """
    try:
        moment = _aware_utc(now, "now")
    except (DomainValidationError, TypeError, ValueError):
        return "timezone_unconfirmed"
    day = moment.astimezone(_NEW_YORK).date()
    try:
        holidays, early = _validate_calendar(DEFAULT_CALENDAR_PATH.read_bytes())
    except Exception:
        return "market_calendar_unconfirmed"
    if day.year != 2026 or day.weekday() >= 5 or day in holidays:
        return "market_closed" if day.year == 2026 else "market_calendar_unconfirmed"
    opens_at = datetime.combine(day, time(9, 30), _NEW_YORK).astimezone(timezone.utc)
    closes_at = datetime.combine(
        day, early.get(day, time(16)), _NEW_YORK
    ).astimezone(timezone.utc)
    if not opens_at <= moment < closes_at:
        return "market_closed"
    return None


def _log_attempt(
    trade_log: TradeLog,
    *,
    now: datetime,
    instrument: str,
    side: str,
    size: str,
    price: str,
    reason: str,
    outcome: str,
    order_id: str = "",
    reference_id: str = "",
    execution_id: str = "",
    ny_date: str = "",
) -> None:
    moment = _aware_utc(now, "now")
    record = {
        "timestamp_utc": moment.isoformat(),
        "ny_date": ny_date,
        "instrument": instrument,
        "side": side,
        "size": size,
        "price": price,
        "reason": reason,
        "outcome": outcome,
        "broker_order_id": order_id,
        "broker_reference_id": reference_id,
        "broker_execution_id": execution_id,
    }
    trade_log.append(record)
    logger.warning(
        "demo order %s reason=%s instrument=%s side=%s size=%s price=%s order_id=%s reference_id=%s execution_id=%s",
        outcome,
        reason,
        instrument,
        side,
        size,
        price,
        order_id,
        reference_id,
        execution_id,
    )


def place_demo_order(
    *,
    config: DemoOrderConfig,
    limits: DemoOrderLimits,
    risk_limits: RiskLimits,
    attempt: DemoOrderAttempt,
    profile_reader: Callable[[], Mapping],
    submit: Callable[[str, Mapping], DemoBrokerReceipt],
    kill_switch: KillSwitch,
    trade_log: TradeLog,
    now: datetime,
    staged_fills_path: Path | str | None = None,
    ticker_by_instrument_id: Mapping[int, str] | None = None,
) -> DemoOrderResult:
    """Check every gate, then send at most one allowlisted demo order."""
    symbol = str(getattr(attempt, "symbol", "") or "").strip().upper()
    side = str(getattr(attempt, "side", "") or "").strip().lower()
    try:
        moment = _aware_utc(now, "now")
        ny_date = moment.astimezone(_NEW_YORK).date().isoformat()
    except (DomainValidationError, TypeError, ValueError, AttributeError):
        moment = datetime.now(timezone.utc)
        ny_date = ""
        _refuse(trade_log, moment, symbol, side, "", "", "timezone_unconfirmed", "rejected_guard", ny_date)
        return DemoOrderResult(False, "rejected_guard", "timezone_unconfirmed")

    def refuse(outcome: str, reason: str, *, price: str = "", size: str = "") -> DemoOrderResult:
        _refuse(trade_log, moment, symbol, side, size, price, reason, outcome, ny_date)
        return DemoOrderResult(False, outcome, reason)

    if not isinstance(config, DemoOrderConfig) or config.enabled is not True or config.environment != Environment.DEMO:
        return refuse("rejected_guard", "demo_order_placement_disabled")
    if not isinstance(risk_limits, RiskLimits) or risk_limits.environment != Environment.DEMO:
        return refuse("rejected_guard", "demo_risk_limits_required")
    if not demo_order_endpoint(attempt.method, attempt.path):
        return refuse("rejected_guard", "endpoint_not_allowlisted")
    if side != "buy":
        return refuse("rejected_guard", "long_only")
    if not isinstance(kill_switch, KillSwitch):
        return refuse("rejected_guard", "kill_switch_unconfirmed")
    status = kill_switch.status()
    if status.environment != Environment.DEMO or not status.writes_allowed:
        return refuse("rejected_guard", "kill_switch_blocked")
    session_reason = regular_session_reason(moment)
    if session_reason:
        return refuse("rejected_guard", session_reason)

    try:
        amount = _exact_money(attempt.amount, "amount")
        preview_ask = _exact_money(attempt.preview_ask, "preview_ask")
        fresh_ask = _exact_money(attempt.fresh_ask, "fresh_ask")
        equity = _exact_money(attempt.equity, "equity")
        exposure = _exact_money(attempt.instrument_exposure, "instrument_exposure")
        instrument_id = int(attempt.instrument_id)
    except (DemoOrderBlocked, TypeError, ValueError):
        return refuse("rejected_guard", "order_evidence_unconfirmed")
    if (isinstance(attempt.instrument_id, bool) or instrument_id <= 0 or amount <= 0
            or preview_ask <= 0 or fresh_ask <= 0 or equity <= 0 or exposure < 0
            or not symbol):
        return refuse("rejected_guard", "order_evidence_unconfirmed")
    price = decimal_text(fresh_ask)
    size = decimal_text(amount)
    try:
        if price_move_exceeds_band(preview_ask, fresh_ask, risk_limits.reapproval_price_band):
            return refuse("rejected_limits", "price_band", price=price, size=size)
    except Exception:
        return refuse("rejected_guard", "price_band_unconfirmed", price=price)
    if not isinstance(attempt.fee_preview, Mapping):
        return refuse("rejected_guard", "fee_model_unconfirmed", price=price, size=size)
    try:
        upfront, _reserved = preview_costs(attempt.fee_preview)
        quoted_ask = _exact_money(
            attempt.fee_preview["confirmation"]["quote"]["ask"], "preview_quote_ask"
        )
    except (DemoOrderBlocked, KeyError, TypeError, ValueError):
        return refuse("rejected_guard", "fee_model_unconfirmed", price=price, size=size)
    if quoted_ask != preview_ask or upfront < 0:
        return refuse("rejected_guard", "fee_model_unconfirmed", price=price, size=size)
    commitment = exact_sum((amount, upfront))
    projected = exact_sum((exposure, commitment))
    cap = min(multiply(equity, limits.max_position_equity_fraction), limits.max_position_notional_usd)
    if projected > cap:
        return refuse("rejected_limits", "position_size", price=price, size=size)
    if isinstance(attempt.open_positions, bool) or not isinstance(attempt.open_positions, int):
        return refuse("rejected_guard", "open_positions_unconfirmed", price=price, size=size)
    if attempt.open_positions < 0:
        return refuse("rejected_guard", "open_positions_unconfirmed", price=price, size=size)
    if exposure == 0 and attempt.open_positions >= limits.max_open_positions:
        return refuse("rejected_limits", "open_position_count", price=price, size=size)
    try:
        if ny_date in trade_log.halted_dates():
            return refuse("rejected_limits", "daily_loss_halt", price=price, size=size)
    except DemoOrderBlocked:
        return refuse("rejected_guard", "trade_log_unreadable", price=price, size=size)
    if attempt.daily_loss_fraction is None:
        return refuse("rejected_guard", "daily_loss_unconfirmed", price=price, size=size)
    try:
        daily_loss = _exact_money(attempt.daily_loss_fraction, "daily_loss")
    except DemoOrderBlocked:
        return refuse("rejected_guard", "daily_loss_unconfirmed", price=price, size=size)
    if daily_loss < 0:
        return refuse("rejected_guard", "daily_loss_unconfirmed", price=price, size=size)
    if daily_loss >= limits.max_daily_loss_fraction:
        try:
            _log_attempt(
                trade_log, now=moment, instrument=f"{symbol}:{instrument_id}", side=side,
                size=size, price=price, reason="daily_loss_halt", outcome="daily_loss_halt",
                ny_date=ny_date,
            )
            kill_switch.engage(operator_id="demo-order-guard", reason="daily loss halt", now=moment)
        except (DemoOrderBlocked, KillSwitchError, OSError) as exc:
            logger.warning("demo order daily loss halt could not be recorded (%s)", type(exc).__name__)
        return DemoOrderResult(False, "rejected_limits", "daily_loss_halt")

    try:
        profile = profile_reader()
        confirm_demo_account(profile, attempt.expected_account_id)
    except DemoOrderBlocked as exc:
        return refuse("rejected_guard", str(exc) or "demo_account_unconfirmed", price=price, size=size)
    except Exception:
        return refuse("rejected_guard", "broker_unavailable", price=price, size=size)
    if not kill_switch.status().writes_allowed:
        return refuse("rejected_guard", "kill_switch_blocked", price=price, size=size)
    if not demo_order_endpoint(attempt.method, attempt.path):
        return refuse("rejected_guard", "endpoint_not_allowlisted", price=price, size=size)

    body = {
        "action": "open",
        "transaction": "buy",
        "instrumentId": instrument_id,
        "settlementType": "real",
        "orderType": "mkt",
        "leverage": 1,
        "amount": amount,
        "orderCurrency": "usd",
    }
    try:
        EtoroDemoExecutionClient._validate_open_payload(body)
    except Exception:
        return refuse("rejected_guard", "order_payload_refused", price=price, size=size)
    path = SUBMIT_DEMO_ORDER_V3.path
    if not demo_order_endpoint("POST", path):
        return refuse("rejected_guard", "endpoint_not_allowlisted", price=price, size=size)
    try:
        receipt = submit(path, body)
    except Exception:
        _refuse(
            trade_log, moment, f"{symbol}:{instrument_id}", side, size, price,
            "broker_unavailable", "broker_error", ny_date,
        )
        return DemoOrderResult(False, "broker_error", "broker_unavailable")
    if (not isinstance(receipt, DemoBrokerReceipt) or not str(receipt.order_id).strip()
            or not str(receipt.reference_id).strip()):
        _refuse(
            trade_log, moment, f"{symbol}:{instrument_id}", side, size, price,
            "broker_response_unconfirmed", "broker_error", ny_date,
        )
        return DemoOrderResult(False, "broker_error", "broker_response_unconfirmed")
    order_id = str(receipt.order_id).strip()
    reference_id = str(receipt.reference_id).strip()
    try:
        _log_attempt(
            trade_log, now=moment, instrument=f"{symbol}:{instrument_id}", side=side,
            size=size, price=price, reason="accepted", outcome="accepted",
            order_id=order_id, reference_id=reference_id, ny_date=ny_date,
        )
    except DemoOrderBlocked:
        logger.warning("demo order accepted but the trade log refused the record")
    _stage_fills(
        receipt, trade_log=trade_log, now=moment, symbol=symbol, instrument_id=instrument_id,
        side=side, ny_date=ny_date, staged_fills_path=staged_fills_path,
        ticker_by_instrument_id=ticker_by_instrument_id or {},
    )
    return DemoOrderResult(True, "accepted", "accepted", order_id, reference_id)


def _refuse(trade_log, moment, symbol, side, size, price, reason, outcome, ny_date) -> None:
    try:
        _log_attempt(
            trade_log, now=moment, instrument=symbol, side=side, size=size, price=price,
            reason=reason, outcome=outcome, ny_date=ny_date,
        )
    except DemoOrderBlocked:
        logger.warning("demo order %s reason=%s (trade log refused the record)", outcome, reason)


def _stage_fills(
    receipt: DemoBrokerReceipt,
    *,
    trade_log: TradeLog,
    now: datetime,
    symbol: str,
    instrument_id: int,
    side: str,
    ny_date: str,
    staged_fills_path,
    ticker_by_instrument_id: Mapping[int, str],
) -> None:
    executions = tuple(receipt.executions or ())
    if not executions:
        return
    admissible = []
    described = []
    for execution in executions:
        execution_id = str(getattr(execution, "broker_execution_id", "") or "")
        fill_price = getattr(execution, "price", None)
        units = getattr(execution, "units", None)
        try:
            price_text = decimal_text(_exact_money(fill_price, "fill_price"))
            size_text = decimal_text(_exact_money(units, "fill_units"))
        except DemoOrderBlocked:
            price_text = ""
            size_text = ""
        long_demo = (
            getattr(execution, "environment", None) == Environment.DEMO
            and str(getattr(execution, "side", "")).lower() == "long"
            and str(getattr(execution, "action", "")).lower() == "buy"
            and price_text
            and size_text
        )
        if not long_demo:
            _refuse(
                trade_log, now, f"{symbol}:{instrument_id}", side, size_text, price_text,
                "fill_not_demo_long", "rejected_guard", ny_date,
            )
            continue
        admissible.append(execution)
        described.append((execution_id, size_text, price_text, execution))
    if not admissible:
        return
    if staged_fills_path is None or str(ticker_by_instrument_id.get(instrument_id, "")).strip().upper() != symbol:
        _refuse(
            trade_log, now, f"{symbol}:{instrument_id}", side, "", "",
            "fill_not_staged", "broker_error", ny_date,
        )
        return
    try:
        stage_broker_executions(
            staged_fills_path,
            executions=tuple(admissible),
            ticker_by_instrument_id=dict(ticker_by_instrument_id),
            broker="etoro",
        )
    except Exception:
        _refuse(
            trade_log, now, f"{symbol}:{instrument_id}", side, "", "",
            "fill_not_staged", "broker_error", ny_date,
        )
        return
    for execution_id, size_text, price_text, execution in described:
        try:
            _log_attempt(
                trade_log, now=now, instrument=f"{symbol}:{instrument_id}", side=side,
                size=size_text, price=price_text, reason="fill", outcome="fill",
                order_id=str(getattr(execution, "broker_order_id", "") or receipt.order_id),
                reference_id=str(getattr(execution, "broker_reference_id", "") or receipt.reference_id),
                execution_id=execution_id, ny_date=ny_date,
            )
        except DemoOrderBlocked:
            logger.warning("demo fill staged but the trade log refused the record")
