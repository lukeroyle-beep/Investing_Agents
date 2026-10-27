# Bounded eToro Demo integration pilot

`demo-pilot-v1` is an explicitly selected manual virtual integration policy.
The existing strict policy, configuration defaults, historical-loss requirements
and qualification flags are unchanged. This pilot's accounting ledger attributes
broker position IDs within the existing Demo account; it is not another broker
account, an account reset, or a real-money pilot.

## Admission and accounting

The pilot admits only USD amount-based market buys of eligible equities or ETFs,
with leverage one and unambiguous real-asset settlement. Each opening reserves
principal, explicit opening charges and the estimated embedded bid/ask spread.
The reserve cannot exceed $100. New pilot capital, actual opening costs and
unresolved commitments cannot exceed $500; unresolved commitments consume a
position slot and at most five pilot positions are permitted. At least 20% of
whole-account equity must remain available cash after the proposed commitment.
The embedded spread is reserved conservatively but is never debited twice.

All legacy holdings, fees, exposure and observed inherited breaches are retained
at inception. Gross/net, issuer, sector, asset-type and settlement breaches are
reported. The new policy can admit an otherwise eligible order despite unrelated
inherited breaches. It never closes legacy holdings or adds to an issuer that
would exceed the existing 5% issuer limit. Reviewed issuer/sector classifications
are bound to live complete broker instrument metadata and immutable preparation
evidence. Share classes use the same issuer; fund providers are grouped
conservatively. Unknown or additional holdings need reviewed classification.

The Fill Agent owns `pilot.sqlite3`. Exclusive creation establishes an immutable
inception from a verified starting capture. Reopening cannot initialize missing
state. The runner also retains an account-bound inception marker and refuses to
initialize around existing control files. A missing/corrupt ledger requires
restoring its retained state; a new directory is not a reset procedure.

An atomic persistent claim covers the account and Europe/London calendar day,
including DST. The dispatch expires at London midnight as well as the evidence
deadlines. No second submission is allowed before whole-account reconciliation,
even on a later day or under a new command. Claims persist before the existing
per-command single-send coordinator. A crash between these claims remains held.

Pilot results begin prospectively at the verified inception. Current asset value
is allocated principal plus broker unrealised P&L. Actual opening cash debits
must reconcile exactly to principal plus actual fees and taxes; those proven
external costs are deducted once in pilot performance. Embedded spread is already
in the mark. Results and a permanent $25 loss latch persist across restarts.
Historical whole-account daily loss and drawdown remain `null`; no full-day
history or execution qualification is awarded.

The first-fill path supports fully opened direct positions. Missing or changed
pilot positions, external closures, partial fills, new unrelated positions,
changed legacy holdings and unexplained carrying-fee/cash movements stop new
entries. They are not represented as zero realised loss. Closing/activity
evidence must be reconciled before such a hold can be resolved; this runner does
not close positions or automatically release those holds. Realised P&L is zero
only while retained evidence proves that no attributed position has closed.

## Supported source and freshness contract

The actual execution host supplies the supported eToro MCP tools. There is no
credential loader, connector-token extraction, direct HTTP fallback or retry
loop. Profile verification checks Demo CID plus Demo read/write scopes, and
portfolio position CIDs corroborate the pinned Demo identity.

A capture reads `/api/v1/me`, then brackets `/api/v1/trading/info/demo/pnl`
between two `/api/v1/trading/info/demo/portfolio` reads. All seven pending-order
collections must be explicitly empty, mirrors/bonus credit absent, and all
positions, units, allocated principal and costs structurally stable. Nonempty
pending/mirror shapes that cannot be valued are rejected, not dropped.

The P&L route supplies account currency ID 1 (USD), margin currency, asset
currency IDs, account-currency P&L/exposure and per-position source timestamps.
This removes the CID-header instrument-breakdown dependency within this pilot.
Asset currency IDs remain source IDs; no missing currency code or FX rate is
invented. USD fields, coverage, money values, aggregate rounding and long-x1
`exposure = allocated principal + P&L` are checked.

Cash has no asserted broker timestamp. Its explicit pilot freshness bound starts
at the original request time: each request lasts at most 10 seconds, the
portfolio/P&L/portfolio bracket at most 15 seconds, and admission expires at
30 seconds from the earliest relevant request/source clock. Offset-bearing
position timestamps retain their seventh fractional digit. Receipts do not
become broker observation times. Validation reparses retained source bytes and
checks the derived projection, rejecting re-dated or altered evidence.

Market admission requires the exact instrument's broker `isExchangeOpen`,
`isCurrentlyTradable`, `isActiveInPlatform` and `isBuyEnabled`, no delisting, and
a realtime quote no older than 30 seconds. State requests last at most 10 seconds
and state freshness starts at the original request, expiring after 15 seconds.
The pilot does not require the separate `isOpen` field, which the supported live
search did not return. The retained [official `getRates` OpenAPI contract](evidence/demo_pilot_get_rates_contract.json) explicitly documents `date` as UTC;
only that field's offset-free form is interpreted as UTC, with raw spelling
retained. Unknown negative-zero offsets, future 100ns values and stale rates are
rejected. This does not change the unresolved strict aggregate timestamp rule.

Raw MCP receipts are saved privately before parsing. Sealed order tokens remain
in memory; prepare/place receipts retain exact numeric values and a raw-response
hash while recursively redacting those tokens, including nested JSON bodies.

## Manual host bridge

Run `scripts/run_demo_pilot.py` in an echo-disabled JSON-lines session with
`--runtime PRIVATE_PATH --identity-receipt PINNED_PROFILE_RECEIPT`. The first ever
initialization additionally requires `--initialize` and a new control directory.
The runtime and receipts belong under ignored private storage, never in Git.

The bridge emits one `tool_request` and accepts one complete MCP response per
line. The host routes only `etoro_execute_read`, `etoro_prepare_trade`,
`etoro_place_trade` and the actual usage tool. No tool call is retried. After
preview it emits `AWAITING EXACT-ORDER APPROVAL`, the exact confirmation, costs,
warnings, confirmation hash, expiry and policy limits. The host stops there.
Only Luke's explicit approval of those terms permits the exact
`approve_confirmation_hash` input. Expiry requires a fresh preview and approval.
No elapsed-time, generic project or old-preview approval is accepted.

After approval, the bridge refreshes usage, market and account evidence and
revalidates all gates. By default, every available usage window must retain at least 25%:
the 20% work floor plus five percentage points for recovery. An unavailable
primary window is unknown, not omitted. The persistent pilot kill switch is
initialized only for the first explicitly approved order; an existing engaged
switch is not silently reset. Strict/global configuration is not altered.

The default remains a 20% work floor plus 5% recovery reserve. An explicit user
instruction may select the pilot-only 10% work floor using
`--work-floor-percent 10 --usage-authorization 'USER_AUTHORIZATION_REFERENCE'`.
That profile stops work at 10% and requires at least 15% before submission.
Its authorization reference and thresholds are retained with quota evidence;
the displayed approval limits use the selected profile. Missing authorization,
unknown/stale quota or a reduced recovery reserve still block admission. The
strict connector rejects the pilot usage type and retains its original floor.

The existing connector places once. Uncertainty permits lookup only, using
actual broker order/reference IDs. Use `--recover COMMAND_ID` for this path;
recovery never initializes missing state or submits. A lost response without
an actual broker handle remains held. Once terminal full-fill evidence exists,
the Fill Agent requires exact cash/principal/fees/taxes, exact new position IDs,
unchanged legacy structure and full-account coverage before releasing the hold.
Operational completion follows the economic commit and is restart-idempotent.

The reviewed runner currently selects the minimum $10 SBUX equity order for
integration testing, subject to fresh admission and exact approval. This is not
an investment recommendation. Other candidates need a reviewed identity mapping.

## Validation and rollback

Run the four `tests/test_pilot_*` / `tests/test_demo_pilot_operator.py` suites and
the complete repository pytest suite. Synthetic tests cover cap and tiny-cost
boundaries, loss persistence, wrong accounts, missing ledger, concurrent and
duplicate claims, London midnight/DST, stale/future data, preview substitution,
unknown usage, missing recovery handles, partial fills and exact reconciliation.
The bridge is tested with synthetic local callbacks; those tests are not live
fill evidence.

Leave the review PR unmerged. Before a send, rollback means leaving this additive
path unused while preserving all evidence. After any claim, rollback never
permits resend or inception reset: retain state and resolve by lookup and
reconciliation. Success remains a confirmed virtual fill and reconciliation.
