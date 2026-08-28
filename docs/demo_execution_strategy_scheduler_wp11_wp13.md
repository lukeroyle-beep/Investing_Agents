# WP11–WP13: Demo execution, strategy evidence, and scheduled qualification

The software paths for Gates B and C are implemented, but the checked-in
configuration remains disabled. No live Demo mutation is part of this package,
and no Real-account route exists.

## Gate B execution boundary

The operation-specific contract snapshot was checked on 2026-08-28 against
eToro Core API v1.365.0 and the current official changelog.

- Open: `POST /api/v3/trading/execution/demo/orders`. A 202 response is only a
  queued acknowledgement.
- Lookup: `GET /api/v2/trading/info/demo/orders:lookup`, using exactly one of
  the broker order ID or persisted reference ID.
- Close/partial close:
  `POST /api/v1/trading/execution/demo/market-close-orders/positions/{positionId}`.
- Cancel: `DELETE /api/v3/trading/execution/demo/orders/{orderId}`. A 202
  response means cancellation is pending and can race a fill.

`BrokerCommand.logical_request_id` is persisted before send and is used as the
eToro `x-request-id`. A transport timeout, lost response, malformed accepted
response, or server error moves the command to `unknown`; recovery performs a
lookup using the same reference. It never creates a second logical request or
blindly replays the mutation.

Only long, unleveraged equities/ETFs with real settlement, USD amount sizing,
an immutable numeric instrument mapping, an accepted current risk decision,
and a fresh single-use human approval are supported. Separate
`ETORO_DEMO_WRITE_*` credential names prevent accidental reuse of read-only or
Real credentials. `config/brokers/etoro_demo_write.yaml` remains disabled.

Broker-confirmed executions are staged in `runtime/state/broker_fills.csv`.
Staging changes no cash or holdings. Fill Agent validates that the execution is
Demo, derives an idempotent fill identity from broker/environment/execution,
and remains the only code allowed to apply the economic mutation. The trade
fill ledger preserves broker order, position, execution, reference, price,
fee, tax, currency, and environment identities in CSV and SQLite.

The offline contract suite covers queued open, lookup, rejection, partial fill,
pending cancel, cancelled-partial-fill, rejected-partial-fill, timeout before
acknowledgement, reference recovery, and duplicate fill delivery.

Official references:

- <https://api-portal.etoro.com/core/changelog>
- <https://api-portal.etoro.com/api-reference/trading--demo/submit-an-order-for-asynchronous-processing>
- <https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details>
- <https://api-portal.etoro.com/api-reference/trading--demo/close-demo-position-by-units>

## Strategy experiment boundary

`strategy/` provides:

- strict chronological train/validation/test windows;
- rolling walk-forward windows;
- feature-availability look-ahead checks;
- a point-in-time universe that retains delisted securities before their
  delisting date and removes them afterward;
- spread, slippage, fee, tax, and cost-stress evidence;
- immutable experiment identity over strategy version, data snapshot,
  point-in-time universe, parameters, splits, cost model, code revision, and
  environment;
- append-only experiment and promotion records.

Backtesting snapshots are copied to
`runtime/runs/<run_id>/experiments/<experiment_id>/`. The current legacy
backtest uses a static ticker universe and calendar-day equity projection, so
the recorded evidence explicitly marks point-in-time universe, survivorship,
exchange-calendar, and deterministic-reproduction checks as unpassed. It
therefore cannot promote itself.

Promotion evaluation uses criteria defined before the test evidence and can
grant `advisory_only` capability only. It cannot grant credentials, broker
access, approval authority, scheduling rights, or execution rights.

## Gate C scheduled qualification boundary

`config/scheduler.yaml` is disabled and permanently sets
`broker_submission_enabled: false`. The scheduler can run advisory preparation,
create short-lived approval waits, record heartbeats, and check reconciliation.
Its constructor has no broker-submit callback.

The run lock uses exclusive creation. An existing or corrupt lock is never
treated as stale automatically; it requires recovery review. Approval waits
expire after at most five minutes and cannot be approved without explicit
human identity. Schedule runs, approval waits, heartbeats, operator actions,
mutation cycles, and fault drills are durable under `runtime/control/`.

Gate C remains unqualified until real evidence contains at least 30 distinct
clean trading sessions and 20 manually approved Demo mutation cycles, with:

- zero duplicate orders;
- zero unresolved command or cycle reconciliation;
- recovery verified for every cycle;
- finalisation and nightly checks passing for every recorded schedule run;
- every configured fault drill passing.

Inspect the evidence without changing it:

```bash
uv run --frozen python tools/demo_qualification_status.py
```

The command exits nonzero and lists blockers until all thresholds are genuinely
met. Synthetic unit tests prove the evaluator logic but do not populate the
operator runtime or count toward qualification.
