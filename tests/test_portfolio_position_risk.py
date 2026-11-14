from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from agents.portfolio_agent.portfolio_agent import (
    build_portfolio_orders,
    calculate_position_risk_fields,
)


@pytest.mark.parametrize("atr_pct", ["0.0025", 0.0025])
def test_near_zero_atr_stop_quantizes_to_zero_and_size_is_zero(atr_pct):
    fields = calculate_position_risk_fields({"atr_pct": atr_pct}, 1)

    assert fields["stop_loss_pct"] == 0.0
    assert fields["raw_position_size_pct"] == 0.0
    assert type(fields["stop_loss_pct"]) is float
    assert type(fields["raw_position_size_pct"]) is float
    assert fields["risk_budget_pct"] == 1


def test_normal_atr_stop_is_twice_atr_and_size_uses_that_stop():
    fields = calculate_position_risk_fields({"atr_pct": "2.0"}, 1)

    assert fields["stop_loss_pct"] == 4.0
    assert fields["raw_position_size_pct"] == 25.0
    assert type(fields["stop_loss_pct"]) is float
    assert type(fields["raw_position_size_pct"]) is float


@pytest.mark.parametrize("atr_pct", ["0", "0.00", "-1.5", 0])
def test_non_positive_stop_sizes_zero_without_dividing(atr_pct):
    fields = calculate_position_risk_fields({"atr_pct": atr_pct}, 1)

    assert fields["stop_loss_pct"] <= 0
    assert fields["raw_position_size_pct"] == 0.0


def test_binary_float_residue_is_rejected():
    # The shortest spelling of float('0.0025') * 2 is '0.005'; the bug is the
    # longer binary residue, which round() treats as above the half-even tie.
    residue = format(float("0.0025") * 2, ".60f").rstrip("0")
    assert residue.startswith("0.0050000000000000001")

    with pytest.raises(ValueError, match="binary float noise"):
        calculate_position_risk_fields({"atr_pct": residue}, 1)
    with pytest.raises(ValueError, match="binary float noise"):
        calculate_position_risk_fields({"atr_pct": Decimal(float("0.0025"))}, 1)
    with pytest.raises(ValueError, match="binary float noise"):
        calculate_position_risk_fields({"atr_pct": 0.1 + 0.2}, 1)


def test_order_notionals_use_decimal_cents_and_stay_float_columns():
    selected = pd.DataFrame(
        [
            {
                "ticker": "XOM",
                "name": "Exxon Mobil",
                "latest_close": "100.10",
                "scaled_target_allocation_pct": "25.71",
                "stop_loss_pct": "4",
                "risk_notes": "clear",
                "internal_instrument_id": "inst-1",
                "exchange": "NYSE",
                "currency": "USD",
                "asset_class": "equity",
                "sector": "Energy",
                "market_regime": "neutral",
                "adjusted_setup_score": 5,
                "adjusted_setup_status": "actionable",
                "risk_decision": "approved",
                "headline_count": 0,
                "categories_found": "",
                "has_news": False,
                "risk_budget_pct": 0.6,
                "raw_position_size_pct": 15.0,
                "portfolio_action": "buy",
                "portfolio_checked_at": "2026-01-01T00:00:00+00:00",
            }
        ]
    )

    orders = build_portfolio_orders(
        selected,
        {"notional_portfolio_value": 10000, "default_take_profit_pct": 12},
        "RUN_RISK",
    )

    assert orders["entry_price"].iloc[0] == 100.1
    assert orders["position_size_pct"].iloc[0] == 25.71
    assert orders["capital_allocated"].iloc[0] == 2571.0
    assert orders["sizing_value"].iloc[0] == 2571.0
    assert orders["stop_loss_price"].iloc[0] == 96.096
    assert orders["take_profit_price"].iloc[0] == 112.112
    for column in (
        "entry_price",
        "position_size_pct",
        "capital_allocated",
        "sizing_value",
        "stop_loss_price",
        "take_profit_price",
    ):
        assert orders[column].dtype == "float64"
