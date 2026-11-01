# Consolidated P2 repairs for PRs #20–22

Base: fresh `origin/master` at `4d46d6c4e10d446f26024592b570efe452811d9c`,
containing merged #18 (finite execution), #19 (usage floor/reserve),
#20 (advisory history), #21 (read-only inspection), and #22 (external lifecycle).
The unchanged base passed 2,333 Python tests locally. All new inputs and runtime
stores used for this repair are disposable synthetic fixtures.

## Findings, reproduction and fixes

| Reported finding | Local pre-fix evidence | Repair and regression coverage |
| --- | --- | --- |
| [#20: batch normalization](https://github.com/lukeroyle-beep/Investing_Agents/pull/20#discussion_r4409346443) | Four deterministic failures: keyed batches of 2, 32 and 1,000 were normalized one row at a time; recurring archive validation repeated those calls. | One keyed batch supplies identities; original rows still supply conflicts, output and hashes. Tests retain one canonical-output validation, frozen pre-repair plan/output bytes, manual/extra-column conflicts, missing keys, tie ordering, prepared/completed archive recovery and appended advice. |
| [#21: rollback journal](https://github.com/lukeroyle-beep/Investing_Agents/pull/21#discussion_r4409493209) | A subprocess exits without committing after spilling a correctly digested economic event into the main SQLite file. The bytes pass `quick_check`; recovering a disposable copy removes the event. The original inspector nevertheless accepts the source. | Every original store's rollback journal is checked before any SQLite deserialization. Hot or ambiguous headers reject; absent, empty and fully zeroed cold headers remain supported. Tests check real DELETE/TRUNCATE/PERSIST states, malformed/truncated headers, all three stores, WAL rejection, exact unchanged sources/sidecars and sanitized invalid-evidence CLI output. |
| [#22: accepted account checkpoint](https://github.com/lukeroyle-beep/Investing_Agents/pull/22#discussion_r4409941156) | Both open and no-fill reconciliation reject valid source-rebuilt 100 ms captures after import, while zero-duration controls pass. Two rollback tests also fail at that premature checkpoint check. | The internal reconciliation mark binds the command's exact accepted post-account and current checkpoint. Other captures retain freshness, source, overlap and cash/holdings checks. Tests cover both durations and paths, altered captures, stale/overlapping marks, later checkpoints, terminal accounting, rollback, replay and idempotent journal recovery. |

These are local reproductions, separate from the original review reports. Early
rollback-fixture construction failures were corrected before the successful
pre-fix reproduction; they are not counted as evidence of the production defect.

The rollback policy follows SQLite's [rollback-journal commit semantics](https://www.sqlite.org/lockingv3.html):
PERSIST clears the header at commit. The inspector accepts a nonempty sidecar
only when its complete 28-byte header is zero; it conservatively rejects short,
partially initialized or nonzero headers. It does not infer safety from lock or
super-journal state, open the original stores with SQLite, or perform recovery.
The regression's recovery operation is confined to a disposable fixture copy.

## Reproducible history benchmark

Run the same script against the before/after code, using the frozen environment:

```sh
.venv/bin/python scripts/benchmark_advisory_history.py --rows 1000 --repeats 3
```

The script creates a temporary 1,000-keyed-row CSV and completed archive. It times
planning and repeated `validate_journal_history` calls independently, including
archive reading and reconstruction. For the base measurement the script was
added before changing the production implementation; its content was identical
for both runs. Python 3.12.13, pandas 3.0.5, macOS 26.6.2 arm64:

| Operation | Before, seconds (3 samples) | After, seconds (3 samples) | Median before → after |
| --- | --- | --- | --- |
| Planning | 1.568701, 1.564847, 1.553704 | 0.017997, 0.012653, 0.012403 | 1.564847 → 0.012653 |
| Recurring archive validation | 1.543457, 1.522593, 1.506791 | 0.013895, 0.013972, 0.013526 | 1.522593 → 0.013895 |

Timings describe this local run and have no pass/fail threshold. Deterministic
validation-call-count tests enforce two calls for any nonempty keyed set (one
keyed batch plus the existing canonical check), and none for wholly unkeyed
history. The frozen synthetic archive fixture records its base commit and
preserves the old plan hash, row mappings and original text exactly.

## Validation and boundaries

Focused coverage is in `test_advisory_journal_migration.py`,
`test_journal_agent.py`, `test_external_reconciliation_admission.py`,
`test_external_reconciliation_checkpoint.py`, `test_external_lifecycle.py`,
`test_pilot_ledger.py`, `test_finite_demo_mandate.py`,
`test_finite_demo_operator.py` and `test_execution_journal.py`.
The full suite also exercises usage reserves, dispatch/approval/freshness gates,
kill switch, finite migration, execution journalling and recovery from #18–22.

Final-commit local and hosted results and independent exact-head review are
recorded on the repair PR. Applicable CI comprises lock/export verification,
Python 3.11–3.13 on Linux/macOS (pytest, Node relay, deptry, compile, pip check),
and both platforms' strict dependency audit and complete-history Gitleaks scan.
Missing or pending checks must not be reported as passing.

This repair changes no approved limits, raw financial evidence, inception,
existing claims or real runtime. Public external lifecycle import remains
disabled pending authoritative source qualification. No broker orders, live
migration, trading activation or merge are part of the repair. Passing synthetic
checks does not establish autonomous Demo readiness.
