# Supported eToro Demo connector contract review

Reviewed 26 October 2026 against connector catalog API v1.400.0, skill 1.21.3,
the complete visible catalog of 197 routes, and official route specifications.
This report is contract evidence, not submission, execution, or reconciliation.

## Opening and recovering one order

The supported connector opening path is `prepare_trade(account="demo", ...)`,
display its complete confirmation and warnings, obtain explicit approval, then
one `place_trade(token=...)` call. The token is connection-bound and expires.
Opening through `execute_write` is not the supported connector flow. Connector
access does not expose or imply environment-variable credentials.

The observed preview is `verdict=ready`, with `account`, `token`, `expiresAt`,
`confirmation` and `warnings`. It includes instrument, direction, order type,
amount, leverage, estimated units, quote, costs, balance impact and a subset of
eligibility. It does **not** expose the selected settlement or a pre-send
reference ID. The bridge therefore binds a separate exact eligibility response,
and permits only an unambiguous `real` / `long` / leverage 1 configuration.

An authorized read-only eligibility request for AAPL, instrument 1001, returned
minimum amount and minimum exposure USD 10, fractional units, amount sizing,
maximum 5,945 units, and exactly one long x1 configuration: `real`, non-potential.
CFD long configurations were leveraged. A W-8BEN requirement remains an explicit
preview warning. This does not establish account-wide risk admission.

The bridge stores only token and confirmation hashes, exact order and expiry,
claims durable dispatch before placement, and never retries a placement. Unlike
the connector's documented same-token retry option, this project requires
lookup-only recovery. A timeout with no returned order/reference ID is an
explicit recovery-handle blocker; the local UUID is not a broker reference.
The supported profile callback must freshly confirm the expected `demoCid` and
`etoro-public:trade.demo:write` before preview and immediately before dispatch.
Only the Demo identity, required scope and verification time are retained;
personal profile fields are discarded. The dispatch deadline is rechecked after
the profile read so its latency cannot extend stale source or approval evidence.

[V3 Demo submission](https://api-portal.etoro.com/api-reference/trading--demo/submit-an-order-for-asynchronous-processing)
accepts asynchronous processing; HTTP 202 is not a fill. V3 market orders require
eligible settlement, one instrument identifier and one sizing method.
[Order lookup](https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details)
accepts exactly one of `orderId` or `referenceId` (the submitted X-Request-Id).
States 3 and 5 mean filled/partially filled; 1, 2, 11 and 12 remain in flight.

Lookup `openingData` contains cumulative units and average price, not a stable
individual-fill ID. The bridge stages only final full-fill position aggregates,
using stable order-plus-position identity; contradictory repeated values fail.
Partial or partially rejected/cancelled results remain unreconciled and unstaged.
Missing execution fees, taxes or conversion are unknown. Fill Agent is still
the sole economic writer; staging and broker lifecycle completion do not prove
economic reconciliation.
The official position execution also exposes `investedAmountCurrency`, defined
as invested amount in account currency. The bridge requires that explicit
principal, zero `addedFunds`, and unchanged `remainingUnits`; it never derives
principal by multiplying units by the potentially rounded `avgPrice`. Fill
Agent reconciles that principal and actual fees/taxes against the complete
post-trade detailed account and cash.

## Account time, accounting and coverage

[Aggregate portfolio](https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot)
describes its timestamp as calculation time, without establishing a timezone
for offset-free responses. Its DailyPnl gain field is explicitly unrealised
daily gain; it cannot replace cash-flow-adjusted account performance.
`dailyCutoffUtc` expresses the user's local-midnight instant in UTC.

Aggregate available cash equals balance less frozen cash. Total used margin
includes frozen orders and mirror margins. Therefore removing frozen cash from
used margin is necessary when reconciling invested capital against total cash;
margin must not be substituted for market exposure.

[Closed-trade history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history)
uses `minDate`, `page`, and `pageSize`. It is not complete funding, reset,
dividend, financing or individual execution history. The full visible catalog
has no Demo-wide complete ledger for those categories. Other cash transactions,
transfers and balance-history routes concern different account surfaces and
cannot be substituted for Demo history. Missing categories remain unknown.

The [rates route](https://api-portal.etoro.com/api-reference/market-data/retrieve-bidask-rates-for-one-or-more-instruments)
reports realtime/delayed quote type and UTC quote time. Instrument search
reports open, exchange-open, current-tradeability, active, buy-enabled and
delisted flags. The manual operator requires fresh realtime quotes, regular
session and all applicable current state flags, then reevaluates full-account
risk and usage reserve immediately before its single send.

Qualification after 30 sessions/20 cycles is not required for this first manual
cycle. Fresh complete history, admissible exposure and exact-order approval are
required; this implementation does not manufacture those prerequisites.

`python scripts/validate_connector_demo.py --output-dir <new-empty-path>` runs
the credential-free callback bridge and v2 Fill Agent through one synthetic
cycle. It retains a pre-existing holding, reconciles explicit principal and
cash, restarts through lookup, and verifies replay deduplication. All source
clocks, complete history, profile, prices, execution and approval are invented
for this test; its report explicitly records zero live submissions and fills.
