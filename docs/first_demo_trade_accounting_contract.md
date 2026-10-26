# First Demo trade accounting contracts

Contract versions: `account-performance-v1`, `account-performance-store-v1`.
These contracts calculate and retain evidence. They do not establish that a
connector supports Demo history, grant order authority, or write economic fills.

## Account identity and precision

`execution.accounting.AccountValuation` requires account-currency Decimal
values and exactly reconciles `equity = cash + invested_amount + unrealised_pnl`.
Broker margin is collateral; gross exposure is the sum of absolute account-currency
position exposures. Both are independent values and neither substitutes for
invested principal. Monetary input sums retain every supplied digit. Unitisation
uses exact rational arithmetic; public ratios use 50 significant Decimal digits.
No float or monetary rounding tolerance is admitted by this contract.

`valuation_at` means an authoritative atomic account observation. A component
model keeps it null, uses the explicit `etoro-component-time-v1` model and
retains a source-evidence digest plus component interval. Performance evidence retains the
complete current valuation so risk checks can bind its actual amounts and clocks,
not merely a caller-supplied snapshot identifier. The interval is a
history coverage bound, not an atomic timestamp. Every underlying component
must pass the component model's current freshness, skew, identity and coverage
checks before conversion and at risk admission. `valuation_from_component_time`
performs this conversion, and `AccountPerformanceStore.record` independently
requires and revalidates the same typed source proof for component valuations. Merely supplying a digest or a
reconciliation reference does not prove the source. No live source adapter may
construct this input from an unqualified saved capture.

## History and daily performance

Currency is USD and risk days are Europe/London calendar days, including BST/GMT
transitions. A day starts at local midnight. Full-day P&L requires a reconciled,
authoritatively timed valuation exactly at that boundary and verified, gap-free
history through the current valuation. A component capture ending at midnight
cannot manufacture that boundary value. A midday start remains partial day.

`daily_pnl = current_equity - day_open_equity - net_external_cash_flows`.
`daily_loss_fraction = max(0, -daily_pnl / day_open_equity)` uses the fixed
opening-equity denominator. Current equity or unrealised gains cannot replace
this measure. Coverage must explicitly include every category:

| Category | Treatment |
| --- | --- |
| Deposits / withdrawals | External signed flows; excluded from performance. |
| Purchases / sales | Internal transfers; principal is not an external flow. Realised gains remain in equity performance. |
| Dividends | Performance, already represented in reconciled equity. |
| Fees / financing | Performance costs, already represented in reconciled equity; no second deduction. |
| Currency conversion | Internal movement; account-currency valuation changes and costs remain performance. Preserve authoritative converted amount, original amount/currency and rate. |
| Account reset | Breaks continuity and blocks the result. Never silently rebases the ledger. |

A missing category, unsupported Demo route, incomplete pagination/cursors,
missing history or unknown conversion is unknown, never a declared zero. The
`HistoryCoverage` flags are inputs to a calculator: only an independently
validated source adapter may assert broker verification. Synthetic tests are
not such verification.

## Drawdown and persistence

Drawdown starts at one immutable, fresh, reconciled inception valuation and
retains every observed peak. For a deposit or withdrawal, an immediate
pre-flow authoritative account valuation is necessary. The change in units is
`external_flow / pre_flow_unit_value`; it changes invested units rather than
performance. Offsetting flows still need separate pre-flow valuations and
ordering. Exhausted equity and unproven simultaneous-flow order block drawdown.

Complete transaction history does not establish complete peak valuation history.
When the latter is unproven, observed equity/peak indexes may be inspected but
`drawdown_fraction` remains null and risk qualification fails. New risk days
change daily measurement only; they never reset the inception peak or erase loss.

`AccountPerformanceStore` writes append-only, hash-linked SQLite records with
immutable account, currency, time zone and inception. New records must be fresh,
strictly increasing and cover complete verified history from inception. Prior
events must repeat identically. Exact replay returns the original dated evidence
and creates no fresh observation. Source evidence and databases remain private.
The existing `RiskBaselineStore` remains explicitly unqualified; its formula now
requires explicit invested principal for holdings instead of substituting margin.

These are risk-evidence stores, not the economic ledger. Fill Agent remains the
sole writer of executions into economic state. A first manual order does not
require the later 30-session/20-cycle qualification, but all current account,
history, exposure, quote/session and exact-order approval gates still apply.

## Usage admission

`execution.usage_reserve.UsageAdmission` binds a fresh account-wide usage-tool
snapshot to its source reference/digest and complete list of available quota
windows. A source that supplies only a weekly primary does not imply a missing
five-hour window. Every available primary/secondary allowance must be known,
and at least one primary must be present. Missing data never means unlimited.

Immediately before sending an order, the snapshot must be no more than 60
seconds old, and the smallest remaining allowance must be at least 25%: the
user's 20% floor plus at least five percentage points reserved for lookup and
reconciliation. Continued independent work stops at 20%. Configuration can
raise either requirement but cannot lower it. There is no reset or credit
bypass, and this quota reserve is not a guarantee about recovery token costs.
