# SQLite Dual-Write Phase 1

This repository still uses CSV as the active source of truth.

The first-pass SQLite sidecar is additive only:

- CSV reads remain authoritative everywhere
- CSV writes still happen first
- SQLite writes are best-effort dual writes for low-risk append-only or control-oriented data

## Included In SQLite

- `event_log`
- `run_history`
- `run_reconciliation_summary`
- `cash_ledger`
- `processed_fills`
- `portfolio_equity_history`

## Not Yet In SQLite

- `portfolio_state.csv` remains CSV-only and authoritative
- `cash_state.csv` remains CSV-only and authoritative
- advisory and strategy output CSVs remain CSV-only unless explicitly migrated later

## Integration Points

- `shared/event_log.py`: append-only event rows dual-write into SQLite
- `shared/run_history.py`: run lifecycle rows upsert into SQLite
- `shared/run_reconciliation.py`: reconciliation summaries upsert into SQLite
- `agents/fill_agent/fill_agent.py`: processed fills and cash ledger rows dual-write into SQLite
- `agents/portfolio_equity_agent/portfolio_equity_agent.py`: latest validated equity-history row dual-writes into SQLite

## Operational Notes

- The SQLite file lives at `runtime/state/trading_system.sqlite3`
- SQLite dual-write is intentionally best-effort for this phase
- If a SQLite write fails, CSV behavior continues and a warning is emitted
- This keeps daily workflow stable while allowing migration validation and parity testing
