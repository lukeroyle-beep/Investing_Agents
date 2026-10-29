# External Demo lifecycle mechanics (application unavailable)

This implementation provides internal append-only economic and journal recovery
mechanics. **It does not provide a supported external import or establish Demo
activation readiness.** `import_external_lifecycle` always fails before opening
any runtime file. There is no CLI, `verified` flag, accepted planner report, or
production source qualifier that can enable application. The private bundle and
engine exist for synthetic tests and future reviewed integration; constructing
that bundle does not grant public admission.

## Required future source admission

A separate reviewed adapter must derive every projection from retained,
authenticated, exact source bytes: typed Demo account/order/position identity,
currency, source event time, complete cash-event coverage, original opening
debit, costs and closure proceeds. Its source contracts must reconcile conflicts
without an operator assertion or a balancing cash total. Zero cash residual is
necessary accounting consistency, never proof of complete or authoritative
evidence. No source adapter or public application contract is implemented here.
Unknown adjustments, dividends, account or carrying fees remain unsupported;
the internal cash kernel only represents account-level deposits and withdrawals.
Read-only reconciliation output is diagnostic and cannot authorize this engine.

## Internal transaction and attribution

The engine requires the original existing ledger, engaged kill switch and
private operator lock, exact reviewed plan digest, source hashes and original
inception/account binding. It appends one economic lifecycle event in a single
SQLite transaction and replays it before committing. A changed switch detected
before commit rolls the transaction back. A switch change just after commit can
only leave the economic event awaiting its journal; it cannot undo history or
authorize execution. No switch, submission, inception or consumed connector
claim is rewritten. The import adds no submission, intent, BrokerCommand, Fill,
broker execution, or finite mandate claim.

Baseline closure events retire only exactly matched baseline positions from
current expected composition. Their immutable inception records remain, and
their gains do not enter pilot PnL. External openings contribute account-currency
principal plus proven opening costs to committed capital, position count and
same-day opening limits. Their broker marked PnL subtracts opening fees and taxes
once. Existing pilot positions and future finite positions share these limits
and the cumulative loss latch, including stricter retained finite loss limits.
A historical hard-cap breach is preserved and held, not erased to admit entry.

Quote currency, quote price, order type and time-in-force remain provenance.
For example, retaining a synthetic GBX quote and IOC order does not convert its
quote value into USD principal and does not expand supported finite order entry.
Source times preserve sub-microsecond precision. Every new event must fall after
the latest admitted account cutoff. Cash-event identities and order identities
cannot be reused across imports or claimed pilot orders. Later marks cannot
regress behind an external account checkpoint.
Marks also cannot silently advance cash across an unqualified cash movement;
only a reconciled economic event advances that cash checkpoint.

## Journal recovery and limits

A committed event creates an unresolved journal obligation. Recovery reads that
event into a distinct `external_import` execution-journal phase and independently
reads back the hash chain. Only then may the ledger append its completion
receipt binding the exact journal path, chain hash and economic record hash.
Recovery is idempotent after either transaction boundary; it never resends or
reapplies economic effects. A missing journal, partial/corrupt chain, mismatched
reference, or valid older journal snapshot blocks ledger replay and further
claims. Pending imports without a completion receipt remain recoverable.

These are local integrity checks, not a remote tamper seal. Coordinated rollback
of all economic and journal stores needs an independent trusted checkpoint to
detect. Application to real retained runtime data remains unavailable until
source admission, current reviewed source evidence and the public application
contract are implemented and reviewed. Synthetic tests neither validate current
account economics nor authorize trading, a schedule, broker calls or migration.

The focused tests cover public rejection without changes to database/sidecar
bytes, original nonempty submission/history preservation, eight synthetic legacy
closures, external costs/PnL, combined finite limits, identity collisions, exact
money/time, crash recovery and journal rollback. All fixtures use unrelated
synthetic account and economic values.
