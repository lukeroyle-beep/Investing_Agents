# Historical pilot replay checkpoint

21 October 2026. Follows merged pull request 5. The objective is virtual
functionality with no capital-return target. This milestone demonstrates an
offline replay using saved eToro market prices, not a live virtual-account run.

## Scope contract

- AC-1: require exact configured strategy and benchmark membership, valid
  open/high/low/close (OHLC) bars, unique chronological daily labels and the
  same observed dates across every instrument before producing replay output.
- AC-2: honour the selected shared research universe in the offline command;
  reject a conflicting saved configuration or synthetic/pilot combination.
- AC-3: normalize successful, explicitly untruncated eToro daily responses
  offline, bind candle identifiers to securities metadata, retain source hashes
  and exclude the current New York date conservatively.
- AC-4: replay the actual six-stock pilot with unchanged strategy parameters
  and costs, then reproduce identical results from its saved inputs/configuration.
- NG-1: no orders, account resets, credential or policy changes, scheduling,
  execution enablement, strategy tuning or automatic merging.
- NG-2: no performance qualification, whole-market claim, independent exchange
  calendar qualification or inferred corporate-action convention.

## Actual dataset and result

The connected eToro read service returned 600 `OneDay` candles for each of
eight instruments. Capture completed at 08:35 BST (07:35 UTC). This is local
receipt time, not a broker attestation of candle finality. The potentially
incomplete 21 October candle was excluded from every series.

| Role | Instruments | Retained observations each | Observed dates |
| --- | --- | ---: | --- |
| Strategy | AAPL, MSFT, NVDA, AMZN, GOOGL, META | 599 | 14 May 2024 to 20 October 2026 |
| Benchmarks | SPY, QQQ | 599 | 14 May 2024 to 20 October 2026 |

All 4,792 retained bars passed the input audit. This is a deliberately limited
six-equity pilot with two exchange-traded fund (ETF) benchmarks. Bonds,
commodities, currencies, futures and the wider market are not included. The
legacy scanner and default backtest universes are unchanged; use
`INVESTING_RESEARCH_UNIVERSE=config/demo_research_universe.yaml` to select the
shared pilot explicitly.

The existing fixed-parameter strategy completed **73 simulated trades**, with
**1,017 rejected candidates** and **four rolling evaluation folds**. All seven
functional checks passed: completed trades, deterministic replay, deterministic
rolling evaluation, non-negative cash, flat final holdings, cash reconciliation
and sufficient folds. A second run from the saved CSV (comma-separated values)
and configuration produced an identical validation report and input/result
fingerprints. Parameters and costs were not adjusted to obtain these results.

The report also records an observed 13.63% return and 5.21% maximum drawdown
for this particular unqualified simulation. These observations are not a return
target, an expected return, an independently validated performance record or
grounds to enable trading. SPY is the configured primary fold comparator; QQQ
is now required and audited, but the existing fold calculation still uses SPY.

See the checked-in [evidence summary](evidence/historical_pilot_20261021.json)
for per-instrument identifiers, source-body hashes, the capture hash, actual
coverage and all functional checks. Raw market responses and replay artifacts
are retained separately in `etoro-historical-pilot-evidence-20261021.zip`.
No private account data or credentials are in that bundle or the repository.

## Reproduce from the evidence bundle

Extract the bundle into a fresh directory under `runtime/validation/`. It
contains `capture.json`, `normalized/`, `replay/`, `reproduced/` and a checksum
inventory. The following paths assume its contents are under
`runtime/validation/etoro-history-20261021/`.

Normalize the saved raw responses again into a new directory:

```bash
uv run --frozen python scripts/prepare_etoro_history.py --capture runtime/validation/etoro-history-20261021/capture.json --config runtime/validation/etoro-history-20261021/normalized/config.yaml --completed-before 2026-10-21 --output-dir runtime/validation/etoro-history-reprepared
```

Replay the saved inputs into a new directory:

```bash
uv run --frozen python scripts/validate_virtual_backtest.py --prices runtime/validation/etoro-history-20261021/normalized/historical_prices.csv --config runtime/validation/etoro-history-20261021/normalized/config.yaml --output-dir runtime/validation/etoro-history-replayed
```

No credentials or network connection are used by either command. Existing
output directories are rejected. If a research-universe environment selection
is present, it must match the saved pilot configuration. Unset it when replaying
an unrelated or synthetic configuration.

Expected input fingerprint:
`88c85f98518c354d94e53e997a1d6f409bd1f6e665ac00b0f1fd735177b3f476`

Expected result fingerprint:
`74998ffb6fa41efb8386e84b99372b2ec82e727759648463fb9c459da9963dcc`

## Source and validation limits

The inspected official eToro contract exposes
`GET /api/v1/market-data/instruments/{instrumentId}/history/candles/{direction}/{interval}/{candlesCount}`
with up to 1,000 candles per request. This capture requested 600, ascending,
`OneDay`. Securities were resolved with
`GET /api/v2/market-data/instruments?symbols=AAPL,MSFT,NVDA,AMZN,GOOGL,META,SPY,QQQ&pageSize=100`;
the response explicitly had no further page. The normalizer requires exact
symbol and identifier coverage, correct interval, complete response envelopes,
the requested count and valid prices before excluding any current-day bars.

The API describes `fromDate` as the candle-period start. Actual observations
were explicit UTC-midnight strings. The adapter takes their **literal dates
as provider labels**, without shifting to the previous New York date, filling
gaps, sorting, adjusting prices or repairing the calendar. That is a declared
research convention, not proof of the broker's exchange-session assignment.
Offset-free, intraday, duplicate, unordered or future labels are rejected.

Equal date sets cannot expose a session absent from all eight series. An
independent complete calendar for the observed 2024–2026 interval is not yet
bound. The earlier Nasdaq 2026 execution calendar does not cover this whole
interval or SPY's listing venue. Split/dividend adjustments, point-in-time
universe membership, survivorship and costs remain unqualified. The raw
response format does not establish those conventions. Current-day exclusion
also cannot prove that older provider candles will never be revised.

The baseline configuration requests data from 2022. This bounded capture only
starts in May 2024. The audit records both ranges explicitly; the supplied-file
validator replays all supplied sessions and does not claim to fulfil the full
configured historical acquisition range. The rolling windows remain
252 training / 63 validation / 63 test observations, stepping by 63. At least
504 observations are needed for three folds; these are fixed-parameter windows,
not model fitting or an autonomous self-improvement loop.

Hashes bind the saved bytes and computations, not an authenticated provider
signature. A caller-created capture remains a caller-supplied snapshot.
`strategy_promotion_eligible` and `live_demo_qualified` remain false, and
`broker_orders_submitted` remains zero. Existing account timestamp, cash-flow,
exposure-policy and live-operator blockers described in the
[previous checkpoint](demo_market_evidence_milestone.md) are unchanged.

## Next bounded work

Verification: the full maintained suite passed 602 tests. Independent review
then identified ambiguous duplicate CSV headers being renamed by pandas; the
reader now rejects duplicate headers and ragged rows before parsing. All 74
focused history, normalization and replay tests passed after that correction,
including six new CSV regressions. Source compilation, dependency declarations
and whitespace checks passed. Independent re-review found no required fixes.
The actual saved-input replay also matched its complete first validation report.

Recommendation: bind historical session coverage and clarify the candle
adjustment/dividend contract before treating historical performance as qualified.
The broker account contract questions can be resolved in parallel when provider
answers are available. A successful historical replay does not authorise a
demo order or replace actual supervised broker lifecycle evidence.

Stop at this pull request for review to conserve weekly usage. Remaining weekly
usage is not visible to the agent, so the requested 20% threshold cannot be
measured. No background collector or trading process is running.

Primary source: [eToro API reference](https://api-portal.etoro.com/), exact
operations and response schemas inspected through the connected official
catalogue during this capture. The saved raw responses and hashes establish
what was observed in this run; future responses may differ.
