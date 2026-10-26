# Exact broker economic runtime

`agents.fill_agent.broker_runtime` implements the Fill-owned
`fill-broker-csv-v2` runtime for a first supervised eToro Demo buy. It uses a new,
empty private directory. Its canonical `portfolio_state.csv`, `cash_state.csv`,
`trade_fills.csv`, `cash_ledger.csv` and `processed_fills.csv` remain the economic
authority for that runtime. It does not create a parallel economic database or
write the existing advisory runtime.

The old runtime has float schema columns, a REAL-valued SQLite sidecar and a
synthetic USD100,000 default. It is unchanged for manual/fake fixtures. Its Fill
runner now refuses actual broker fills and refuses a `broker_runtime_manifest.json`
marker before creating a run ID, event log, cash or positions. Direct legacy
position/trade writers also reject actual broker rows. Copying a staged eToro
fill into the manual input does not bypass its retained execution identity.
Do not point the legacy advisory pipeline or runtime bootstrap at a v2 directory.

## Full-account initialization

```python
account = ReconciledBrokerAccount.from_component(component_proof, detailed, detailed_payload=raw_detail)
runtime = BrokerEconomicRuntime.initialize(new_empty_directory, account=account, now=now)
```

The source must be a currently qualified component-time proof and the complete
retained raw detailed payload and parsed `EtoroDetailedPortfolio` whose source ID
matches its first or closing detailed read. The raw payload is reparsed on every
admission; changing principal fields while retaining an old digest is rejected. Initialization rechecks time, identity, complete direct holdings,
empty pending collections, exact position IDs/units, principal and cash. No
source/capture timestamp becomes an atomic account timestamp. Per-position
invested principal comes from detailed `amount`, not margin, exposure, a quoted
average price or a synthetic starting balance. All existing positions, including
holdings outside the new-order universe, are retained by broker position ID.
Copies, unknown pending coverage or unsupported holdings block the whole account.

The immutable account manifest pins Demo, eToro, account ID, USD, schema and the
initial source identity. The full source proof is retained in the supporting
`broker_reconciliation.jsonl` audit. Initialization builds and commits a private
sibling staging directory, then atomically renames it to the requested destination.
It never overwrites a nonempty directory or rebases an established account.

## Terminal execution application

```python
receipt = runtime.apply_confirmed(
    executions=confirmed_executions,
    command=terminal_command,
    broker_payload=original_exact_payload,
    post_account=fresh_reconciled_full_account,
    now=now,
)
```

This path accepts only terminal, bound Demo long, unleveraged USD real-equity/ETF
opening buys. Command, payload hash, order ID, broker reference, instrument,
execution IDs and source times must agree. Every execution needs explicit finite
Decimal units, price, broker principal, fees and taxes. Principal is the broker's
`positionExecutions[].investedAmountCurrency`, carried as
`BrokerExecution.executed_amount`. A rounded `openingData.avgPrice` is retained
but never multiplied by units to invent principal. Missing principal or costs
block application.

Before writing, the runtime reconciles every prior holding plus the confirmed
execution quantities and principal against the fresh complete post-broker book.
It checks exactly:

`post_cash = prior_cash - executed_principal - explicit_fees - explicit_taxes`.

The authoritative cash source time and each new or changed position valuation
must be at or after its execution. A later collection completion cannot replace
those source clocks; unchanged holding marks may remain older within freshness.
Original execution timestamp text is required and retained alongside its datetime view; all
ordering comparisons preserve a supplied seventh fractional digit exactly.

Any missing/changed unrelated holding, unexplained cash movement, execution
principal discrepancy, identity mismatch or unknown coverage blocks the entire
transaction. No cash, position, fill marker or economic log is updated on a
reconciliation mismatch. Resets, deposits during the cycle, closes and sales are
outside this bounded application path; they cannot silently alter the baseline.

CSV values remain Decimal text, including all supplied fractional digits. Fill
IDs are durable and a replay must match the full original execution and command.
Each terminal command also pins one complete execution ID set; changing every
execution ID cannot apply the same order again. An identical replay reconciles
against the current broker book and returns
`replay=True` without repeating economic writes. Conflicting or partial terminal
batch replays stop for reconciliation.

## Transaction recovery and completion

A single file lock protects every read/write through `BrokerEconomicRuntime`.
Each transaction durably records its exact target CSV bytes, expected predecessor
hashes and source-backed receipt before replacing files. Replacement is atomic
per file. An interrupted multi-file commit remains explicitly pending; a reader
or `recover()` validates and finishes that same journal once. Unknown file changes
or a corrupt journal/manifest fail closed. Recovery neither resubmits an order nor
recalculates economics from rounded data. Source audit history is committed in
the same transaction.

A failed initializer leaves its private `.broker-init-*` staging evidence for
inspection; it does not expose the requested runtime as ready. A recovered staging
directory is not automatically promoted to the requested path.

`EconomicApplicationReceipt.reconciled=True` is returned only after full broker
post-state reconciliation and canonical commit (or an identical verified replay).
Broker submission, terminal fills, economic application and post-state reconciliation
remain separate facts. Synthetic tests and the offline host lifecycle do not
establish a live broker trade or a live baseline.
