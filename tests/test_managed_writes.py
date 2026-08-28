from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from shared.io_utils import (
    ManagedSchemaMismatchError,
    ProducerOwnershipError,
    UnknownManagedArtifactError,
    write_csv,
    write_managed_csv_with_schema,
)
from shared.schemas import PORTFOLIO_MONITOR_SCHEMA, PORTFOLIO_STATE_SCHEMA
from tests.helpers import (
    open_position_row,
    portfolio_monitor_frame,
    portfolio_monitor_row,
    portfolio_state_frame,
)


def test_economic_artifact_rejects_anonymous_and_wrong_owner_writes(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.csv"
    state = portfolio_state_frame([open_position_row()])

    with pytest.raises(ProducerOwnershipError, match="requires producer='Fill Agent'"):
        write_csv(state, path)

    with pytest.raises(ProducerOwnershipError, match="Position Tracking Agent"):
        write_csv(state, path, producer="Position Tracking Agent")

    with pytest.raises(ProducerOwnershipError, match="Fill Agent "):
        write_csv(state, path, producer="Fill Agent ")

    assert not path.exists()


def test_managed_write_accepts_exact_registered_owner(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_state.csv"
    state = portfolio_state_frame([open_position_row()])

    write_managed_csv_with_schema(
        state,
        path,
        schema=PORTFOLIO_STATE_SCHEMA,
        producer="Fill Agent",
    )

    assert pd.read_csv(path)["position_id"].tolist() == ["POS001"]


def test_managed_write_rejects_unknown_artifact_and_conflicting_schema(tmp_path: Path) -> None:
    monitor = portfolio_monitor_frame([portfolio_monitor_row()])

    with pytest.raises(UnknownManagedArtifactError, match="no schema-registry owner"):
        write_managed_csv_with_schema(
            monitor,
            tmp_path / "unknown.csv",
            schema=PORTFOLIO_MONITOR_SCHEMA,
            producer="Position Tracking Agent",
        )

    conflicting_schema = replace(
        PORTFOLIO_MONITOR_SCHEMA,
        column_order=list(reversed(PORTFOLIO_MONITOR_SCHEMA.column_order)),
    )
    with pytest.raises(ManagedSchemaMismatchError, match="canonical column order"):
        write_managed_csv_with_schema(
            monitor,
            tmp_path / "portfolio_monitor.csv",
            schema=conflicting_schema,
            producer="Position Tracking Agent",
        )


def test_managed_atomic_replace_failure_preserves_economic_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "portfolio_state.csv"
    initial = portfolio_state_frame([open_position_row(position_id="POS_INITIAL")])
    replacement = portfolio_state_frame([open_position_row(position_id="POS_REPLACEMENT")])
    write_managed_csv_with_schema(
        initial,
        path,
        schema=PORTFOLIO_STATE_SCHEMA,
        producer="Fill Agent",
    )
    before = path.read_bytes()

    def fail_replace(_src, _dst) -> None:
        raise PermissionError("simulated atomic replace failure")

    monkeypatch.setattr("shared.io_utils.os.replace", fail_replace)
    with pytest.raises(PermissionError, match="simulated atomic replace failure"):
        write_managed_csv_with_schema(
            replacement,
            path,
            schema=PORTFOLIO_STATE_SCHEMA,
            producer="Fill Agent",
        )

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".portfolio_state.*.tmp")) == []


def test_concurrent_monitor_writes_are_never_observed_partially(tmp_path: Path) -> None:
    path = tmp_path / "portfolio_monitor.csv"
    expected_columns = list(PORTFOLIO_MONITOR_SCHEMA.column_order)
    write_managed_csv_with_schema(
        portfolio_monitor_frame([portfolio_monitor_row(current_price=100.0)]),
        path,
        schema=PORTFOLIO_MONITOR_SCHEMA,
        producer="Position Tracking Agent",
    )

    def writer(price: float) -> None:
        frame = portfolio_monitor_frame(
            [
                portfolio_monitor_row(
                    current_price=price,
                    market_value=price * 10,
                    pnl_abs=(price - 100) * 10,
                    pnl_pct=price - 100,
                )
            ]
        )
        for _ in range(20):
            write_managed_csv_with_schema(
                frame,
                path,
                schema=PORTFOLIO_MONITOR_SCHEMA,
                producer="Position Tracking Agent",
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(writer, price) for price in (101.0, 102.0, 103.0)]
        for _ in range(100):
            observed = pd.read_csv(path)
            assert list(observed.columns) == expected_columns
            assert len(observed) == 1
            assert observed.iloc[0]["current_price"] in {100.0, 101.0, 102.0, 103.0}
        for future in futures:
            future.result()

    assert list(tmp_path.glob(".portfolio_monitor.*.tmp")) == []
