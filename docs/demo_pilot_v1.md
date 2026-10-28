# Bounded eToro Demo integration pilot

`demo-pilot-v1` is an explicitly selected virtual integration policy. The manual
MCP path retains exact-order approval. A separate standing-mandate API path is
limited to the one attempt described below. The existing strict policy,
configuration defaults, historical-loss requirements and qualification flags
are unchanged. This pilot's accounting ledger attributes
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

The actual execution host supplies supported eToro MCP observations, or the
standing worker uses its separately selected direct read transport. The manual
MCP bridge has no credential loader, token
extraction, HTTP fallback or retry loop. The separately authorized standing
workflow injects dedicated credentials into the bounded official Demo API client;
it does not extract or reuse MCP credentials. Both routes verify the same Demo
CID and Demo read/write scopes, while portfolio position CIDs corroborate the
pinned Demo identity.

The standing worker's `--direct-reads` option performs the same bounded reads
in process, avoiding a host/model round trip for every observation. It requires
the existing explicitly selected usage waiver. A separate read-only allowlist
permits the profile, instrument metadata, search, rates, Demo portfolio and Demo
P&L GET routes, plus the read-semantic Demo eligibility and cost POST routes.
It cannot create an order. The execution client's write allowlist is unchanged.
Dedicated credentials remain in memory; no proxy, environment credentials,
redirect or transport retry is used. Original response bytes are saved privately
before parsing and linked by hash to the actual request path, body and clocks.
Direct receipts identify themselves as `etoro_direct_read`; they are never
represented as MCP responses. The same source and freshness checks apply.

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

## Standing mandate API workflow

Luke's explicit 27 October 2026 authorization permits a separately bounded API
workflow instead of another per-order MCP approval. Its scope is exactly one
$10 USD principal market buy of SBUX (broker instrument 1142), an equity, long,
leverage one, with real-asset settlement in the existing Demo account. All
pilot cost, capital, issuer, cash, loss, daily, freshness and reconciliation
controls above still apply. The work floor is 10% with five additional percentage
points reserved for recovery, so an opening send requires at least 15% remaining
in every available usage window.

Luke subsequently authorized work down to 5% remaining on 27 October. That
additional allowance is recorded in an exclusive `pilot-usage-amendment.json`
bound to the original mandate, Demo account and ledger inception. It does not
rewrite the standing mandate, reset any claim, change trade scope, or reduce
the five-point recovery reserve. Selecting `--work-floor-percent 5` therefore
requires at least 10% remaining for a new opening and stops work at 5%.
The original 10% standing default and 20% strict default remain unchanged.
The amendment's human authorization reference must match the fresh usage
evidence, and its integrity is rechecked immediately before dispatch.

Luke later explicitly removed usage limits for this Demo attempt. An exclusive
`pilot-usage-waiver.json` binds that instruction to the same original mandate,
account and ledger inception. With `--ignore-usage-limits` and the matching
`--usage-authorization`, the standing worker makes no quota calls and imposes
no quota floor, reserve or quota freshness deadline. The original mandate and
earlier allowance amendment remain intact. This waiver does not change the
order scope, account/market/cost freshness, financial risk checks, kill switch,
one-attempt claim, or lookup-only recovery. It is validated before credentials
are loaded, before the claim, and again immediately before transport. Without
explicit waiver selection, the existing quota policy still applies; the manual
MCP and strict execution paths do not accept the waiver.

The default standing assessment still rejects an omitted
`additionalBufferPercent` with `assessment_additional_cash_buffer_unknown`.
Changing that rule requires a separately authorized, exclusive
`pilot-buffer-amendment.json`, bound to the original mandate, account, ledger and
inception. No runtime amendment is installed merely because the optional code
path exists. Selection requires `--allow-unreported-buffer` and the artifact's
exact `--buffer-authorization` reference. The operator validates it before the
claim and the transport guard revalidates it immediately before sending; the
approval retains its hash and source reference.

Under that explicit amendment only, omission remains `not_reported` with an
unknown percentage. It is never converted into zero or explicit broker null.
The reserve calculation continues to cover principal, opening charges and spread;
it does not assert continuous available-cash coverage while the broker temporarily
reserves funds. Both the initial economic commitment and fully reconciled terminal
cash must meet the existing cash floor. No further entry can occur while a claim
is unresolved, every pending collection must be empty at reconciliation, and
unexplained cash debits remain a hold. An explicitly reported nonzero buffer is
still unsupported. Instrument 1142, amount, costs, capital, issuer, loss, freshness
and one-attempt limits are unchanged.

`standing-mandate.json` is exclusively created with the current human source
reference and issue time. It binds the Demo account, exact scope and original
pilot ledger path and inception hash. There is no invented calendar expiry:
its scope ends at the first submission or revocation. Any earlier claim,
including a rejected order or lost response, blocks another opening attempt
across restarts and subsequent days. The worker stops after its first confirmed
fill and reconciliation. A later attempt requires human review and separate
authorization; creating a new ledger or replacing this artifact is not a retry
procedure.

Fresh accepted pilot risk, account and quote evidence generate an expiring
`Approval` marked `authorization_kind=standing_mandate`. This records delegated
authority; it does not pretend that a human confirmed the newly generated order.
The exact eight-field official order payload, including real settlement, is
hashed and bound to that approval. The shared Demo kill switch is rechecked
before the durable claim. An engaged, missing or invalid switch prevents use.

The direct execution client permits only profile verification, the official
Demo create route `/api/v2/trading/execution/demo/orders`, and its order lookup.
A request UUID and claim are persisted before the single POST. There are no
transport retries or redirects. Uncertain outcomes use lookup by that retained
reference; they never repeat the POST. Raw responses are retained privately
before parsing. The generic client is not authority to trade: the mandate,
operator and original ledger enforce the one-shot scope.

### One-time dedicated-key setup

Prerequisites are the existing private runtime, verified `pilot.sqlite3` and
matching `pilot-inception.json`. The setup helper never initializes a runtime,
creates an inception, scans credentials, reads environment keys, or launches a
worker or schedule. Obtain dedicated API credentials through the provider's
normal user-controlled setup; do not put them in chat, Git, shell arguments or
an approval reference.

Run this yourself in an interactive terminal, using the existing runtime and
actual current-chat authorization reference:

```sh
.venv/bin/python scripts/setup_autonomous_demo_pilot.py \
  --runtime PRIVATE_RUNTIME \
  --authorization-reference 'CURRENT_HUMAN_STANDING_AUTHORIZATION_REFERENCE' \
  --activate
```

Both keys are entered through hidden prompts. The helper refuses nonterminal or
insecure input fallback. It verifies `/api/v1/me` for the existing Demo CID and
both Demo read/write grants before saving keys. The dedicated
`demo-api-credentials.json` uses exclusive creation, owner-only mode `0600`, and
an existing owner-only `0700` runtime directory. The loader rejects symlinks,
hard links, broader permissions and malformed files. Secret values are never
included in tool arguments, normal output or diagnostic messages.

Without `--activate`, setup saves the dedicated credentials and mandate and
leaves a missing kill switch uninitialized. It refuses a preexisting writable
switch unless `--activate` is explicit, so pending setup cannot inherit write
permission. Setup rereads the ledger and switch under the shared operator lock
and checks again after identity verification and before activation. A concurrent
revocation stops setup and remains engaged. With `--activate`, a wholly absent
switch may be initialized and enabled under the recorded mandate. An existing
engaged or invalid switch is never silently reset. No setup invocation submits
an order or starts the worker.

If both artifacts were successfully saved and activation remains pending, run
the same command with `--activate --resume-activation`. This explicitly reloads
the matching files, verifies Demo identity again, and does not prompt for keys
or overwrite either artifact. A partial write or an interruption that left the
switch engaged remains blocked for review; preserve every artifact rather than
reinitializing. After setup, the worker still needs current eligible market,
account, cost and quota evidence before any send.

Repository implementation and synthetic tests alone do not establish that
credentials are configured, activation occurred, a worker is running, or a
virtual fill happened. Those require separate inspected runtime and broker
evidence.

### Unattended cycle and scheduled host

`scripts/run_autonomous_demo_pilot.py` requires an existing runtime, dedicated
setup, a clean checkout at `--reviewed-commit FULL_SHA`, and the explicit usage
authorization reference. It does not initialize missing state or reset switches.
Its JSON-lines host accepts only `etoro_execute_read` and fresh Codex usage
results. It never requests MCP prepare, placement or write tools. The direct
client performs the single authorized Demo POST inside the worker.

With `--direct-reads --ignore-usage-limits` and the existing waiver's exact
authorization reference, no JSON-lines host bridge is needed: the worker acquires
and validates all sources itself before the same single-send operation. This
reduces orchestration delay but does not make stale broker data fresh or change
the instrument, amount, financial limits, or one-attempt rule.

Run the worker from its reviewed checkout using the existing Python environment:

```sh
python scripts/run_autonomous_demo_pilot.py \
  --runtime EXISTING_PRIVATE_RUNTIME --reviewed-commit REVIEWED_FULL_SHA \
  --work-floor-percent 10 --usage-authorization 'CURRENT_HUMAN_USAGE_AUTHORIZATION'
```

The host must use an echo-disabled, noncanonical terminal for the JSON-lines
session (`stty -echo -icanon` before starting Python), so large raw receipts are
not echoed or truncated. For each `tool_request`, execute exactly its requested
read or the actual Codex usage tool and return the entire original result as one
JSON line. Do not synthesize timestamps, quotes, usage, responses or approval.
Retain the bounded runner's terminal event; raw account data stays private.

A scheduled task may start a fresh cycle while no submission has been claimed.
Closed markets, stale evidence and insufficient usage never authorize a send.
After any claim, the same entrypoint detects it and uses lookup only. Missing
or conflicting state, an uncertain lookup, partial execution, fees/cash mismatch
or a revoked switch remains held. A reconciled economic ledger with an unfinished
operational command is repaired through lookup; a fully reconciled command exits
without another host or broker call.

After first-fill reconciliation, stop the schedule. A one-time scheduled check
is not evidence of execution: record its actual invocation, terminal event and
broker reconciliation separately. The computer and desktop app must remain
running for local scheduled work. Schedule creation is a separate host action;
the setup script itself only saves and activates the bounded mandate.

## Validation and rollback

Run the pilot, usage, mandate, setup and operator suites, plus the complete
repository pytest suite. Synthetic tests cover cap and tiny-cost
boundaries, loss persistence, wrong accounts, missing ledger, concurrent and
duplicate claims, London midnight/DST, stale/future data, preview substitution,
unknown usage, missing recovery handles, partial fills and exact reconciliation.
The bridge is tested with synthetic local callbacks; those tests are not live
fill evidence.

Leave the review PR unmerged. Before a send, rollback means leaving this additive
path unused while preserving all evidence. After any claim, rollback never
permits resend or inception reset: retain state and resolve by lookup and
reconciliation. Success remains a confirmed virtual fill and reconciliation.
