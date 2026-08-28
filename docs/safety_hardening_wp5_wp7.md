# WP5–WP7 Safety Contracts

## Fail-closed finalisation

`run_pipeline.py` creates one run-history row and one pre-economic checksum
record before agent work. A run may then transition only through:

```text
started -> validating -> succeeded | failed
```

`shared.run_finalizer` owns terminal validation. It requires the registered CSV
artifacts, schema validity, lifecycle success, centralized data-health mode,
explained economic changes, reconciliation, and required CSV/SQLite parity.
Failure at any point is redacted and persisted as terminal `failed` by the
orchestrator. No completion event is emitted before finalisation.

The versioned manifest records each required artifact's relative path,
registered producer, schema version, production timestamp, SHA-256, row count,
and required flag, plus pre/post economic-state checksums. The separate
finalisation record pins the manifest hash and is the durable success proof.

Required parity covers event log, run history, run reconciliation, cash state,
portfolio state, trade fills, cash ledger, processed fills, and equity history.
CSV remains authoritative; SQLite is a required mirror, not the economic write
authority.

## Bootstrap, backup, migration, and recovery

`tools/bootstrap_runtime.py` atomically creates a schema-current, synthetic,
zero-position baseline. It does not call market providers or execute the
pipeline. It is intended for clean software validation only.

`tools/backup_runtime.py` writes a permission-restricted ZIP, internal file
manifest, and external SHA-256 sidecar under the configured runtime. Restore
rejects external archives, permissive modes, missing/mismatched checksums,
duplicate or unexpected members, traversal, and symbolic links. A restore
always preserves a new pre-restore backup.

`tools/migrate_runtime_schemas.py` always creates and verifies a backup, changes
a copied state directory, normalizes legacy run statuses and provider-health
rows, rebuilds SQLite from CSV authority, validates the result, and only then
installs it. Re-running it is safe and still creates a new recovery backup.

`tools/resolve_interrupted_run.py` defaults ambiguous `started` or `validating`
runs to `failed`. It permits `succeeded` only when the complete finalisation
record and all referenced state validate. Resolutions are audited under
`runtime/control/`.

## Freshness and operating modes

`config/freshness.yaml` is the centralized policy. `shared.freshness` uses an
injected clock and XNYS calendar rules for weekends, US holidays, early closes,
and daylight-saving transitions. Missing, malformed, future, stale, or
materially contradictory critical inputs become `no_trade`; non-critical
failures become `degraded`.

Research agents consume the centralized outcome and skip non-actionable data.
The Data Freshness Gate runs before portfolio construction. Provider failures
are summarized and credential-shaped values are redacted. yfinance remains a
research source; it is never execution-price authority.
