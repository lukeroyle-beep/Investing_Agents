"""Normalize saved eToro daily read responses for offline, unqualified research.

No credentials, network access or broker execution. A capture is a locally
recorded response bundle, not independently authenticated market evidence.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_virtual_backtest import load_validation_config
from strategy.history_validation import audit_histories


def _response(envelope):
    if (not isinstance(envelope, dict) or envelope.get("statusCode") != 200
            or envelope.get("isSuccess") is not True
            or envelope.get("bodyTruncated") is not False or envelope.get("error")):
        raise ValueError("capture requires successful, explicitly untruncated responses")
    body = envelope.get("body")
    if not isinstance(body, str):
        raise ValueError("capture must preserve the raw response body string")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("response body must be an object")
    return parsed, hashlib.sha256(body.encode("utf-8")).hexdigest()


def normalize_capture(capture, config, *, completed_before=None):
    if (type(capture.get("schema_version")) is not int or capture["schema_version"] != 1
            or capture.get("source") != "etoro_public_api"):
        raise ValueError("unsupported capture schema or source")
    captured = datetime.fromisoformat(capture["captured_at_utc"].replace("Z", "+00:00"))
    if captured.utcoffset() is None or captured.utcoffset().total_seconds() != 0:
        raise ValueError("capture receipt time must explicitly use UTC")
    # The pilot is US listed. Exclude its current local date even before open.
    # This is conservative exclusion, not proof of finality or exchange dates.
    current_date = captured.astimezone(ZoneInfo("America/New_York")).date()
    cutoff = date.fromisoformat(completed_before) if completed_before else current_date
    if cutoff > current_date:
        raise ValueError("completed-before cannot include the current New York date")
    request = capture.get("request", {})
    count = request.get("candles_count")
    if (request.get("direction") != "asc" or request.get("interval") != "OneDay"
            or type(count) is not int or not 1 <= count <= 1000):
        raise ValueError("only bounded ascending OneDay captures are supported")
    strategy = config["tickers"]
    benchmarks = config["benchmark_tickers"]
    expected = set(strategy) | set(benchmarks)
    securities, securities_hash = _response(capture["securities"])
    pagination = securities.get("pagination", {})
    if pagination.get("hasNext") is not False:
        raise ValueError("security metadata pagination is incomplete")
    metadata = securities.get("results", [])
    if (len(metadata) != len(expected)
            or {item.get("symbol") for item in metadata} != expected):
        raise ValueError("security metadata must match all configured symbols exactly")
    identities = {item["symbol"]: item for item in metadata}
    ids = [item.get("instrumentId") for item in metadata]
    if (any(type(value) is not int or value <= 0 for value in ids) or len(set(ids)) != len(ids)):
        raise ValueError("security instrument identifiers must be positive and unique")
    if set(capture.get("histories", {})) != expected:
        raise ValueError("captured histories must match all configured symbols exactly")
    histories = {"strategy": {}, "benchmark": {}}
    observations = []
    for symbol in sorted(expected):
        identity = identities[symbol]
        role = "strategy" if symbol in strategy else "benchmark"
        expected_type = "Stocks" if role == "strategy" else "ETF"
        if identity.get("type") != expected_type:
            raise ValueError(f"{symbol}: unsupported pilot instrument type")
        body, raw_hash = _response(capture["histories"][symbol])
        groups = body.get("candles", [])
        if (body.get("interval") != "OneDay" or len(groups) != 1
                or groups[0].get("instrumentId") != identity["instrumentId"]):
            raise ValueError(f"{symbol}: candle interval or instrument identity mismatch")
        candles = groups[0].get("candles", [])
        if len(candles) != count:
            raise ValueError(f"{symbol}: requested candle count was not returned")
        rows, omitted, previous = [], [], None
        for candle in candles:
            if candle.get("instrumentID") != identity["instrumentId"]:
                raise ValueError(f"{symbol}: candle instrument identity mismatch")
            raw_date = candle.get("fromDate", "")
            if not isinstance(raw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00Z", raw_date):
                raise ValueError(f"{symbol}: only explicit UTC-midnight daily labels supported")
            label = date.fromisoformat(raw_date[:10])
            if previous is not None and label <= previous:
                raise ValueError(f"{symbol}: candle labels must be ascending and unique")
            if label > current_date:
                raise ValueError(f"{symbol}: future candle label")
            previous = label
            prices = [candle.get(name) for name in ("open", "high", "low", "close")]
            if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in prices):
                raise ValueError(f"{symbol}: candle OHLC must be finite and positive")
            open_, high, low, close = prices
            if not low <= min(open_, close) <= max(open_, close) <= high:
                raise ValueError(f"{symbol}: inconsistent candle OHLC range")
            volume = candle.get("volume")
            if type(volume) not in (int, float) or not math.isfinite(volume) or volume < 0:
                raise ValueError(f"{symbol}: invalid candle volume")
            if label >= cutoff:
                omitted.append(label.isoformat())
                continue
            rows.append({"Date": pd.Timestamp(label), "Open": open_, "High": high,
                         "Low": low, "Close": close, "Volume": volume})
        if not rows:
            raise ValueError(f"{symbol}: no completed historical labels remain")
        histories[role][symbol] = pd.DataFrame(rows)
        observations.append({"symbol": symbol, "role": role,
            "instrument_id": identity["instrumentId"], "exchange_id": identity.get("exchangeId"),
            "route": f"/api/v1/market-data/instruments/{identity['instrumentId']}/history/candles/asc/OneDay/{count}",
            "raw_body_sha256": raw_hash, "received_candles": len(candles),
            "retained_candles": len(rows), "excluded_labels": omitted})
    audit = audit_histories(histories["strategy"], histories["benchmark"], config)
    frame = pd.concat([history.assign(role=role, ticker=symbol)
                       for role, collection in histories.items()
                       for symbol, history in sorted(collection.items())], ignore_index=True)
    manifest = {
        "schema_version": 1, "source": "etoro_public_api_saved_responses_unqualified",
        "captured_at_utc": capture["captured_at_utc"], "completed_before": cutoff.isoformat(),
        "securities_route": "/api/v2/market-data/instruments",
        "securities_raw_body_sha256": securities_hash, "request": request,
        "instruments": observations, "historical_input_audit": audit,
        "normalization": "UTC-midnight provider label's literal date; no timezone shift, price adjustment, filling, sorting or calendar repair",
        "qualification": {"provider_session_label_semantics": False,
            "exchange_calendar": False, "corporate_action_treatment": False,
            "dividend_treatment": False, "independently_authenticated_capture": False,
            "strategy_promotion_eligible": False, "live_demo_qualified": False},
        "broker_orders_submitted": 0, "return_target_pct": None,
    }
    return frame, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/backtesting.yaml"))
    parser.add_argument("--completed-before", help="Exclusive daily-label cutoff, ISO date")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    raw = args.capture.read_bytes()
    config = load_validation_config(args.config)
    frame, manifest = normalize_capture(json.loads(raw), config, completed_before=args.completed_before)
    manifest["capture_file_sha256"] = hashlib.sha256(raw).hexdigest()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    prices_path = args.output_dir / "historical_prices.csv"
    frame.to_csv(prices_path, index=False)
    manifest["normalized_csv_sha256"] = hashlib.sha256(prices_path.read_bytes()).hexdigest()
    (args.output_dir / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    (args.output_dir / "source_manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
