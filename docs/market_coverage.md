# Configured market coverage

Verified against the merged pull request 2 baseline on 21 October 2026.
This describes checked-in configuration, not proof that a live scan is running.

## Opt-in aligned pilot

The next account/risk milestone adds `config/demo_research_universe.yaml`.
Setting `INVESTING_RESEARCH_UNIVERSE=config/demo_research_universe.yaml` selects
the same six equities in both scanner and backtester: AAPL, MSFT, NVDA, AMZN,
GOOGL and META. SPY and QQQ remain separate backtest benchmarks. Run
`python tools/research_coverage.py` under that selection to inspect the exact
membership and checksum without fetching data. This is configuration alignment,
not a completed historical study or broker permission. Without the opt-in,
the legacy coverage below remains in effect. See the
[checkpoint and remaining work](demo_account_risk_milestone.md).

## Scanner universe

`data_sources/stock_universe.csv` is a **curated list of 61 instruments**, not
the whole market and not the eToro instrument catalogue.

| Classification in the file | Count | Examples |
| --- | ---: | --- |
| Equities | 26 | Apple, Microsoft, Shell, ASML, Toyota |
| Exchange-traded funds (ETFs) | 15 | SPY, QQQ, IWM, TLT, SH |
| Commodity proxies | 5 | GLD, SLV, USO, UNG, DBC |
| Futures reference series | 6 | Gold, crude oil, natural gas, two equity indices, US Treasury bonds |
| Foreign exchange (FX) pairs | 6 | EUR/USD, GBP/USD, USD/JPY, GBP/JPY, EUR/GBP, AUD/USD |
| Cryptocurrencies | 3 | Bitcoin, Ethereum, Solana |

The five commodity proxies are fund-based products grouped separately by the
CSV classification. The 15 ETF entries include broad-market/index funds, sector
funds, inverse funds and the TLT Treasury bond fund. There are **no individual
bonds** in this list. Bond exposure appears only through TLT and a Treasury
bond futures reference series. A Yahoo continuous futures reference is not an
identified, expiring broker futures contract.

The universe scanner attempts the list using Yahoo Finance through `yfinance`,
with six months of adjusted daily open, high, low, close and volume data.
Missing, stale or unusable histories and histories with fewer than 61 closes
are excluded. Six conditions yield pass at five or six, watch at two through
four, and reject at zero or one. Signal Agent only considers the resulting
pass/watch subset and applies additional signal checks.

Macro context is a separate three-symbol read: SPY, QQQ and the VIX volatility
index (`^VIX`). These are market proxies, not a feed of economic releases.

## Backtest coverage is different

`config/backtesting.yaml` independently selects **12 US stocks**:

`AAPL, MSFT, NVDA, AMZN, GOOGL, META, NFLX, COST, CVX, RTX, PANW, DDOG`.

It requests adjusted daily history from 1 January 2022, with no fixed end date,
and compares against SPY and QQQ. SPY is the primary walk-forward benchmark.
Only six names overlap with the scanner list: AAPL, MSFT, NVDA, AMZN, GOOGL and
META. Backtest success must therefore not be presented as validation of all 61
scanner entries. Standard `run_pipeline.py` does not run Backtesting Agent.

The completed offline functionality validation used only two artificial
strategy series and one artificial benchmark, over 600 weekday observations.
Those results are not a completed historical study of the configured stocks.

## Research coverage is not execution permission

The current execution scope is Demo only, long purchases of unleveraged
equities/ETFs, with exact internal and broker identities. The operator fixture
uses fixed-notional US-dollar sizing. Broker writes remain disabled.
Futures, currencies and crypto in the research universe are not thereby
eligible for broker execution. Availability of any fund as an unleveraged
underlying asset must be checked for the particular account and jurisdiction;
a matching name does not establish settlement eligibility.

The current freshness configuration also assumes the New York Stock Exchange
calendar globally. International equities, futures, currency pairs and crypto
do not yet have qualified asset-specific session, volume, currency and
corporate-action handling. No claim of reliable whole-market scanning is made.

## Recommended expansion order

Engineering recommendation: first prove one small, explicitly reviewed demo
equity/ETF set end to end. Then align scanner and backtest membership, validate
each exchange calendar and instrument mapping, and expand asset classes one
at a time with suitable accounting, data and risk models. Expanding the symbol
list alone would not establish multi-asset trading capability.
