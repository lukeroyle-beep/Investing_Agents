from __future__ import annotations

import pandas as pd
import pytest
import yaml

from scripts.validate_virtual_backtest import load_validation_config, read_snapshot, validate
from shared.paths import config_path
from strategy.history_validation import audit_histories


def pilot():
    settings = {"tickers": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"],
                "benchmark_tickers": ["SPY", "QQQ"],
                "start_date": "2022-01-01", "end_date": ""}
    bars = pd.DataFrame({"Date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
                         "Open": [100., 101., 102.], "High": [102., 103., 104.],
                         "Low": [99., 100., 101.], "Close": [101., 102., 103.]})
    return ({ticker: bars.copy() for ticker in settings["tickers"]},
            {ticker: bars.copy() for ticker in settings["benchmark_tickers"]}, settings)


@pytest.mark.parametrize("role,ticker,replacement", [
    ("strategy", "AAPL", None), ("strategy", "AAPL", "NFLX"),
    ("benchmark", "QQQ", None), ("benchmark", "QQQ", "IWM"),
])
def test_reduced_or_relabelled_universes_fail_before_output_creation(tmp_path, role, ticker, replacement):
    histories, benchmarks, settings = pilot()
    frames = histories if role == "strategy" else benchmarks
    removed = frames.pop(ticker)
    if replacement:
        frames[replacement] = removed
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError, match="membership mismatch"):
        validate(histories, benchmarks, settings, output, "supplied_snapshot")
    assert not output.exists()


@pytest.mark.parametrize("change", ["empty", "duplicate", "overlap", "blank", "unnormalized"])
def test_ambiguous_configured_membership_is_rejected(change):
    histories, benchmarks, settings = pilot()
    if change == "empty":
        settings["tickers"] = []
    elif change == "duplicate":
        settings["benchmark_tickers"].append("QQQ")
    elif change == "overlap":
        settings["benchmark_tickers"].append("AAPL")
    elif change == "blank":
        settings["tickers"][0] = ""
    else:
        settings["tickers"][0] = "aapl"
    with pytest.raises(ValueError, match="configured"):
        audit_histories(histories, benchmarks, settings)


@pytest.mark.parametrize("ticker", ["SPY", "QQQ"])
def test_interior_benchmark_gap_cannot_hide_behind_valid_endpoints(ticker):
    histories, benchmarks, settings = pilot()
    benchmarks[ticker] = benchmarks[ticker].drop(index=1)
    with pytest.raises(ValueError, match=f"{ticker}: observed-session coverage mismatch"):
        audit_histories(histories, benchmarks, settings)


@pytest.mark.parametrize("column,value", [("Open", float("nan")), ("Close", float("inf")),
                                         ("Low", -1), ("High", 1), ("Low", 200)])
@pytest.mark.parametrize("ticker", ["SPY", "QQQ"])
def test_invalid_interior_benchmark_prices_cannot_be_ignored(ticker, column, value):
    histories, benchmarks, settings = pilot()
    benchmarks[ticker].loc[1, column] = value
    with pytest.raises(ValueError, match="OHLC"):
        audit_histories(histories, benchmarks, settings)


def test_boolean_bars_cannot_masquerade_as_unit_prices():
    histories, benchmarks, settings = pilot()
    benchmarks["QQQ"] = benchmarks["QQQ"].astype(object)
    benchmarks["QQQ"].loc[1, ["Open", "High", "Low", "Close"]] = True
    with pytest.raises(ValueError, match="must not be booleans"):
        audit_histories(histories, benchmarks, settings)


@pytest.mark.parametrize("dates", [
    ["2024-01-02", "2024-01-02", "2024-01-04"],
    ["2024-01-03", "2024-01-02", "2024-01-04"],
    ["2024-01-02", "2024-01-03T12:00:00", "2024-01-04"],
    ["2024-01-02", "2024-01-03T00:00:00+00:00", "2024-01-04"],
    ["2024-01-02", "NaT", "2024-01-04"],
])
def test_csv_does_not_silently_sort_or_convert_invalid_session_labels(tmp_path, dates):
    _, benchmarks, _ = pilot()
    frame = benchmarks["QQQ"].assign(Date=dates, role="benchmark", ticker="QQQ")
    path = tmp_path / "prices.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match="daily labels"):
        read_snapshot(path)


def test_shared_missing_day_remains_explicitly_unqualified():
    histories, benchmarks, settings = pilot()
    for frames in (histories, benchmarks):
        for ticker in frames:
            frames[ticker] = frames[ticker].drop(index=1)
    audit = audit_histories(histories, benchmarks, settings)
    assert audit["observed_sessions_aligned"] is True
    assert audit["exchange_calendar_qualified"] is False
    assert "sessions_missing_from_every_series" in audit["limitations"][0]
    assert audit["requested_range"] == {"start": "2022-01-01", "end": None}
    assert audit["actual_range"] == {"start": "2024-01-02", "end": "2024-01-04"}
    assert len(audit["instruments"]) == 8


def test_selected_pilot_is_saved_and_replays_without_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTING_RESEARCH_UNIVERSE", "config/demo_research_universe.yaml")
    selected = load_validation_config(config_path("backtesting.yaml"))
    assert selected["tickers"] == pilot()[2]["tickers"]
    saved = tmp_path / "config.yaml"
    saved.write_text(yaml.safe_dump(selected))
    assert load_validation_config(saved) == selected
    monkeypatch.delenv("INVESTING_RESEARCH_UNIVERSE")
    assert load_validation_config(saved) == selected


@pytest.mark.parametrize("field,value", [("research_universe_version", "another_version"),
                                       ("research_universe_checksum", "changed"),
                                       ("tickers", ["AAPL"])])
def test_selected_pilot_cannot_relabel_saved_configuration(tmp_path, monkeypatch, field, value):
    monkeypatch.setenv("INVESTING_RESEARCH_UNIVERSE", "config/demo_research_universe.yaml")
    selected = load_validation_config(config_path("backtesting.yaml"))
    selected[field] = value
    saved = tmp_path / "config.yaml"
    saved.write_text(yaml.safe_dump(selected))
    with pytest.raises(ValueError, match="conflicts with saved"):
        load_validation_config(saved)


def test_synthetic_input_cannot_claim_pilot_membership(tmp_path, monkeypatch):
    monkeypatch.setenv("INVESTING_RESEARCH_UNIVERSE", "config/demo_research_universe.yaml")
    with pytest.raises(ValueError, match="synthetic validation"):
        load_validation_config(config_path("backtesting.yaml"), synthetic=True)
    saved = tmp_path / "pilot.yaml"
    saved.write_text(yaml.safe_dump(load_validation_config(config_path("backtesting.yaml"))))
    monkeypatch.delenv("INVESTING_RESEARCH_UNIVERSE")
    with pytest.raises(ValueError, match="synthetic validation"):
        load_validation_config(saved, synthetic=True)


def test_partial_saved_universe_metadata_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("INVESTING_RESEARCH_UNIVERSE", raising=False)
    saved = tmp_path / "config.yaml"
    saved.write_text("research_universe_version: demo_us_equities_v1\n")
    with pytest.raises(ValueError, match="both version and checksum"):
        load_validation_config(saved)


@pytest.mark.parametrize("duplicate", ["Close", "Date", "role", "ticker"])
def test_csv_duplicate_headers_are_rejected_before_pandas_can_rename_them(tmp_path, duplicate):
    snapshot = tmp_path / "ambiguous.csv"
    snapshot.write_text(f"role,ticker,Date,Open,High,Low,Close,{duplicate}\n"
                        "strategy,AAPL,2024-01-02,100,102,99,101,-5\n")
    with pytest.raises(ValueError, match="headers.*unique"):
        read_snapshot(snapshot)


@pytest.mark.parametrize("row", [
    "ignored,strategy,AAPL,2024-01-02,100,102,99,101",
    "strategy,AAPL,2024-01-02,100,102,99",
])
def test_csv_row_width_cannot_be_silently_reinterpreted(tmp_path, row):
    snapshot = tmp_path / "ragged.csv"
    snapshot.write_text("role,ticker,Date,Open,High,Low,Close\n" + row + "\n")
    with pytest.raises(ValueError, match="header width"):
        read_snapshot(snapshot)
