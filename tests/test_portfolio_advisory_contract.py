from __future__ import annotations

import pandas as pd
import pytest

from agents.advisory_agent import advisory_agent
from agents.portfolio_agent import portfolio_agent
from shared.run_context import RUN_ID_ENV_VAR


def _patch_advisory_paths(workspace, monkeypatch):
    data_dir = workspace / "data"
    config_dir = workspace / "config"
    monkeypatch.setattr(advisory_agent, "PORTFOLIO_ORDERS_PATH", data_dir / "portfolio_orders.csv")
    monkeypatch.setattr(advisory_agent, "ADVISORY_TRADES_PATH", data_dir / "advisory_trades.csv")
    monkeypatch.setattr(advisory_agent, "data_path", lambda name="": data_dir / name if name else data_dir)
    monkeypatch.setattr(advisory_agent, "config_path", lambda name="": config_dir / name if name else config_dir)
    monkeypatch.setattr(portfolio_agent, "FINAL_SHORTLIST_FILE", data_dir / "final_shortlist.csv")
    monkeypatch.setattr(portfolio_agent, "MACRO_REGIME_FILE", data_dir / "macro_regime.csv")
    monkeypatch.setattr(portfolio_agent, "NEWS_FLAGS_FILE", data_dir / "news_flags.csv")
    monkeypatch.setattr(portfolio_agent, "PORTFOLIO_STATE_FILE", data_dir / "portfolio_state.csv")
    monkeypatch.setattr(portfolio_agent, "GOVERNANCE_FILE", config_dir / "governance.yaml")
    monkeypatch.setattr(
        portfolio_agent,
        "PORTFOLIO_CANDIDATES_FILE",
        data_dir / "portfolio_candidates.csv",
    )
    monkeypatch.setattr(portfolio_agent, "PORTFOLIO_ORDERS_FILE", data_dir / "portfolio_orders.csv")
    monkeypatch.setattr(
        portfolio_agent,
        "PORTFOLIO_POSITIONS_FILE",
        data_dir / "portfolio_positions.csv",
    )


def _write_governance(workspace):
    config_dir = workspace / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "governance.yaml").write_text(
        "\n".join(
            [
                "execution_mode: advisory_only",
                "allow_order_submission: false",
                "manual_signoff_required: true",
                "max_position_pct: 20.0",
                "default_take_profit_pct: 12.0",
                "default_entry_buffer_pct: 0.5",
                "block_if_open_position_exists: true",
                "high_news_severity_block: true",
                "blocked_tickers: []",
                "blocked_asset_types: []",
                "notional_portfolio_value: 10000",
            ]
        ),
        encoding="utf-8",
    )


def _write_portfolio_inputs(workspace):
    data_dir = workspace / "data"
    data_dir.mkdir(exist_ok=True)

    pd.DataFrame(
        [
            {
                "ticker": "XOM",
                "name": "Exxon Mobil",
                "latest_close": 100.0,
                "atr_pct": 2.0,
                "adjusted_setup_score": 5,
                "adjusted_setup_status": "actionable",
                "risk_decision": "approved",
                "risk_notes": "clear",
            },
            {
                "ticker": "T",
                "name": "AT&T",
                "latest_close": 25.0,
                "atr_pct": 1.5,
                "adjusted_setup_score": 4,
                "adjusted_setup_status": "watch",
                "risk_decision": "caution",
                "risk_notes": "review sizing",
            },
        ]
    ).to_csv(data_dir / "final_shortlist.csv", index=False)

    pd.DataFrame([{"market_regime": "neutral"}]).to_csv(data_dir / "macro_regime.csv", index=False)
    pd.DataFrame(columns=["ticker", "status"]).to_csv(data_dir / "portfolio_state.csv", index=False)
    pd.DataFrame(
        [
            {"ticker": "XOM", "headline_count": 0, "categories_found": "", "has_news": False},
            {"ticker": "T", "headline_count": 0, "categories_found": "", "has_news": False},
        ]
    ).to_csv(data_dir / "news_flags.csv", index=False)
    pd.DataFrame(columns=["ticker", "has_news", "news_severity", "news_pass", "news_notes"]).to_csv(
        data_dir / "news_review.csv", index=False
    )

    pd.DataFrame(
        [
            {
                "ticker": "MSFT",
                "direction": "long",
                "asset_type": "equity",
                "entry_price": 420,
                "position_size_pct": 5,
                "capital_allocated": 500,
                "stop_loss_price": 399,
                "take_profit_price": 470,
                "recommendation_status": "approved",
                "recommendation_notes": "stale sample row",
            }
        ]
    ).to_csv(data_dir / "portfolio_recommendations.csv", index=False)


def test_portfolio_orders_are_current_run_advisory_contract(isolated_workspace, monkeypatch):
    monkeypatch.setenv(RUN_ID_ENV_VAR, "RUN_CONTRACT_CURRENT")
    _patch_advisory_paths(isolated_workspace, monkeypatch)
    _write_governance(isolated_workspace)
    _write_portfolio_inputs(isolated_workspace)

    portfolio_agent.main()
    orders = pd.read_csv(isolated_workspace / "data" / "portfolio_orders.csv", dtype=str, keep_default_na=False)

    required_columns = {
        "run_id",
        "ticker",
        "direction",
        "asset_type",
        "entry_price",
        "position_size_pct",
        "capital_allocated",
        "stop_loss_price",
        "take_profit_price",
        "recommendation_status",
        "recommendation_notes",
    }
    assert required_columns.issubset(set(orders.columns))
    assert set(orders["run_id"]) == {"RUN_CONTRACT_CURRENT"}
    assert set(orders["ticker"]) == {"XOM", "T"}

    advisory_agent.run()
    advisory = pd.read_csv(isolated_workspace / "data" / "advisory_trades.csv", dtype=str, keep_default_na=False)

    assert set(advisory["ticker"]) == {"XOM", "T"}
    assert "MSFT" not in set(advisory["ticker"])
    assert set(advisory["run_id"]) == {"RUN_CONTRACT_CURRENT"}


def test_advisory_filters_portfolio_orders_to_current_run(isolated_workspace, monkeypatch):
    monkeypatch.setenv(RUN_ID_ENV_VAR, "RUN_CURRENT")
    _patch_advisory_paths(isolated_workspace, monkeypatch)
    _write_governance(isolated_workspace)
    data_dir = isolated_workspace / "data"
    data_dir.mkdir(exist_ok=True)

    pd.DataFrame(columns=["ticker", "status"]).to_csv(data_dir / "portfolio_state.csv", index=False)
    pd.DataFrame(columns=["ticker", "has_news", "news_severity", "news_pass", "news_notes"]).to_csv(
        data_dir / "news_review.csv", index=False
    )
    pd.DataFrame(
        [
            {
                "run_id": "RUN_STALE",
                "ticker": "MSFT",
                "direction": "long",
                "asset_type": "equity",
                "entry_price": 420,
                "position_size_pct": 5,
                "capital_allocated": 500,
                "stop_loss_price": 399,
                "take_profit_price": 470,
                "recommendation_status": "ready_for_review",
                "recommendation_notes": "stale",
            },
            {
                "run_id": "RUN_CURRENT",
                "ticker": "XOM",
                "direction": "long",
                "asset_type": "equity",
                "entry_price": 100,
                "position_size_pct": 5,
                "capital_allocated": 500,
                "stop_loss_price": 95,
                "take_profit_price": 112,
                "recommendation_status": "ready_for_review",
                "recommendation_notes": "current",
            },
        ]
    ).to_csv(data_dir / "portfolio_orders.csv", index=False)

    advisory_agent.run()
    advisory = pd.read_csv(data_dir / "advisory_trades.csv", dtype=str, keep_default_na=False)

    assert advisory["ticker"].tolist() == ["XOM"]
    assert advisory["run_id"].tolist() == ["RUN_CURRENT"]


def test_advisory_rejects_portfolio_orders_without_run_id(isolated_workspace, monkeypatch):
    monkeypatch.setenv(RUN_ID_ENV_VAR, "RUN_MISSING_ID")
    _patch_advisory_paths(isolated_workspace, monkeypatch)
    _write_governance(isolated_workspace)
    data_dir = isolated_workspace / "data"
    data_dir.mkdir(exist_ok=True)

    pd.DataFrame(
        [
            {
                "ticker": "XOM",
                "direction": "long",
                "asset_type": "equity",
                "entry_price": 100,
                "position_size_pct": 5,
                "capital_allocated": 500,
                "stop_loss_price": 95,
                "take_profit_price": 112,
                "recommendation_status": "ready_for_review",
                "recommendation_notes": "missing run id",
            }
        ]
    ).to_csv(data_dir / "portfolio_orders.csv", index=False)

    with pytest.raises(ValueError, match="portfolio_orders.csv must include run_id"):
        advisory_agent.run()
