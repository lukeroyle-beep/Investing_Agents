# SQLite Migration Design

## Objective
Migrate the repository's current CSV-backed storage to SQLite while preserving these existing semantics:

- `data/portfolio_state.csv` remains the canonical portfolio state conceptually
- Fill processing remains the economic mutation boundary
- closed positions remain economically immutable after closure
- Exit Agent and Portfolio Equity Agent remain non-mutating
- cash/event ledgers remain append-only
- pipeline orchestration remains deterministic
- advisory-only governance behavior remains unchanged

This design is for migration planning only. No migration code is proposed here.

## Current Storage Classification

### Current-state files
These represent the latest canonical or near-canonical state.

- `data/portfolio_state.csv`
- `data/cash_state.csv`
- `data/portfolio_equity.csv`

### Append-only ledger files
These record economic or processing history and should remain append-only in database form.

- `data/cash_ledger.csv`
- `data/processed_fills.csv`

### Append-only audit / event files
These record operational and audit history.

- `data/event_log.csv`

### Run history files
These track pipeline lifecycle and control state.

- `data/run_history.csv`
- `data/run_reconciliation_summary.csv`

### Derived analytics / reporting files
These are reproducible from stronger sources and should be treated as derived tables or materialized outputs.

- `data/portfolio_equity_history.csv`
- `data/performance_summary.csv`
- `data/lifecycle_integrity_report.csv`
- `data/exit_advice.csv`
- `data/position_alerts.csv`

### Input / staging files that likely remain external for a while
These are not good first candidates for authoritative DB ownership.

- `data/manual_fills.csv`
- advisory / agent output CSVs used in the existing pipeline

## Recommended SQLite Table Model

## 1. Canonical Current-State Tables

### `portfolio_positions`
Purpose: canonical latest portfolio state, replacing `portfolio_state.csv`.

Suggested columns:
- `position_id TEXT PRIMARY KEY`
- `ticker TEXT NOT NULL`
- `side TEXT NOT NULL CHECK (side IN ('long','short'))`
- `status TEXT NOT NULL CHECK (status IN ('open','exit_required','closed'))`
- `quantity REAL NOT NULL`
- `entry_price REAL NOT NULL`
- `entry_date TEXT NOT NULL`
- `capital_allocated REAL`
- `stop_loss REAL`
- `take_profit REAL`
- `regime_at_entry TEXT`
- `sector TEXT`
- `signal_score REAL`
- `highest_price_since_entry REAL`
- `lowest_price_since_entry REAL`
- `current_price REAL`
- `market_value REAL`
- `pnl_abs REAL`
- `pnl_pct REAL`
- `exit_flag TEXT`
- `exit_reason TEXT`
- `last_updated TEXT`
- `run_id TEXT`
- `realised_pnl_abs REAL`
- `fees_total REAL`
- `closed_at TEXT`
- `exit_price REAL`

Constraints:
- `position_id` unique
- one active position per `(ticker, side)` via partial unique index:
  - unique index on `(ticker, side)` where `status IN ('open','exit_required')`

Notes:
- preserve current `exit_flag` storage flexibility initially as `TEXT`, because current CSV data is inconsistent
- do not introduce `closing` unless the repo actually adopts it behaviorally

### `cash_state`
Purpose: latest cash snapshot, replacing `cash_state.csv`.

Suggested columns:
- `state_id INTEGER PRIMARY KEY CHECK (state_id = 1)`
- `as_of TEXT NOT NULL`
- `cash_balance REAL NOT NULL`

Constraint:
- single-row table enforced by fixed `state_id = 1`

### `portfolio_equity_snapshot`
Purpose: latest one-row snapshot, replacing `portfolio_equity.csv`.

Suggested columns:
- same fields as latest row of `portfolio_equity_history`
- optional fixed key `snapshot_id INTEGER PRIMARY KEY CHECK (snapshot_id = 1)`

This can also be omitted if the latest history row is always queried instead.

## 2. Append-Only Ledger Tables

### `cash_ledger`
Purpose: authoritative economic cash movement log, replacing `cash_ledger.csv`.

Suggested columns:
- `ledger_id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL`
- `timestamp TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `position_id TEXT NOT NULL`
- `ticker TEXT NOT NULL`
- `side TEXT NOT NULL`
- `action TEXT NOT NULL`
- `amount REAL NOT NULL`
- `fees REAL NOT NULL`
- `cash_balance_after REAL NOT NULL`
- `notes TEXT NOT NULL DEFAULT ''`

Constraints:
- append-only by trigger
- foreign key from `position_id` to `portfolio_positions(position_id)` should be considered optional at first because some historical rows may outlive row lifecycle assumptions

### `processed_fills`
Purpose: authoritative fill-processing deduplication boundary, replacing `processed_fills.csv`.

Suggested columns:
- `fill_id TEXT PRIMARY KEY`
- `processed_at TEXT NOT NULL`
- `run_id TEXT NOT NULL`

Constraint:
- `fill_id` unique guarantees "each processed fill appears exactly once"

## 3. Append-Only Audit / Event Tables

### `event_log`
Purpose: append-only audit/event stream, replacing `event_log.csv`.

Suggested columns:
- `event_id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL`
- `event_time TEXT NOT NULL`
- `agent_name TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `entity_type TEXT NOT NULL`
- `entity_id TEXT NOT NULL`
- `ticker TEXT NOT NULL DEFAULT ''`
- `position_id TEXT NOT NULL DEFAULT ''`
- `order_id TEXT NOT NULL DEFAULT ''`
- `severity TEXT NOT NULL`
- `message TEXT NOT NULL`
- `before_json TEXT NOT NULL DEFAULT ''`
- `after_json TEXT NOT NULL DEFAULT ''`
- `metadata_json TEXT NOT NULL DEFAULT ''`

Constraints:
- append-only by trigger
- `event_type` constrained to current shared taxonomy:
  - `fill_processed`
  - `position_opened`
  - `position_closed`
  - `cash_adjusted`
  - `exit_decision_generated`
  - `equity_snapshot_recorded`
  - `validation_passed`
  - `validation_failed`
  - `run_started`
  - `run_completed`
  - `run_failed`

## 4. Run History Tables

### `run_history`
Purpose: authoritative pipeline lifecycle table, replacing `run_history.csv`.

Suggested columns:
- `run_id TEXT PRIMARY KEY`
- `started_at TEXT NOT NULL`
- `completed_at TEXT NOT NULL DEFAULT ''`
- `status TEXT NOT NULL CHECK (status IN ('running','success','failed'))`
- `failed_agent TEXT NOT NULL DEFAULT ''`
- `error_message TEXT NOT NULL DEFAULT ''`
- `notes TEXT NOT NULL DEFAULT ''`

Constraints:
- one row per `run_id`
- terminal states require `completed_at != ''`

### `run_reconciliation_summary`
Purpose: operator-facing post-run summary table, replacing `run_reconciliation_summary.csv`.

Suggested columns:
- `run_id TEXT PRIMARY KEY`
- `started_at TEXT NOT NULL`
- `completed_at TEXT NOT NULL`
- `status TEXT NOT NULL`
- `failed_agent TEXT NOT NULL DEFAULT ''`
- `fills_processed INTEGER NOT NULL`
- `positions_opened INTEGER NOT NULL`
- `positions_closed INTEGER NOT NULL`
- `positions_marked_exit_required INTEGER NOT NULL`
- `cash_delta REAL NOT NULL`
- `realised_pnl_delta REAL NOT NULL`
- `unrealised_pnl_delta REAL NOT NULL`
- `equity_delta REAL NOT NULL`
- `exposure_delta REAL NOT NULL`
- `validation_warning_count INTEGER NOT NULL`
- `validation_failure_count INTEGER NOT NULL`
- `notes TEXT NOT NULL DEFAULT ''`

## 5. Derived Analytics Tables

### `portfolio_equity_history`
Purpose: append-only derived history of portfolio equity.

Suggested columns:
- `timestamp TEXT NOT NULL`
- `run_id TEXT NOT NULL`
- `cash_balance REAL NOT NULL`
- `open_market_value REAL NOT NULL`
- `gross_exposure REAL NOT NULL`
- `net_exposure REAL NOT NULL`
- `unrealised_pnl_abs REAL NOT NULL`
- `realised_pnl_abs REAL NOT NULL`
- `total_equity REAL NOT NULL`
- `open_positions INTEGER NOT NULL`
- `closed_positions INTEGER NOT NULL`
- `peak_equity REAL NOT NULL`
- `drawdown_abs REAL NOT NULL`
- `drawdown_pct REAL NOT NULL`

Suggested key:
- surrogate `equity_history_id INTEGER PRIMARY KEY`
- unique `(run_id, timestamp)`

### `performance_summary`
Purpose: latest derived performance rollup.

Suggested columns:
- current CSV fields as one-row table or keyed by `as_of_run_id`
- likely better as:
  - `summary_id INTEGER PRIMARY KEY CHECK (summary_id = 1)`
  - latest summary columns

### `lifecycle_integrity_report`
Purpose: persisted validation report history.

Suggested columns:
- `report_row_id INTEGER PRIMARY KEY`
- `run_id TEXT NOT NULL`
- `checked_at TEXT NOT NULL`
- `record_type TEXT NOT NULL`
- `severity TEXT NOT NULL`
- `invariant_name TEXT`
- `rule TEXT`
- `position_id TEXT`
- `ticker TEXT`
- `detail TEXT`
- `total_checks INTEGER`
- `passed_checks INTEGER`
- `warning_count INTEGER`
- `failure_count INTEGER`

### `exit_advice`
Purpose: derived advisory output, non-authoritative.

Suggested columns:
- existing CSV fields
- add optional `run_id TEXT NOT NULL`

### `position_alerts`
Purpose: derived alert output.

Suggested columns:
- existing CSV fields
- add normal PK if persisted historically

## Transactional Boundaries

## Boundary 1: Fill Processing
This is the most important transaction boundary and should remain the sole economic mutation boundary.

One fill-processing transaction should include:
- insert into `processed_fills`
- insert into `cash_ledger`
- update `cash_state`
- insert/update `portfolio_positions`
- insert corresponding `event_log` rows

Semantics:
- all succeed or all roll back
- duplicate `fill_id` should fail on unique constraint before economic duplication occurs

## Boundary 2: Run Lifecycle Control
Pipeline runner should persist:
- `run_history` start row
- `run_started` event
then later:
- `run_history` terminal update
- `run_completed` or `run_failed` event
- `run_reconciliation_summary` upsert

These can be separate deterministic control transactions.

## Boundary 3: Derived Snapshot Production
Portfolio Equity Agent transaction:
- read canonical current state
- compute snapshot
- insert into `portfolio_equity_history`
- refresh latest `portfolio_equity_snapshot` or equivalent
- refresh `performance_summary`
- append `equity_snapshot_recorded` event

This must remain non-mutating with respect to `portfolio_positions` and `cash_state`.

## Boundary 4: Validation / Integrity Gate
Lifecycle Integrity Agent transaction:
- read canonical tables
- write `lifecycle_integrity_report`
- append validation event

No canonical state writes.

## Enforcing Closed-Position Immutability in SQLite

This is the key semantic protection.

Recommended approach:
- enforce in the application layer first, as today
- backstop with SQLite triggers

### Trigger policy
On `portfolio_positions`, create a `BEFORE UPDATE` trigger that rejects updates when:
- old row has `status = 'closed'`
- and any immutable economic field changes

Immutable fields should match current repository semantics:
- `status`
- `quantity`
- `entry_price`
- `entry_date`
- `closed_at`
- `exit_price`
- `realised_pnl_abs`
- `fees_total`

Trigger behavior:
- allow no-op updates
- allow maybe non-economic metadata changes only if explicitly desired
- otherwise `RAISE(ABORT, 'closed position economic fields are immutable')`

Important nuance:
- if the team wants fully frozen closed rows, extend trigger to reject all updates
- for semantic parity with current invariant logic, freeze at least the economic fields above

## Enforcing Append-Only Ledgers and Audit Logs

## `cash_ledger`
Enforce append-only with:
- `BEFORE UPDATE` trigger => abort
- `BEFORE DELETE` trigger => abort

## `event_log`
Enforce append-only with:
- `BEFORE UPDATE` trigger => abort
- `BEFORE DELETE` trigger => abort

## `processed_fills`
This is logically append-only too:
- `BEFORE UPDATE` trigger => abort
- `BEFORE DELETE` trigger => abort
- `PRIMARY KEY(fill_id)` ensures exactly-once semantics

## `portfolio_equity_history`
Likely append-only as well:
- `BEFORE UPDATE` and `BEFORE DELETE` triggers can be used if history should be immutable

## Key Constraints and Indexes

### `portfolio_positions`
- `PRIMARY KEY(position_id)`
- partial unique index on active `(ticker, side)`
- index on `status`
- index on `run_id`

### `processed_fills`
- `PRIMARY KEY(fill_id)`
- index on `run_id`

### `cash_ledger`
- `PRIMARY KEY(ledger_id)`
- index on `run_id`
- index on `position_id`
- index on `timestamp`

### `event_log`
- `PRIMARY KEY(event_id)`
- index on `run_id`
- index on `event_type`
- index on `(entity_type, entity_id)`
- index on `event_time`

### `run_history`
- `PRIMARY KEY(run_id)`
- index on `status`

### `portfolio_equity_history`
- unique `(run_id, timestamp)`
- index on `timestamp`

## Migration Phasing

## Phase 0: Additive Scaffolding
- introduce a shared DB path and connection helper only
- do not change agents yet
- do not change CSV behavior

## Phase 1: Dual-Write for Strong Tables
Start with:
- `processed_fills`
- `cash_ledger`
- `event_log`
- `run_history`

Reason:
- append-only semantics are easier to preserve
- lower risk than changing canonical state first

## Phase 2: Canonical State Dual-Write
Dual-write:
- `portfolio_positions`
- `cash_state`

Keep CSVs as operational outputs for rollback safety.

## Phase 3: Derived Tables from DB Reads
Move:
- `portfolio_equity_history`
- `performance_summary`
- `run_reconciliation_summary`
- `lifecycle_integrity_report`

## Phase 4: Read Path Cutover
Agents read SQLite first, optionally export CSV snapshots for compatibility.

## Phase 5: CSV Decommissioning
Only after:
- parity checks pass for multiple runs
- reconciliation against CSV outputs is stable
- operational tooling no longer depends on direct CSV reads

## Recommended Source-of-Truth Mapping

During migration:

- canonical truth for positions: `portfolio_positions` plus CSV parity export
- canonical truth for cash: `cash_state` plus CSV parity export
- canonical truth for processed fill dedupe: `processed_fills`
- canonical truth for economic cash trail: `cash_ledger`
- canonical truth for run lifecycle: `run_history`
- canonical truth for audit trail: `event_log`

Derived outputs remain reproducible and can still be exported as CSV for operator familiarity.

## Determinism Considerations

To preserve orchestration determinism:
- keep agent execution order unchanged
- keep one SQLite file for the local pipeline
- use explicit transactions at agent write boundaries
- avoid background writers
- avoid DB-generated timestamps for business semantics; keep application-generated ISO timestamps where current code already does that
- keep event IDs generated by application code, not DB auto IDs, if stable formatting matters

## Closed-Position and Non-Mutating-Agent Semantics

### Fill Agent
Still the only agent allowed to:
- mutate `portfolio_positions`
- mutate `cash_state`
- append `cash_ledger`
- append `processed_fills`

### Exit Agent
Read-only against canonical state.
Can only write:
- `exit_advice`
- `event_log`

### Portfolio Equity Agent
Read-only against canonical state.
Can only write:
- `portfolio_equity_history`
- `performance_summary`
- `event_log`

### Lifecycle Integrity Agent
Read-only against canonical state.
Can only write:
- `lifecycle_integrity_report`
- `event_log`

These boundaries should be documented in the DB access layer, not left implicit.

## Main Migration Risks

## Semantic drift
Risk:
- SQLite constraints may accidentally become stricter than today's CSV behavior

Examples:
- `exit_flag` currently inconsistent in stored values
- Exit Agent advice vocabulary still drifts from some shared schema expectations

Mitigation:
- preserve current accepted values first
- standardize later, after parity

## Partial transaction behavior changes
Risk:
- CSV writes today may implicitly allow partial persistence in failure cases
- DB transactions will make writes atomic

Mitigation:
- this is desirable, but call it out as an operational change and test carefully around Fill Agent

## Historical CSV inconsistency
Risk:
- legacy files may contain blanks, type drift, duplicates, or stale columns that violate proposed constraints

Mitigation:
- run pre-migration audits before loading
- allow staging tables for messy imports
- validate then promote

## Trigger overreach
Risk:
- overly strict immutability triggers could block legitimate non-economic metadata updates

Mitigation:
- start by freezing only the exact economic fields already treated as immutable

## Tooling compatibility
Risk:
- existing scripts and operators may rely on opening CSV files directly

Mitigation:
- keep CSV exports during migration
- add parity checks between DB and exported CSVs

## Read-path split brain
Risk:
- some agents reading CSV while others read DB can create inconsistent behavior

Mitigation:
- dual-write first, then coordinated read cutover by table group

## Recommended First SQLite Targets

Best first candidates:
1. `run_history`
2. `processed_fills`
3. `cash_ledger`
4. `event_log`

Reason:
- structurally simple
- append-only or single-row semantics
- lower risk than moving `portfolio_state` first
- strong control/audit value immediately

Most sensitive table:
- `portfolio_positions` because it carries the core economic state and closed-position immutability rules

## Summary

The practical migration path is:

1. move append-only control/audit tables first
2. dual-write canonical economic state next
3. migrate derived analytics after canonical state is stable
4. enforce invariants with DB constraints and triggers, especially:
   - unique processed fills
   - one run row per run
   - append-only ledgers/events
   - closed-position economic immutability
5. keep CSV exports during migration so current pipeline semantics and operator workflows remain intact

If you want, the next step can be a Phase 1 SQLite schema spec written as actual `CREATE TABLE` statements without wiring any code yet.
