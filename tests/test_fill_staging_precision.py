from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from brokers.base import BrokerExecution
from execution.domain import Environment
from execution.fill_ingestion import BrokerFillIngestionError, stage_broker_executions


def execution(identifier="execution-1"):
    return BrokerExecution(identifier, "123", 1001, "456", "reference", Environment.DEMO,
        "long", "buy", D("0.123456789123456789"), D("123.123456789123456789"),
        datetime(2026, 10, 26, tzinfo=timezone.utc), "rate", D("0.000123456789123456"), D("0"), "USD")


def stage(path, *rows):
    return stage_broker_executions(path, executions=rows, ticker_by_instrument_id={1001: "AAPL"}, broker="etoro")


def test_append_and_replay_preserve_every_original_decimal_digit(tmp_path):
    path = tmp_path / "fills.csv"
    first = execution()
    stage(path, first)
    row_before = path.read_text().splitlines()[1]
    stage(path, first, execution("execution-2"))
    assert path.read_text().splitlines()[1] == row_before
    assert len(stage(path, first)) == 2


def test_same_identity_changed_economics_or_broker_mapping_fails(tmp_path):
    path = tmp_path / "fills.csv"
    first = execution()
    stage(path, first)
    before = path.read_bytes()
    for changed in (replace(first, units=D("1")), replace(first, broker_position_id="other")):
        with pytest.raises(BrokerFillIngestionError, match="contradictory"):
            stage(path, changed)
        assert path.read_bytes() == before


def test_conflicting_duplicates_in_same_response_fail_without_writing(tmp_path):
    path = tmp_path / "fills.csv"
    first = execution()
    with pytest.raises(BrokerFillIngestionError, match="contradictory"):
        stage(path, first, replace(first, fees=D("4")))
    assert not path.exists()


def test_authoritative_principal_survives_and_is_part_of_execution_identity(tmp_path):
    path = tmp_path / "fills.csv"
    first = replace(execution(), executed_amount=D("10.00000000000000000000000000001"))
    stage(path, first)
    before = path.read_bytes()
    assert str(first.executed_amount) in path.read_text()
    stage(path, first)
    assert path.read_bytes() == before
    with pytest.raises(BrokerFillIngestionError, match="contradictory"):
        stage(path, replace(first, executed_amount=D("10")))
    assert path.read_bytes() == before


def test_legacy_precision_is_preserved_when_new_principal_column_is_added(tmp_path):
    path = tmp_path / "fills.csv"
    stage(path, execution())
    stage(path, replace(execution("execution-2"), executed_amount=D("10")))
    output = stage(path, execution())
    assert output.iloc[0]["quantity"] == str(execution().units)
    assert output.iloc[0]["executed_amount"] == ""
    assert output.iloc[1]["executed_amount"] == "10"
