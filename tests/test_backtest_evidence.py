from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3

import pandas as pd
import pytest
import yaml

from agents.backtesting_agent import backtesting_agent as agent
from scripts.validate_virtual_backtest import read_snapshot, synthetic_histories, validate
from strategy.domain import StrategyValidationError, canonical_json
from strategy.validation import PointInTimeUniverse


def config():
    return {
        **agent.DEFAULT_CONFIG,
        "tickers": ["SYNTH_A", "SYNTH_B"], "benchmark_tickers": ["SYNTH_BENCH"],
        "position_size_pct": 60,
        "validation": {"minimum_walk_forward_folds": 3,
                       "walk_forward": {"train_sessions": 252, "validation_sessions": 63,
                                        "test_sessions": 63, "step_sessions": 63}},
        "cost_model": {"spread_bps": 5, "slippage_bps": 5, "fee_bps": 5,
                       "tax_bps": 0, "stress_multiplier": 2},
    }


def test_walk_forward_replays_windows_and_uses_actual_drawdown():
    histories, benchmark = synthetic_histories()
    splits, evidence = agent.evaluate_walk_forward(histories, benchmark, config())
    assert len(splits) == len(evidence) == 4
    assert evidence["test_max_drawdown_pct"].min() < -1
    assert all(left.test_end < right.test_start for left, right in zip(splits, splits[1:]))
    assert evidence["validation_excess_return_pct"].equals(
        evidence["validation_return_pct"] - evidence["validation_benchmark_return_pct"])
    assert evidence["test_excess_return_pct"].equals(
        evidence["test_return_pct"] - evidence["benchmark_return_pct"])
    assert evidence["test_trades"].sum() > 0


def test_later_prices_cannot_change_an_earlier_fold():
    histories, benchmark = synthetic_histories()
    splits, original = agent.evaluate_walk_forward(histories, benchmark, config())
    revised = {key: frame.copy() for key, frame in histories.items()}
    for frame in revised.values():
        future = frame["Date"] > pd.Timestamp(splits[0].test_end)
        frame.loc[future, ["Open", "High", "Low", "Close"]] *= 2
    _, changed = agent.evaluate_walk_forward(revised, benchmark, config())
    pd.testing.assert_series_equal(original.iloc[0], changed.iloc[0])


def test_partition_starts_flat_and_never_uses_exits_after_its_end():
    histories, _ = synthetic_histories()
    sessions = agent.history_sessions(histories)
    start, end = sessions[300], sessions[340]
    result = agent.evaluate_partition(histories, config(), start, end)
    trades = result.accepted_trades
    assert not trades.empty
    assert (pd.to_datetime(trades["entry_date"]) >= start).all()
    assert (pd.to_datetime(trades["exit_date"]) <= end).all()
    assert result.equity_curve.iloc[-1]["open_positions"] == 0
    assert (result.equity_curve["cash"] >= 0).all()


def test_too_few_windows_and_missing_benchmark_are_not_success():
    histories, benchmark = synthetic_histories()
    short = {key: value.iloc[:450] for key, value in histories.items()}
    with pytest.raises(StrategyValidationError, match="insufficient walk-forward"):
        agent.evaluate_walk_forward(short, benchmark, config())
    with pytest.raises(StrategyValidationError, match="benchmark"):
        agent.evaluate_walk_forward(histories, {}, config())


def test_missing_last_or_middle_prices_fail_before_simulated_liquidation():
    histories, _ = synthetic_histories()
    for missing in (-1, 350):
        incomplete = {name: frame.copy() for name, frame in histories.items()}
        incomplete["SYNTH_A"] = incomplete["SYNTH_A"].drop(incomplete["SYNTH_A"].index[missing])
        sessions = agent.history_sessions(histories)
        with pytest.raises(StrategyValidationError, match="coverage"):
            agent.evaluate_partition(incomplete, config(), sessions[300], sessions[-1])


def test_hypothetical_candidate_exits_do_not_suppress_later_signals():
    dates = pd.bdate_range("2024-01-02", periods=20)
    close = [100 + index for index in range(20)]
    history = pd.DataFrame({"Date": dates, "Open": close, "Close": close,
                            "High": [value + 1 for value in close],
                            "Low": [value - 1 for value in close]})
    trades = agent.simulate_long_trades_for_ticker(history, "TEST", 10, 50, 50, 10, 2, 3, 3)
    assert trades[1]["entry_date"] < trades[0]["exit_date"]


def test_stop_gap_executes_at_available_open_not_stale_stop_price():
    dates = pd.bdate_range("2024-01-02", periods=8)
    close = [100, 101, 102, 103, 104, 70, 72, 73]
    history = pd.DataFrame({"Date": dates, "Open": close, "Close": close,
                            "High": [value + 1 for value in close],
                            "Low": [value - 1 for value in close]})
    trades = agent.simulate_long_trades_for_ticker(
        history, "TEST", 10, 8, 50, 10, 2, 3, 3)
    assert trades[0]["exit_reason"] == "stop_gap"
    assert trades[0]["exit_price"] == 70
    assert trades[0]["exit_price"] < trades[0]["stop_price"]


def test_serialized_inputs_replay_identically_without_any_return_target(tmp_path):
    histories, benchmark = synthetic_histories()
    first = validate(histories, benchmark, config(), tmp_path / "first", "synthetic")
    restored_histories, restored_benchmark = read_snapshot(tmp_path / "first" / "historical_prices.csv")
    second = validate(restored_histories, restored_benchmark, config(), tmp_path / "second", "synthetic")
    assert first["input_sha256"] == second["input_sha256"]
    assert first["result_sha256"] == second["result_sha256"]
    assert first["functional_result"] == "pass"
    assert first["return_target_pct"] is None
    assert first["live_demo_qualified"] is False
    assert first["strategy_promotion_eligible"] is False
    assert first["rejected_trades"] > 0
    with pytest.raises(FileExistsError):
        validate(histories, benchmark, config(), tmp_path / "first", "synthetic")


def test_negative_returns_can_pass_functional_validation(tmp_path):
    histories, benchmark = synthetic_histories()
    settings = config()
    settings["cost_model"]["fee_bps"] = 500
    report = validate(histories, benchmark, settings, tmp_path / "loss-making", "synthetic")
    assert report["observed_return_pct"] < 0
    assert report["functional_result"] == "pass"
    assert report["return_target_pct"] is None


def test_no_trade_run_cannot_prove_a_trading_lifecycle(tmp_path):
    histories, benchmark = synthetic_histories()
    for frame in histories.values():
        frame[["Open", "High", "Low", "Close"]] = 100.0
    report = validate(histories, benchmark, config(), tmp_path / "idle", "synthetic")
    assert report["accepted_trades"] == 0
    assert report["functional_checks"]["completed_trade_observed"] is False
    assert report["functional_result"] == "fail"


def test_immutable_experiment_saves_inputs_and_measured_evidence(tmp_path, monkeypatch):
    histories, benchmark = synthetic_histories()
    settings = {**config(), "experiment_registry_enabled": True}
    monkeypatch.setattr(agent, "run_path", lambda run_id, *parts: tmp_path / run_id / Path(*parts))
    monkeypatch.setattr(agent, "EXPERIMENT_REGISTRY_PATH", tmp_path / "experiments.sqlite3")
    for name in ("BACKTEST_TRADES_FILE", "BACKTEST_SUMMARY_FILE", "BACKTEST_EQUITY_CURVE_FILE",
                 "BACKTEST_DRAWDOWN_FILE", "BACKTEST_BENCHMARK_FILE", "BACKTEST_REJECTED_FILE"):
        monkeypatch.setattr(agent, name, tmp_path / name)
    identifier = agent.record_immutable_experiment(
        run_id="test-evidence", config=settings, trades_df=pd.DataFrame(),
        histories=histories, benchmark_history=benchmark)
    artifact = tmp_path / "test-evidence" / "experiments" / identifier / "experiment.json"
    payload = json.loads(artifact.read_text())
    metrics = payload["metrics"]
    assert float(metrics["test_max_drawdown_pct"]) < -1
    assert metrics["walk_forward_total_folds"] == 4
    assert metrics["deterministic_reproduction"] is True
    assert metrics["cost_model_passed"] is False
    assert metrics["point_in_time_universe_passed"] is False
    assert {item["file"] for item in payload["artifacts"]} >= {"historical_prices.csv", "walk_forward.csv"}


def test_canonical_hash_supports_nested_timestamp_lists():
    value = {"rows": [{"Date": pd.Timestamp("2024-01-02"), "Close": 100.0}]}
    assert json.loads(canonical_json(value))["rows"][0]["Date"] == "2024-01-02T00:00:00"


def test_agent_run_twice_reconciles_and_reuses_immutable_experiment(tmp_path, monkeypatch):
    histories, benchmarks = synthetic_histories()
    all_histories = {**histories, **benchmarks}
    sessions = agent.history_sessions(histories)
    settings = {
        **config(), "experiment_registry_enabled": True,
        "strategy_version": "synthetic_integration_v1",
        "start_date": str(sessions[0].date()), "end_date": str(sessions[-1].date()),
    }
    settings_path = tmp_path / "backtesting.yaml"
    settings_path.write_text(yaml.safe_dump(settings), encoding="utf-8")
    monkeypatch.setattr(agent, "BACKTEST_CONFIG_FILE", settings_path)
    monkeypatch.setattr(agent, "run_path", lambda run_id, *parts: tmp_path / "runs" / run_id / Path(*parts))
    registry_path = tmp_path / "control" / "experiments.sqlite3"
    monkeypatch.setattr(agent, "EXPERIMENT_REGISTRY_PATH", registry_path)
    output_names = {
        "BACKTEST_TRADES_FILE": "backtest_trades.csv",
        "BACKTEST_SUMMARY_FILE": "backtest_summary.csv",
        "BACKTEST_EQUITY_CURVE_FILE": "backtest_equity_curve.csv",
        "BACKTEST_DRAWDOWN_FILE": "backtest_drawdown.csv",
        "BACKTEST_BENCHMARK_FILE": "backtest_benchmark_comparison.csv",
        "BACKTEST_REJECTED_FILE": "backtest_rejected_trades.csv",
    }
    for attribute, filename in output_names.items():
        monkeypatch.setattr(agent, attribute, tmp_path / "state" / filename)
    run_ids = iter(("integration-first", "integration-second"))
    monkeypatch.setattr(agent, "get_or_create_run_id", lambda: next(run_ids))
    monkeypatch.setenv("INVESTING_CODE_REVISION", "synthetic-integration-revision")
    downloads = []

    def download_fixture(ticker, start_date, end_date, **kwargs):
        downloads.append((ticker, start_date, end_date))
        return all_histories[ticker].copy()

    monkeypatch.setattr(agent, "download_price_history", download_fixture)
    monkeypatch.setattr(agent, "fetch_price_history", lambda *args, **kwargs: pytest.fail("network fetch forbidden"))
    health_calls = []
    monkeypatch.setattr(agent, "append_market_data_health_artifact", lambda results: health_calls.append(list(results)))

    manifests = []
    for run_id in ("integration-first", "integration-second"):
        agent.run()
        trades = pd.read_csv(agent.BACKTEST_TRADES_FILE)
        equity = pd.read_csv(agent.BACKTEST_EQUITY_CURVE_FILE)
        summary = pd.read_csv(agent.BACKTEST_SUMMARY_FILE).iloc[0]
        assert len(trades) > 0
        assert (equity["cash"] >= 0).all()
        assert equity.iloc[-1]["open_positions"] == 0
        assert equity.iloc[-1]["cash"] == pytest.approx(settings["initial_capital"] + trades["net_pnl"].sum())
        assert equity["equity"].tolist() == pytest.approx((equity["cash"] + equity["exposure_notional"]).tolist())
        assert (equity["equity"] - settings["initial_capital"]).tolist() == pytest.approx(
            (equity["realised_pnl"] + equity["unrealised_pnl"]).tolist(), abs=1e-7)
        assert summary["objective"] == "virtual_functionality_no_return_target"
        assert pd.isna(summary["return_target_pct"])
        assert summary["run_id"] == run_id
        assert summary["total_trades"] == len(trades)
        artifacts = list((tmp_path / "runs" / run_id / "experiments").glob("*/experiment.json"))
        assert len(artifacts) == 1
        payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
        names = {item["file"] for item in payload["artifacts"]}
        assert names == {*output_names.values(), "historical_prices.csv", "walk_forward.csv"}
        for item in payload["artifacts"]:
            copied = artifacts[0].parent / item["file"]
            assert hashlib.sha256(copied.read_bytes()).hexdigest() == item["sha256"]
        copied_summary = pd.read_csv(artifacts[0].parent / "backtest_summary.csv").iloc[0]
        assert copied_summary["run_id"] == run_id
        manifests.append(payload)

    assert manifests[0]["experiment_id"] == manifests[1]["experiment_id"]
    assert manifests[0]["spec"] == manifests[1]["spec"]
    assert manifests[0]["metrics"] == manifests[1]["metrics"]
    with sqlite3.connect(registry_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0] == 1
    assert len(downloads) == 2 * len(all_histories)
    assert len(health_calls) == 2


def _utc(value) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").isoformat()


def _membership_row(symbol, start, *, end=None, delisted=None):
    return {
        "internal_instrument_id": symbol.lower(),
        "symbol": symbol,
        "exchange": "XNYS",
        "effective_from": _utc(start),
        "effective_to": None if end is None else _utc(end),
        "delisted_at": None if delisted is None else _utc(delisted),
    }


def _rising(dates) -> pd.DataFrame:
    rows = []
    for index, day in enumerate(dates):
        close = 100.0 + index
        rows.append({
            "Date": day,
            "Open": close,
            "High": close + 1.0,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 1000 + index,
        })
    return pd.DataFrame(rows)


def _pit_config():
    return {
        **agent.DEFAULT_CONFIG,
        "ma_fast_window": 2,
        "ma_slow_window": 3,
        "return_lookback_window": 3,
        "stop_loss_pct": 50.0,
        "take_profit_pct": 80.0,
        "max_hold_days": 2,
        "position_size_pct": 10.0,
        "initial_capital": 100000.0,
    }


def _trade_keys(frame: pd.DataFrame) -> list[tuple[str, str, str]]:
    if frame.empty:
        return []
    return list(zip(frame["ticker"], frame["entry_date"], frame["exit_date"], strict=True))


def test_permanent_members_replay_like_the_static_window():
    dates = pd.bdate_range("2024-01-02", periods=30)
    histories = {"STAY": _rising(dates), "ALSO": _rising(dates)}
    universe = PointInTimeUniverse.from_frame(pd.DataFrame([
        _membership_row("STAY", "2020-01-01"),
        _membership_row("ALSO", "2020-01-01"),
    ]))
    settings = _pit_config()
    start, end = str(dates[0].date()), str(dates[-1].date())
    static = agent.evaluate_partition(histories, settings, start, end)
    pointed = agent.evaluate_partition(histories, settings, start, end, universe=universe)
    assert _trade_keys(static.accepted_trades) == _trade_keys(pointed.accepted_trades)
    assert not static.accepted_trades.empty


def test_point_in_time_replay_includes_listings_and_stops_at_delisting():
    dates = pd.bdate_range("2024-01-02", periods=40)
    listing = dates[15]
    delisting = dates[22]
    histories = {
        "STAY": _rising(dates),
        "ENTER": _rising(dates[dates >= listing]),
        "GONE": _rising(dates),
        "TODAY": _rising(dates),
    }
    universe = PointInTimeUniverse.from_frame(pd.DataFrame([
        _membership_row("STAY", "2020-01-01"),
        _membership_row("ENTER", listing),
        _membership_row("GONE", "2020-01-01", delisted=delisting),
    ]))
    settings = _pit_config()
    start, end = str(dates[0].date()), str(dates[-1].date())
    result = agent.evaluate_partition(histories, settings, start, end, universe=universe)
    trades = result.accepted_trades
    assert set(trades["ticker"]) == {"STAY", "ENTER", "GONE"}

    def listed(symbol, session) -> bool:
        return symbol in set(universe.members(session)["symbol"].astype(str))

    assert not listed("ENTER", dates[0])
    assert listed("ENTER", listing)
    assert listed("GONE", dates[21])
    assert not listed("GONE", delisting)
    for record in trades.to_dict(orient="records"):
        assert listed(record["ticker"], record["entry_date"])
        assert listed(record["ticker"], record["exit_date"])
    gone_exits = pd.to_datetime(trades.loc[trades["ticker"] == "GONE", "exit_date"])
    enter_entries = pd.to_datetime(trades.loc[trades["ticker"] == "ENTER", "entry_date"])
    assert gone_exits.max() < pd.Timestamp(delisting)
    assert enter_entries.min() >= pd.Timestamp(listing)
    assert result.equity_curve.iloc[0]["date"] == start
    assert result.equity_curve.iloc[-1]["date"] == end
    assert int(result.equity_curve.iloc[-1]["open_positions"]) == 0

    static_names = {name: histories[name] for name in ("STAY", "GONE", "TODAY")}
    static = agent.evaluate_partition(static_names, settings, start, end).accepted_trades
    static_gone = pd.to_datetime(static.loc[static["ticker"] == "GONE", "entry_date"])
    assert (static_gone >= pd.Timestamp(delisting)).any()
    assert "TODAY" in set(static["ticker"])

    with pytest.raises(StrategyValidationError, match="coverage"):
        agent.evaluate_partition(histories, settings, start, end)
    with pytest.raises(StrategyValidationError, match="missing price history"):
        agent.evaluate_partition(
            {name: histories[name] for name in ("STAY", "ENTER", "TODAY")},
            settings, start, end, universe=universe,
        )
    holed = {name: frame.copy() for name, frame in histories.items()}
    holed["GONE"] = holed["GONE"].drop(index=10).reset_index(drop=True)
    with pytest.raises(StrategyValidationError, match="incomplete point-in-time session coverage"):
        agent.evaluate_partition(holed, settings, start, end, universe=universe)


def test_point_in_time_file_selects_overlapping_members_only(tmp_path, monkeypatch):
    monkeypatch.delenv("INVESTING_RESEARCH_UNIVERSE", raising=False)
    assert agent.configured_point_in_time_universe(agent.load_backtest_config()) is None
    frame = pd.DataFrame([
        _membership_row("OLD", "2010-01-01", delisted="2020-01-01"),
        _membership_row("GONE", "2020-01-01", delisted="2024-06-01"),
        _membership_row("ENTER", "2024-03-01"),
        _membership_row("FUTURE", "2026-01-01"),
    ])
    path = tmp_path / "membership.csv"
    frame.to_csv(path, index=False)
    universe = agent.configured_point_in_time_universe({"point_in_time_universe_file": str(path)})
    assert agent.point_in_time_symbols(universe, "2024-01-01", "2024-12-31") == ["ENTER", "GONE"]
    with pytest.raises(StrategyValidationError, match="missing"):
        agent.configured_point_in_time_universe(
            {"point_in_time_universe_file": str(tmp_path / "absent.csv")}
        )


def test_survivorship_flags_follow_an_applied_point_in_time_universe(tmp_path, monkeypatch):
    histories, benchmark = synthetic_histories()
    dates = agent.history_sessions(histories)
    delisting = dates[len(dates) // 2]
    universe = PointInTimeUniverse.from_frame(pd.DataFrame([
        _membership_row("SYNTH_A", dates[0]),
        _membership_row("SYNTH_B", dates[0], delisted=delisting),
    ]))
    settings = {**config(), "experiment_registry_enabled": True}
    monkeypatch.setattr(agent, "run_path", lambda run_id, *parts: tmp_path / run_id / Path(*parts))
    monkeypatch.setattr(agent, "EXPERIMENT_REGISTRY_PATH", tmp_path / "experiments.sqlite3")
    for name in ("BACKTEST_TRADES_FILE", "BACKTEST_SUMMARY_FILE", "BACKTEST_EQUITY_CURVE_FILE",
                 "BACKTEST_DRAWDOWN_FILE", "BACKTEST_BENCHMARK_FILE", "BACKTEST_REJECTED_FILE"):
        monkeypatch.setattr(agent, name, tmp_path / name)
    identifier = agent.record_immutable_experiment(
        run_id="pit-evidence", config=settings, trades_df=pd.DataFrame(),
        histories=histories, benchmark_history=benchmark, universe=universe,
    )
    payload = json.loads((tmp_path / "pit-evidence" / "experiments" / identifier / "experiment.json").read_text())
    assert payload["spec"]["point_in_time_universe_checksum"] == universe.checksum
    assert payload["metrics"]["point_in_time_universe_passed"] is True
    assert payload["metrics"]["survivorship_checks_passed"] is True
    assert payload["metrics"]["exchange_calendar_passed"] is False
    assert payload["metrics"]["cost_model_passed"] is False


