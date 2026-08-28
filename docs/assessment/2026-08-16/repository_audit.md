# Phase 0 Repository Audit

Assessment date: 2026-08-16
Repository: `Investing_Agents`
Posture: advisory-only; no live broker path

## Executive assessment

The repository is a disciplined deterministic advisory portfolio engine, not yet an intraday multi-asset trading platform. Its strongest property is controlled economic mutation: CSV portfolio state is canonical, Fill Agent alone owns economic changes, lifecycle rules protect closed history, and fills are deduplicated. Reconciliation, audit, and SQLite-parity mechanisms exist, but the checked-in baseline does not prove that every run reconciles successfully.

That governance architecture should be retained. The safe evolution is a hybrid system: keep deterministic orchestration as the control plane, then add a typed event-driven market-data and execution plane around the existing mutation boundary. Broker connectivity, live-data freshness, portfolio-level risk, executable instrument identity, strategy promotion, and a real dashboard are missing and must remain gated.

## Audit baseline

- `run_pipeline.py` orchestrates a fixed sequence of subprocess agents.
- The accepted Phase 0 control-plane patch corrects the dependency order to Universe → Macro → Signal → Risk → News → Portfolio and makes Fill and Position Tracking consume the canonical pipeline run ID.
- `data/portfolio_state.csv` is the canonical economic state.
- Fill Agent is the sole economic-state mutator.
- Lifecycle Integrity runs around monitoring/exit stages and checks shared invariants.
- Event, ledger, run-history, and reconciliation schemas/code provide a traceability design, but the checked-in `data/` baseline is incomplete: `event_log.csv` is absent.
- SQLite is configured in write-ahead-log mode and receives best-effort shadow writes; CSV remains authoritative.
- The repository contains 84 collected tests spanning invariants, fill idempotency, append-only semantics, atomic CSV writes, recovery guards, market-data adapters, backtesting, parity, reconciliation, and pipeline smoke/control-plane behavior.
- Verification on 2026-08-16: `.venv-test/bin/python -m pytest -q` returned `84 passed in 0.75s`.
- Sentinel separately accepted the narrow control-plane patch after the 84-test suite and a disposable subprocess proof showed one canonical run ID propagating through run-scoped artifacts.
- The post-patch full pipeline in that disposable copy exited successfully with the corrected order and exact run ID, but its reconciliation reported `fills=0`, `opened=0`, `closed=0`, `cash_delta=-32.00`, `equity_delta=-32.00`, and `validation_failures=0`. This is evidence that pipeline exit 0 and a generated reconciliation row do not establish clean economic reconciliation.
- The repository-level nightly checklist currently fails because required artifacts are missing and the checked-in `portfolio_orders.csv` schema is stale. Direct SQLite parity also fails: SQLite contains run/event/equity rows absent from CSV, while CSV contains processed-fill rows absent from SQLite.
- Successful pipeline status is currently written before reconciliation/parity. `emit_reconciliation_summary()` catches all exceptions, and a non-passing parity report is printed rather than raised; reconciliation/parity failure therefore does not fail the run.
- The assessment pack does not modify runtime code or the README.

## Current architecture

1. Universe reads a curated CSV and downloads history.
2. Macro and Signal produce research artifacts.
3. Risk applies setup-level screening, after which News adds review evidence.
4. Portfolio and Advisory emit proposed trades.
5. Fill consumes manually supplied fills and alone mutates cash and positions.
6. Lifecycle Integrity checks state and historical invariants.
7. Position Tracking marks open positions; Exit remains advisory.
8. Portfolio Equity and Journal write derived history and review artifacts.
9. The control plane records run start and terminal pipeline status, then attempts reconciliation and SQLite parity as advisory output. These final checks are not currently part of the success transaction.

The Backtesting and Execution agents exist as separate modules. Backtesting is not part of the normal pipeline. Execution currently formats proposed orders into a CSV review status; it does not connect to a broker or implement an order lifecycle.

## What works and should be retained

| Capability | Evidence | Decision |
|---|---|---|
| Canonical state | `portfolio_state.csv` has a registered schema and one owner | Retain the concept; change storage authority only through a gated migration |
| Mutation authority | Fill Agent owns fills, cash, positions, and processed-fill evidence | Retain as the economic-state projector |
| Idempotency | Processed fill IDs and interrupted-fill evidence checks block blind replay | Retain and generalise to broker events and commands |
| Lifecycle integrity | Shared transitions, invariants, snapshots, and closed-position immutability | Retain and extend for order/fill lifecycle |
| Deterministic orchestration | Fixed ordered pipeline and one run ID | Retain as control plane |
| Failure handling | Unresolved `running` runs block subsequent runs | Retain; add controlled recovery tooling |
| Audit/reconciliation | Append-only event and run-summary mechanisms, cash/equity/exposure deltas | Retain the design; make artifact completeness and final checks fail closed |
| Atomic CSV replacement | Same-directory temp file, flush, `fsync`, atomic replace | Retain during CSV-authoritative phase |
| SQLite shadow | WAL, transactions, parity checks, read-side preference | Retain as migration evidence, not a live concurrency solution |
| Test discipline | 84 passing tests cover the main existing controls | Retain; make Sentinel acceptance independent of implementation |
| Provider seam | `MarketDataProvider` and deterministic fake providers | Retain the seam; split it into richer capability adapters |
| Backtest v1 | Next-open entry, deterministic fixtures, costs, slippage, benchmarks, drawdown | Retain as a baseline engine, not promotion evidence |

## Limitations and missing capabilities

### Market data

- The provider contract covers only historical price frames and news.
- The default `YFinanceMarketDataProvider` uses `max_staleness_days=None`, so production-default age-based staleness never activates.
- Universe ignores `metadata.stale`; Signal, Macro, News, and Backtesting generally warn and continue.
- No live quote/trade stream, bid/ask, sequence-gap detection, snapshot recovery, venue timestamp, feed entitlement, corporate actions, economic calendar, or options-chain contract exists.
- yfinance documents itself as a research/educational client and notes personal-use constraints; it should remain a research adapter, not an execution-price authority. [yfinance documentation](https://ranaroussi.github.io/yfinance/index.html)

### Instruments and calendars

- The curated universe labels equities, ETFs, futures, FX, commodity proxies, and crypto, but ticker remains the effective key.
- Continuous futures such as `ES=F` are research series, not executable contracts with expiry, multiplier, tick size, or notice dates.
- There is no immutable internal instrument ID, time-versioned symbol map, venue MIC, option contract model, settlement model, or venue calendar service.
- Trading logic, sector mapping, sizing, and portfolio schemas remain predominantly equity-centric.

### Execution and risk

- Execution Agent is a CSV formatter with `ready` or `hold_for_review`; there is no broker interface, account sync, client-order ID, partial-fill state machine, cancel/replace, reject recovery, or reconciliation loop.
- Governance exposes `advisory_only` and booleans, not the requested Modes 0–6 as deny-by-default capabilities.
- Risk Agent performs three setup checks using hard-coded thresholds. It does not implement portfolio exposure, leverage, buying power, margin, daily/weekly loss, drawdown, concentration, correlation, spread, trading hours, event risk, or pending-order exposure.
- No persistent kill switch exists.

### Strategy validation and learning

- Backtesting uses one configured historical interval and constant basis-point cost/slippage inputs.
- No chronological train/validation/test split, walk-forward evaluation, trial registry, multiple-testing correction, parameter stability, point-in-time universe, or champion/challenger registry exists.
- Strategy promotion is described in the roadmap but not implemented.
- The journal is not yet a structured trade-memory model containing hypothesis, feature snapshot, regime, execution quality, maximum favourable/adverse excursion, benchmark return, and post-trade lesson.
- No production model or code may self-modify; learning must propose versioned challengers for offline evaluation.

### Persistence and events

- SQLite dual writes are explicitly best effort; failures warn rather than block CSV authority.
- WAL permits concurrent readers but still only one writer, which is appropriate only while the system remains on one host with one economic writer. [SQLite WAL](https://www.sqlite.org/wal.html)
- The CSV event log has immutable rows but no durable global offset, schema version, causation ID, payload hash, concurrency-safe append service, or replay contract.
- The checked-in `event_log.csv` is missing, so the repository snapshot is not a complete replay/audit package. The nightly checklist and SQLite parity both fail against the checked-in artifacts.
- Run finalization is ordered incorrectly for a safety gate: success is committed before reconciliation/parity, and reconciliation exceptions or parity mismatches are swallowed/reported without changing terminal status.
- Runtime CSVs and `data/trading_system.sqlite3` are tracked in Git, including portfolio state and processed-fill artifacts. They currently contain no discovered credentials, but this is unsuitable once data becomes personal, licensed, or live-account-derived.
- No backup/restore automation or evidenced restore drill is present.

### Dashboard and operations

- Mission Control helpers calculate status/data-health cards, but there is no web application, API, responsive UI, authentication, or Tailscale deployment definition.
- There is no scheduler for pre-market, session, monitoring, close, reconciliation, learning, or backup work.
- Observability is artifact- and console-oriented; no structured metrics, trace correlation, alert routing, service supervision, or health endpoint exists.

## Documentation drift

| Document | Drift |
|---|---|
| `docs/roadmap_2026.md` | April–August targets are past due; M0/M1/M3 statuses do not reflect implemented provider, fault, and contract work; later live dates are no longer credible gates |
| `docs/sqlite_migration_design.md` | Says it is planning-only, although phase-one shadow schema, dual writes, WAL, parity, and analytics reads exist |
| `docs/testing.md` | Windows/PowerShell-centric despite the Mac mini deployment target; the functioning local runner is `.venv-test/bin/python` |
| `README.md` | Correctly calls the platform advisory-only, but data-health language is stronger than the default freshness enforcement and Mission Control is a projection layer rather than a deployed dashboard |
| `docs/operator_runbook.md` | Recovery guidance is strong, but some repair steps still require direct CSV editing; backup/restore and Mac service procedures are incomplete |
| Schema/lifecycle docs | `closing` appears in allowed schema values while the documented canonical lifecycle is `open → exit_required → closed` |

## Security assessment

### Existing strengths

- `.env` and `.env.*` are ignored.
- No broker client or committed broker credentials were found.
- Broker access and order submission are disabled in governance.
- The repository does not currently expose a network service.
- Economic-state mutation is constrained and audited.

### Gaps before any connected environment

1. Remove live/runtime state and the SQLite database from future source-control tracking; retain sanitized fixtures only.
2. Define secrets through host-level environment/service configuration or a secret manager; never log secrets or include them in prompts and events.
3. Use separate paper and live credentials, endpoints, ports, and operating-system identities.
4. Give broker credentials least privilege and disable withdrawal or unrelated account capabilities where the broker supports it.
5. Add dependency-vulnerability and secret scanning in CI; there is no CI workflow or dependency lock/evidence manifest today.
6. Normalize `requirements.txt`, which is UTF-16 LE, into the standard packaging approach during a controlled tooling change.
7. Bind any dashboard only to loopback and expose it through Tailscale Serve; do not use Funnel. Tailscale recommends Serve for tailnet-only services and Funnel for public exposure. [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve), [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
8. Keep the initial dashboard read-only. Approval, kill, and reactivation actions need separate authorization, confirmation, CSRF protection, idempotency, and audit records.

## Ranked technical debt

### Critical

| Debt | Why critical | Required disposition |
|---|---|---|
| Freshness is not a universal hard gate | Stale or unknown data can still influence advice | Implement calendar-aware, data-class-aware fail-closed policies before live paper data |
| No independent pre-trade risk engine or kill switch | A broker path would have no deterministic last line of defence | Add and independently test before any sandbox submission |
| No broker/order lifecycle or reconciliation adapter | Retries, partial fills, rejects, and reconnects cannot be handled safely | Build read-only sync first, then paper submission behind modes and risk |
| Ticker-based multi-asset model | Wrong contract/venue/session can create economically wrong orders | Add immutable instrument master and calendars before derivatives/FX execution |
| Execution modes are not capability-isolated | A string or boolean change could eventually expose live functions | Implement deny-by-default capability matrices and separate credentials |

### High

| Debt | Consequence | Disposition |
|---|---|---|
| Event log is not a replay-safe event plane | Concurrent and retry-driven intraday processing is unsafe | Add versioned envelopes, offsets, idempotent consumers, and durable outbox |
| CSV authority cannot scale to concurrent live services | Lost updates and append races become plausible | Complete SQLite transactional authority gate; assess PostgreSQL only on demonstrated need |
| Backtest lacks out-of-sample/promotion controls | Strategy selection can overfit | Add walk-forward, trial registry, multiple-testing correction, and independent promotion |
| Tracked runtime/economic data | Privacy, licensing, and accidental live-data disclosure risk | Untrack runtime state after backup and provide sanitized fixtures |
| No backup/restore drill | Recovery claims are incomplete | Automate backups and prove restore plus reconciliation |
| Reconciliation/parity are advisory after success | A run can be recorded successful despite missing artifacts or divergent SQLite/CSV evidence | Add current-run artifact manifests, run final checks before success, and fail closed on required reconciliation/parity errors |
| Reconciliation semantics miss unexplained deltas | The disposable accepted-patch run remained successful with zero fills but a `-32.00` cash/equity delta and zero validation failures | Define conservation checks and make unexplained economic deltas block success |
| No real dashboard auth/deployment | Remote operational control cannot be safely exposed | Build read-only API/UI behind Tailscale Serve and grants |

### Medium

| Debt | Consequence | Disposition |
|---|---|---|
| Hard-coded risk and sector thresholds | Config drift and equity bias | Move to versioned, asset-aware policy definitions |
| No unified scheduler/service supervision | Intraday tasks may overlap or silently stop | Add an explicit schedule and supervised services after replay mode |
| Weak structured observability | Latency, drift, and partial failure are hard to diagnose | Add JSON logs, metrics, traces, alert severity, and run/event correlation |
| Windows-only test documentation | Mac operations are not reproducible from docs | Add macOS/Linux bootstrap and CI commands |
| No dependency/CI evidence | Builds may not be repeatable or continuously checked | Add packaging metadata, locked environments, CI, and security checks |
| Documentation milestone drift | Operators may trust obsolete dates/status | Replace calendar promises with evidence gates |

### Low

| Debt | Consequence | Disposition |
|---|---|---|
| Duplicate/legacy CSV artifacts | Confusing ownership and stale reads | Classify, deprecate, archive, then remove under tests |
| Naming inconsistency (`ticker`, `asset_type`, `asset_class`) | Adapter and schema friction | Standardize during instrument-master work |
| Console-oriented agent output | Poor machine observability | Preserve human summaries while adding structured output |

## Audit decision

Major refactoring is not authorized by this assessment. Phase 1 should harden the existing advisory engine and establish contracts and acceptance gates. Broker sandbox submission remains deferred until freshness, instrument identity, modes, independent risk, kill-switch behavior, replay, recovery, and Sentinel acceptance are evidenced.
