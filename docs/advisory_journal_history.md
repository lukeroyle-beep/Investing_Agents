# Preserving legacy advisory Journal history

The current Journal uses `(run_id, ticker)` as the identity of advice. Older
files can contain observations without a run ID, or equivalent keyed retries
with different journal timestamps. The Journal still rejects these inputs; it
does not silently repair history during an ordinary pipeline run.

`scripts/migrate_advisory_journal.py` is a separate, reviewed migration. Its
default dry run reads the exact source and returns counts, hashes, and the
canonical absolute source path without writing a lock, archive, or journal:

```sh
python scripts/migrate_advisory_journal.py --source /absolute/private/runtime/state/trade_journal.csv
```

Review the dry run against the original records before applying. Apply requires
both returned SHA-256 hashes, the exact reviewed clean code commit, a review
reference, and an explicit `--writers-stopped` attestation. Stop old pipeline
processes first: older checkouts do not use the new Journal lock. No broker,
economic ledger, inception, execution journal, mandate, or kill switch changes.

The migration retains the exact original bytes at
`trade_journal.csv.history/<source-sha256>/original.csv`. Its immutable
`plan.json` maps **every original data row**, including every journal timestamp
and row hash, to its operational row or an unresolved historical observation.
`operational.csv` retains the reviewed initial keyed view. `prepared.json`
binds the source path, hashes, code revision and review; `completed.json` is
created only after atomic source replacement and successful readback. These
private files belong with the original runtime backup and must stay out of Git.

Missing run IDs or tickers are never fabricated. All such rows remain
individually preserved as `unresolved_missing_identity` in the original archive
and row map, outside the operational keyed view. They are historical coverage
gaps, not deduplicated retries or newly verified pipeline runs. Conflicting
keyed advice, manual fields, or extra columns stop the migration. Only keyed
rows identical in **every field except `journaled_at`** may share an operational
row; the earliest timezone-aware timestamp is retained, with every other
timestamp still in the archive and row map. Empty review fields are not
wildcards, and numeric/string formatting differences are not guessed away.

The current Journal shares a process lock with the migration and validates
retained history before writing new advice. An interrupted migration blocks
new Journal writes. Retrying the same plan can finish from its saved original
bytes when the current source is still either the exact original or the exact
planned view. An existing partial or conflicting archive is never overwritten;
inspect it before further recovery. A completed retry does not remove new
advice subsequently appended to the journal.

The migration is a history-preserving repair, not evidence of a live pipeline
run or broker fill ingestion. File hashes detect ordinary corruption; they do
not protect against coordinated deletion or replacement of all local history.
Rollback requires a reviewed restore from `original.csv` while writers are
stopped, preserving any newer advice and the migration receipts. Do not blindly
copy an old journal over new rows.
