from __future__ import annotations

import pandas as pd

from agents.signal_agent import signal_agent
from shared.market_data import MarketDataMetadata, MarketDataResult


ELEVATED_VOLUME = 5_000.0
QUIET_VOLUME = 1_000.0


def _frame(closes: list[float], last_volume: float) -> pd.DataFrame:
    volumes = [QUIET_VOLUME] * (len(closes) - 1) + [last_volume]
    index = pd.date_range("2026-01-01", periods=len(closes), freq="D", name="Date")
    close = pd.Series(closes, index=index, dtype="float64")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": volumes,
        },
        index=index,
    )


class _HistoryProvider:
    def __init__(self, frame: pd.DataFrame) -> None:
        self._frame = frame

    def fetch_history(self, ticker: str, **_kwargs: object) -> MarketDataResult:
        return MarketDataResult(
            ticker=ticker,
            data=self._frame,
            metadata=MarketDataMetadata(
                source="fake",
                fetched_at="2026-05-01T12:00:00+00:00",
                as_of="2026-05-01T00:00:00+00:00",
            ),
        )


def _setup(closes: list[float], *, last_volume: float) -> dict[str, object]:
    result = signal_agent.fetch_signal_data(
        "TEST",
        "Test Asset",
        market_data_provider=_HistoryProvider(_frame(closes, last_volume)),
    )
    assert result is not None
    return result


def _quiet_closes() -> list[float]:
    return [80.0 + (index % 2) * 0.4 for index in range(60)]


def test_confirmed_breakout_requires_close_above_prior_20_session_high() -> None:
    # Today is the max of the inclusive 20 sessions, but the close 20 sessions
    # back is higher. Including the signal bar makes `close >= high_20` true.
    inclusive_max = _quiet_closes()
    inclusive_max[39] = 110.0
    inclusive_max[40:59] = [100.0] * 19
    inclusive_max[59] = 105.0
    not_a_break = _setup(inclusive_max, last_volume=ELEVATED_VOLUME)
    assert not_a_break["confirmed_breakout"] is False
    assert not_a_break["breakout_type"] != "confirmed_breakout"

    # Matching the preceding 20-session close high does not exceed it.
    equal_high = _quiet_closes()
    equal_high[39:59] = [99.0] * 20
    equal_high[50] = 100.0
    equal_high[59] = 100.0
    matched = _setup(equal_high, last_volume=ELEVATED_VOLUME)
    assert matched["confirmed_breakout"] is False

    # A close above those preceding 20 sessions breaks out even when an older
    # session, outside that window, printed a higher close.
    cleared = _quiet_closes()
    cleared[38] = 150.0
    cleared[39:59] = [100.0] * 20
    cleared[59] = 101.0
    broken = _setup(cleared, last_volume=ELEVATED_VOLUME)
    assert broken["confirmed_breakout"] is True
    assert broken["breakout_type"] == "confirmed_breakout"

    quiet = _setup(cleared, last_volume=QUIET_VOLUME)
    assert quiet["confirmed_breakout"] is False
