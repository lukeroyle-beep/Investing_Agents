# Retained Demo PnL evidence contract

`brokers.etoro.saved_pnl_evidence.assess_saved_pnl_capture` revalidates an existing
capture entirely in memory. It accepts five decoded source payloads: opening
detailed portfolio, PnL portfolio, one complete securities page, currency
metadata, and closing detailed portfolio. It does not invoke the launcher or
create an attempt. Existing pinned launchers and their evidence stay unchanged.

Decode retained detail, PnL and currency JSON using
`json.loads(decoded_bytes.decode("utf-8"), parse_float=Decimal)`, matching the
online client's exact numeric decoding. Decode the securities JSON normally.
Ordinary binary floats can lose precision or decimal scale before validation
and therefore cannot reliably reproduce the original source hashes. Do not
round, reformat or coerce monetary values to make a digest match.

The caller supplies the independently reviewed expected Demo customer identity,
original capture start/completion times, original age limit, saved account
evidence and independently retained canonical evidence digest. It must first
verify the source receipts, decoded/raw byte relationship, endpoint sequence,
independent identity provenance and integrity of the supplied clocks/digest.
Those transport and provenance checks remain separate from this pure API.
Matching hashes cannot authenticate a broker response or defend against an
attacker replacing both the evidence and its trusted digest.

The saved digest is `payload_hash(evidence)` (canonical JSON), not the file hash
of pretty-printed `account-evidence.json`. Replay recomputes the evidence and
requires both it and the saved account object to match the supplied digest.
Original saved report booleans are never trusted or modified. Changing a clock,
an age limit or evidence fields without updating the trusted digest fails.

## Validation and outcomes

Strict existing parsers and `validate_pnl_account_evidence` recheck identity,
supported holdings, exact position/security/currency coverage, explicitly empty
pending groups and mirrors, structural and cash agreement across the bracket,
and position PnL reconciliation. The online capture calls the same pure
validator after its existing reads and early guards. More than one securities
page is outside this offline API's scope and fails without fetching anything.

| Result | Meaning |
| --- | --- |
| `source_consistency_verified` | Supplied inputs pass shared validation and reproduce the saved canonical evidence and digest. |
| `historical_restricted_capture_accepted` | Reconstructed position marks passed the original freshness limit at capture completion. |
| `present_valuations_fresh` | Those same marks pass the limit at the caller's explicit `assessed_at`, which must be aware and at or after completion. |
| `risk_input_compatible` / `baseline_input_compatible` | Always false for this evidence type under the current contracts. |
| `trading_readiness` / `qualification_credit` | `BLOCKED` / false; this API awards no execution qualification. |

Schema, identity, timing, reconciliation and hash errors raise without returning
an assessment. A reconciled stale/future capture can be assessed, but historical
acceptance remains false. A once-fresh old capture remains historically accepted
even when every mark is now stale. Its evidence is private: the returned object
contains account/position identifiers and values and is not a public report.

## Accounting and timestamp boundaries

Equity uses **cash + invested amount + unrealized PnL**, after the empty-pending
and mirror checks. Broker margin and market exposure remain separate quantities;
neither substitutes for invested amount even if the numbers happen to coincide.
Original per-position timestamp text and exact decimal seconds are preserved.
Capture time, receipt time and a maximum/minimum position time cannot establish
a whole-account observation time. `observed_at`, daily PnL and peak equity stay
null. Bracket agreement does not make the observations an atomic snapshot.

## Remaining risk contract work

The current `risk.pretrade.AccountEvidence` requires an authoritative whole-account
timestamp, trusted full-account daily PnL and peak equity. The baseline ledger
accepts a different, margin-based `BrokerAccountSnapshot` contract and needs
complete cash-flow/execution coverage for later intervals. Its since-inception
indexed drawdown is not automatically the evaluator's raw peak-equity measure.
This replay returns neither input type and never instantiates their stores.

The next architecture decision is whether to obtain an authoritative whole-account
observation or specify a separately reviewed component-time risk model. That
decision must also define cash-flow-adjusted daily PnL and drawdown semantics;
it cannot be resolved by timestamp substitution. Existing exposure limits,
instrument/issuer/sector mappings, tradeability/session evidence, operator
integration and explicit execution authorization remain separate requirements.
The replay does not assess proposed trades or resolve any exposure breach.

## Validation and rollback

Synthetic tests cover integrity failures, source inconsistencies, unsupported
pagination, historical/current freshness, exact seventh-digit timestamps and
divergent margin/invested amounts. Existing capture/operator tests protect read
order and early stops. Run them with network denied and credentials absent.
Any replay of private retained inputs writes results only to a separate audit
artifact and verifies original file hashes/mtimes remain unchanged. Rollback is
a source revert; no capture migration, repinning or runtime state change exists.
