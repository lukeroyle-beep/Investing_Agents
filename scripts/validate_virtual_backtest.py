"""Offline functionality evidence. Never imports a broker or submits orders.

Run with --synthetic for an explicitly artificial scenario, or --prices for a
saved historical_prices.csv containing role, ticker, Date, Open, High, Low,
Close. An explicit output directory keeps this separate from trading state.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.backtesting_agent import backtesting_agent as agent
from shared.research_universe import selected_research_universe
from strategy.domain import content_hash
from strategy.history_validation import audit_histories, session_dates


def synthetic_histories():
    """Deterministic test data with trends, reversals and competing signals."""
    frames = {}
    dates = pd.bdate_range("2020-01-02", periods=600)
    for ticker, slope, amplitude in (("SYNTH_A", .06, 12), ("SYNTH_B", .04, 9),
                                     ("SYNTH_BENCH", .03, 5)):
        rows = []
        previous = 100.0
        for index, date in enumerate(dates):
            close = 100 + slope * index + amplitude * math.sin(index / 17)
            open_ = previous * (1 + .001 * math.sin(index / 3))
            rows.append({"Date": date, "Open": open_, "High": max(open_, close) * 1.005,
                         "Low": min(open_, close) * .995, "Close": close, "Volume": 1000})
            previous = close
        frames[ticker] = pd.DataFrame(rows)
    return {key: value for key, value in frames.items() if key != "SYNTH_BENCH"}, {"SYNTH_BENCH": frames["SYNTH_BENCH"]}


def read_snapshot(path):
    # pandas renames duplicate headers before DataFrame-level validation. Reject
    # that ambiguity in the original file instead of choosing the first column.
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, strict=True)
        header = next(reader, [])
        if not header or any(not name for name in header) or len(header) != len(set(header)):
            raise ValueError("snapshot CSV headers must be nonempty and unique")
        if any(row and len(row) != len(header) for row in reader):
            raise ValueError("snapshot CSV rows must match the header width")
    frame = pd.read_csv(path, float_precision="round_trip")
    required = {"role", "ticker", "Date", "Open", "High", "Low", "Close"}
    if not required.issubset(frame.columns) or frame.empty:
        raise ValueError("snapshot must contain role, ticker and complete historical price columns")
    if frame[["role", "ticker"]].isna().any().any() or not set(frame["role"]).issubset({"strategy", "benchmark"}):
        raise ValueError("snapshot roles must be strategy or benchmark, with named instruments")
    groups = {"strategy": {}, "benchmark": {}}
    for (role, ticker), history in frame.groupby(["role", "ticker"], sort=True):
        history = history.drop(columns=["role", "ticker"]).reset_index(drop=True)
        history["Date"] = session_dates(history["Date"], f"{role} {ticker}")
        groups[role][ticker] = history
    return groups["strategy"], groups["benchmark"]


def load_validation_config(path, *, synthetic=False):
    """Apply an explicit pilot selection without relabelling saved evidence."""
    supplied = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if supplied is not None and not isinstance(supplied, dict):
        raise ValueError("validation configuration must be a mapping")
    config = {**agent.DEFAULT_CONFIG, **(supplied or {})}
    metadata_keys = ("research_universe_version", "research_universe_checksum")
    metadata_present = [key in config for key in metadata_keys]
    if any(metadata_present) and (not all(metadata_present) or any(
        not isinstance(config[key], str) or not config[key].strip() for key in metadata_keys
    )):
        raise ValueError("saved research universe metadata must contain both version and checksum")
    selection = selected_research_universe()
    if synthetic:
        if selection is not None or any(metadata_present):
            raise ValueError("synthetic validation cannot use a selected or saved research universe")
    elif selection is not None:
        if any(metadata_present) and (
            config[metadata_keys[0]] != selection.version or
            config[metadata_keys[1]] != selection.checksum or
            config.get("tickers") != list(selection.tickers)
        ):
            raise ValueError("selected research universe conflicts with saved configuration")
        config.update(tickers=list(selection.tickers), research_universe_version=selection.version,
                      research_universe_checksum=selection.checksum)
    return config


def validate(histories, benchmarks, config, output_dir, source_kind):
    input_audit = audit_histories(histories, benchmarks, config)
    output = Path(output_dir)
    # Do not overwrite prior evidence, even if a caller reuses the directory.
    output.mkdir(parents=True, exist_ok=False)
    sessions = agent.history_sessions(histories)
    start, end = str(sessions[0].date()), str(sessions[-1].date())
    replay = agent.evaluate_partition(histories, config, start, end)
    replay_again = agent.evaluate_partition(histories, config, start, end)
    splits, folds = agent.evaluate_walk_forward(histories, benchmarks, config)
    repeated_splits, repeated_folds = agent.evaluate_walk_forward(histories, benchmarks, config)
    checks = {
        "completed_trade_observed": len(replay.accepted_trades) > 0,
        "identical_replay": all(getattr(replay, name).equals(getattr(replay_again, name))
                                for name in ("accepted_trades", "rejected_trades", "equity_curve", "drawdown")),
        "identical_walk_forward": splits == repeated_splits and folds.equals(repeated_folds),
        "nonnegative_cash": bool((replay.equity_curve["cash"] >= 0).all()),
        "flat_final_portfolio": int(replay.equity_curve.iloc[-1]["open_positions"]) == 0,
        "final_cash_reconciles": math.isclose(
            float(replay.equity_curve.iloc[-1]["cash"]),
            float(config["initial_capital"]) + float(replay.accepted_trades["net_pnl"].sum()),
            rel_tol=1e-9, abs_tol=1e-7),
        "configured_fold_count": len(splits) >= int(config["validation"]["minimum_walk_forward_folds"]),
    }
    snapshot = pd.concat([history.assign(role=role, ticker=ticker)
                          for role, frames in (("strategy", histories), ("benchmark", benchmarks))
                          for ticker, history in sorted(frames.items())], ignore_index=True)
    outputs = {"historical_prices.csv": snapshot, "trades.csv": replay.accepted_trades,
               "rejected_trades.csv": replay.rejected_trades, "equity.csv": replay.equity_curve,
               "drawdown.csv": replay.drawdown, "walk_forward.csv": folds}
    for name, frame in outputs.items():
        frame.to_csv(output / name, index=False)
    (output / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    report = {
        "objective": "virtual_functionality", "return_target_pct": None,
        "source_kind": source_kind, "functional_checks": checks,
        "historical_input_audit": input_audit,
        "functional_result": "pass" if all(checks.values()) else "fail",
        "broker_orders_submitted": 0, "live_demo_qualified": False,
        "strategy_promotion_eligible": False,
        "unverified": ["point_in_time_universe", "survivorship", "exchange_calendar",
                       "corporate_action_treatment", "broker_cost_calibration", "live_demo_execution"],
        "sessions": len(sessions), "walk_forward_folds": len(splits),
        "accepted_trades": len(replay.accepted_trades), "rejected_trades": len(replay.rejected_trades),
        "observed_return_pct": float(replay.equity_curve.iloc[-1]["cumulative_return_pct"]),
        "observed_max_drawdown_pct": float(replay.drawdown["drawdown_pct"].min()),
        "input_sha256": content_hash(snapshot.to_dict("records")),
        "result_sha256": content_hash({key: value.to_dict("records") for key, value in outputs.items()}),
    }
    (output / "validation.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true")
    source.add_argument("--prices", type=Path)
    parser.add_argument("--config", type=Path, default=agent.BACKTEST_CONFIG_FILE)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = load_validation_config(args.config, synthetic=args.synthetic)
    histories, benchmarks = synthetic_histories() if args.synthetic else read_snapshot(args.prices)
    if args.synthetic:
        config.update(tickers=list(histories), benchmark_tickers=list(benchmarks), position_size_pct=60)
    report = validate(histories, benchmarks, config, args.output_dir,
                      "synthetic_functionality_fixture" if args.synthetic else "supplied_historical_snapshot_unqualified")
    print(json.dumps(report, indent=2))
    return 0 if report["functional_result"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
