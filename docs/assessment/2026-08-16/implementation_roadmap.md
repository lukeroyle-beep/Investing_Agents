# Phase 0 Implementation Roadmap

Assessment date: 2026-08-16
Planning rule: evidence gates replace calendar promises

## Delivery principles

1. Preserve the deterministic governance core.
2. Establish tests and contracts before changing a subsystem.
3. Keep live credentials and live submission absent until a future approved phase.
4. Forge implements; Sentinel independently accepts.
5. Every phase has a rollback path and produces an evidence pack.
6. No failed gate is waived by moving to a later phase.
7. Technology is introduced only when the workload demonstrates the need.

Complexity scale: S = small, M = medium, L = large, XL = programme-sized.
Change-risk scale describes implementation risk to existing controls, not trading risk.

## Retain / modify / replace / add / defer matrix

### Retain

| Component | Importance | Dependencies | Change risk | Complexity | Direction |
|---|---|---|---|---|---|
| Fill Agent mutation authority | Critical | Schemas, invariants, ledgers | High | M | Preserve as the sole economic-state projector |
| Canonical portfolio-state concept | Critical | Fill, reconciliation, recovery | High | M | Preserve semantics even if storage changes |
| Closed-position immutability | Critical | Lifecycle snapshots/invariants | High | S | Preserve and extend with correcting events |
| Processed-fill idempotency | Critical | Fill IDs and recovery evidence | High | M | Generalise to broker command/event idempotency |
| Deterministic pipeline | High | Run context/history | Medium | M | Keep the accepted order and canonical run-ID propagation as the control plane baseline |
| Lifecycle Integrity | Critical | Shared invariants and schemas | Medium | M | Extend to orders, modes, risk approvals, and event replay |
| Append-only audit principle | Critical | Event schema, storage | High | L | Preserve; strengthen durability and replay semantics |
| Reconciliation and parity | Critical | State, ledgers, broker sync | High | L | Preserve and broaden to broker/account evidence |
| Atomic CSV writes | High in transition | CSV authority | Low | S | Retain until database authority gate passes |
| SQLite shadow/parity phase | High | CSV baseline and tests | Medium | M | Continue as migration evidence |
| Provider protocol seam | High | Market-data consumers | Medium | M | Preserve concept while splitting capabilities |
| Backtesting v1 | Medium | Historical data and strategy rules | Medium | M | Preserve deterministic base engine |
| Existing 84-test safety baseline | Critical | Reproducible test environment | Low | M | Keep green throughout all phases |

### Modify

| Component | Importance | Dependencies | Change risk | Complexity | Required change |
|---|---|---|---|---|---|
| Market-data layer | Critical | Instrument/calendar services | High | XL | Add live quotes/trades, provenance, sequence, recovery, and fail-closed freshness |
| Governance configuration | Critical | Mode/risk schemas | High | L | Replace broad booleans with versioned deny-by-default capabilities |
| Risk Agent | Critical | Account, positions, orders, data quality | High | XL | Become independent portfolio and pre-trade Risk Engine |
| Event log | High | Transactional storage/outbox | High | L | Add schema versions, offsets, causation, hashes, and consumer checkpoints |
| SQLite persistence | High | Transaction design, backups | High | L | Move from best-effort shadow to gated transactional authority |
| Backtester | High | Point-in-time data, trial registry | Medium | XL | Add walk-forward, multiple-testing controls, asset costs, and replay |
| Journal | High | Trade memory schema | Medium | L | Store decision, feature, execution, excursion, exit, and lesson evidence |
| Mission Control helpers | Medium | Read API and observability | Low | M | Turn projections into stable read models |
| Run recovery and finalization | Critical | Event/order/broker state | High | L | Add current-run artifact manifests, controlled resolution/replay, and fail-closed final checks; avoid direct CSV repair |
| Universe | High | Instrument master | Medium | L | Resolve research symbols into immutable, executable or non-executable instruments |
| Documentation/test setup | High | Packaging and CI | Low | M | Add Mac/Linux setup, current statuses, and evidence-based gates |

### Replace

| Existing approach | Importance | Dependencies | Change risk | Complexity | Replacement |
|---|---|---|---|---|---|
| Execution Agent CSV formatter as “execution” | Critical | Order lifecycle, modes, risk, broker adapter | High | XL | Execution Coordinator plus adapters and reconciliation |
| Ticker as instrument identity | Critical | Instrument/reference data | High | L | Immutable internal instrument ID plus versioned aliases |
| yfinance as prospective live authority | Critical | Production data adapter | Medium | L | Licensed/appropriate live feed; keep yfinance for research only. [yfinance terms](https://ranaroussi.github.io/yfinance/index.html) |
| Warning-only stale handling | Critical | Calendar-aware freshness policy | High | M | Hard decision/submission gates with explicit degraded modes |
| Flat cost/slippage as final model | High | Quote/trade history and TCA | Medium | L | Spread, size, volatility, latency, fill-probability, and market-impact models |
| Tracked runtime/economic data | High | Backup and sanitized fixtures | Medium | M | Ignored runtime directories and deliberately versioned fixtures |
| Obsolete milestone dates | Medium | New gate definitions | Low | S | Evidence-based status and acceptance records |

### Add

| Capability | Importance | Dependencies | Change risk | Complexity | Notes |
|---|---|---|---|---|---|
| Instrument master and alias history | Critical | Reference-data sources | Medium | L | Includes venue MIC and asset-specific extensions |
| Venue/product calendar service | Critical | Instrument master | Medium | L | Named timezones, sessions, breaks, holidays, expiries |
| Data-quality/freshness service | Critical | Adapters and calendars | High | L | Central gate, not agent-specific warnings |
| Durable event/outbox model | High | Transactional database | High | L | In-process first; distributed broker deferred |
| Order and execution domain | Critical | Broker-neutral state machine | High | XL | Follow FIX-like semantics. [FIX order states](https://www.fixtrading.org/online-specification/order-state-changes/) |
| Fake/replay broker adapter | Critical | Order domain and fake clock | Medium | L | Enables deterministic acceptance testing |
| Paper broker adapter | Critical later | Risk, kill, modes, reconciliation | High | XL | Read-only sync before submission |
| Persistent kill switch | Critical | Risk, execution, commands | High | L | Survives restart; blocks, cancels, audits, deliberate reset |
| Account and broker reconciliation | Critical | Broker adapter and state | High | L | Positions, balances, orders, executions |
| Scheduler and service supervision | High | Control/event planes | Medium | L | Explicit market-session workflows |
| Parquet analytical history | High | Data normalization and manifests | Medium | M | Market/features/replay only. [Apache Parquet](https://parquet.apache.org/) |
| Experiment/trial registry | High | Backtester and datasets | Medium | L | Records every attempted strategy/parameter version |
| Strategy lifecycle and Champion/Challenger | High | Validation and governance | High | L | No self-promotion |
| Structured observability | High | Shared context IDs | Medium | L | Logs, metrics, traces, alerts, health endpoints |
| Read API and responsive dashboard | High | Stable read models | Medium | XL | Read-only first; mobile-specific views |
| Tailscale Serve deployment/grants | High | Loopback backend and operator identities | Medium | M | No Funnel. [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve), [grants](https://tailscale.com/docs/reference/syntax/grants) |
| Secrets/dependency/CI controls | Critical before connections | Packaging and repository cleanup | Medium | M | Scanning, locks, separate paper/live secrets |
| Backup/restore automation | Critical before DB authority | Storage and service supervision | High | M | Restoration must be reconciled and drilled |

### Defer

| Capability | Importance now | Dependency before reconsideration | Risk | Complexity | Decision |
|---|---|---|---|---|---|
| Mode 6 Autonomous Live activation | None | Future explicit user decision plus all live gates | Extreme | XL | Locked and unavailable |
| Uncontrolled self-modifying production code | None | Not an acceptable target | Extreme | XL | Prohibited |
| Public dashboard/Funnel | None | No justified dependency | High | M | Prohibited; tailnet-only |
| High-frequency/colocated trading | Low | Different latency, data, risk, and infrastructure programme | Extreme | XL | Out of scope |
| Kafka or distributed event platform | Low | Measured single-host transport failure/scale need | High | XL | Use database outbox first |
| Kubernetes/microservice decomposition | Low | Operational need and multiple-host scale | High | XL | Keep modular monolith/services on Mac mini first |
| PostgreSQL migration | Medium later | Demonstrated writer contention, multi-host, or HA need | High | L | SQLite first; PostgreSQL only on trigger. [PostgreSQL MVCC](https://www.postgresql.org/docs/current/mvcc-intro.html) |
| Time-series database | Low | Query/ingest benchmarks show Parquet + SQL inadequate | Medium | L | Defer |
| Direct options/futures/FX trading | High long term | Instrument, calendar, margin, valuation, risk, adapter tests | Extreme | XL | Research/replay before submission |
| Dashboard economic mutation | Low | Separate command API security and Sentinel acceptance | High | L | Read-only dashboard first |

## Phased programme

## Phase 0 — Assessment and freeze

Status: this assessment pack completes the documentation output; approval remains with Codex/user.

Deliverables:

- repository audit, target architecture, roadmap, and team log
- explicit retain/modify/replace/add/defer decisions
- accepted narrow control-plane patch: Universe → Macro → Signal → Risk → News → Portfolio dependency order
- accepted canonical run-ID propagation for Fill and Position Tracking, with regression tests
- Sentinel acceptance evidence: 84 passing tests plus a disposable subprocess run-ID proof
- no destructive refactor
- no broker credentials or connectivity

Gate 0:

- Codex confirms architectural direction and scope.
- Sentinel accepted the narrow control-plane patch; this does not accept the wider future architecture.
- Existing 84-test baseline remains green.
- The patch is kept narrow; repository finalization and artifact-integrity gaps remain explicitly assigned to Phase 1.

## Phase 1 — Advisory safety and reproducibility

Objective: close safety gaps without adding a broker.

Work:

- central freshness/data-quality policy with instrument/session-aware tests
- make stale/unknown critical data block actionable advice
- current-run artifact manifest defining required outputs, producer, schema/version, run ID, timestamp, and checksum
- run reconciliation and required SQLite parity before terminal success; exceptions and mismatches fail the run closed
- reconciliation conservation rules that reject unexplained cash/equity movement, including non-zero deltas when no fills/open/close activity explains them
- make the nightly checklist pass on a clean checked-in or deliberately bootstrapped baseline
- Mac/Linux bootstrap, standardized packaging, lock/evidence manifest, CI
- secret and dependency scanning
- classify and untrack runtime/economic data after protected backup
- backup and restore procedure for CSV and SQLite
- controlled interrupted-run resolution tool
- reconcile roadmap, SQLite, testing, and operator documentation

Gate 1 evidence:

- deterministic repeated runs have no unintended economic drift
- no run can be marked successful until required current-run artifacts, reconciliation, and parity checks pass
- a synthetic zero-fill run with unexplained cash/equity deltas is rejected and records the failed conservation rule
- stale, malformed, missing, and contradictory data tests fail closed
- backup restore returns to parity and passes invariants
- clean-machine Mac setup runs all tests
- no credentials or live-account data are tracked
- Sentinel acceptance; no critical/high safety defect remains in this phase

Rollback: retain current CSV authority and advisory-only configuration.

## Phase 2 — Domain contracts and deterministic simulation

Objective: define the future trading domains without external submission.

Work:

- immutable instrument master, alias history, MICs, and calendar service
- versioned event envelope, outbox, and consumer checkpoint contracts
- Modes 0–6 capability matrix with Mode 6 locked
- order intent, risk decision, order, execution, and fill state machines
- independent Risk Engine v1 and persistent kill switch
- fake market stream, fake clock, fake broker, and complete market replay
- structured trade-memory schema

Gate 2 evidence:

- no research ticker can become an executable contract without resolution
- DST, early-close, holiday, expiry, and closed-session tests pass
- duplicate/out-of-order events cannot duplicate orders or economic mutations
- hard risk blocks cannot be overridden by a strategy
- kill latch survives restart and cancels eligible fake orders
- replay is deterministic from a frozen event set
- Sentinel acceptance of risk, modes, and lifecycle behavior

Rollback: all new components remain simulation-only and cannot access broker credentials.

## Phase 3 — Transactional state and analytical data

Objective: make persistence suitable for a single-host event-driven paper system.

Work:

- transactional SQLite authority for order/fill/cash/position/event/outbox unit
- migration/backfill tooling with CSV parity and reversible cutover
- immutable Parquet market/feature/replay partitions with manifests and checksums
- experiment/trial registry and reproducible dataset snapshots
- automated SQLite backup and restore drills

Gate 3 evidence:

- each fill transaction is all-or-nothing across dedupe, ledger, state, audit, and outbox
- interruption at every transaction boundary recovers deterministically
- migration can roll back to the known-good CSV baseline
- restored state reconciles and replays without duplicate effects
- measured SQLite write/reader load remains within service objectives
- Sentinel acceptance before CSV authority is retired

Rollback: restore backed-up CSV authority and rebuild the SQLite shadow.

## Phase 4 — Read-only operations and dashboard

Objective: make health and portfolio state observable without adding a mutation path.

Work:

- structured logs, metrics, traces, service health, and alerts
- supervised schedules for preparation, replay, reconciliation, journal, and backups
- stable read models and read-only API
- responsive laptop/iPad/iPhone dashboard
- loopback deployment through Tailscale Serve with deny-by-default grants
- explicit display of mode, kill status, freshness, last reconciliation, and as-of times

Gate 4 evidence:

- no dashboard route can mutate economic or execution state
- LAN/public access tests fail; authorized tailnet access succeeds
- identity-header spoofing cannot bypass loopback proxying
- mobile critical cards meet acceptance layouts
- service restart and backup schedules recover automatically
- critical faults surface in the dashboard and supervisory channel/process

Rollback: stop Serve and dashboard; core advisory pipeline remains independent.

## Phase 5 — Paper broker integration

Objective: prove broker connectivity without capital risk.

Stage A is read-only account synchronization. Stage B permits paper submission only after Stage A acceptance.

Work:

- first broker paper adapter behind the common interface
- separate paper endpoint and credentials
- read-only accounts, balances, positions, orders, executions, and reconciliation
- paper submit/cancel/replace with stable client-order IDs
- partial fill, reject, disconnect, retry, correction, and rate-limit handling
- human approval as configured; independent Risk and kill checks on every order
- transaction-cost and execution-quality measurement

Gate 5 evidence:

- no live endpoint or credential is present in the service environment
- broker/canonical reconciliation passes across reconnect and restart
- duplicate submission and ambiguous-timeout tests do not create extra orders
- all required broker lifecycle and failure cases pass
- a sustained paper observation window completes with no unresolved reconciliation, freshness, or control failures
- measured paper limitations are documented; paper is not treated as live proof. [Alpaca paper limitations](https://docs.alpaca.markets/us/docs/paper-trading)
- Sentinel acceptance

Rollback: revoke paper credentials, block adapter capabilities, and return to replay.

## Phase 6 — Strategy validation and shadow live

Objective: prove statistical and operational validity without broker submission.

Work:

- chronological out-of-sample and walk-forward evaluation
- point-in-time universe/data controls and trial registry
- Deflated Sharpe or appropriate multiple-testing correction. [Bailey and López de Prado](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- parameter/regime/asset stability and execution-cost stress tests
- Champion/Challenger registry and auditable promotion decisions
- Mode 4 shadow intents compared with subsequent market and paper outcomes
- degradation, drift, suspension, and rollback rules

Gate 6 evidence:

- every promoted strategy has immutable evidence and sufficient effective sample
- all tried candidates are counted, including rejected variants
- challenger cannot replace champion without approved transition
- paper/shadow execution degradation remains within strategy-specific limits
- no unresolved operational or risk failure exists
- Sentinel acceptance and Codex/user approval

Rollback: suspend strategy version and reactivate the last accepted Champion or no-trade state.

## Phase 7 — Human-approved live readiness, not activation

Objective: prepare evidence for a future user decision on Mode 5.

Work:

- least-privilege live credential design, initially absent from runtime
- restricted instruments, capital, order types, hours, and loss/drawdown limits
- per-order approval with expiration and exact-order binding
- live-readiness, emergency, rollback, and incident runbooks
- independent security, risk, recovery, and operational review

Gate 7:

- zero unresolved critical/high defects
- all earlier gates remain valid under current versions
- disaster recovery, kill, broker reconciliation, and credential-revocation drills pass
- legal/regulatory/account constraints are reviewed separately
- Sentinel recommends acceptance
- Codex presents evidence; only an explicit future user decision may authorize activation

Mode 6 remains outside this roadmap and locked.

## Evidence pack required at every gate

- versioned requirements and architecture decision records
- exact code/config/schema/data versions
- test and Sentinel acceptance reports
- known limitations and unresolved defects
- reconciliation/parity results
- security and dependency scan results
- backup/restore or rollback evidence
- operational metrics from the phase
- named approver, timestamp, decision, and rationale

## Immediate work order

1. Preserve the accepted Phase 0 order/run-ID patch and its Sentinel evidence.
2. Implement Phase 1 current-run manifests and fail-closed finalization, then make the nightly checklist/parity clean.
3. Implement Phase 1 freshness policy and clean-machine reproducibility.
4. Define instrument, calendar, mode, risk, event, and broker contracts in Phase 2 before choosing a live vendor.
5. Keep broker installation and credential work blocked until Phase 4 is accepted and Phase 5 Stage A begins.
