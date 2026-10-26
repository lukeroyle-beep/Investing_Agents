"""Synthetic integration evidence is explicitly separate from a broker trade."""
import json
from decimal import Decimal

import pytest

from scripts.validate_connector_demo import exercise


@pytest.mark.parametrize("crash", [False, True])
def test_synthetic_host_full_cycle_preserves_existing_holdings_and_exact_cash(tmp_path, crash):
    output = tmp_path / "new-runtime"
    report = exercise(output, crash_after_response=crash)
    assert report["functional_result"] == "pass"
    assert all(report["functional_checks"].values())
    assert report["live_broker_submissions"] == report["live_fills"] == 0
    assert not report["live_economic_reconciliation"] and not report["qualification_credit"]
    assert report["human_approval_simulated"] and report["cash_clock_and_complete_history_simulated"]
    assert report["final_state"]["cash_balance"] == Decimal("9989.99")
    saved = json.loads((output / "validation.json").read_text())
    assert saved["source_kind"] == "synthetic_only"
    assert saved["final_state"]["cash_balance"] == "9989.99"
    assert len(saved["final_state"]["positions"]) == 2
    assert b"SYNTHETIC-NONSECRET-TOKEN" not in (output / "synthetic_control" / "connector.sqlite3").read_bytes()


def test_synthetic_validator_refuses_existing_directory(tmp_path):
    retained = tmp_path / "retained-evidence"
    retained.mkdir()
    (retained / "marker").write_text("preserve")
    with pytest.raises(FileExistsError):
        exercise(retained)
    assert (retained / "marker").read_text() == "preserve"
