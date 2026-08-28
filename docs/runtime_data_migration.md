# Runtime Data Migration

## WP2 migration record

The legacy tracked `data/` runtime was quarantined on 2026-08-28 before any
index or path change.

- Backup convention: sibling `../Investing_Agents-backups/<UTC>/`
- WP2 backup: `../Investing_Agents-backups/20260828T083953Z/`
- Files copied: 34
- Directory mode: `0700`; file modes: `0600`
- Verification: source and backup SHA-256 manifests were identical, and every
  copied file passed a checksum verification from the backup directory.

The tracked migration record deliberately contains no portfolio rows, database
content, credentials, or file hashes.

## Directory ownership

```text
runtime/
  state/       canonical CSV state and the SQLite mirror
  runs/        run-scoped artifacts and manifests
  control/     persistent operator controls
  cache/       replaceable provider caches
  logs/        local operational logs
  backups/     optional local recovery copies
```

`runtime/` and the legacy `data/` path are ignored. Sanitized inputs belong in
`tests/fixtures/`; deliberate research inputs belong in `data_sources/`.

The default runtime root is `<repository>/runtime`. An isolated invocation can
set `INVESTING_RUNTIME_DIR` to another root before Python imports project
modules. The override changes the whole tree, so its state directory is always
`$INVESTING_RUNTIME_DIR/state`.

## Restore procedure

1. Stop all pipeline and agent processes.
2. Confirm the target runtime has no active or unresolved run.
3. Create and verify a second backup of the current target state.
4. Inspect `TRACKED_FILES.txt`; reject absolute paths, `..`, links, devices, or
   any entry outside the expected `data/` subtree.
5. From inside the selected external backup, verify `BACKUP_SHA256SUMS`.
6. Restore the backed-up `data/` contents into a temporary `state/` directory,
   preserving filenames but removing the legacy `data/` prefix.
7. Validate schemas, event log, reconciliation, and CSV/SQLite parity against
   the temporary runtime before replacing `runtime/state/`.
8. Replace the state directory only under an explicit recovery operation.

WP2 records and relocates the existing state; automated restore and interrupted
run resolution remain WP6. Until those tools exist, restoration is an
operator-controlled procedure and the canonical pipeline must not be used as a
repair mechanism.

## Security notes

- No credentials were discovered by the WP2 redacted pattern scan of the
  working tree or Git history. Gitleaks was not installed locally; full scanner
  enforcement is part of WP3 CI.
- Do not print file contents, credential values, or raw broker responses during
  backup/restore work.
- A discovered credential must be treated as compromised. Rotation or Git
  history rewriting requires separate authorization.
