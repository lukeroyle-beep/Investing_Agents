# Operator Runbook

This project is advisory-only. It never submits broker orders and it must not be used as an autonomous execution system.

## Clean-machine bootstrap

1. Install Python 3.11+.
2. Create and activate a virtual environment outside any checked-in `.venv`.
3. Install dependencies:

```bash
python -m pip install -r requirements.txt -r requirements-test.txt
```

4. Run the validation suite before trusting local state:

```bash
python -m pytest
```

## Pre-run checklist

Before running `run_pipeline.py`:

1. Confirm `data/portfolio_state.csv` exists and is the intended canonical state.
2. Confirm there is no unresolved `running` row in `data/run_history.csv`.
3. Confirm any real-world fills have been manually reviewed before they are placed in the fill input path.
4. Confirm the run is advisory-only and no broker automation is connected.
5. Optionally set an explicit run id for traceability:

```bash
export TRADING_PIPELINE_RUN_ID=RUN_YYYYMMDDTHHMMSSZ
```

## Run command

```bash
python run_pipeline.py
```

Expected behaviour:

- `run_history.csv` receives one new `running` row at start.
- Successful runs move that row to `success`.
- Failed runs move that row to `failed` with failure details.
- A new run is blocked if a previous row is still `running`.

## Post-run checklist

1. Review the printed reconciliation summary.
2. Review SQLite parity output.
3. Check `data/run_history.csv` for a terminal status: `success` or `failed`.
4. Review append-only audit events for unexpected warnings or errors.
5. Review advisory outputs manually before any real-world action.

## Interrupted-run handling

The pipeline now fails closed when `run_history.csv` contains a previous `running` row. Treat that as an interrupted or concurrent run until proven otherwise.

Do **not** start a new run by editing around the guard casually. First determine whether the interrupted run changed economic state.

Recommended triage:

1. Identify the unresolved run id in `data/run_history.csv`.
2. Review audit log entries for that run id.
3. Review fill, portfolio state, reconciliation, and parity outputs for that run id.
4. If no economic mutation occurred, manually mark the run failed with a clear note in `run_history.csv` and rerun validation.
5. If economic mutation may have occurred, do not replay blindly. Reconcile portfolio state, fills, cash movement, realised PnL, and SQLite parity first.
6. Only retry once the canonical CSV state and shadow SQLite state are understood and stable.

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

Each market-data provider call should emit a row into `data/data_source_health.csv` with ticker, source, error, stale flag, retry count, fetch time, and provider `as_of` time.

Current behaviour:

- Universe Agent writes a fresh health artifact at the start of a pipeline run.
- Signal, Macro, News, and Backtesting append their own provider checks instead of overwriting earlier evidence.
- `shared.mission_control_data_health.build_data_source_health_card()` converts the artifact into an operator card: `OK`, `Hold`, or `Missing`.

Operator rule: any provider error or stale row is a Mission Control hold until reviewed. Advisory output should not be treated as clean when the health card is `Hold` or `Missing`.

## Manual repair boundaries

Allowed manual repairs:

- marking an unresolved run as `failed` after investigation
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
- Parity failure: keep CSV authoritative; compare SQLite shadow rows and repair via controlled re-sync, not direct economic mutation.
- Reconciliation failure: inspect cash, realised/unrealised PnL, exposure, and activity-count deltas.
- Missing critical file: restore the file from source control or backup; do not fabricate economic state.
