# Account component-time contract v1

`AccountComponentTimeEvidence` is an additive, versioned non-atomic account
model (`etoro-component-time-v1`). `EtoroPnlAccountEvidence` and its canonical
serialization remain unchanged, preserving retained PR13 evidence digests and
launcher pins. The existing aggregate route remains the preferred authoritative
account observation when it supplies a valid explicit-offset account timestamp.
An absent offset is unknown; this model does not assign UTC to it.

The component model retains `observed_at=None`. `assessed_at` is when the local
assessment ran. `earliest_component_at` and `latest_component_at` are expressly
the local collection bracket, not broker account observation times. Every
position retains its broker timestamp text and exact Decimal epoch seconds.
No oldest/newest position time becomes an account timestamp.

## Components and coverage

| Component | Evidence and admission rule |
| --- | --- |
| Cash and structure | Three strictly parsed detailed views agree exactly on cash, bonus credit, every holding's identity/units/invested amount/direction/leverage/settlement, and every pending/mirror count. Local collection age is bounded. This agreement alone does **not** prove broker cash freshness. |
| Cash source time | An independent `ComponentCashObservation` must have an actual aware broker cash observation time, original timestamp text, same Demo identity, exact same USD cash, Demo endpoint, source field and retained source digest. Missing cash observation is `cash_component_observation_unproven`. The adapter must verify the official field semantics; a digest alone authenticates neither the response nor its timestamp claim. |
| Position valuation | Every holding appears exactly once in PnL, currency metadata and account risk positions, and every held instrument has metadata. All valuations must be fresh both at original capture completion and current evaluation. |
| Pending orders and mirrors | All seven documented pending families and mirrors must be explicit at every detailed read and the metadata read. Nonempty pending orders or copies block the entire supported account; they are never discarded to reduce risk exposure. An additional unknown order family fails parsing. |
| Currency | Account calculations use broker USD-denominated fields, independently matched asset currency metadata, and the documented USD currency ID. Numeric GBX or margin currency mappings are not invented. |

The first implementation deliberately requires a direct authoritative cash
observation. A cash-history proof is not implemented by replacing it with a
`complete=true` assertion. A future history-based cash adapter must retain a
source-backed opening cash anchor, exhaustive cash-affecting events, complete
pagination/window coverage, and exact closing-cash reconciliation before
claiming a cash component time. The separate accounting history contract grants
no such proof by itself.

## Freshness, skew and consistency

`assess_pnl_component_time_evidence` returns an assessment with explicit blockers;
`qualify_pnl_component_time_evidence` raises unless every component passes.
Consumers must call `validate_at(now, age_limit)` again at each use. A prior
`qualified` result is not a reusable permission. The supported age limit is
at most 15 seconds, or a stricter existing configured limit. Future times have
zero allowance. The entire local bracket must remain within that age limit.

The maximum spread across both local bracket endpoints, every position source
time and the independent cash source time must be no greater than the model's
configured skew ceiling, which cannot exceed 15 seconds. Freshness and skew
are separate checks. Decimal comparisons preserve seventh-digit (100 ns)
position boundaries; there is no float conversion or timestamp rounding.
Cash timestamp text is parsed with the same exact grammar; its datetime view
cannot silently discard a seventh fractional digit during qualification.

For final host dispatch, `freshness_deadline_epoch_seconds` returns the earliest
expiry among structural bracket start, every position clock and the independent
cash clock. `freshness_deadline` provides a datetime rounded down to microseconds
so host scheduling cannot extend that exact deadline. This derived expiry is
never a source observation timestamp.

Qualification recomputes exact position/currency/security coverage, metadata
digests, source-evidence digest, position exposure/PnL agreement and account
arithmetic. The initial capture validator remains responsible for comparing
the three actual source structures before constructing the immutable retained
evidence. Trusted capture adapters must preserve source receipts and identity
provenance; arbitrary construction of an evidence object is not broker proof.

Account equity equals **cash + invested amount + unrealised PnL**. Invested
amount is the sum of detailed position `amount`; broker margin and exposure
remain separate. The aggregate path now also carries explicit invested amount
and compares aggregate margin to instrument margins, not to invested capital.
Position fees are retained without a second deduction from broker PnL.

## Replay and performance boundaries

`PnlReplayAssessment.assess_component_time` is an explicit additive assessment.
It keeps the original replay's compatibility/qualification flags unchanged.
It never changes source clocks: old structural evidence and old valuations
remain stale, and a mark that was future at the original capture cannot become
qualified simply by waiting for it. A new cash observation cannot rehabilitate
an old bracket.

Component qualification establishes only current account valuation evidence.
It supplies neither full-day PnL, a midnight/day-start baseline, execution or
cash-flow history completeness, historical peak/drawdown, instrument mapping,
session/quote evidence, risk approval, nor execution authority. Baseline
admission must separately require fresh reconciled components and explicit
history coverage. Starting a prospective baseline partway through a day does
not turn earlier losses or unknown history into zero.

Synthetic contracts exercise exact freshness/skew boundaries, original-time
replay, missing/stale cash proof, identity/value mismatch, exhaustive coverage,
provenance and distinct invested/margin/exposure arithmetic. They establish no
live broker acceptance or trade readiness.
