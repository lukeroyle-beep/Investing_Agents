# Demo P&L account capture

26 October 2026. The bounded operator now defaults to the documented Demo
profit-and-loss route. The aggregate parser and its timezone rejection are
unchanged. This branch starts at `d6f3c56`, current master after PR10, in an
isolated worktree; the dirty primary checkout is preserved.

## Source contract and calculations

The [P&L reference](https://api-portal.etoro.com/api-reference/trading--demo/get-account-pnl-and-portfolio-details)
and [official OpenAPI](https://api-portal.etoro.com/api-reference/openapi.json)
define `clientPortfolio`, singular `credit`, `accountCurrencyId`, the numeric
portfolio `unrealizedPnL`, and each position's nested `unrealizedPnL` object.
The guide examples' top-level `credits` and the reference example's flattened
position fields are inconsistent with that schema. The parser accepts the
nested schema, matching the user-reported connector observation. A connector
read does not qualify the repository transport, account or valuations.

The [equity guide](https://api-portal.etoro.com/core/guides/calculate-equity)
and [cash guide](https://api-portal.etoro.com/core/guides/calculate-available-cash)
reduce to the following only after all seven pending collections and mirrors
have been explicitly checked empty:

- Available cash = `credit`.
- Invested amount = sum of position `amount`.
- Unrealised P&L = sum of position `unrealizedPnL.pnL`, reconciled against the
  portfolio total within the existing USD 0.02 tolerance.
- Equity = available cash + invested amount + unrealised P&L.

Broker margin and account-currency exposure are retained separately. Neither
replaces invested amount. The guide does not prescribe another subtraction of
position fees; overnight fees/dividends, external fees and taxes are retained
without double deduction. A margin-equals-invested identity, an independently
verified price/FX calculation, or fee-netness beyond the guide is not asserted.
The report names the precise reconciliation scope.

## Read bracket and scope

The sequence is opening detailed portfolio, P&L portfolio, required security
metadata, then closing detailed portfolio. Metadata comprises the existing v2
securities lookup plus one unfiltered
[Demo instrument-breakdown](https://api-portal.etoro.com/api-reference/trading--demo/get-instrument-breakdown-demo)
read for explicit asset-currency codes. This is normally five requests, with
metadata pagination still subject to the original total request budget.

The extra metadata read is necessary because P&L exposes currency IDs without
a documented GBX mapping and the securities response has no currency code.
Its fixed parameters include all positions, orders and mirrors. Currency
metadata must match exact position/instrument coverage, units, direction,
settlement and leverage; all its order groups and mirrors must be empty. Its
prices, P&L and timestamps are never valuation evidence for this capture.
Missing/unsupported metadata stops the capture; no ID mapping is guessed.
The documented USD ID is checked; other numeric asset/margin currency-ID
mappings remain explicitly unverified;
contradictory ID/code pairings within the bracket are rejected. Arithmetic
uses the broker fields explicitly denominated in account currency.

Supported holdings remain long, leverage-one stocks, ETFs and crypto, real or
CFD settlement, with USD/GBX assets in a USD account. Every position's identity
must match the independently supplied Demo identity. Opening, P&L and closing
reads must have exactly unchanged cash, bonus credit, position IDs, instrument
IDs, units, amounts, settlement, direction and leverage. All seven pending
families and mirrors are explicit at every primary read. Decimal JSON numbers
are preserved before comparison. An empty account cannot corroborate identity
through this route and is rejected explicitly.

## Three different time claims

Capture start/completion bound collection, not broker observation. Structural
and cash consistency compare the required fields across the bracket, not raw
payload equality: changing marks do not by themselves change holdings.
Each position retains its original offset-bearing timestamp and exact Decimal
seconds, including all seven fractional digits. Missing offsets, unknown
`-00:00` offsets and unsupported precision fail closed.

At completion every position is independently checked against the existing
15-second maximum age and zero future-time allowance; a stricter configured
limit is honoured. A 100-nanosecond boundary violation fails. One stale or
future valuation makes the captured account valuation unqualified, even if
others are current. Cached marks do not become fresh at receipt. No receipt,
oldest/newest mark, metadata time or ambiguous aggregate time is substituted.

`EtoroPnlAccountEvidence.observed_at` is explicitly null. This separate evidence
model is not a `BrokerAccountSnapshot` or risk `AccountEvidence`. The risk
contract explicitly rejects a missing whole-account observation time. There
is no baseline or order conversion in the P&L report.

## Capture status and operator handoff

| Evidence layer | Current result |
| --- | --- |
| Repository transport | NOT ATTEMPTED: both normal Demo credential environment variables are absent in this process. |
| Repository reconciliation | NOT OBSERVED LIVE; tested with synthetic cases only. |
| Repository valuation freshness | NOT OBSERVED LIVE; exact stale/future boundaries tested offline. |
| Trading readiness | BLOCKED; no baseline, daily-loss/drawdown qualification, history qualification or broker-backed order execution. |

No connector credentials were copied or discovered. Zero new broker requests
and zero support messages were sent. The one new capture allowance remains
unused; prior consumed attempts and their evidence remain untouched.

The owner handoff uses the isolated reviewed source, existing private Demo
identity provenance, a new owner-only evidence directory and the normal
`ETORO_DEMO_API_KEY` / `ETORO_DEMO_USER_KEY` environment. The private local
operator entry supplies hidden prompts, checks pinned source/setup hashes,
and claims one named attempt before capture. The final review response links
that local entry. Never reuse a consumed attempt, change its name to retry,
or substitute a connector credential. Inspect retained evidence after any
interruption or terminal result. An unavailable metadata route is a terminal
result, not permission to fall back or relax scope.

The existing requests transport retains verified TLS, no redirects, zero
retries, a 16-request ceiling, a 15-second initiation deadline, reduced socket
timeouts and a separate 15-second capture interval check. Raw bytes/receipts
precede decoding, which precedes parsing. Headers stay private and are never
serialized. Source identity, per-position timestamps and financial amounts
are retained only in owner-only ignored evidence. The sanitized report keeps
transport, reconciliation, valuation freshness and trading readiness separate.

Validation results and exact reviewed commit are recorded in the pull request.
No execution flags, risk limits, schedules, deployment or merge settings are
changed. Rollback is closing this unmerged PR; retain private evidence.
