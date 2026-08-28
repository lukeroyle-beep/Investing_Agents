# Execution safety and eToro Demo Gate A

This package implements WP8 through WP10 without enabling a broker mutation.
The checked-in defaults keep intent import disabled, broker writes disabled, and
the eToro adapter disabled.

## Capability boundaries

- Advisory agents emit portfolio-order candidates. They cannot import or
  receive a broker adapter or credentials.
- The broker-neutral execution coordinator accepts only immutable internal
  instrument UUIDs. Ticker-only rows fail closed.
- The coordinator owns operational intent, approval, command, and lifecycle
  records in `runtime/control/execution.sqlite3`. It has no tables or methods
  for cash, holdings, cost basis, fills, realised P&L, or position lifecycle.
- Fill Agent remains the only economic-state mutation authority.
- The eToro Gate A adapter is Demo/read-only. Every mutation method raises
  `BrokerWriteDisabled`.

## Durable execution lifecycle

`OrderIntent`, `RiskDecision`, `Approval`, and `BrokerCommand` are immutable
domain records. Intent hashes cover the full business payload but not the
generated record UUID, allowing duplicate business intents to be detected.
The operational store enforces unique intent hashes and logical request UUIDs,
single-use approvals, optimistic command revisions, and an append-only command
event sequence.

Allowed command transitions are explicit. Invalid transitions and ambiguous
cancel/fill outcomes fail rather than being coerced. An ambiguous submission
must enter `unknown`, then `reconciling`; it is never blindly replayed.

## Independent pre-trade gate

`config/risk.yaml` contains conservative Demo validation limits:

- 1% equity per order
- 5% per issuer/position
- 20% per sector
- 50% gross and net exposure
- 20% cash buffer
- leverage exactly 1
- 1% daily-loss stop and 5% drawdown stop
- regular trading hours only

The evaluator consumes neutral, immutable account, pending-order, and quote
evidence rather than a broker client. Missing pending-order completeness,
duplicate contradictions, stale or future evidence, an environment mismatch,
or any breached limit rejects the intent. Pending orders are deduplicated by
broker order ID before exposure is recomputed.

Every order needs a human-issued approval bound to the exact intent hash and
risk decision. Submission rechecks the approval, current risk/quote age,
regular-session state, persistent kill switch, and write configuration. The
approval is consumed atomically and cannot be replayed.

The kill switch is stored under `runtime/control/` with mode-restricted state
and a hash-chained audit. Missing or corrupt state means writes are disabled.
Reset requires operator identity, a reason, and the exact interactive
acknowledgement defined in `risk.kill_switch`.

## eToro Gate A contract snapshot

Checked against the official documentation on 2026-08-28:

- authentication headers: `x-api-key`, `x-user-key`, and a unique
  `x-request-id` per request;
- exact instrument resolution: `GET /api/v1/market-data/search` using
  `internalSymbolFull`, with an exact symbol/exchange match before the numeric
  `instrumentId` is cached;
- execution-price evidence: `GET /api/v1/market-data/instruments/rates`,
  including bid, ask, timestamp, and `priceRateID`;
- Demo account read: `GET /api/v1/trading/info/demo/aggregate-portfolio`.

The local client caps account reads at 48/minute and market reads at 96/minute.
It honors `Retry-After`, otherwise uses bounded exponential full-jitter retry
for read-only requests. Response bodies and authentication values are excluded
from exceptions and filtered from logs.

REST remains authoritative. No WebSocket mutation path exists. The aggregate
portfolio response is saved only as an explicitly non-economic reconciliation
read model; pending-order and execution completeness remain false until later
contracts prove them. It never overwrites `portfolio_state.csv` or another
Fill-owned artifact.

Official references:

- <https://api-portal.etoro.com/core/getting-started/authentication>
- <https://api-portal.etoro.com/core/guides/get-instrument-id>
- <https://api-portal.etoro.com/api-reference/market-data/get-instrument-market-rates>
- <https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot>
- <https://api-portal.etoro.com/core/getting-started/rate-limits>
- <https://api-portal.etoro.com/core/changelog>

The 2026-08-18 changelog entry still records Agent Portfolio endpoint
withdrawal. No Agent Portfolio route or Real route is implemented.

An operator can establish a reviewed internal identity without contacting a
broker:

```bash
uv run --frozen python tools/register_instrument.py AAPL NASDAQ \
  --asset-type equity --currency USD --sector technology
```

The returned UUID may be placed in the corresponding advisory row only after
the exchange, asset type, and currency are verified. eToro `instrumentId`
mapping is a separate Gate A exact-search step; the registration command does
not perform it.

## Gate state

Gate A's live read-only smoke completed successfully on 2026-08-28. Gate B
submission, lookup, close, partial-close, cancel, and broker-fill ingestion are
now implemented behind disabled configuration and per-order controls. No live
Demo mutation has been performed by the implementation package. See
`docs/demo_execution_strategy_scheduler_wp11_wp13.md`.
