# External Demo lifecycle reconciliation admission

`scripts/inspect_external_demo_reconciliation.py` rebuilds an inspection report
from the original private runtime and retained source bytes. It does not make a
broker request, create or update a runtime, import positions, clear a hold,
engage/disengage a switch, or activate trading. There is no `--apply` option.

The economic import and its explicit external-import journal phase remain
**unimplemented**. A zero cash residual never authorizes application: closed
trade history does not establish complete non-trade cash-event coverage. This
planner makes missing evidence and prospective accounting effects reviewable
without introducing a permissive migration path.

## Original-state and source contract

The caller selects the canonical existing runtime. The planner checks its
inception marker, inception and existing ledger-row digests, and snapshots the
original economic, execution and operator files. Nonempty SQLite WAL state is
rejected. The already-read economic database bytes are deserialized into memory
so SQLite cannot write sidecars beside original stores. Main files and existing
or absent sidecars are checked again before returning.

The manifest and every referenced file must be regular, owner-only files. Paths
cannot escape the manifest directory, and a reference contains only its relative
path and SHA-256. Raw response bytes must also match their original response
receipt. JSON rejects duplicate keys, non-finite values, and numeric amplification;
source numbers are bounded to 100 significant digits and absolute exponent 100,
with positive int64 identities. Monetary arithmetic is exact within these bounds.

The account is rebuilt with the existing strict `build_pilot_account` parser at
its original capture time. That historical result is separate from freshness at
inspection time. The planner accepts direct official API receipts only, rather
than a caller-provided account projection or `verified` flag.

History is matched against the exact known missing baseline identities,
instrument/direction/leverage/units/principal and close times. Matched known
identities are not a claim that all account events have been enumerated. The
official history route has no total count; a short page remains a heuristic.

External position candidates are matched to original order-ID lookup requests,
account identity, terminal status/name, opening order/position identities, units,
instrument, costs and exact source times. Seven-digit fractional source times
are preserved. Events cannot postdate their own receipt or the account cutoff.
An invested-amount conflict remains a blocker; margin is never substituted for
principal. Historic opening-cash-debit proof is not yet admitted by this planner.

## Invocation and manifest

```sh
python scripts/inspect_external_demo_reconciliation.py \
  --runtime /absolute/original/private/runtime \
  --manifest /absolute/private/evidence/admission-input.json
```

The private manifest has exactly these keys:

```json
{
  "schema": "external-demo-reconciliation-admission-v1",
  "runtime": "/absolute/original/private/runtime",
  "inception_hash": "<original inception SHA-256>",
  "account_receipts": {"path": "account-receipt-index.json", "sha256": "<SHA-256>"},
  "history": [{
    "request": {"path": "history.request.json", "sha256": "<SHA-256>"},
    "receipt": {"path": "history.receipt.json", "sha256": "<SHA-256>"},
    "body": {"path": "history.response.bin", "sha256": "<SHA-256>"}
  }],
  "opening_lookups": [{
    "request": {"path": "lookup.request.json", "sha256": "<SHA-256>"},
    "receipt": {"path": "lookup.response.json", "sha256": "<SHA-256>"},
    "body": {"path": "lookup.response.bin", "sha256": "<SHA-256>"}
  }]
}
```

`account-receipt-index.json` has `profile`, `first`, `pnl`, `last` original direct
read receipts. Their absolute `raw_response.body_file` locations must be inside
the evidence directory and match their retained raw bytes.

History bundles use GET `/api/v1/trading/info/trade/demo/history` with explicit
`minDate`, sequential `page`, and stable `pageSize`. Up to ten pages are admitted;
this bound does not assert completeness. Lookup bundles use the inspected
`DirectDemoClient` GET `/api/v2/trading/info/demo/orders:lookup` capture format
with exactly the original `orderId` query. The planner never obtains a new order
identity or substitutes one after a failed lookup.

Exit 1 means inspection succeeded and migration remains blocked. Exit 2 means
invalid/unavailable evidence or arguments. Both emit sanitized JSON without raw
account, order or position IDs. Valid reports always state `apply_supported=false`,
`activation_ready=false`, `migration_not_applied=true`, and zero new claims.

## Currency and prospective accounting

The current official eToro MCP history contract states that monetary amounts use
account currency and `netProfit` excludes separately reported fees. The fresh
account parser independently requires USD. The known-event calculation is:

`inception cash + sum(closed investment + netProfit - fees) - observed external opening principal/costs`

Any difference from observed cash is explicit. Even if this difference is zero,
offsetting unknown cash events remain possible; `cash_event_coverage` is unknown.
The public REST history schema does not itself document that currency/fee
convention, so this dependency is stated rather than inferred from a symbol.

Prospective external capital and P&L describe the current candidate positions;
they have not been imported. Legacy closure profit is never credited to pilot
performance. A future reviewed import must retain inception and consumed claims,
append uniquely keyed external lifecycle events, count imported project capital
and P&L toward limits, and journal them without fabricated commands or dispatches.

The next application design requires a qualified same-account complete cash-event
source or complete close-proceeds plus non-trade-event evidence, original opening
cash-debit proof, and resolution of conflicting principal/currency semantics.
Only then can exact-plan application, original operator locking, engaged-switch
checks, append-only transactions and journal crash recovery be implemented and
independently reviewed. Those obligations are not completed by this planner.

Official sources inspected on 29 October 2026:

- [Demo closed-trade history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history)
- [Demo order lookup](https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details)
- [Demo close-order details](https://api-portal.etoro.com/api-reference/trading--demo/get-close-order-information-and-closed-position-details)
- Official live MCP `get_my_trading_history` contract, reachable through the
  [documented official MCP service](https://api-portal.etoro.com/core/ai-agents/etoro-skill).

The test suite uses synthetic original-byte fixtures only. No credentials,
private broker receipts, account identifiers or live ledger files belong in Git.
