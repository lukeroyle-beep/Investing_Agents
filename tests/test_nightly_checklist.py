from __future__ import annotations

import pandas as pd

from scripts import nightly_checklist
from shared.schema_registry import get_file_schema


def _with_registered_columns(file_name: str, rows: list[dict]) -> pd.DataFrame:
    columns = get_file_schema(file_name).canonical_column_order
    return pd.DataFrame(rows, columns=columns).fillna("")


def _patch_data_dir(isolated_workspace, monkeypatch):
    data_dir = isolated_workspace / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(nightly_checklist, "DATA_DIR", data_dir)
    return data_dir


def _write_minimum_required_files(data_dir, run_id="RUN_NIGHTLY"):
    pd.DataFrame(
        [
            {
                "run_id": run_id,
                "started_at": "2026-04-24T19:00:00+00:00",
                "completed_at": "2026-04-24T19:05:00+00:00",
                "status": "success",
                "failed_agent": "",
                "error_message": "",
                "notes": "",
            }
        ]
    ).to_csv(data_dir / "run_history.csv", index=False)

    _with_registered_columns(
        "portfolio_state.csv",
        [
            {
                "position_id": "POS1",
                "ticker": "AAPL",
                "side": "long",
                "quantity": 1,
                "entry_price": 100,
                "entry_date": "2026-04-01",
                "current_price": 110,
                "market_value": 110,
                "cost_basis": 100,
                "pnl_abs": 10,
                "pnl_pct": 10,
                "status": "open",
                "exit_flag": "",
                "exit_reason": "",
                "closed_at": "",
                "exit_price": "",
                "realised_pnl_abs": "",
                "fees_total": 0,
                "run_id": run_id,
            }
        ],
    ).to_csv(data_dir / "portfolio_state.csv", index=False)

    _with_registered_columns(
        "cash_state.csv",
        [
            {
                "cash_account_id": "MAIN",
                "currency": "USD",
                "cash_balance": 10000,
                "available_cash": 10000,
                "reserved_cash": 0,
                "updated_at": "2026-04-24T19:05:00+00:00",
                "as_of": "2026-04-24T19:05:00+00:00",
                "run_id": run_id,
            }
        ],
    ).to_csv(data_dir / "cash_state.csv", index=False)

    _with_registered_columns("processed_fills.csv", []).to_csv(data_dir / "processed_fills.csv", index=False)

    pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "action": "review_trade",
                "direction": "long",
                "entry_zone_low": 99,
                "entry_zone_high": 101,
                "suggested_size_pct": 5,
                "suggested_size_cash": 500,
                "stop_loss": 95,
                "take_profit": 112,
                "risk_reward_ratio": 2.4,
                "estimated_cash_risk": 25,
                "time_in_force_note": "Manual entry only",
                "manual_review_required": True,
                "advice_status": "ready_for_manual_review",
                "advice_notes": "",
                "advice_generated_at": "2026-04-24T19:05:00+00:00",
                "run_id": run_id,
            }
        ]
    ).to_csv(data_dir / "advisory_trades.csv", index=False)

    pd.DataFrame(
        columns=[
            "position_id",
            "ticker",
            "exit_action",
            "reason",
            "status",
            "exit_reason",
            "current_price",
            "stop_loss",
            "take_profit",
            "pnl_abs",
            "pnl_pct",
            "generated_at",
            "run_id",
        ]
    ).to_csv(data_dir / "exit_advice.csv", index=False)

    _with_registered_columns(
        "lifecycle_integrity_report.csv",
        [
            {
                "run_id": run_id,
                "invariant_name": "all",
                "status": "passed",
                "severity": "info",
                "position_id": "",
                "ticker": "",
                "message": "ok",
                "checked_at": "2026-04-24T19:05:00+00:00",
                "record_type": "summary",
                "rule": "all",
                "detail": "ok",
                "total_checks": 1,
                "passed_checks": 1,
                "warning_count": 0,
                "failure_count": 0,
            }
        ],
    ).to_csv(data_dir / "lifecycle_integrity_report.csv", index=False)

    _with_registered_columns(
        "run_reconciliation_summary.csv",
        [
            {
                "run_id": run_id,
                "status": "success",
                "fills_processed": 0,
                "positions_opened": 0,
                "positions_closed": 0,
                "positions_marked_exit_required": 0,
                "cash_delta": 0,
                "realised_pnl_delta": 0,
                "unrealised_pnl_delta": 0,
                "equity_delta": 0,
                "exposure_delta": 0,
                "validation_warning_count": 0,
                "validation_failure_count": 0,
                "reconciliation_notes": "",
                "generated_at": "2026-04-24T19:05:00+00:00",
                "started_at": "2026-04-24T19:00:00+00:00",
                "completed_at": "2026-04-24T19:05:00+00:00",
                "failed_agent": "",
                "notes": "",
            }
        ],
    ).to_csv(data_dir / "run_reconciliation_summary.csv", index=False)

    pd.DataFrame([{"ticker": "AAPL", "risk_decision": "approved", "adjusted_setup_score": 5}]).to_csv(
        data_dir / "final_shortlist.csv", index=False
    )
    pd.DataFrame(
        [
            {
                "run_id": run_id,
                "ticker": "AAPL",
                "entry_price": 100,
                "position_size_pct": 5,
                "capital_allocated": 500,
            }
        ]
    ).to_csv(data_dir / "portfolio_orders.csv", index=False)
    pd.DataFrame([{"ticker": "AAPL"}]).to_csv(data_dir / "signal_setups.csv", index=False)
    pd.DataFrame([{"ticker": "AAPL"}]).to_csv(data_dir / "signal_top_setups.csv", index=False)
    pd.DataFrame([{"ticker": "AAPL", "has_news": False}]).to_csv(data_dir / "news_flags.csv", index=False)
    pd.DataFrame([{"market_regime": "neutral"}]).to_csv(data_dir / "macro_regime.csv", index=False)
    pd.DataFrame(
        [
            {
                "ticker": "AAPL",
                "source": "yfinance",
                "error": "",
                "stale": False,
                "retry_count": 0,
                "fetched_at": "2026-04-24T19:05:00+00:00",
                "as_of": "2026-04-24T00:00:00+00:00",
            }
        ]
    ).to_csv(data_dir / "data_source_health.csv", index=False)


def test_nightly_checklist_passes_with_minimum_valid_artifacts(isolated_workspace, monkeypatch):
    data_dir = _patch_data_dir(isolated_workspace, monkeypatch)
    _write_minimum_required_files(data_dir)

    assert nightly_checklist.main() == 0


def test_nightly_checklist_fails_on_stale_run_scoped_artifact(isolated_workspace, monkeypatch):
    data_dir = _patch_data_dir(isolated_workspace, monkeypatch)
    _write_minimum_required_files(data_dir, run_id="RUN_CURRENT")
    advisory = pd.read_csv(data_dir / "advisory_trades.csv")
    advisory["run_id"] = "RUN_STALE"
    advisory.to_csv(data_dir / "advisory_trades.csv", index=False)

    issues = nightly_checklist.run_checks()

    assert any("advisory_trades.csv: no rows for latest run_id RUN_CURRENT" in issue.message for issue in issues)
    assert nightly_checklist.main() == 1


def test_nightly_checklist_fails_on_duplicate_fill_ids(isolated_workspace, monkeypatch):
    data_dir = _patch_data_dir(isolated_workspace, monkeypatch)
    _write_minimum_required_files(data_dir)
    fills = pd.DataFrame(
        [
            {"fill_id": "FILL1", "order_id": "O1", "ticker": "AAPL", "side": "buy", "quantity": 1, "fill_price": 100, "fees": 0, "filled_at": "2026-04-24T19:00:00+00:00", "status": "processed", "processed_at": "2026-04-24T19:01:00+00:00", "run_id": "RUN_NIGHTLY"},
            {"fill_id": "FILL1", "order_id": "O1", "ticker": "AAPL", "side": "buy", "quantity": 1, "fill_price": 100, "fees": 0, "filled_at": "2026-04-24T19:00:00+00:00", "status": "processed", "processed_at": "2026-04-24T19:01:00+00:00", "run_id": "RUN_NIGHTLY"},
        ]
    )
    fills.to_csv(data_dir / "processed_fills.csv", index=False)

    issues = nightly_checklist.run_checks()

    assert any("duplicate fill_id" in issue.message for issue in issues)
