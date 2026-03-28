from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from shared.io_utils import write_csv


def test_write_csv_replaces_existing_file_atomically(tmp_path: Path) -> None:
    path = tmp_path / "state.csv"

    write_csv(pd.DataFrame([{"ticker": "AAPL", "value": 1}]), path)
    write_csv(pd.DataFrame([{"ticker": "MSFT", "value": 2}]), path)

    output_df = pd.read_csv(path)

    assert output_df.to_dict(orient="records") == [{"ticker": "MSFT", "value": 2}]
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_csv_preserves_existing_file_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state.csv"
    write_csv(pd.DataFrame([{"ticker": "AAPL", "value": 1}]), path)
    before_text = path.read_text(encoding="utf-8")

    def fail_replace(_src, _dst) -> None:
        raise PermissionError("destination is locked")

    monkeypatch.setattr("shared.io_utils.os.replace", fail_replace)

    with pytest.raises(PermissionError, match="destination is locked"):
        write_csv(pd.DataFrame([{"ticker": "MSFT", "value": 2}]), path)

    after_text = path.read_text(encoding="utf-8")

    assert after_text == before_text
    assert list(tmp_path.glob("*.tmp")) == []
