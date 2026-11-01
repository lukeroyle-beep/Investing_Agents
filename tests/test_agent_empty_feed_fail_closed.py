from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import run_pipeline
from agents.macro_agent import macro_agent
from agents.portfolio_agent import portfolio_agent
from agents.risk_agent import risk_agent
from agents.signal_agent import signal_agent
from agents.universe_agent import universe_agent
from shared.market_data import (
    MarketDataMetadata,
    MarketDataResult,
    append_market_data_health_artifact,
    market_data_is_actionable,
)


def _feed_frame(kind: str) -> pd.DataFrame:
    columns = ["Open", "High", "Low", "Close", "Volume"]
    if kind == "empty":
        return pd.DataFrame(columns=columns)

    rows = 10
    close = [100.0 + float(index) for index in range(rows)]
    return pd.DataFrame(
        {
            "Open": close,
            "High": [value + 1.0 for value in close],
            "Low": [value - 1.0 for value in close],
            "Close": close,
            "Volume": [1_000_000.0] * rows,
        },
        index=pd.date_range("2026-10-01", periods=rows, freq="D"),
    )


def _history_result(kind: str) -> MarketDataResult:
    result = MarketDataResult(
        ticker="TEST",
        data=_feed_frame(kind),
        metadata=MarketDataMetadata(
            source="fake",
            fetched_at="2026-11-01T12:00:00+00:00",
            as_of="2026-10-31T21:00:00+00:00",
            stale=False,
            mode="normal",
            freshness_outcome="fresh",
        ),
    )
    assert market_data_is_actionable(result)
    return result


def _patch_history(monkeypatch, module, kind: str) -> None:
    result = _history_result(kind)

    def fetch_price_history(_ticker: str, **_kwargs):
        return result

    monkeypatch.setattr(module, "fetch_price_history", fetch_price_history)


def _redirect_appended_health(monkeypatch, module, health_path) -> None:
    def append(results, path=None):
        append_market_data_health_artifact(
            results,
            health_path if path is None else path,
        )

    monkeypatch.setattr(module, "append_market_data_health_artifact", append)


def _assert_health_stays_actionable(health_path) -> None:
    health = pd.read_csv(health_path)
    assert not health.empty
    assert set(health["mode"].astype(str).str.lower()) == {"normal"}


def _assert_removed(paths) -> None:
    for path in paths:
        assert not path.exists()


@pytest.mark.parametrize("kind", ["empty", "short"])
def test_universe_agent_does_not_leave_previous_leads(kind, tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outputs = {
        "DATA_SOURCE_HEALTH_PATH": data_dir / "health.csv",
        "UNIVERSE_SNAPSHOT_PATH": data_dir / "universe_snapshot.csv",
        "TOP_LEADS_PATH": data_dir / "top_leads.csv",
        "WATCHLIST_PATH": data_dir / "watchlist.csv",
        "REJECTS_PATH": data_dir / "rejects.csv",
    }
    for name, path in outputs.items():
        monkeypatch.setattr(universe_agent, name, path)
    for path in outputs.values():
        if path.name != "health.csv":
            path.write_text("ticker,name\nSTALE,Previous Lead\n", encoding="utf-8")

    monkeypatch.setattr(
        universe_agent,
        "load_assets",
        lambda: [
            {
                "ticker": "TEST",
                "name": "Test Asset",
                "asset_class": "equity",
                "region": "US",
                "exchange": "NASDAQ",
                "source": "curated",
                "index_membership": "",
                "currency": "USD",
                "sector": "Technology",
            }
        ],
    )
    _patch_history(monkeypatch, universe_agent, kind)

    with pytest.raises(RuntimeError, match="No results collected"):
        universe_agent.main()

    _assert_removed(
        [
            outputs["UNIVERSE_SNAPSHOT_PATH"],
            outputs["TOP_LEADS_PATH"],
            outputs["WATCHLIST_PATH"],
            outputs["REJECTS_PATH"],
        ]
    )
    _assert_health_stays_actionable(outputs["DATA_SOURCE_HEALTH_PATH"])
    monkeypatch.setattr(signal_agent, "TOP_LEADS_FILE", outputs["TOP_LEADS_PATH"])
    monkeypatch.setattr(signal_agent, "WATCHLIST_FILE", outputs["WATCHLIST_PATH"])
    assert signal_agent.load_candidate_assets().empty


@pytest.mark.parametrize("kind", ["empty", "short"])
def test_macro_agent_does_not_leave_previous_regime(kind, tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    proxies_path = data_dir / "macro_proxies.csv"
    regime_path = data_dir / "macro_regime.csv"
    health_path = data_dir / "health.csv"
    proxies_path.write_text("ticker,name\nSTALE,Previous Proxy\n", encoding="utf-8")
    regime_path.write_text("market_regime\nrisk_on\n", encoding="utf-8")
    monkeypatch.setattr(macro_agent, "MACRO_PROXIES_PATH", proxies_path)
    monkeypatch.setattr(macro_agent, "MACRO_REGIME_PATH", regime_path)
    _redirect_appended_health(monkeypatch, macro_agent, health_path)
    _patch_history(monkeypatch, macro_agent, kind)

    with pytest.raises(RuntimeError, match="could not collect all required proxy data"):
        macro_agent.main()

    _assert_removed([proxies_path, regime_path])
    _assert_health_stays_actionable(health_path)
    monkeypatch.setattr(signal_agent, "MACRO_REGIME_FILE", regime_path)
    monkeypatch.setattr(portfolio_agent, "MACRO_REGIME_FILE", regime_path)
    assert signal_agent.load_market_regime() == "neutral"
    assert portfolio_agent.load_macro_regime() == "neutral"


@pytest.mark.parametrize("kind", ["empty", "short"])
def test_signal_agent_does_not_leave_previous_setups(kind, tmp_path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    leads_path = data_dir / "top_leads.csv"
    watchlist_path = data_dir / "watchlist.csv"
    regime_path = data_dir / "macro_regime.csv"
    setups_path = data_dir / "signal_setups.csv"
    top_setups_path = data_dir / "signal_top_setups.csv"
    health_path = data_dir / "health.csv"
    leads_path.write_text("ticker,name\nTEST,Test Asset\n", encoding="utf-8")
    setups_path.write_text("ticker,name\nSTALE,Previous Setup\n", encoding="utf-8")
    top_setups_path.write_text("ticker,name\nSTALE,Previous Setup\n", encoding="utf-8")
    monkeypatch.setattr(signal_agent, "TOP_LEADS_FILE", leads_path)
    monkeypatch.setattr(signal_agent, "WATCHLIST_FILE", watchlist_path)
    monkeypatch.setattr(signal_agent, "MACRO_REGIME_FILE", regime_path)
    monkeypatch.setattr(signal_agent, "SIGNAL_SETUPS_PATH", setups_path)
    monkeypatch.setattr(signal_agent, "SIGNAL_TOP_SETUPS_PATH", top_setups_path)
    _redirect_appended_health(monkeypatch, signal_agent, health_path)
    _patch_history(monkeypatch, signal_agent, kind)

    with pytest.raises(RuntimeError, match="No signal results collected"):
        signal_agent.main()

    _assert_removed([setups_path, top_setups_path])
    _assert_health_stays_actionable(health_path)
    monkeypatch.setattr(risk_agent, "SIGNAL_SETUPS_FILE", setups_path)
    assert risk_agent.load_signal_setups().empty
    assert leads_path.exists()


def test_pipeline_stops_before_downstream_when_an_agent_exits_nonzero(monkeypatch) -> None:
    calls: list[str] = []

    def fake_run(argv, **_kwargs):
        module = argv[-1]
        calls.append(module)
        return SimpleNamespace(
            returncode=1,
            stdout="No results collected.\n",
            stderr="RuntimeError: No results collected.\n",
        )

    monkeypatch.setattr(run_pipeline.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Universe Agent failed"):
        run_pipeline.run_pipeline(
            [
                ("Universe Agent", "agents.universe_agent.universe_agent"),
                ("Macro Agent", "agents.macro_agent.macro_agent"),
                ("Signal Agent", "agents.signal_agent.signal_agent"),
                ("Portfolio Agent", "agents.portfolio_agent.portfolio_agent"),
            ]
        )

    assert calls == ["agents.universe_agent.universe_agent"]
