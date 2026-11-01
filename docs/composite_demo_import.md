# Bounded historical Demo reconciliation

`scripts/import_composite_demo.py` provides a reviewed source adapter and public
plan/apply/recovery path for eight known inception-position closures and one
original, zero-cost real-long IOC opening. It uses the existing Fill ledger and
execution journal. It does not authorize a broker request or enable trading.

The supported accounting basis is **net cash effects**, not an enumerated cash
statement. Retained broker History totals and their displayed formula establish
an aggregate net fee-minus-dividend adjustment. Actual cash observations bound
its net effect; those bounds are not individual posting timestamps. The UI's
reporting dates remain separate, with no inferred timezone. Gross fees,
dividends and individual attribution remain unknown. Explicit fee details and
obscured column headings retain those distinctions. Zero residual alone cannot
qualify a source bundle.

Original account receipts, exact history rows, original consumed connector
records, independent opening cash/position readback and detailed position fields
are cross-checked. The original execution investment and independent debit take
precedence over a conflicting later investment field. The contradiction is
retained, without substituting margin or silently changing settlement strings.
Other unexplained identity, principal, cost or position changes stop admission.

All source files and reviewed screenshot transcriptions are private and bound
to their original hashes. Images and safe source bodies accompany the economic
record; preview approval material is excluded. The transcript and exact manifest
must be independently checked against the images. Source-byte hashing is an
integrity check, not automatic proof that a transcription is accurate.

## Plan and review

Use the original runtime and a `composite-retained-evidence-v1` manifest. The
manifest contains original-runtime bindings, a file map and groups for both
account brackets, history, the original opening/lookup, and reviewed image
observations. The private schema is exercised with unrelated synthetic values
in `tests/test_composite_source.py`. No live account identifiers or receipts
belong in Git.

```sh
python scripts/import_composite_demo.py plan \
  --runtime /absolute/original/runtime \
  --manifest /absolute/private/source-manifest.json \
  --reviewed-commit FULL_REVIEWED_COMMIT \
  --review-reference EXACT_INDEPENDENT_REVIEW_REFERENCE
```

Planning deserializes original databases into memory, checks source bytes again
before returning, and emits a sanitized plan digest. It does not open original
SQLite stores or change files. The code, manifest, image extraction and exact
plan digest all require independent review before application.

## Application and recovery

Apply requires a clean checkout at the reviewed commit, exact manifest and plan
hashes, the original existing stores, owner-only operator lock and an engaged
valid kill switch. It never bootstraps or migrates the operational store. It may
create the separate execution journal once; existing, corrupt or missing
previously used journals are never replaced. A missing or disengaged switch is
a stop, not permission to initialize or reset one.

```sh
python scripts/import_composite_demo.py apply \
  --runtime /absolute/original/runtime \
  --manifest /absolute/private/source-manifest.json \
  --reviewed-commit FULL_REVIEWED_COMMIT \
  --review-reference EXACT_INDEPENDENT_REVIEW_REFERENCE \
  --expected-manifest-sha256 REVIEWED_MANIFEST_SHA256 \
  --expected-plan-sha256 REVIEWED_PLAN_SHA256
```

The Fill transaction appends a v2 lifecycle event while preserving v1 record
shapes, inception, existing submissions and old events. Closures retire matched
baseline positions; their gains and the account-period aggregate do not become
pilot performance. The source-proven opening principal and costs count toward
pilot limits. No BrokerCommand, Fill, order intent or submission is fabricated.
This establishes no historical daily P&L or drawdown qualification.

Historical application validates the account at its retained capture time. It
does not refresh that account or weaken execution freshness checks. The kill
switch remains engaged. New execution still needs current account, eligibility,
cost, quote, session and usage evidence, and an exact approved finite mandate.
Subsequent unexplained cash changes remain blocking.

After economic commit, a journal obligation blocks execution until the journal
chain is independently read back and an append-only completion receipt is
recorded. If application stops or its response is lost, inspect the original
economic event before doing anything else. Recovery uses its exact plan digest:

```sh
python scripts/import_composite_demo.py recover \
  --runtime /absolute/original/runtime \
  --reviewed-commit FULL_REVIEWED_COMMIT \
  --event-id EXISTING_LOCAL_EVENT_ID \
  --expected-plan-sha256 EXISTING_REVIEWED_PLAN_SHA256
```

Recovery never rereads a new source plan, reapplies economics or resends a trade.
Repeat recovery is idempotent. Journal corruption or rollback remains a hold.
Code rollback is separate from economic history: never delete the import or
restore an older ledger to undo it.
