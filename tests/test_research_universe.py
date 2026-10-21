import pytest

from agents.backtesting_agent.backtesting_agent import load_backtest_config
from agents.universe_agent.universe_agent import load_assets
from shared.research_universe import SELECTION_VARIABLE, selected_research_universe
from tools.research_coverage import coverage_report


def test_shared_pilot_aligns_scanner_and_backtest_without_granting_execution(monkeypatch):
    monkeypatch.setenv(SELECTION_VARIABLE, "config/demo_research_universe.yaml")
    selection = selected_research_universe()
    assert selection.version == "demo_us_equities_v1"
    assert len(selection.checksum) == 64
    assert [asset["ticker"] for asset in load_assets()] == load_backtest_config()["tickers"] == list(selection.tickers)
    assert len(selection.tickers) == 6
    assert load_backtest_config()["point_in_time_universe_required"] is True
    assert load_backtest_config()["return_target_pct"] is None
    report = coverage_report()
    assert report["membership_aligned"] is True
    assert report["scanner_count"] == report["backtest_strategy_count"] == 6
    assert report["historical_data_validated"] is report["whole_market"] is False


def test_no_selection_preserves_explicit_legacy_coverage(monkeypatch):
    monkeypatch.delenv(SELECTION_VARIABLE, raising=False)
    assert selected_research_universe() is None
    assert len(load_assets()) == 61
    assert len(load_backtest_config()["tickers"]) == 12


@pytest.mark.parametrize("tickers", ["[AAPL, AAPL]", "[ETH-USD]", "[UNKNOWN]", "[]", "[aapl]"])
def test_bad_selection_fails_in_both_consumers(tmp_path, monkeypatch, tickers):
    manifest = tmp_path / "universe.yaml"
    manifest.write_text("schema_version: 1\nversion: test\nobjective: virtual_functionality\ntickers: " + tickers)
    monkeypatch.setenv(SELECTION_VARIABLE, str(manifest))
    for consumer in (load_assets, load_backtest_config):
        with pytest.raises(ValueError):
            consumer()
