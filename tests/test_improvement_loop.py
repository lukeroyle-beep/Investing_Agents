"""The improvement loop compares variants and does not trade.

Prices in these tests are examples. They are not market data and not a
claim about a strategy's return.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from agents.backtesting_agent import backtesting_agent as backtest
from shared.paper_signal_log import HorizonSummary, PaperSignalLogError
from strategy.backtest_portfolio import PortfolioReplayResult
from strategy.domain import CostModel, StrategyValidationError
from strategy.improvement_loop import (
    MAX_VARIANTS,
    ImprovementLoopError,
    build_variants,
    buy_and_hold_metrics,
    choose_proposal,
    convert_frame_to_gbp,
    load_loop_config,
    oos_beats_baseline,
    run_loop,
    write_report,
)
from strategy.validation import PointInTimeUniverse, chronological_split


ROOT = Path(__file__).resolve().parents[1]
LOOP_FILES = (
    ROOT / "strategy" / "improvement_loop.py",
    ROOT / "scripts" / "improvement_loop.py",
)
FORBIDDEN_MODULES = (
    "execution.demo_orders",
    "execution.fill_ingestion",
    "brokers",
)
FORBIDDEN_CALLS = {
    "place_demo_order",
    "stage_broker_executions",
    "cancel_order",
    "submit_order",
}


def _dates(periods: int = 30) -> pd.DatetimeIndex:
    return pd.bdate_range("2024-01-02", periods=periods)


def _history(dates, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    rows = []
    for index, day in enumerate(dates):
        close = start + index * step
        rows.append({
            "Date": day,
            "Open": close,
            "High": close + 1.0,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 1000 + index,
            "Dividends": 0.0,
        })
    return pd.DataFrame(rows)


def _fx(dates, rate: float = 2.0) -> pd.DataFrame:
    frame = _history(dates, start=rate, step=0.0)
    frame["Close"] = rate
    return frame


def _membership(symbol: str, start, *, end=None) -> dict[str, object]:
    def stamp(value):
        if value is None:
            return None
        return pd.Timestamp(value).tz_localize("UTC").isoformat()

    return {
        "internal_instrument_id": f"example:{symbol}",
        "symbol": symbol,
        "exchange": "EXAMPLE",
        "effective_from": stamp(start),
        "effective_to": stamp(end),
        "delisted_at": None,
    }


def _universe(rows: list[dict[str, object]]) -> PointInTimeUniverse:
    return PointInTimeUniverse.from_frame(pd.DataFrame(rows))


def _settings(**overrides) -> dict:
    settings = {
        **backtest.DEFAULT_CONFIG,
        "start_date": "2024-01-02",
        "end_date": str(_dates()[-1].date()),
        "ma_fast_window": 2,
        "ma_slow_window": 3,
        "return_lookback_window": 3,
        "stop_loss_pct": 40.0,
        "take_profit_pct": 80.0,
        "max_hold_days": 3,
        "position_size_pct": 10.0,
        "initial_capital": 100000.0,
        "transaction_cost_bps": 0.0,
        "slippage_bps": 0.0,
        "min_point_in_time_price_coverage": 0.95,
        "experiment_registry_enabled": False,
        "validation": {"train_fraction": 0.6, "validation_fraction": 0.2},
        "cost_model": {
            "spread_bps": 0,
            "slippage_bps": 0,
            "fee_bps": 0,
            "tax_bps": 0,
            "stress_multiplier": 1,
        },
    }
    settings.update(overrides)
    return settings


def _loop_config(variants: list[dict] | None = None) -> dict:
    return {
        "point_in_time_universe_file": "unused-when-injected",
        "session_calendar_ticker": "SPY",
        "excess_benchmark": "VWRL.L",
        "member_quote_currency": "GBP",
        "benchmark_currencies": {"SPY": "GBP", "VWRL.L": "GBP"},
        "variants": variants or [{"id": "current", "parameters": {}}],
        "baselines": [
            {"id": "buy_and_hold_spy", "kind": "buy_and_hold", "ticker": "SPY"},
            {"id": "buy_and_hold_vwrl", "kind": "buy_and_hold", "ticker": "VWRL.L"},
            {"id": "do_nothing", "kind": "do_nothing"},
        ],
    }


def _market(dates, symbols: list[str]) -> dict[str, pd.DataFrame]:
    return {symbol: _history(dates, start=80.0 + offset * 10) for offset, symbol in enumerate(symbols)}


def _benchmarks(dates) -> dict[str, pd.DataFrame]:
    return {"SPY": _history(dates, start=100.0, step=0.0), "VWRL.L": _history(dates, start=50.0, step=0.0)}


def _isolate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("INVESTING_MIN_POINT_IN_TIME_PRICE_COVERAGE", raising=False)
    monkeypatch.delenv("INVESTING_POINT_IN_TIME_UNIVERSE", raising=False)
    monkeypatch.delenv("INVESTING_RESEARCH_UNIVERSE", raising=False)
    monkeypatch.setattr(backtest, "run", lambda: (_ for _ in ()).throw(AssertionError("backtest run writes artifacts")))
    monkeypatch.setattr(
        backtest,
        "record_immutable_experiment",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("experiment registry is not part of the loop")),
    )


def _run(monkeypatch, tmp_path, **kwargs):
    _isolate(monkeypatch, tmp_path)
    dates = kwargs.pop("dates", _dates())
    symbols = kwargs.pop("symbols", ["STAY"])
    universe = kwargs.pop("universe", _universe([_membership("STAY", "2020-01-01")]))
    return run_loop(
        loop_config=kwargs.pop("loop_config", _loop_config()),
        backtest_config=kwargs.pop("backtest_config", _settings()),
        universe=universe,
        histories=kwargs.pop("histories", _market(dates, symbols)),
        benchmark_histories=kwargs.pop("benchmark_histories", _benchmarks(dates)),
        fx_histories=kwargs.pop("fx_histories", None),
        paper_log_path=kwargs.pop("paper_log_path", tmp_path / "missing-signals.jsonl"),
        demo_trade_log_path=kwargs.pop("demo_trade_log_path", tmp_path / "missing-trades.jsonl"),
        broker_fills_path=kwargs.pop("broker_fills_path", tmp_path / "missing-fills.csv"),
        **kwargs,
    )


def _replay(return_pct: float) -> PortfolioReplayResult:
    return PortfolioReplayResult(
        accepted_trades=pd.DataFrame({"trade_return_pct": [1.0, -0.5]}),
        rejected_trades=pd.DataFrame(),
        equity_curve=pd.DataFrame({"cumulative_return_pct": [return_pct]}),
        drawdown=pd.DataFrame({"drawdown_pct": [-1.5]}),
    )


def test_checked_in_variants_stay_inside_the_cap(monkeypatch):
    monkeypatch.delenv("INVESTING_RESEARCH_UNIVERSE", raising=False)
    loaded = backtest.load_backtest_config()
    specs = build_variants(load_loop_config(), loaded)
    assert len(specs) <= MAX_VARIANTS
    by_id = {item["id"]: item for item in specs}
    assert {"current", "buy_and_hold_spy", "buy_and_hold_vwrl", "do_nothing"} <= set(by_id)
    assert by_id["current"]["parameters"]["ma_fast_window"] == loaded["ma_fast_window"]
    assert by_id["buy_and_hold_vwrl"]["ticker"] == "VWRL.L"
    assert by_id["do_nothing"]["kind"] == "do_nothing"


def test_variant_cap_and_unknown_parameters_fail_before_a_search():
    too_many = [{"id": f"v{index}", "parameters": {}} for index in range(MAX_VARIANTS)]
    with pytest.raises(ImprovementLoopError, match="variant cap"):
        build_variants(_loop_config(too_many), _settings())
    with pytest.raises(ImprovementLoopError, match="not signal or exit"):
        build_variants(
            _loop_config([{"id": "current", "parameters": {"allow_short": True}}]),
            _settings(),
        )


def test_proposal_uses_in_sample_excess_only():
    rows = [
        {"id": "current", "in_sample": {"excess_pct": 1.0}},
        {"id": "wider_exits", "in_sample": {"excess_pct": 4.0}},
        {"id": "buy_and_hold_vwrl", "in_sample": {"excess_pct": 3.0}},
    ]
    assert choose_proposal(rows, qualified=True) == "wider_exits"
    assert choose_proposal(rows, qualified=False) is None
    tied = [
        {"id": "b", "in_sample": {"excess_pct": 1.0}},
        {"id": "a", "in_sample": {"excess_pct": 1.0}},
    ]
    assert choose_proposal(tied, qualified=True) == "a"
    assert oos_beats_baseline("parameter", 0.0) is False
    assert oos_beats_baseline("parameter", 0.01) is True
    assert oos_beats_baseline("buy_and_hold", 5.0) is None


def test_split_keeps_the_test_window_out_of_the_choice(monkeypatch, tmp_path):
    dates = _dates()
    split = chronological_split(dates, train_fraction=0.6, validation_fraction=0.2)
    calls = []

    def fake_partition(histories, config, start, end, history_start=None, universe=None, stress=False):
        calls.append({
            "start": start,
            "end": end,
            "history_start": history_start,
            "fast": int(config["ma_fast_window"]),
        })
        in_sample = start == split.train_start.isoformat()
        fast = int(config["ma_fast_window"])
        # fast=2 wins in-sample and loses out of sample. fast=4 does the reverse.
        if fast == 2:
            result = 5.0 if in_sample else -3.0
        else:
            result = 1.0 if in_sample else 9.0
        return _replay(result)

    monkeypatch.setattr(
        "strategy.improvement_loop.backtest.evaluate_partition",
        fake_partition,
    )
    report = _run(
        monkeypatch,
        tmp_path,
        dates=dates,
        loop_config=_loop_config([
            {"id": "current", "parameters": {"ma_fast_window": 2}},
            {"id": "slower_trend", "parameters": {"ma_fast_window": 4, "ma_slow_window": 5}},
        ]),
    )
    assert report["ranking_qualified"] is True
    assert report["proposed_variant"] == "current"
    assert report["split"]["out_of_sample_start"] > report["split"]["in_sample_end"]
    assert report["split"]["in_sample_start"] == split.train_start.isoformat()
    assert report["split"]["out_of_sample_start"] == split.test_start.isoformat()
    assert calls
    assert all(call["history_start"] == split.train_start.isoformat() for call in calls)
    out_of_sample = [call for call in calls if call["start"] == split.test_start.isoformat()]
    assert out_of_sample
    assert all(call["start"] > report["split"]["in_sample_end"] for call in out_of_sample)
    by_id = {row["id"]: row for row in report["variants"]}
    assert by_id["current"]["oos_beats_baseline"] is False
    assert by_id["slower_trend"]["oos_beats_baseline"] is True
    assert [row["id"] for row in report["variants"][:2]] == ["current", "slower_trend"]
    assert by_id["buy_and_hold_vwrl"]["in_sample"]["excess_pct"] == 0.0
    assert by_id["do_nothing"]["in_sample"]["trade_count"] == 0
    assert by_id["do_nothing"]["in_sample"]["excess_pct"] == pytest.approx(
        -by_id["buy_and_hold_vwrl"]["in_sample"]["return_pct"]
    )


def test_parameter_replay_respects_point_in_time_membership(monkeypatch, tmp_path):
    dates = _dates()
    listing = dates[10]
    universe = _universe([
        _membership("STAY", "2020-01-01"),
        _membership("ENTER", listing),
    ])
    seen_universes = []
    real_partition = backtest.evaluate_partition

    def wrapped(*args, **kwargs):
        seen_universes.append(kwargs.get("universe"))
        return real_partition(*args, **kwargs)

    monkeypatch.setattr("strategy.improvement_loop.backtest.evaluate_partition", wrapped)
    report = _run(
        monkeypatch,
        tmp_path,
        dates=dates,
        symbols=["STAY", "ENTER"],
        universe=universe,
    )
    assert report["coverage"]["covered_count"] == 2
    assert report["coverage"]["survivorship_checks_passed"] is True
    assert report["ranking_qualified"] is True
    current = next(row for row in report["variants"] if row["id"] == "current")
    assert current["in_sample"]["trade_count"] > 0
    assert current["coverage_status"].startswith("2/2")
    assert seen_universes and all(item is not None for item in seen_universes)
    traded = real_partition(
        _market(dates, ["STAY", "ENTER"]),
        _settings(),
        report["split"]["in_sample_start"],
        report["split"]["in_sample_end"],
        history_start=report["split"]["in_sample_start"],
        universe=universe,
    )
    enter = traded.accepted_trades.loc[traded.accepted_trades["ticker"] == "ENTER", "entry_date"]
    assert not enter.empty
    assert pd.to_datetime(enter).min() >= pd.Timestamp(listing)


def test_coverage_below_the_gate_is_not_a_ranking(monkeypatch, tmp_path):
    dates = _dates()
    universe = _universe([
        _membership("STAY", "2020-01-01"),
        _membership("NODATA", "2020-01-01"),
    ])
    report = _run(
        monkeypatch,
        tmp_path,
        dates=dates,
        symbols=["STAY"],
        universe=universe,
        loop_config=_loop_config([
            {"id": "current", "parameters": {}},
            {"id": "wider_exits", "parameters": {"stop_loss_pct": 20.0}},
        ]),
    )
    assert report["coverage"]["below_gate"] is True
    assert report["coverage"]["covered_count"] == 1
    assert report["coverage"]["member_count"] == 2
    assert report["coverage"]["missing"] == ["NODATA"]
    assert report["coverage"]["survivorship_checks_passed"] is False
    assert report["coverage"]["point_in_time_universe_passed"] is True
    assert report["ranking_qualified"] is False
    assert report["proposed_variant"] is None
    assert [row["rank"] for row in report["variants"]] == [None] * len(report["variants"])
    assert [row["id"] for row in report["variants"][:2]] == ["current", "wider_exits"]
    text = __import__("strategy.improvement_loop", fromlist=["render_markdown"]).render_markdown(report)
    assert "NOT A QUALIFIED RANKING" in text
    assert "NOTHING IS PROPOSED" in text
    assert "NODATA" in text
    assert text.index("current") < text.index("wider_exits")


def test_missing_logs_are_no_data_yet_and_a_corrupt_log_raises(monkeypatch, tmp_path):
    report = _run(monkeypatch, tmp_path)
    for lane in (report["paper_signal_log"], report["demo_trade_log"], report["broker_fills"]):
        assert lane["status"] == "no data yet"
        assert lane["return_gbp"] == "no data yet"
        assert lane["return_gbp"] != 0
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    blank_fills = tmp_path / "fills.csv"
    blank_fills.write_text("fill_id,ticker\n", encoding="utf-8")
    report = _run(
        monkeypatch,
        tmp_path,
        paper_log_path=empty,
        demo_trade_log_path=empty,
        broker_fills_path=blank_fills,
    )
    assert report["paper_signal_log"]["status"] == "no data yet"
    assert report["demo_trade_log"]["trade_count"] == "no data yet"
    assert report["broker_fills"]["trade_count"] == "no data yet"
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(PaperSignalLogError):
        _run(monkeypatch, tmp_path, paper_log_path=corrupt)
    secret = "super-secret-value"
    leaked = tmp_path / "leaked.jsonl"
    leaked.write_text(json.dumps({"api_key": secret, "outcome": "fill"}) + "\n", encoding="utf-8")
    with pytest.raises(ImprovementLoopError, match="secret-shaped") as raised:
        _run(monkeypatch, tmp_path, demo_trade_log_path=leaked)
    assert secret not in str(raised.value)


def test_present_paper_log_keeps_the_scored_summary_and_not_a_zero_return(monkeypatch, tmp_path):
    path = tmp_path / "signal_log.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "strategy.improvement_loop.read_signals",
        lambda log_path: [{"signal_id": "example"}],
    )

    def fake_evaluate(config):
        summary = HorizonSummary(
            horizon_months=1,
            signal_group="all",
            signals=1,
            pending=0,
            flagged=0,
            scored=1,
            hits=1,
            hit_rate="100.0000%",
            mean_excess_return="1.5000%",
            cost_bps="10",
            gbp=1,
            fx_missing=0,
        )
        evaluation = type("Evaluation", (), {
            "cost_bps": "10",
            "benchmark_ticker": "VWRL.L",
            "summaries": (summary,),
        })()
        return [], evaluation

    monkeypatch.setattr("strategy.improvement_loop.evaluate_log", fake_evaluate)
    report = _run(monkeypatch, tmp_path, paper_log_path=path)
    lane = report["paper_signal_log"]
    assert lane["status"] == "present"
    assert lane["return_gbp"] == "no data yet"
    assert lane["summaries"][0]["mean_excess_return"] == "1.5000%"
    assert lane["summaries"][0]["cost_bps"] == "10"


def test_gbp_conversion_uses_the_paper_fx_rule_and_fails_when_a_session_is_missing():
    from datetime import timedelta

    dates = _dates(4)
    frame = _history(dates, start=125.0, step=0.0)
    fx = {day.date(): Decimal("1.25") for day in dates}
    converted, carried = convert_frame_to_gbp(frame, "USD", fx)
    assert carried == ()
    assert converted["Close"].tolist() == [100.0, 100.0, 100.0, 100.0]
    pence, pence_carried = convert_frame_to_gbp(frame, "GBp", None)
    assert pence_carried == ()
    assert pence["Close"].tolist() == [1.25, 1.25, 1.25, 1.25]
    previous = dict(fx)
    previous[dates[-2].date()] = Decimal("2.5")
    previous.pop(dates[-1].date())
    carried_frame, carried_days = convert_frame_to_gbp(frame, "USD", previous)
    assert carried_days == (dates[-1].date(),)
    assert carried_frame["Close"].iloc[-1] == 50.0
    stale = {dates[0].date() - timedelta(days=30): Decimal("1.25")}
    with pytest.raises(ImprovementLoopError, match="fx_missing"):
        convert_frame_to_gbp(frame, "USD", stale)


def test_constant_fx_preserves_the_percent_return_and_a_move_does_not(monkeypatch, tmp_path):
    dates = _dates()
    gbp = _run(monkeypatch, tmp_path, dates=dates)
    usd_config = _loop_config()
    usd_config["member_quote_currency"] = "USD"
    usd_config["benchmark_currencies"] = {"SPY": "USD", "VWRL.L": "USD"}
    fx = {"GBPUSD=X": _fx(dates, rate=2.0)}
    usd = _run(
        monkeypatch,
        tmp_path,
        dates=dates,
        loop_config=usd_config,
        fx_histories=fx,
    )
    gbp_return = next(row for row in gbp["variants"] if row["id"] == "current")["in_sample"]["return_pct"]
    usd_return = next(row for row in usd["variants"] if row["id"] == "current")["in_sample"]["return_pct"]
    assert usd_return == pytest.approx(gbp_return, abs=1e-4)
    assert usd["fx_carried_sessions"] == []
    shifted = _fx(dates, rate=2.0)
    shifted.loc[shifted.index[5]:, "Close"] = 4.0
    changed = _run(
        monkeypatch,
        tmp_path,
        dates=dates,
        loop_config=usd_config,
        fx_histories={"GBPUSD=X": shifted},
    )
    changed_return = next(row for row in changed["variants"] if row["id"] == "current")["in_sample"]["return_pct"]
    assert changed_return != pytest.approx(gbp_return, abs=1e-4)


def test_buy_and_hold_costs_reduce_the_return_and_a_gap_is_not_skipped():
    dates = _dates(8)
    history = _history(dates, start=100.0, step=1.0)
    start, end = str(dates[0].date()), str(dates[-1].date())
    free = CostModel(spread_bps=0, slippage_bps=0, fee_bps=0, tax_bps=0, stress_multiplier=1)
    paid = CostModel(spread_bps=5, slippage_bps=5, fee_bps=5, tax_bps=0, stress_multiplier=1)
    assert buy_and_hold_metrics(history, start, end, paid)["return_pct"] < buy_and_hold_metrics(
        history, start, end, free,
    )["return_pct"]
    dipped = history.copy()
    dipped.loc[3, "Close"] = 50.0
    dipped.loc[3, "Low"] = 49.0
    assert buy_and_hold_metrics(dipped, start, end, free)["max_drawdown_pct"] < 0
    with pytest.raises((ImprovementLoopError, StrategyValidationError)):
        buy_and_hold_metrics(history.iloc[:-1].reset_index(drop=True), start, end, free)


def test_missing_benchmark_and_missing_shared_sessions_fail(monkeypatch, tmp_path):
    dates = _dates()
    with pytest.raises(ImprovementLoopError, match="missing benchmark history"):
        _run(monkeypatch, tmp_path, dates=dates, benchmark_histories={"SPY": _history(dates)})
    other = pd.bdate_range("2025-01-02", periods=30)
    with pytest.raises(ImprovementLoopError, match="share no sessions"):
        _run(
            monkeypatch,
            tmp_path,
            dates=dates,
            benchmark_histories={"SPY": _history(dates), "VWRL.L": _history(other)},
        )


def test_report_write_does_not_touch_strategy_config(monkeypatch, tmp_path):
    before = {
        path: path.read_bytes()
        for path in (ROOT / "config" / "backtesting.yaml", ROOT / "config" / "improvement_loop.yaml")
    }
    report = _run(monkeypatch, tmp_path)
    destination = tmp_path / "reports"
    markdown_path, json_path = write_report(report, destination)
    assert {path.name for path in destination.iterdir()} == {"improvement_loop.md", "improvement_loop.json"}
    assert "NOT A QUALIFIED RANKING" not in markdown_path.read_text(encoding="utf-8") or report["coverage"]["below_gate"]
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["orders_sent"] is False
    assert payload["config_written"] is False
    assert payload["broker_calls"] is False
    after = {path: path.read_bytes() for path in before}
    assert after == before


def test_loop_source_does_not_import_or_call_the_demo_order_path():
    for path in LOOP_FILES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        modules: list[str] = []
        calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    calls.append(func.id)
                elif isinstance(func, ast.Attribute):
                    calls.append(func.attr)
        for module in modules:
            assert not any(
                module == forbidden or module.startswith(forbidden + ".")
                for forbidden in FORBIDDEN_MODULES
            ), module
        assert FORBIDDEN_CALLS.isdisjoint(calls)
        assert "/api/v3/trading/execution" not in source
        assert "etoro.com" not in source


def test_importing_the_loop_does_not_load_the_broker_or_demo_order_modules():
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(ROOT)!r}); "
        "import strategy.improvement_loop; "
        "names = set(sys.modules); "
        "assert 'execution.demo_orders' not in names; "
        "assert 'execution.fill_ingestion' not in names; "
        "assert not any(name == 'brokers' or name.startswith('brokers.') for name in names)"
    )
    completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
