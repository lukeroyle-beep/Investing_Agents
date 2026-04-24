from __future__ import annotations

import pandas as pd
import pytest

from agents.universe_agent import universe_agent


def test_normalise_universe_source_deduplicates_and_preserves_multi_asset_tickers() -> None:
    raw = pd.DataFrame(
        [
            {
                "ticker": " aapl ",
                "name": " Apple Inc ",
                "asset_class": " Equity ",
                "region": "US",
                "exchange": "NASDAQ",
                "source": " Curated ",
                "index_membership": "S&P 500",
                "currency": "usd",
                "sector": "Information Technology",
            },
            {
                "ticker": "AAPL",
                "name": "Duplicate Apple",
                "asset_class": "equity",
                "region": "US",
                "exchange": "NASDAQ",
                "source": "curated",
            },
            {
                "ticker": "eurusd=x",
                "name": "EUR/USD Spot FX",
                "asset_class": "fx",
                "region": "Global",
                "exchange": "FX",
                "source": "curated",
                "currency": "usd",
            },
            {
                "ticker": "gc=f",
                "name": "Gold Futures",
                "asset_class": "futures",
                "region": "Global",
                "exchange": "COMEX",
                "source": "curated",
                "currency": "usd",
            },
            {
                "ticker": "btc-usd",
                "name": "Bitcoin USD",
                "asset_class": "crypto",
                "region": "Global",
                "exchange": "Crypto",
                "source": "curated",
                "currency": "usd",
            },
        ]
    )

    assets = universe_agent.normalise_universe_source(raw)

    assert assets["ticker"].tolist() == ["BTC-USD", "AAPL", "GC=F", "EURUSD=X"]
    apple = assets[assets["ticker"] == "AAPL"].iloc[0]
    assert apple["name"] == "Apple Inc"
    assert apple["asset_class"] == "equity"
    assert apple["source"] == "curated"
    assert apple["currency"] == "USD"


def test_normalise_universe_source_rejects_missing_required_metadata() -> None:
    raw = pd.DataFrame(
        [
            {
                "ticker": "SPY",
                "name": "SPDR S&P 500 ETF Trust",
                "asset_class": "etf",
                "region": "US",
                "exchange": "",
                "source": "curated",
            }
        ]
    )

    with pytest.raises(ValueError, match="blank required metadata"):
        universe_agent.normalise_universe_source(raw)


def test_load_assets_reads_multi_asset_csv_safely(tmp_path) -> None:
    source = tmp_path / "stock_universe.csv"
    source.write_text(
        "ticker,name,asset_class,region,exchange,source,index_membership,currency,sector,notes\n"
        "SPY,SPDR S&P 500 ETF Trust,etf,US,NYSE Arca,curated,S&P 500,USD,Broad Market,ETF\n"
        "CL=F,Crude Oil Futures WTI,futures,Global,NYMEX,curated,WTI Futures,USD,Energy,Futures\n"
        "GBPUSD=X,GBP/USD Spot FX,fx,Global,FX,curated,GBPUSD,USD,Foreign Exchange,FX\n"
        "ETH-USD,Ethereum USD,crypto,Global,Crypto,curated,Ethereum,USD,Crypto,Crypto\n"
    )

    assets = universe_agent.load_assets(source)

    assert [asset["ticker"] for asset in assets] == ["ETH-USD", "SPY", "CL=F", "GBPUSD=X"]
    for asset in assets:
        for column in universe_agent.REQUIRED_SOURCE_COLUMNS:
            assert asset[column]


def test_curated_stock_universe_csv_has_required_metadata_and_no_duplicates() -> None:
    assets = universe_agent.load_assets()
    tickers = [asset["ticker"] for asset in assets]

    assert len(assets) >= 50
    assert len(tickers) == len(set(tickers))
    assert {"equity", "etf", "commodity_proxy", "futures", "fx", "crypto"}.issubset(
        {asset["asset_class"] for asset in assets}
    )
    for asset in assets:
        for column in universe_agent.REQUIRED_SOURCE_COLUMNS:
            assert asset[column]
