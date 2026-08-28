# Operator Runbook

This project is advisory by default. It contains a manually approved eToro Demo
execution path, but every broker-write and scheduler switch is disabled in the
checked-in configuration. It must not be used as an autonomous execution
system, and it contains no Real-money execution path.

## Clean-machine bootstrap

1. Install `uv` 0.11.6.
2. Synchronize the locked Python 3.12 environment:

```bash
uv sync --frozen --all-groups
```

3. Run the validation suite before trusting local state:

```bash
uv run --frozen python -m pytest -q
uv run --frozen deptry .
uv run --frozen python -m compileall -q agents shared tools scripts run_pipeline.py
uv run --frozen python -m pip check
uv run --frozen python -m pip_audit --local --strict
```

`pyproject.toml` declares project and development dependencies, and `uv.lock`
is authoritative. `requirements.txt` is a generated, hash-pinned UTF-8 export
for compatibility only. Regenerate it after an intentional lock update with:

```bash
uv export --quiet --frozen --no-dev --format requirements.txt --no-annotate --no-header --output-file requirements.txt
```

Never edit `requirements.txt` by hand. CI rejects lock drift and export drift.

Runtime output is never stored in Git. The default runtime root is `runtime/`,
with canonical CSV and SQLite state under `runtime/state/`. Set
`INVESTING_RUNTIME_DIR` to an absolute directory when an isolated runtime is
required; relative overrides are resolved from the repository root.

For a new offline installation, create a deliberately synthetic zero-position
baseline. This does not call any provider or run the pipeline:

```bash
uv run --frozen python tools/bootstrap_runtime.py
uv run --frozen python scripts/nightly_checklist.py
uv run --frozen python tools/validate_event_log.py
uv run --frozen python tools/validate_runtime_artifacts.py
uv run --frozen python tools/validate_sqlite_parity.py
```

The bootstrap starts with USD 100,000 of explicitly synthetic cash. It is a
software-validation fixture, not a representation of an account. Never use it
to replace real economic state.

## Pre-run checklist

Before running `run_pipeline.py`:

1. Confirm `runtime/state/portfolio_state.csv` exists and is the intended canonical state.
2. Confirm `runtime/state/cash_state.csv` exists and contains the intended canonical cash balance.
3. Confirm there is no unresolved `started` or `validating` row in `runtime/state/run_history.csv`.
4. Confirm any real-world fills have been manually reviewed before they are placed in the fill input path.
5. Confirm the run is advisory-only unless an exact Demo command has received
   its own current human approval; scheduling never supplies that approval.
6. Optionally set an explicit run id for traceability:

```bash
export TRADING_PIPELINE_RUN_ID=RUN_YYYYMMDDTHHMMSSZ
```

## Run command

```bash
uv run --frozen python run_pipeline.py
```

Expected behaviour:

- `run_history.csv` receives one new `started` row at start.
- Finalisation moves the row to `validating` while checking artifacts,
  reconciliation, economic checksums, lifecycle evidence, data freshness, and
  required CSV/SQLite parity.
- Successful runs move that row to `succeeded` only after those checks pass.
- Failed runs move that row to `failed` with failure details.
- A new run is blocked if a previous row is still `started` or `validating`.
- Success has durable proof in
  `runtime/runs/<run_id>/artifact_manifest.json` and
  `runtime/runs/<run_id>/run_finalization.json`.

## Economic-state and mark ownership

The Fill Agent is the sole writer of canonical holdings, cash, cash-ledger, and processed-fill state. Managed writes compare the declared producer with the schema-registry owner and fail closed on an absent, unknown, or conflicting identity.

Position Tracking reads `portfolio_state.csv` and atomically regenerates `portfolio_monitor.csv`. It does not rewrite canonical holdings. Current price, market value, unrealised P&L, and high/low marks are authoritative only in `portfolio_monitor.csv`; similarly named columns retained in canonical state are legacy Fill snapshots.

Exit and Portfolio Equity require the monitor to cover every active canonical position exactly. A missing position, unexpected position, duplicate identifier, or contradiction in ticker, side, status, quantity, or entry price stops the consumer rather than falling back to stale canonical-state marks. Resolve this by rerunning Position Tracking after confirming canonical state—not by editing either file by hand.

## Post-run checklist

1. Check `runtime/state/run_history.csv` for a terminal status: `succeeded` or `failed`.
2. Validate `runtime/runs/<run_id>/run_finalization.json` and its manifest.
3. Run the nightly checklist, event-log validator, and required parity check.
4. Confirm `portfolio_state.csv`, `cash_state.csv`, `cash_ledger.csv`, and `processed_fills.csv` did not change after the Fill Agent completed.
5. Review append-only audit events for unexpected warnings or errors.
6. Review advisory outputs manually before any real-world action.

## Interrupted-run handling

The pipeline fails closed when `run_history.csv` contains a previous `started`
or `validating` row. Treat that as interrupted or concurrent until proven
otherwise.

Do **not** start a new run by editing around the guard casually. First determine whether the interrupted run changed economic state.

Recommended triage:

1. Identify the unresolved run id in `runtime/state/run_history.csv`.
2. Review audit log entries for that run id.
3. Review fill, portfolio state, reconciliation, and parity outputs for that run id.
4. Resolve it with the controlled resolver; do not edit the row directly:

```bash
uv run --frozen python tools/resolve_interrupted_run.py RUN_ID
```

5. The resolver defaults the run to `failed`. It preserves `succeeded` only
   when the complete finalisation record, manifest, required artifact hashes,
   and post-economic checksums already validate.
6. If economic mutation may have occurred, do not replay blindly. Reconcile
   portfolio state, fills, cash movement, realised PnL, and SQLite parity first.
7. Only retry once canonical CSV state and the SQLite mirror are understood and stable.

## Duplicate-fill prevention after interruption

If interruption happened after the Fill Agent started:

- verify whether fills were already written to `processed_fills.csv`
- verify whether `portfolio_state.csv` changed
- verify whether matching audit events exist
- verify whether matching cash movements exist in `cash_ledger.csv`
- verify SQLite parity for affected rows
- do not re-submit or replay the same fills until idempotency has been proven for that exact run context

### Fill Agent interrupted-run recovery guard

Before processing a fill from `manual_fills.csv`, the Fill Agent compares every unprocessed `fill_id` against existing economic evidence in `processed_fills.csv`, `event_log.csv`, `cash_ledger.csv`, `portfolio_state.csv`, and the SQLite sidecar when present.

Recovery decision rule:

- If the `fill_id` is present in canonical `processed_fills.csv`, replay is treated as already processed and the fill is skipped.
- If the `fill_id` is missing from canonical `processed_fills.csv` and there is matching audit, cash-ledger, portfolio-state, or SQLite evidence that the fill already mutated state, the Fill Agent fails closed and names the evidence found.
- If there is no processed marker and no mutation evidence, the Fill Agent may process the fill as first-time input.

When the guard fails closed, do **not** auto-mark the fill processed and do **not** replay blindly. Manually reconcile `portfolio_state.csv`, `cash_ledger.csv`, `event_log.csv`, `processed_fills.csv`, and SQLite parity. Then either restore/repair the missing processed-fill marker under documented control, or restore the affected state from backup before retrying.

## Data-source health and Mission Control holds

Each market-data provider call emits centralized evidence into
`runtime/state/data_source_health.csv`: source, data kind, observation and
retrieval timestamps, exchange session/calendar, freshness outcome,
contradiction status, operating mode, redacted reason, retry count, and the
legacy `fetched_at`/`as_of` compatibility fields.

Current behaviour:

- Universe Agent writes a fresh health artifact at the start of a pipeline run.
- Signal, Macro, News, and Backtesting append their own provider checks instead of overwriting earlier evidence.
- The Data Freshness Gate runs after research collection and before portfolio
  construction. Critical missing, malformed, future, stale, or contradictory
  evidence produces `no_trade` and stops the pipeline.
- Non-critical policy failures may produce `degraded`; they remain visibly
  non-normal and cannot silently become fresh.
- `shared.mission_control_data_health.build_data_source_health_card()` converts
  the artifact into `OK`, `Degraded`, `No Trade`, `Hold`, or `Missing`.

Default policies are: broker quote 30 seconds, account/order snapshot 15
seconds, daily research price through the latest completed XNYS session, and
approval age five minutes. yfinance is research-only and never substitutes for
a broker execution quote.

## Execution and eToro Demo safety gates

The checked-in `config/execution.yaml` and
`config/brokers/etoro_demo.yaml` keep intent import, broker writes, and eToro
connectivity disabled. Do not enable them as a routine pipeline step.

- `runtime/control/execution.sqlite3` is an operational command ledger, not an
  economic-state authority.
- Ticker-only portfolio orders cannot become intents; register and review the
  immutable internal instrument UUID first.
- Missing or corrupt kill-switch state is engaged by definition. Do not repair
  it by hand or delete its audit.
- An approval is per intent, per environment, short-lived, and single-use.
- A current accepted risk decision, open regular session, fresh eToro quote,
  fresh account evidence, valid approval, reset kill switch, and enabled Demo
  write configuration are all required before a command may become
  `submission_pending`.
- Gate A account snapshots are reconciliation read models only. They never
  overwrite Fill-owned holdings or cash.
- `unknown` broker outcomes must be reconciled by persisted logical request and
  broker identities before any retry. A new request identity is not a recovery
  mechanism.

Gate A live read-only connectivity has been smoke-tested. Gate B is
offline-contract complete but remains disabled. `config/brokers/etoro_demo_write.yaml`
uses separate `ETORO_DEMO_WRITE_*` credential names, and a live mutation still
requires a separately reviewed exact request and one fresh human approval.

The v3 open and cancel operations are asynchronous. A 202 response is never a
fill or cancellation result. Reconcile through the persisted order/reference
identity; a timeout or lost response enters `unknown` and must not be retried
with a new request ID. Broker-confirmed fills enter `broker_fills.csv` as
non-economic staging evidence, then Fill Agent applies each execution identity
at most once.

Scheduled Demo preparation is disabled in `config/scheduler.yaml` and has no
broker-submit capability. Its approval waits expire after five minutes. Check
Gate C evidence with:

```bash
uv run --frozen python tools/demo_qualification_status.py
```

Nonzero status is expected until 30 clean trading sessions, 20 manually
approved Demo mutation cycles, and all fault drills have genuinely completed.
See `docs/demo_execution_strategy_scheduler_wp11_wp13.md`.

Backtests are captured as immutable experiments. Promotion grants advisory
eligibility only; static-universe, look-ahead, survivorship, calendar, cost,
benchmark, walk-forward, or reproducibility failures block promotion.

## Manual repair boundaries

Allowed manual repairs:

- resolving an interrupted run with `tools/resolve_interrupted_run.py`
- restoring from a known-good backup
- correcting clearly malformed fixture/test data before a run

High-risk repairs requiring extra caution:

- editing closed positions
- editing realised PnL
- editing cash balances
- deleting audit events
- deleting processed fill records
- altering SQLite directly without matching CSV reconciliation

Closed economic history should be treated as immutable unless a documented correction process is followed.

## Failure triage quick map

- Schema failure: fix malformed input; rerun tests before pipeline retry.
- Lifecycle failure: inspect status transition and closed-position immutability evidence.
- Monitor failure: verify exact active-position coverage and copied economic context, then rerun Position Tracking.
- Parity failure: keep CSV authoritative; compare SQLite shadow rows and repair via controlled re-sync, not direct economic mutation.
- Reconciliation failure: inspect cash, realised/unrealised PnL, exposure, and activity-count deltas.
- Missing critical file: restore it from a checksum-verified runtime backup or
  an explicitly documented synthetic bootstrap; runtime state is not available
  from source control and economic state must not be fabricated.

## Runtime backup and migration boundary

- Repository fixtures under `tests/fixtures/` are synthetic and must never be
  replaced with portfolio or broker output.
- `data_sources/stock_universe.csv` remains a deliberately versioned research
  input. Everything below `runtime/` is local and ignored.
- Before moving or repairing runtime state, create a permission-restricted
  checksum-verified backup:

```bash
uv run --frozen python tools/backup_runtime.py --label operator
uv run --frozen python tools/restore_runtime.py runtime/backups/<archive>.zip
```

- Restore accepts archives only from the configured runtime's `backups/`
  directory and rejects checksum mismatch, path traversal, symbolic links,
  unexpected members, and overly permissive archive modes. It creates a
  verified pre-restore backup automatically.
- Run schema migration with
  `uv run --frozen python tools/migrate_runtime_schemas.py`. It always backs up
  first, transforms a copy, rebuilds the SQLite mirror from CSV authority, and
  installs the verified copy only after validation.
- Never restore state over an active run. Stop the pipeline, preserve the
  current runtime as a second backup, validate the archive paths and checksums,
  then restore into `runtime/state/`.
- The WP2 migration record and recovery details are in
  `docs/runtime_data_migration.md`.
