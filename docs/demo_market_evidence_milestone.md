# Demo market evidence checkpoint

21 October 2026. Follows merged pull request 4. Objective remains virtual
functionality with no return target. One bounded pass, ending in human review.

## Scope contract

- AC-1: implement the exact read-only Demo eligibility operation, with a fixed
  US-dollar body, strict requested-instrument coverage and a dedicated limiter.
  Broker configuration must distinguish real assets from contracts for
  difference, long from short, leverage one from other leverage, and available
  from potential configurations that require additional user action.
- AC-2: bind the six pilot equities to the official Nasdaq 2026 regular-session
  calendar, including full holidays, early closes and timezone transitions.
  Unsupported instruments, exchanges, years and modified calendars fail closed.
- AC-3: combine exact internal identity, broker instrument metadata, exchange
  metadata, eligibility and completion-time session evidence in a read-only
  diagnostic. Retain source hashes and capture timing; never grant submission.
- AC-4: identify the precise remaining Demo timestamp and cash-flow contract
  gaps, without substituting real or Cash-product account data.
- NG-1: no orders, credentials changes, account resets, risk-policy changes,
  execution enablement, scheduling or automatic merging.
- NG-2: no claim that broker permission means the market is open, that scheduled
  hours prove absence of a halt, or that closed trades establish funding history.

## Delivered behaviour

`get_demo_eligibility` uses only
`POST /api/v2/trading/info/demo/eligibility`, which the official catalogue marks
read-only. The body contains exact instrument identifiers and USD (US dollars).
The local dedicated limit is 16 reads per minute against the documented 20.
It cannot be repurposed into an arbitrary POST or a real-account route. Exchange
names come from the separately allowlisted exchange metadata read.

The diagnostic matches the supplied internal instrument to current broker
security metadata and exchange identity, then matches eligibility back to the
same symbol and identifier. Minimum exposure, minimum margin and maximum units
are retained, but no proposed order size is assessed in this milestone.

The annual calendar is bound to an approved source-file checksum and covers only
2026, Nasdaq, and AAPL/MSFT/NVDA/AMZN/GOOGL/META. It uses America/New_York to compute
scheduled hours and produces UK-local display times through Europe/London.
It assesses at capture completion, so crossing a close during the reads returns
closed evidence. The optional Python `now` argument is for offline tests only;
live callers omit it. The legacy historical-data calendar is not replaced.

Even a successful result has `submission_available=false` and
`qualification_credit=false`. It still lacks a bound account identity/risk
snapshot, fresh quote, current halt evidence, approved order and live operator.
The eligibility response has no broker snapshot timestamp: the collector's
timestamps describe local receipt and capture, not an invented server timestamp.

Default diagnostic, without loading credentials or creating account stores:

```bash
uv run --frozen python tools/demo_market_preflight.py
```

It exits 2 with an explicit blocked report. A configured read-only connection
can additionally receive `--symbol`, `--internal-instrument-id` (existing exact
internal identity), and `--broker-instrument-id`. These arguments do not approve
an order or establish the account selected for a future order.

## Actual read-only observations

The connected service returned all six pilot equities with available long,
leverage-one, real-asset configurations and permission to open positions.
Security metadata resolved all six to exchange identifier 4; the exchange
catalogue identified 4 as Nasdaq. The new eligibility parser successfully
parsed the actual service response. No private account payload was committed.

This proves connected-service access and parser compatibility for those reads.
It is not acceptance of the repository's separately credentialed transport or
the complete collector. No actual baseline, forward monitor or order was started.

## Remaining external contract questions

The official visible catalogue contained 197 operations, version `v1.398.0`.
The following are precise documentation gaps, not proof that no additional
provider capability exists. They can be sent to eToro support after the user
chooses to do so; no support message was sent in this pass.

1. **Demo aggregate timestamps:** does an offset-free `timestamp` on
   `POST /api/v1/trading/info/demo/aggregate-portfolio` always represent UTC
   (Coordinated Universal Time), and can the service return an explicit offset?
   The Demo contract gives a date-time without that convention. The real-account
   GET reference explicitly says UTC, but that is not authority to reinterpret
   a different Demo response. The parser remains strict.
2. **Complete Demo account movements:** which supported read enumerates external
   funding, resets, adjustments and other movements, with stable event identities,
   timestamps and verifiable interval coverage? Current portfolio PnL (profit and
   loss) is not such a ledger. Mirror deposits/withdrawals concern allocations
   inside a copy relationship, not external account funding.
3. **Closed-trade pagination:** document the page base, size limits, stable order,
   completion marker, upper time bound and durable execution identity. The Demo
   history response is a bare array with `minDate`, `page` and `pageSize`; it does
   not declare a cursor or completeness marker. An empty page cannot prove zero
   funding. Its lookback is less than one year; the instructions suggest windows
   without documenting an upper-date parameter.

Generic historical balances have no documented Demo selector. Cash-account
transactions are a different product. Neither is substituted into the Demo
ledger. `broker_history_verified` remains false, and absent flow coverage still
blocks new ledger observations. Pre-flow valuations are also needed before
cash-flow-adjusted drawdown can be qualified.

Next work: resolve those contracts and validate actual account capture; then
bind complete history, issuer/sector mapping, quote/halt evidence and the reviewed
market diagnostic to the live operator. Existing exposure-policy breaches still
require an explicit account/policy decision. Only then prepare one exact Demo
order for human approval. Historical validation of the six-equity pilot remains
separate and outstanding. Whole-market scanning is not enabled.

## Sources

Verification: 547 local tests passed, including 105 focused market/calendar and
read-client tests. Dependency declarations, Python compilation and whitespace
checks passed. Independent review found no required fixes. The default disabled
diagnostic returned blocked status with zero orders and no credential loading.
Actual-service eligibility parser compatibility is distinct from these
synthetic test results and from full transport/capture acceptance.

The pass stops at a clean pull-request checkpoint to conserve weekly usage.
The agent cannot read the remaining percentage and does not claim to have
measured the requested 20% threshold. No background collection or trading
process was launched. Resume from this checkpoint and its outstanding work.

- [Nasdaq 2026 holidays and regular hours](https://www.nasdaq.com/market-activity/stock-market-holiday-schedule)
- [Nasdaq trader calendar](https://www.nasdaqtrader.com/trader.aspx?id=Calendar)
- [eToro Demo eligibility](https://api-portal.etoro.com/api-reference/trading--demo/check-instrument-trading-eligibility)
- [eToro Demo aggregate](https://api-portal.etoro.com/api-reference/trading--demo/get-a-filtered-aggregated-portfolio-snapshot)
- [eToro Demo closed-trade history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history)
- [eToro Demo portfolio PnL](https://api-portal.etoro.com/api-reference/trading--demo/get-account-pnl-and-portfolio-details)
- [Prior account/risk checkpoint](demo_account_risk_milestone.md)
