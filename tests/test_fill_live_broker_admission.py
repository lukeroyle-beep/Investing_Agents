"""Live broker staging must not fall through to synthetic float economics."""
from pathlib import Path

import pandas as pd
import pytest

from agents.fill_agent import fill_agent
from agents.fill_agent.live_broker_admission import (
    BLOCK_REASON, LiveBrokerEconomicApplicationBlocked,
    assert_legacy_fill_inputs_admissible, assert_legacy_fill_row_admissible,
)
from execution.fill_ingestion import stage_broker_executions
from tests.test_fill_staging_precision import execution


def _stage(path):
    stage_broker_executions(path, executions=(execution(),),
                            ticker_by_instrument_id={1001: "AAPL"}, broker="etoro")


def _bytes_under(path):
    return {str(item.relative_to(path)): item.read_bytes() for item in Path(path).rglob("*") if item.is_file()}


def test_fresh_runtime_keeps_exact_staging_and_creates_no_synthetic_account(tmp_path, monkeypatch):
    stage = tmp_path / "broker_fills.csv"
    _stage(stage)
    before = _bytes_under(tmp_path)
    monkeypatch.setattr(fill_agent, "BROKER_FILLS_PATH", str(stage))
    monkeypatch.setattr(fill_agent, "MANUAL_FILLS_PATH", str(tmp_path / "manual_fills.csv"))
    monkeypatch.setattr(fill_agent, "current_run_id", lambda: pytest.fail("admission must precede run initialization"))
    monkeypatch.setattr(fill_agent, "ensure_event_log_exists", lambda: pytest.fail("admission must precede event writes"))
    with pytest.raises(LiveBrokerEconomicApplicationBlocked, match=BLOCK_REASON):
        fill_agent.run_fill_agent()
    assert _bytes_under(tmp_path) == before
    assert "0.123456789123456789" in stage.read_text()
    assert not (tmp_path / "cash_state.csv").exists()
    assert not (tmp_path / "portfolio_state.csv").exists()


def test_existing_cash_holdings_and_processed_marker_do_not_bypass_guard(isolated_workspace):
    data = isolated_workspace / "data"
    _stage(data / "broker_fills.csv")
    (data / "cash_state.csv").write_text("as_of,cash_balance\n2026-10-26T00:00:00Z,12000.57123456789123456789\n")
    (data / "portfolio_state.csv").write_text("position_id,ticker,quantity\nexisting,AAPL,123.123456789123456789\n")
    (data / "processed_fills.csv").write_text("fill_id,processed_at,run_id\netoro:demo:execution-1,2026-10-26T00:00:00Z,prior\n")
    before = _bytes_under(isolated_workspace)
    for _ in range(2):
        with pytest.raises(LiveBrokerEconomicApplicationBlocked):
            fill_agent.run_fill_agent()
        assert _bytes_under(isolated_workspace) == before


def test_moving_staged_rows_to_manual_file_does_not_hide_broker_origin(tmp_path):
    broker = tmp_path / "broker.csv"
    _stage(broker)
    rows = pd.read_csv(broker, dtype=str, keep_default_na=False)
    rows = rows.drop(columns=["broker", "environment"])
    manual = tmp_path / "manual.csv"
    rows.to_csv(manual, index=False)
    with pytest.raises(LiveBrokerEconomicApplicationBlocked):
        assert_legacy_fill_inputs_admissible(manual_fills_path=manual, broker_fills_path=tmp_path / "absent.csv")


@pytest.mark.parametrize("row", [
    {"broker": "etoro", "environment": "demo", "fill_id": "anything"},
    {"broker": " ETORO ", "environment": "demo", "fill_id": "anything"},
    {"broker": "fake", "environment": "demo", "fill_id": "etoro:demo:e1"},
    {"broker": "another-live-broker", "environment": "demo", "fill_id": "e1"},
    {"broker": "fake", "environment": "real", "fill_id": "fake:real:e1"},
])
def test_nonfixture_or_conflicting_identity_is_rejected_before_numeric_coercion(row):
    with pytest.raises(LiveBrokerEconomicApplicationBlocked):
        assert_legacy_fill_row_admissible(row)


@pytest.mark.parametrize("writer", [fill_agent.open_long_position, fill_agent.close_long_position, fill_agent.append_trade_fill])
def test_direct_economic_writer_entry_rejects_broker_row(writer):
    row = pd.Series({"broker": "etoro", "environment": "demo", "fill_id": "etoro:demo:e1"})
    with pytest.raises(LiveBrokerEconomicApplicationBlocked):
        if writer == fill_agent.append_trade_fill:
            writer(pd.DataFrame(), row, "run")
        else:
            writer(pd.DataFrame(), 100000.0, pd.DataFrame(), row, "run")


def test_direct_input_reader_rejects_before_creating_other_inputs(tmp_path, monkeypatch):
    broker = tmp_path / "broker_fills.csv"
    _stage(broker)
    monkeypatch.setattr(fill_agent, "BROKER_FILLS_PATH", str(broker))
    monkeypatch.setattr(fill_agent, "MANUAL_FILLS_PATH", str(tmp_path / "manual_fills.csv"))
    before = _bytes_under(tmp_path)
    with pytest.raises(LiveBrokerEconomicApplicationBlocked):
        fill_agent.read_fill_input()
    assert _bytes_under(tmp_path) == before


def test_missing_broker_identity_or_ambiguous_csv_fails_without_mutation(tmp_path):
    manual = tmp_path / "manual.csv"
    broker = tmp_path / "broker.csv"
    for text in ("fill_id,quantity\nexecution-1,0.123456789123456789\n",
                 "broker,broker,environment,fill_id\nfake,etoro,demo,e1\n"):
        broker.write_text(text)
        with pytest.raises(LiveBrokerEconomicApplicationBlocked):
            assert_legacy_fill_inputs_admissible(manual_fills_path=manual, broker_fills_path=broker)
        assert broker.read_text() == text


def test_manual_and_fake_fixture_inputs_remain_supported(tmp_path):
    manual = tmp_path / "manual.csv"
    manual.write_text("fill_id,ticker,quantity\nmanual1,AAPL,1\n")
    broker = tmp_path / "broker.csv"
    broker.write_text("fill_id,broker,environment\nfake:demo:fixture1,fake,demo\n")
    assert_legacy_fill_inputs_admissible(manual_fills_path=manual, broker_fills_path=broker)
