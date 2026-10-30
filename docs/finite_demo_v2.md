# Finite multi-instrument Demo execution

This candidate extends the direct official Demo API worker to multiple explicit
candidates under one finite mandate. It removes the single-SBUX constant from
the new path. It does not change the hosted eToro MCP connector, whose prepared
orders still require individual user approval. Repository defaults remain
advisory; installing or merging this code does not activate trading.

## Supported contract

An immutable mandate lists exact instrument IDs, symbols, exchanges, currencies,
issuers, sectors and classification sources. New entries support USD-quoted real
equities and exchange-traded funds (ETFs), USD amount sizing, buy, leverage one,
and explicitly authorised market orders. Funds can represent equity, fixed
income, commodity or crypto exposure when the classification is substantiated.
There are no implied instrument selections or per-class allocation targets.

Direct crypto, foreign exchange, contracts for difference, contract sizing and
non-USD/GBX quotes are rejected. Limit IOC is not implemented in this path and a
limit mandate cannot fall back to a market order. Broker minimums, settlement,
costs, units, current session and eligibility are checked separately for every
candidate. A real-asset contract is not inferred from leverage alone.

The mandate binds the original absolute ledger path, immutable inception,
existing claim hashes, account, code commit, issue time, expiry and source of
authority. It specifies finite submission and daily counts, principal-plus-cost
caps, positions, cash reserve and cumulative loss stop. Code ceilings remain
USD100 per order, USD500 combined pilot capital, five pilot positions, at least
20% whole-account cash and a cumulative loss stop no looser than USD25. A 5%
whole-account issuer limit applies. New mandates cannot reset old capital,
positions, claims or latched losses. Attempts consume budget even when rejected.

## Preservation and serial execution

`scripts/setup_finite_demo_pilot.py` operates only on the original private runtime
with its existing inception marker and ledgers. It requires an engaged kill
switch, writes the explicit mandate and complete account classification catalog,
migrates the same SQLite submission table, and creates an execution journal.
Migration preserves every old submission and digest; the old v1 reservation
method still enforces its original daily limit. Setup never resets the switch,
requests credentials or starts a worker. Partial setup is held for inspection.

The one-cycle worker `scripts/run_finite_demo_pilot.py` accepts a candidate
instrument ID and amount, validates a clean reviewed checkout, and uses the
existing dedicated Demo credentials. It never searches for replacement keys.
Repeated invocations can process distinct candidates within the same mandate;
the original operator lock and atomic shared-ledger claims serialize them. An
unresolved order or incomplete execution journal blocks every new claim.

The worker requires fresh source-backed usage evidence with a 20% work floor and
five percentage points reserved for recovery. It does not inherit the old one-order usage or buffer waivers.
A missing usage host or unreported required broker buffer therefore causes a
hold. There is no automatic scheduler or strategy-to-worker deployment here.

### Genuine Codex usage host

`scripts/codex_usage_bridge.js` exports `serveCodexUsageRequests(api, initialCell)`
for the actual tool host. It accepts an already started process and never starts
a worker or selects a runtime. Bind `api.readUsage` directly to the Codex app
`get_usage_limits` tool and `api.write` to the same host's `write_stdin` tool.
The relay serves only sequential `usage` requests with empty arguments, making
one fresh tool call per request and passing its complete original result to the
worker. A finite cycle requests it twice. Broker requests, missing processes,
tool errors, uncertain response delivery and unexpected output stop the relay;
there is no automatic retry or cached/default percentage. A held relay with a
session ID requires inspection of that process, not a replacement worker.

Preparation can exercise this exact protocol safely by starting only
`python scripts/probe_codex_usage.py`, with a PTY if the host requires one for
stdin. The probe has no runtime or broker arguments. It stores the complete
private tool receipt in a fresh temporary probe directory with restrictive
permissions, disables terminal echo during input, and emits only a sanitized
summary, evidence hash and private receipt location. It never opens the original
ledger, kill switch, mandate or credentials. Its receipt establishes the bridge
read at that time, not quota authority for a later trade.

For both probe and worker, the usage parser preserves every available bucket
and window, fails closed for unknown allowance, and uses the request-start
clock with a 60-second maximum age. A null secondary does not invent another
window. The finite operator itself also enforces the strict 20% floor plus 5%
reserve, so bypassing the CLI cannot reuse the older reduced pilot policy.
The raw response includes private platform account data: do not print it or
include private probe receipts in Git. Operational evidence must come from the
genuine tool binding; synthetic test responses are never activation evidence.

## Clocks, terminal outcomes and journal

Engineering and screening precede preparation. There is no expiring MCP preview
token in the direct finite path. The final HTTP hook enforces a quote source age
of at most 30 seconds, quote receipt-to-hook time of at most five seconds, and
the bounded authorization deadline. Account evidence uses a 15-second gate.
API session evidence uses 15 seconds and must affirm tradability; the separate
60-second rendered-status exception is not implemented or assumed. Source,
receipt and final hook clocks are retained separately from broker execution
time. The hook is not broker receipt. V2 rate UTC semantics are documented, but
freshness is checked on each use; no feed is permanently trusted as fresh.

Independent lookup and whole-account captures verify execution identity,
principal, units, price, costs, unchanged prior positions and cash movement.
Terminal partial statuses 9/10 may reconcile actual fills with no automatic
top-up. Pending status 5 remains lookup-only. Terminal no-fill outcomes need
dated broker evidence, explicitly zero costs and unchanged cash/positions.
New USD execution arithmetic requires exact units times price equal principal;
any broker rounding discrepancy is held for reviewed treatment because the
current contract provides no verified rounding tolerance.

The Fill Agent ledger owns economics. The separate append-only execution
journal records intent, mandate and evidence hashes, dispatch claim, outcomes
and independently checked reconciliation. Exact duplicates are idempotent;
conflicts, corruption or missing evidence fail closed. A journal disk failure
after economic commit cannot admit another order. Retained source receipts can
complete operational and journal recovery offline without resending. Unknown
transport outcomes use the original request identity for lookup only.

Journal and database hashes detect ordinary corruption and inconsistent replay;
they do not claim protection against coordinated rollback of every local store.
Keep private evidence and backups outside public Git. Preserve every historical
runner, consumed dispatch lock and account snapshot. An unexplained position
created outside the original ledger remains a reconciliation hold; this version
does not silently import it or rewrite the baseline.

## Validation and activation boundary

Synthetic integration tests exercise two instruments and underlying classes
under one mandate, terminal full/partial/no-fill outcomes, one-send protection,
unknown outcomes, budget exhaustion, stale evidence, and crashes across economic,
operational and journal commits. All broker transports in these tests are fake.
Offline passing tests do not establish a deployed stack or profitable strategy.
The advisory journal and execution journal remain distinct. Autonomous strategy
learning and promotion are not implemented by this change.

Before an actual run, recover and reconcile the canonical private runtime,
screen real eligible instruments, review the finite mandate with exact limits
and expiry, and deliberately activate that mandate on its reviewed revision.
No account, instruments, scope file or mandate created by tests is authority.

The terminal status contract is from the [official Demo lookup specification](https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details).
Rate provenance is documented in the [retained official getRates contract](evidence/demo_pilot_get_rates_contract.json)
and [current official rate documentation](https://api-portal.etoro.com/api-reference/market-data/retrieve-bidask-rates-for-one-or-more-instruments).
