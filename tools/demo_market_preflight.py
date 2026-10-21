"""Read-only pilot identity, eligibility and scheduled-hours diagnostic."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from brokers.etoro.client import EtoroDemoReadOnlyConfig, EtoroReadOnlyClient
from brokers.etoro.market_evidence import capture_pilot_market_evidence
from execution.domain import dataclass_json
from execution.instruments import Instrument
from execution.market_session import PILOT_SYMBOLS
from shared.paths import ETORO_DEMO_CONFIG_PATH


def preflight_report(*, client=None, instrument=None, broker_instrument_id=None, now=None):
    report = {
        "objective": "virtual_functionality", "return_target_pct": None,
        "source_kind": "broker_demo_market_preflight",
        "submission_available": False, "qualification_credit": False,
        "broker_orders_submitted": 0, "market_evidence_captured": False,
        "blockers": ["account_risk_and_identity_not_bound", "quote_and_halt_evidence_missing",
                     "exact_order_not_approved", "live_operator_not_bound"],
    }
    if client is None or instrument is None or broker_instrument_id is None:
        report["blockers"].insert(0, "read_only_client_or_exact_instrument_identity_missing")
        return report
    try:
        evidence = capture_pilot_market_evidence(client, instrument,
                    broker_instrument_id=broker_instrument_id, now=now)
    except Exception as exc:
        report["blockers"].insert(0, "pilot_market_capture_failed")
        report["capture_error_type"] = type(exc).__name__
        return report  # Never copy response bodies, keys or customer IDs.
    report["market_evidence_captured"] = True
    report["evidence"] = json.loads(dataclass_json(evidence))
    for field in ("opens_at", "closes_at"):
        value = getattr(evidence.session, field)
        report[f"session_{field}_uk"] = value.astimezone(ZoneInfo("Europe/London")).strftime("%Y-%m-%d %H:%M %Z") if value else None
    if not evidence.supported_pilot_configuration:
        report["blockers"].append("pilot_broker_configuration_unavailable")
    if not evidence.session.scheduled_regular_session_open:
        report["blockers"].append("outside_supported_regular_session")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ETORO_DEMO_CONFIG_PATH)
    parser.add_argument("--symbol", choices=sorted(PILOT_SYMBOLS))
    parser.add_argument("--internal-instrument-id", type=UUID)
    parser.add_argument("--broker-instrument-id", type=int)
    args = parser.parse_args(argv)
    client = instrument = None
    setup_error = None
    try:
        config = EtoroDemoReadOnlyConfig.load(args.config)
        if config.enabled and args.symbol and args.internal_instrument_id and args.broker_instrument_id:
            instrument = Instrument(args.internal_instrument_id, args.symbol, "NASDAQ", "equity", "USD")
            client = EtoroReadOnlyClient.from_config(args.config)
    except Exception as exc:
        setup_error = type(exc).__name__
    report = preflight_report(client=client, instrument=instrument, broker_instrument_id=args.broker_instrument_id)
    if setup_error:
        report["setup_error_type"] = setup_error
    print(json.dumps(report, sort_keys=True, indent=2))
    return 2  # A successful diagnostic never grants order permission.


if __name__ == "__main__":
    raise SystemExit(main())
