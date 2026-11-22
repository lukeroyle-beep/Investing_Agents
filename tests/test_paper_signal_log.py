"""Paper signal log and horizon maths.

Price rows under tests/fixtures/paper_signal_log are EXAMPLE DATA, not market
prices. They are labelled EXAMPLE_DATA_NOT_A_MARKET_PRICE in the fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from agents.advisory_agent import advisory_agent
from agents.exit_agent import exit_agent
from shared.market_data import MarketDataMetadata, MarketDataResult
from shared.paper_signal_log import (
    PaperSignalConfig,
    PaperSignalLogConflict,
    PaperSignalLogError,
    add_months,
    advisory_signal,
    append_signal,
    evaluate_signals,
    exit_signal,
    format_report,
    load_config,
    log_advisory_view,
    log_exit_view,
    read_signals,
)
from shared.run_context import RUN_ID_ENV_VAR
from tests.helpers import (
    closed_position_row,
    open_position_row,
    portfolio_monitor_row,
    write_portfolio_monitor_csv,
    write_portfolio_state_csv,
)
from tests.test_portfolio_advisory_contract import (
    _patch_advisory_paths,
    _write_governance,
    _write_portfolio_inputs,
)

import scripts.paper_signal_log as paper_signal_cli


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "paper_signal_log" / "example_prices.csv"
EXAMPLE_LABEL = "EXAMPLE_DATA_NOT_A_MARKET_PRICE"
SIGNALLED_AT = "2024-01-16T21:00:00+00:00"


def _config(path: Path, benchmark: str = "BENCH", horizons: tuple[int, ...] = (1, 3)) -> PaperSignalConfig:
    return PaperSignalConfig(
        benchmark_ticker=benchmark,
        benchmark_name="example benchmark, not a market listing",
        cost_bps=Decimal("10"),
        horizons_months=horizons,
        log_path=path,
        pending_session_lag_days=7,
    )


def _frames() -> dict[str, pd.DataFrame]:
    table = pd.read_csv(FIXTURE)
    assert set(table["fixture_label"]) == {EXAMPLE_LABEL}
    frames: dict[str, pd.DataFrame] = {}
    for ticker, rows in table.groupby("ticker"):
        frame = pd.DataFrame(
            {
                "Close": rows["close"].astype(float).tolist(),
                "Stock Splits": rows["stock_split"].astype(float).tolist(),
            },
            index=pd.to_datetime(rows["session_date"]),
        )
        frames[str(ticker)] = frame
    return frames


class ExampleProvider:
    """Serves the labelled example fixture and records the fetch arguments."""

    def __init__(self, frames: dict[str, pd.DataFrame] | None = None, error: str | None = None) -> None:
        self.frames = frames if frames is not None else _frames()
        self.error = error
        self.calls: list[dict[str, object]] = []

    def fetch_history(self, ticker: str, **kwargs: object) -> MarketDataResult:
        self.calls.append({"ticker": ticker, **kwargs})
        if self.error:
            metadata = _metadata(error=self.error)
            return MarketDataResult(ticker=ticker, data=pd.DataFrame(), metadata=metadata)
        frame = self.frames.get(ticker, pd.DataFrame())
        return MarketDataResult(ticker=ticker, data=frame, metadata=_metadata())


def _metadata(error: str | None = None) -> MarketDataMetadata:
    return MarketDataMetadata(
        source="example-fixture",
        fetched_at="2024-08-01T00:00:00+00:00",
        as_of="2024-07-16T00:00:00+00:00",
        error=error,
    )


def _currencies(mapping: dict[str, str]):
    def lookup(ticker: str) -> tuple[str, str | None]:
        return mapping.get(ticker, ""), None

    return lookup


def _record(**overrides: object) -> dict[str, object]:
    body = {
        "schema_version": "1",
        "signal_id": "example-signal",
        "signalled_at": SIGNALLED_AT,
        "signal_date_utc": "2024-01-16",
        "ticker": "EXMPL",
        "signal": "buy",
        "score_rule": "buy_versus_benchmark",
        "price": "100",
        "price_source": "example-fixture",
        "price_field": "close",
        "price_via": "example row, not an agent view",
        "price_currency": "USD",
        "rationale": "example rationale already on the row",
        "agent": "Example",
        "source_view": "ready_for_manual_review",
        "direction": "long",
        "position_side": "long",
        "position_id": "",
        "run_id": "RUN_EXAMPLE",
        "logged_at": SIGNALLED_AT,
        "read_only": True,
    }
    body.update(overrides)
    return body


def _evaluate(records, config, as_of: str, provider: ExampleProvider, currencies: dict[str, str]):
    return evaluate_signals(
        records,
        config,
        as_of=datetime.fromisoformat(as_of),
        provider=provider,
        currency_lookup=_currencies(currencies),
    )


def test_example_fixture_is_labelled() -> None:
    table = pd.read_csv(FIXTURE)
    assert table["fixture_label"].eq(EXAMPLE_LABEL).all()


def test_log_appends_once_and_rejects_a_changed_view(tmp_path: Path) -> None:
    path = tmp_path / "signal_log.jsonl"
    first = log_advisory_view(
        ticker="xom",
        advice_status="ready_for_manual_review",
        direction="long",
        price="100.00",
        currency="USD",
        rationale="clear setup",
        signalled_at="2024-01-16T15:00:00-05:00",
        run_id="RUN_LOG",
        path=path,
    )
    second = log_advisory_view(
        ticker="XOM",
        advice_status="ready_for_manual_review",
        direction="long",
        price=Decimal("100"),
        currency="USD",
        rationale="clear setup",
        signalled_at="2024-01-16T21:30:00+00:00",
        run_id="RUN_LOG",
        path=path,
    )

    assert first.recorded
    assert second.reason == "already_recorded"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    stored = read_signals(path)[0]
    assert stored["signalled_at"] == "2024-01-16T20:00:00+00:00"
    assert stored["signal_date_utc"] == "2024-01-16"
    assert stored["signal"] == "buy"
    assert stored["price"] == "100"
    assert stored["price_currency"] == "USD"
    assert stored["price_source"] == "yfinance"
    assert stored["rationale"] == "clear setup"
    assert stored["read_only"] is True
    before = path.read_bytes()

    with pytest.raises(PaperSignalLogConflict):
        log_advisory_view(
            ticker="XOM",
            advice_status="ready_for_manual_review",
            direction="long",
            price="101",
            currency="USD",
            rationale="clear setup",
            signalled_at=SIGNALLED_AT,
            run_id="RUN_LOG",
            path=path,
        )
    assert path.read_bytes() == before


def test_views_that_are_not_buy_sell_or_hold_are_not_logged(tmp_path: Path) -> None:
    path = tmp_path / "signal_log.jsonl"
    blocked = log_advisory_view(
        ticker="XOM",
        advice_status="blocked",
        direction="long",
        price="100",
        currency="USD",
        rationale="Ticker is blocked by governance",
        signalled_at=SIGNALLED_AT,
        run_id="RUN_SKIP",
        path=path,
    )
    closed = log_exit_view(
        ticker="AAPL",
        exit_action="hold",
        status="no_action",
        exit_reason="already_closed",
        position_side="long",
        price="110",
        rationale="Position already closed.",
        signalled_at=SIGNALLED_AT,
        run_id="RUN_SKIP",
        position_id="POS_CLOSED",
        path=path,
    )
    review = log_exit_view(
        ticker="AAPL",
        exit_action="review",
        status="review_required",
        exit_reason="missing_current_price",
        position_side="long",
        price="",
        rationale="Current price missing.",
        signalled_at=SIGNALLED_AT,
        run_id="RUN_SKIP",
        position_id="POS_REVIEW",
        path=path,
    )

    assert blocked.reason == review.reason == closed.reason == "not_a_buy_sell_hold_view"
    assert not path.exists()
    assert advisory_signal("hold_for_review", "long") == "hold"
    assert exit_signal("take_profit", "exit_required", "take_profit_triggered") == "sell"
    assert exit_signal("close", "exit_required", "stop_loss_triggered") == "sell"
    assert exit_signal("raise_stop", "open", "") is None


def test_missing_price_is_reported_and_not_invented(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "signal_log.jsonl"
    result = log_exit_view(
        ticker="AAPL",
        exit_action="hold",
        status="hold",
        exit_reason="",
        position_side="long",
        price=float("nan"),
        rationale="No exit condition met.",
        signalled_at=SIGNALLED_AT,
        run_id="RUN_NO_PRICE",
        position_id="POS001",
        path=path,
    )
    assert result.reason == "missing_price"
    assert not path.exists()
    message = capsys.readouterr().out
    assert "not recorded" in message
    assert "AAPL" in message


def test_timestamp_without_an_offset_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PaperSignalLogError, match="explicit UTC offset"):
        log_advisory_view(
            ticker="XOM",
            advice_status="ready_for_manual_review",
            direction="long",
            price="100",
            currency="USD",
            rationale="clear",
            signalled_at="2024-01-16T21:00:00",
            run_id="RUN_NAIVE",
            path=tmp_path / "signal_log.jsonl",
        )


def test_month_end_horizon_clamps_to_a_real_day() -> None:
    assert add_months(datetime(2024, 1, 31).date(), 1).isoformat() == "2024-02-29"
    assert add_months(datetime(2023, 1, 31).date(), 1).isoformat() == "2023-02-28"


def test_buy_hit_uses_the_logged_price_and_the_first_later_close(tmp_path: Path) -> None:
    provider = ExampleProvider()
    evaluation = _evaluate(
        [_record()],
        _config(tmp_path / "log.jsonl", benchmark="BENCHUSD"),
        "2024-08-01T00:00:00+00:00",
        provider,
        {"EXMPL": "USD", "BENCHUSD": "USD"},
    )
    one_month = next(row for row in evaluation.outcomes if row.horizon_months == 1)
    three_month = next(row for row in evaluation.outcomes if row.horizon_months == 3)

    assert one_month.status == "scored"
    assert one_month.signal_price == "100"
    assert one_month.outcome_price == "110"
    assert one_month.outcome_session == "2024-02-16"
    assert one_month.benchmark_entry_price == "200"
    assert one_month.benchmark_outcome_price == "220"
    assert one_month.ticker_return == "-0.45"
    assert one_month.benchmark_return == "-0.45"
    assert one_month.ticker_return_local == "0.1"
    assert one_month.benchmark_return_local == "0.1"
    assert one_month.cost_applied_bps == "10"
    assert one_month.excess_return == "-0.001"
    assert one_month.hit == "miss"
    assert one_month.currency_basis == "gbp"
    assert "GBPUSD=X 1.25@2024-01-15 -> 2.5@2024-02-16" in one_month.detail
    assert "8@" not in one_month.detail
    assert three_month.outcome_price == "500"
    assert three_month.outcome_session == "2024-07-16"
    assert three_month.benchmark_outcome_price == "250"
    assert one_month.benchmark_entry_price != "1"
    assert all(call["auto_adjust"] is False and call["actions"] is True for call in provider.calls)
    assert "999" not in format_report([_record()], evaluation)


def test_pending_horizon_is_not_estimated_from_a_later_bar(tmp_path: Path) -> None:
    provider = ExampleProvider()
    evaluation = _evaluate(
        [_record()],
        _config(tmp_path / "log.jsonl"),
        "2024-03-01T00:00:00+00:00",
        provider,
        {"EXMPL": "USD", "BENCH": "GBP"},
    )
    one_month = next(row for row in evaluation.outcomes if row.horizon_months == 1)
    three_month = next(row for row in evaluation.outcomes if row.horizon_months == 3)
    summary = next(row for row in evaluation.summaries if row.horizon_months == 3 and row.signal_group == "all")

    assert one_month.status == "scored"
    assert one_month.outcome_price == "110"
    assert one_month.benchmark_entry_price == "200"
    assert one_month.benchmark_outcome_price == "210"
    assert one_month.currency_basis == "gbp"
    assert one_month.ticker_return == "-0.45"
    assert one_month.benchmark_return == "0.05"
    assert one_month.ticker_return_local == "0.1"
    assert one_month.benchmark_return_local == "0.05"
    assert one_month.excess_return == "-0.501"
    assert one_month.hit == "miss"
    assert "benchmark GBP 200 -> 210" in one_month.detail
    assert three_month.status == "pending"
    assert three_month.detail == "horizon_not_elapsed"
    assert three_month.outcome_price == ""
    assert three_month.excess_return == ""
    assert three_month.hit == ""
    assert summary.pending == 1
    assert summary.scored == 0
    assert summary.hit_rate == "n/a"
    assert "500" not in three_month.detail


def test_sell_hold_and_do_not_enter_rules(tmp_path: Path) -> None:
    provider = ExampleProvider()
    records = [
        _record(signal_id="sell", ticker="WEAK", signal="sell", score_rule="sell_long_versus_benchmark", price="100"),
        _record(
            signal_id="keep",
            signal="hold",
            score_rule="keep_long_versus_benchmark",
            source_view="hold",
            agent="Exit Agent",
        ),
        _record(
            signal_id="wait",
            signal="hold",
            score_rule="do_not_enter_versus_benchmark",
            source_view="hold_for_review",
        ),
    ]
    evaluation = _evaluate(
        records,
        _config(tmp_path / "log.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        provider,
        {"EXMPL": "USD", "WEAK": "USD", "BENCH": "GBP"},
    )
    by_id = {row.signal_id: row for row in evaluation.outcomes}

    assert by_id["sell"].hit == "hit"
    assert by_id["sell"].excess_return == "0.599"
    assert by_id["sell"].ticker_return == "-0.55"
    assert by_id["sell"].ticker_return_local == "-0.1"
    assert by_id["sell"].cost_applied_bps == "10"
    assert by_id["keep"].hit == "miss"
    assert by_id["keep"].excess_return == "-0.5"
    assert by_id["keep"].cost_applied_bps == "0"
    assert by_id["wait"].hit == "hit"
    assert by_id["wait"].excess_return == "0.5"
    assert by_id["wait"].cost_applied_bps == "0"
    sell_summary = next(row for row in evaluation.summaries if row.signal_group == "sell")
    assert sell_summary.hits == 1
    assert sell_summary.hit_rate == "100.0000%"
    assert evaluation.cost_bps == "10"


def test_same_day_close_is_not_the_benchmark_entry(tmp_path: Path) -> None:
    provider = ExampleProvider()
    evaluation = _evaluate(
        [_record()],
        _config(tmp_path / "log.jsonl", benchmark="BENCHUSD", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        provider,
        {"EXMPL": "USD", "BENCHUSD": "USD"},
    )
    assert evaluation.outcomes[0].benchmark_entry_price == "200"

    later = _evaluate(
        [_record(signal_id="next-utc-day", signalled_at="2024-01-17T01:00:00+00:00", signal_date_utc="2024-01-17")],
        _config(tmp_path / "log.jsonl", benchmark="BENCHUSD", horizons=(1,)),
        "2024-08-01T00:00:00+00:00",
        provider,
        {"EXMPL": "USD", "BENCHUSD": "USD"},
    )
    assert later.outcomes[0].status == "scored"
    assert later.outcomes[0].benchmark_entry_price == "1"


def test_split_and_missing_feed_are_explicit(tmp_path: Path) -> None:
    provider = ExampleProvider()
    records = [
        _record(signal_id="split", ticker="SPLIT"),
        _record(signal_id="gap", ticker="GAP", price="100"),
    ]
    evaluation = _evaluate(
        records,
        _config(tmp_path / "log.jsonl", horizons=(1,)),
        "2024-04-01T00:00:00+00:00",
        provider,
        {"SPLIT": "USD", "GAP": "USD", "BENCH": "GBP"},
    )
    by_id = {row.signal_id: row for row in evaluation.outcomes}
    assert by_id["split"].status == "split_basis_mismatch"
    assert by_id["split"].hit == ""
    assert by_id["gap"].status == "outcome_missing"
    assert "GAP" in by_id["gap"].detail

    failed = ExampleProvider(error="example feed down")
    failed_evaluation = _evaluate(
        [_record(signal_id="down")],
        _config(tmp_path / "other.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        failed,
        {"EXMPL": "USD", "BENCH": "GBP"},
    )
    assert failed_evaluation.outcomes[0].status == "data_feed_error"
    assert "example feed down" in failed_evaluation.outcomes[0].detail
    assert failed_evaluation.summaries[0].flagged == 1
    assert failed_evaluation.summaries[0].scored == 0


def test_recent_missing_close_stays_pending(tmp_path: Path) -> None:
    provider = ExampleProvider()
    evaluation = _evaluate(
        [_record(signal_id="gap", ticker="GAP")],
        _config(tmp_path / "log.jsonl", horizons=(1,)),
        "2024-02-18T00:00:00+00:00",
        provider,
        {"GAP": "USD", "BENCH": "GBP"},
    )
    assert evaluation.outcomes[0].status == "pending"
    assert evaluation.outcomes[0].detail.startswith("awaiting_session_close")
    assert evaluation.outcomes[0].outcome_price == ""


def test_pending_signal_does_not_fetch_prices(tmp_path: Path) -> None:
    provider = ExampleProvider()

    def explode(ticker: str) -> tuple[str, str | None]:
        raise AssertionError(ticker)

    evaluation = evaluate_signals(
        [_record()],
        _config(tmp_path / "log.jsonl"),
        as_of=datetime(2024, 1, 20, tzinfo=UTC),
        provider=provider,
        currency_lookup=explode,
    )
    assert provider.calls == []
    assert {row.status for row in evaluation.outcomes} == {"pending"}


def test_short_view_is_logged_and_not_scored(tmp_path: Path) -> None:
    path = tmp_path / "signal_log.jsonl"
    result = log_exit_view(
        ticker="AAPL",
        exit_action="take_profit",
        status="exit_required",
        exit_reason="take_profit_triggered",
        position_side="short",
        price="90",
        rationale="Take profit level has been reached.",
        signalled_at=SIGNALLED_AT,
        run_id="RUN_SHORT",
        position_id="POS_SHORT",
        path=path,
    )
    stored = read_signals(path)[0]
    provider = ExampleProvider()
    evaluation = _evaluate(
        [stored],
        _config(path, horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        provider,
        {},
    )
    assert result.recorded
    assert stored["signal"] == "sell"
    assert stored["rationale"] == "Take profit level has been reached."
    assert evaluation.outcomes[0].status == "short_not_scored"
    assert provider.calls == []


def test_gbp_conversion_covers_eur_jpy_pence_and_missing_fx(tmp_path: Path) -> None:
    provider = ExampleProvider()
    records = [
        _record(signal_id="eur", ticker="EURX", price="200", price_currency="EUR"),
        _record(signal_id="jpy", ticker="JPYX", price="1000", price_currency="JPY"),
        _record(signal_id="pence", ticker="PENCE.L", price="10000", price_currency="GBX"),
    ]
    evaluation = _evaluate(
        records,
        _config(tmp_path / "log.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        provider,
        {"EURX": "EUR", "JPYX": "JPY", "PENCE.L": "GBp", "BENCH": "GBP"},
    )
    by_id = {row.signal_id: row for row in evaluation.outcomes}

    assert by_id["eur"].status == "scored"
    assert by_id["eur"].ticker_return == "0.5"
    assert by_id["eur"].ticker_return_local == "0"
    assert by_id["eur"].excess_return == "0.449"
    assert "EURGBP=X 0.5@2024-01-15 -> 0.75@2024-02-16" in by_id["eur"].detail
    assert "GBP 100 -> 150" in by_id["eur"].detail
    assert "8@" not in by_id["eur"].detail

    assert by_id["jpy"].status == "scored"
    assert by_id["jpy"].ticker_return == "1"
    assert by_id["jpy"].ticker_return_local == "0"
    assert by_id["jpy"].excess_return == "0.949"
    assert "GBPJPY=X 200@2024-01-15 -> 100@2024-02-16" in by_id["jpy"].detail
    assert "8@" not in by_id["jpy"].detail

    assert by_id["pence"].status == "scored"
    assert by_id["pence"].currency_basis == "gbp"
    assert by_id["pence"].ticker_return == "0.1"
    assert by_id["pence"].ticker_return_local == "0.1"
    assert by_id["pence"].excess_return == "0.049"
    assert "ticker GBp/100 GBP 100 -> 110" in by_id["pence"].detail
    assert "GBPUSD=X" not in by_id["pence"].detail

    gbp_provider = ExampleProvider()
    pounds = _evaluate(
        [_record(signal_id="pounds", ticker="GBPX", price="100", price_currency="GBP")],
        _config(tmp_path / "gbp.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        gbp_provider,
        {"GBPX": "GBP", "BENCH": "GBP"},
    ).outcomes[0]
    assert pounds.status == "scored"
    assert pounds.ticker_return == "0.1"
    assert pounds.excess_return == "0.049"
    assert "ticker GBP 100 -> 110" in pounds.detail
    assert not any(str(call["ticker"]).endswith("=X") for call in gbp_provider.calls)

    pence_benchmark = _evaluate(
        [_record(signal_id="usd-pence-bench")],
        _config(tmp_path / "pence.jsonl", benchmark="PENCEBENCH", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        ExampleProvider(),
        {"EXMPL": "USD", "PENCEBENCH": "GBp"},
    ).outcomes[0]
    assert pence_benchmark.status == "scored"
    assert pence_benchmark.benchmark_return == "0.05"
    assert pence_benchmark.benchmark_return_local == "0.05"
    assert pence_benchmark.ticker_return == "-0.45"
    assert "benchmark GBp/100 GBP 200 -> 210" in pence_benchmark.detail
    assert "GBP 20000" not in pence_benchmark.detail

    missing = _evaluate(
        [
            _record(
                signal_id="missing-fx",
                ticker="NOFX",
                price="100",
                price_currency="USD",
                signalled_at="2023-05-02T21:00:00+00:00",
                signal_date_utc="2023-05-02",
            )
        ],
        _config(tmp_path / "missing.jsonl", horizons=(1,)),
        "2023-07-01T00:00:00+00:00",
        ExampleProvider(),
        {"NOFX": "USD", "BENCH": "GBP"},
    )
    missing_row = missing.outcomes[0]
    assert missing_row.status == "fx_missing"
    assert missing_row.currency_basis == "fx_missing"
    assert missing_row.excess_return == ""
    assert missing_row.hit == ""
    assert missing_row.ticker_return == ""
    assert missing_row.ticker_return_local == "0.2"
    assert missing_row.benchmark_return_local == "0.05"
    assert "before the signal UTC date" in missing_row.detail
    assert "8@" not in missing_row.detail
    assert missing.summaries[0].fx_missing == 1
    assert missing.summaries[0].scored == 0
    report = format_report([], missing)
    assert "ticker_return_local" in report
    assert "fx_missing" in report

    held_back = _frames()
    held_back["GBPUSD=X"] = held_back["GBPUSD=X"][held_back["GBPUSD=X"].index < "2024-02-16"]
    waiting = _evaluate(
        [_record(signal_id="fx-wait")],
        _config(tmp_path / "wait.jsonl", horizons=(1,)),
        "2024-02-18T00:00:00+00:00",
        ExampleProvider(held_back),
        {"EXMPL": "USD", "BENCH": "GBP"},
    ).outcomes[0]
    assert waiting.status == "pending"
    assert waiting.detail == "awaiting_fx_close: GBPUSD=X"
    assert waiting.excess_return == ""
    assert waiting.ticker_return_local == "0.1"


def test_gbp_and_pence_are_not_treated_as_the_same_quote(tmp_path: Path) -> None:
    conflict = _evaluate(
        [_record(signal_id="pounds-vs-pence", ticker="PENCE.L", price="100", price_currency="GBP")],
        _config(tmp_path / "conflict.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        ExampleProvider(),
        {"PENCE.L": "GBp", "BENCH": "GBP"},
    ).outcomes[0]
    assert conflict.status == "quote_currency_conflict"
    assert conflict.excess_return == ""
    assert "no rescaling" in conflict.detail

    same_pence = _evaluate(
        [_record(signal_id="gbx", ticker="PENCE.L", price="10000", price_currency="GBX")],
        _config(tmp_path / "same.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        ExampleProvider(),
        {"PENCE.L": "GBp", "BENCH": "GBP"},
    ).outcomes[0]
    assert same_pence.status == "scored"
    assert "GBp/100" in same_pence.detail


def test_logged_currency_must_match_the_outcome_feed(tmp_path: Path) -> None:
    evaluation = _evaluate(
        [_record(price_currency="USD")],
        _config(tmp_path / "log.jsonl", horizons=(1,)),
        "2024-03-01T00:00:00+00:00",
        ExampleProvider(),
        {"EXMPL": "GBP", "BENCH": "GBP"},
    )
    row = evaluation.outcomes[0]
    assert row.status == "quote_currency_conflict"
    assert row.hit == ""
    assert row.excess_return == ""
    assert "no rescaling" in row.detail


def test_corrupt_log_line_is_not_skipped(tmp_path: Path) -> None:
    path = tmp_path / "signal_log.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(PaperSignalLogError, match="not JSON"):
        read_signals(path)


def test_config_cost_is_the_printed_assumption() -> None:
    config = load_config()
    assert config.benchmark_ticker == "VWRL.L"
    assert config.cost_bps == Decimal("10")
    assert config.horizons_months == (1, 3, 6, 12)


def test_cli_prints_pending_log_without_a_price_fetch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "signal_log.jsonl"
    append_signal(_record(), path=path)
    markdown = tmp_path / "report.md"
    exit_code = paper_signal_cli.main(
        ["--log", str(path), "--as-of", "2024-01-20T00:00:00+00:00", "--markdown", str(markdown)]
    )
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "EXMPL" in output
    assert "example rationale already on the row" in output
    assert "pending" in output
    assert "hit_rate" in output
    assert "10 bps" in output
    assert "benchmark beats the ticker" in output
    assert markdown.read_text(encoding="utf-8").startswith("# Paper signal log")


def test_advisory_agent_records_the_buy_it_already_produced(
    isolated_workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "signal_log.jsonl"
    monkeypatch.setenv("PAPER_SIGNAL_LOG_PATH", str(log_path))
    monkeypatch.setenv(RUN_ID_ENV_VAR, "RUN_PAPER_ADVISORY")
    _patch_advisory_paths(isolated_workspace, monkeypatch)
    _write_governance(isolated_workspace)
    _write_portfolio_inputs(isolated_workspace)

    from agents.portfolio_agent import portfolio_agent

    portfolio_agent.main()
    advisory_agent.run()

    stored = read_signals(log_path)
    by_ticker = {row["ticker"]: row for row in stored}
    assert set(by_ticker) == {"XOM", "T"}
    assert by_ticker["XOM"]["signal"] == "buy"
    assert by_ticker["XOM"]["price"] == "100"
    assert by_ticker["XOM"]["price_currency"] == "USD"
    assert by_ticker["XOM"]["price_source"] == "yfinance"
    assert "clear" in by_ticker["XOM"]["rationale"]
    assert by_ticker["XOM"]["signalled_at"].endswith("+00:00")


def test_exit_agent_records_a_hold_and_ignores_a_closed_position(
    isolated_workspace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    log_path = tmp_path / "signal_log.jsonl"
    monkeypatch.setenv("PAPER_SIGNAL_LOG_PATH", str(log_path))
    monkeypatch.setattr(exit_agent, "get_or_create_run_id", lambda: "RUN_PAPER_EXIT")
    data_dir = isolated_workspace / "data"
    open_position = open_position_row()
    write_portfolio_state_csv(
        data_dir / "portfolio_state.csv",
        [open_position, closed_position_row(position_id="POS_CLOSED")],
    )
    write_portfolio_monitor_csv(
        data_dir / "portfolio_monitor.csv",
        [portfolio_monitor_row(open_position)],
    )

    exit_agent.run_exit_agent()

    stored = read_signals(log_path)
    assert [row["position_id"] for row in stored] == ["POS001"]
    assert stored[0]["signal"] == "hold"
    assert stored[0]["score_rule"] == "keep_long_versus_benchmark"
    assert stored[0]["price"] == "105"
    assert stored[0]["price_source"] == "portfolio_monitor"
    assert stored[0]["price_currency"] == "USD"
    assert stored[0]["rationale"] == "No exit condition met."


def test_module_does_not_reference_a_broker_write() -> None:
    source = Path("shared/paper_signal_log.py").read_text(encoding="utf-8").lower()
    assert "etoro" not in source
    assert "place_order" not in source
    assert "submit_order" not in source
