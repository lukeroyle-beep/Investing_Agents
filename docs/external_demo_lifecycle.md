# External Demo lifecycle accounting and journal mechanics

This module provides append-only economic and journal recovery mechanics. Its
original all-event entry point, `import_external_lifecycle`, remains closed.
The separate [composite historical import](composite_demo_import.md) supplies a
reviewed source adapter and exact-plan application for net cash effects, with
explicitly unknown gross event enumeration. Constructing a private bundle or
supplying a `verified` flag cannot grant admission. Neither path establishes
trading readiness.

## Composite historical period accounting

The v2 kernel can retain a separately qualified composite period, while v1
records retain their exact schema, identity calculation and replay behavior.
The source adapter and public application boundary own admission; the private
bundle alone is not evidence that those checks passed.

One `period_reconciliation` records the signed USD cash effect of net fees less
dividends: a negative amount is a cash debit, a positive amount is a net credit,
and zero does not assert that no gross events occurred. It is an account-period
observation, not an invented broker transaction. Its content-derived identifier
belongs to the local import record. It has no broker event ID or manufactured
posting timestamp.

The exact record fields are `kind` (`net_fees_less_dividends`), `amount`,
`account_currency`, `effect_after_observation`, `effect_by_observation`,
`reporting_start_date`, `reporting_end_date`, `reporting_timezone`, `gross_fees`,
`gross_dividends`, `enumeration_complete`, `individual_attribution`,
`baseline_position_ids` and `source_reference`. The two observation bounds are
timezone-bearing and bounded by the admitted account captures. The reporting
dates retain the separate broker UI period; its timezone remains null.
Gross fees and dividends remain null, individual attribution remains `unknown`,
and enumeration completeness is false. The baseline IDs identify exactly the
closures reconciled in that record; they do not assert individual fee allocation.

Source admission must independently substantiate the reported aggregate and
the account cash bridge. A zero residual alone still cannot establish source
coverage. The period source hash cannot be reused in a distinct import, and
cash observation intervals cannot regress behind the prior account checkpoint.
Historical closure gains and the historical aggregate do not enter pilot P&L;
supported external opening costs remain counted normally. The result earns no
historical daily-P&L, drawdown or complete gross cash-event qualification.

`_apply_qualified(..., historical_accounting=True)` is restricted to v2. It
revalidates the retained account at its actual original capture time and rejects
an application time before that capture. The original engaged kill switch and
all transaction/journal controls still apply. This mode changes no execution
freshness gate, does not release the switch, and does not make an old account
observation current. Later trading requires fresh account and market evidence.

## Source admission boundary

A separate reviewed adapter must derive every projection from retained,
authenticated, exact source bytes: typed Demo account/order/position identity,
currency, source event time or supported observation bounds, original opening
debit, costs and closure proceeds. Its source contracts must reconcile conflicts
without an operator assertion or a balancing cash total. Zero cash residual is
necessary accounting consistency, never proof of complete or authoritative
evidence. The composite adapter and public application contract are implemented
in separate modules; the original all-event importer remains closed.
The v1 cash-event kernel only represents individual account-level deposits and
withdrawals. V2 additionally represents the explicitly limited net fee/dividend
period above; it does not supply missing individual cash events, adjustments or
unsupported carrying-fee attribution.
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
detect. Application to retained runtime data requires independent review of the
source bundle and exact composite plan. Synthetic tests neither validate a live
account nor authorize trading, a schedule or broker calls.

The focused tests cover public rejection without changes to database/sidecar
bytes, original nonempty submission/history preservation, eight synthetic legacy
closures, external costs/PnL, combined finite limits, identity collisions, exact
money/time, crash recovery and journal rollback. All fixtures use unrelated
synthetic account and economic values.
