# Advisory Portfolio Engine

A deterministic, advisory-only portfolio engine with controlled mutation, lifecycle enforcement, audit logging, reconciliation, and shadow database persistence.

This project is designed to move beyond a loose chain of scripts into a state-governed portfolio system with clear authority over state, explicit controls around mutation, and a credible path to stronger operational resilience.

## Development environment

Python 3.12 is the operational baseline. Dependencies are declared in
`pyproject.toml` and reproducibly pinned in `uv.lock` using `uv` 0.11.6.

```bash
uv sync --frozen --all-groups
uv run --frozen python -m pytest -q
```

CI rejects undeclared imports and runs the tests, compilation, and dependency
consistency checks on Python 3.11 through 3.13 across Linux and macOS. A
separate security workflow runs `pip-audit` and scans complete Git history with
Gitleaks on both platforms.
`requirements.txt` is a generated UTF-8 compatibility export; it is not the
dependency source of truth.

## Core characteristics

- Deterministic orchestration through `run_pipeline.py`
- Advisory-by-default execution model with per-order human sign-off
- Disabled eToro Demo execution integration; no autonomous or Real submission
- Canonical portfolio state held in untracked `runtime/state/portfolio_state.csv`
- Hard mutation boundary where the Fill Agent is the only component allowed to alter economic state
- Explicit lifecycle progression: `open` → `exit_required` → `closed`
- Closed-position immutability to protect historical economic facts
- Shared schema, validation, and invariant controls across agents
- Append-only audit logging with timestamps, run identifiers, and before/after state where relevant
- Run reconciliation for cash, Profit and Loss (PnL), equity, exposure, and activity checks
- Fail-closed run finalisation with per-run artifact manifests and economic-state checksums
- Centralized exchange-calendar freshness decisions with explicit `normal`, `degraded`, and `no_trade` modes
- Checksum-verified runtime bootstrap, backup, restore, schema migration, and interrupted-run recovery tools
- Portfolio performance memory including equity history, peak equity, drawdown tracking, and summary metrics
- Atomic Comma-Separated Values (CSV) write discipline to reduce partial-write risk
- SQLite (Structured Query Language Lite) shadow persistence with parity checks while CSV remains authoritative
- Test coverage for idempotency, lifecycle enforcement, invariants, non-mutating agents, and end-to-end smoke paths
- Immutable strategy experiments and advisory-only promotion controls
- Disabled scheduled-Demo preparation with durable qualification evidence

## Architecture summary

The engine is built around one central principle:

**economic state must have a single authority and a tightly controlled mutation path.**

In practical terms, that means:

- `runtime/state/portfolio_state.csv` is canonical for holdings and lifecycle; `cash_state.csv` is canonical for cash
- only the Fill Agent can mutate economic history
- all other agents read state and produce advice, analytics, or controls
- lifecycle transitions are validated and enforced
- `portfolio_monitor.csv` is the separate, non-economic authority for current marks
- closed positions cannot be silently rewritten
- every run can be audited and reconciled

This is what allows complexity to grow without turning the system into an opaque or untrustworthy process.

## System flow

A typical pipeline run follows this sequence:

1. Universe
2. Macro
3. Signal
4. Risk
5. News
6. Data Freshness Gate
7. Portfolio
8. Advisory
9. Fill
10. Lifecycle Integrity
11. Position Tracking
12. Lifecycle Integrity
13. Exit
14. Portfolio Equity
15. Journal

This sequencing is intentional.

Mutation is separated from analysis and control. Validation gates sit around the state transition boundary so invalid state does not propagate downstream.

## Agent responsibilities

### Universe Agent
Builds the investable opportunity set and outputs watchlists, leads, rejects, and universe snapshots.

### Signal Agent
Evaluates technical or systematic setups and produces ranked signal candidates.

### Macro Agent
Assesses broad market regime and supporting proxy indicators.

### News Agent
Flags news-sensitive instruments and applies severity-aware review logic.

### Risk Agent
Applies portfolio and trade-level controls, including vetoes, caution states, and shortlist approval.

### Portfolio Agent
Constructs candidate trades and position proposals while respecting portfolio limits and regime-aware constraints.

### Advisory Agent
Produces advisory trade outputs only. It does not execute trades. It enforces governance rules such as open-position blocks and news-based holds.

### Fill Agent
The only economic mutator in the system. It processes fills and updates canonical portfolio state, cash, and realised outcomes.

### Lifecycle Integrity Agent
Validates lifecycle rules, invariant compliance, and historical integrity. It acts as a hard control gate.

### Position Tracking Agent
Builds `portfolio_monitor.csv`, the mark-to-market read model for active positions. It never rewrites Fill-owned `portfolio_state.csv`; current price, market value, unrealised P&L, and high/low marks are authoritative only in the monitor.

### Exit Agent
Produces exit advice based on current position conditions and system rules without mutating state.

### Portfolio Equity Agent
Calculates portfolio-level equity, history, peak equity, and drawdown metrics.

### Journal Agent
Records end-of-run outputs and supports operating review.

## Mission Control status model

Mission Control lifecycle rows use `shared.mission_control_status` to project operational state into a compact dashboard status:

- `closed` lifecycle rows always render as `Idle`
- explicit or derived waits render as `Blocked`
- `open` / `exit_required` rows with a busy `agent_status` or truthy `work_in_progress` render as `Busy`
- all other rows render as `Idle`

Blocked rows still count as WIP/utilisation, but are shown separately with `mission_control_blocked_cause` and `mission_control_blocked_since`. The roll-up chip format is deterministic: `Busy n | Blocked m | Idle k`.

Expected optional fields, when available: `lifecycle`/`status`, `agent_status`, `work_in_progress`, `blocked_flag`, `blocked_reason`, `blocked_since`, `manual_signoff_required`, `external_approval_required` or `approval_status`, `waiting_on_data` or `data_status`, `news_review_status`, `risk_review_status`, and owner-engagement timestamps for stale `exit_required` rows.

## Governance model

The governance model is intentionally strict.

### Execution mode
- Advisory-only
- No automatic broker interaction
- Manual sign-off required before any real-world execution

### State authority
- `runtime/state/portfolio_state.csv` is canonical
- `runtime/state/portfolio_monitor.csv` is authoritative only for current marks
- CSV remains authoritative even with SQLite shadow persistence
- the Fill Agent is the only component allowed to alter economic state

### Lifecycle rules
Positions must move through the following states only:

- `open`
- `exit_required`
- `closed`

Invalid transitions should fail validation.

### Closed-position immutability
Once a position is closed, key historical fields must not change. This protects economic truth and prevents silent corruption of realised history.

### Validation discipline
Shared schemas and invariants are used to remove agent-by-agent interpretation drift and provide one consistent control layer.

## Persistence model

The storage design is conservative by intent.

### Authoritative layer
- CSV files remain the operational source of truth
- atomic writes reduce the risk of partial file corruption

### Shadow layer
- SQLite receives dual writes for persistence hardening
- parity checks verify equivalence between CSV and SQLite outputs
- mutation authority has not been moved to SQLite

This preserves operational simplicity while building confidence in stronger persistence.

## Audit and reconciliation

Every pipeline run is designed to be explainable.

### Event logging
The system maintains append-only audit logs with:

- event identifiers
- run identifiers
- timestamps
- agent names
- event types
- severity
- entity references such as ticker, position, or order identifiers
- before/after JSON (JavaScript Object Notation) payloads where relevant
- metadata payloads where relevant

### Run reconciliation
After each run, the system can reconcile:

- fills processed
- positions opened
- positions closed
- positions marked `exit_required`
- cash movement
- realised PnL
- unrealised PnL
- equity movement
- exposure change
- validation warnings or failures

This provides a practical control against silent drift.

### Run finalisation

Each run progresses through `started → validating → succeeded|failed`.
`succeeded` is unavailable until required artifacts and schemas validate,
economic checksum changes are explained, lifecycle and freshness gates pass,
reconciliation passes, and required CSV/SQLite tables are in parity. The
durable proof lives under `runtime/runs/<run_id>/`; missing or corrupt proof is
treated as failure during interrupted-run recovery.

## Performance tracking

The engine retains portfolio performance memory across runs, including:

- portfolio equity snapshots
- equity history
- peak equity
- drawdown in absolute terms
- drawdown in percentage terms
- one-row summary metrics for quick review

This creates a persistent operating record rather than a single-run snapshot.

## Testing and control coverage

The current test and validation layer covers the most important control surfaces, including:

- idempotency checks
- invariant enforcement
- lifecycle enforcement
- non-mutating agent guarantees
- end-to-end smoke coverage
- parity validation between CSV and SQLite

The purpose of this layer is not just correctness. It is to prove repeatability and fail-closed behaviour.

## Repository structure

The exact structure may evolve, but the current build is organised around:

```text
agents/
  universe_agent/
  signal_agent/
  macro_agent/
  news_agent/
  risk_agent/
  portfolio_agent/
  advisory_agent/
  fill_agent/
  lifecycle_integrity_agent/
  position_tracking_agent/
  exit_agent/
  portfolio_equity_agent/
  journal_agent/
  shared/
config/
data_sources/        # deliberately versioned source inputs
runtime/             # ignored local state, runs, control, cache, logs, backups
  state/
  runs/
  control/
  cache/
  logs/
  backups/
tests/fixtures/       # deliberately versioned sanitized test data
docs/
scripts/
run_pipeline.py
```

## Operating principles

This project should be expanded conservatively.

### What is already strong
- deterministic orchestration
- canonical state authority
- strict mutation boundary
- lifecycle enforcement
- immutable closed history
- append-only auditability
- reconciliation discipline
- shadow database persistence with parity checking

### What still matters most
The main risk is no longer lack of architecture. The main risk is operational slippage.

That means:

- adding new features too early
- weakening validation discipline
- broadening mutation authority prematurely
- assuming resilience before interrupted-run and recovery paths are proven

## Recommended next steps

### 1. Prove repeatability
Run the pipeline repeatedly against controlled fixtures and stable inputs to prove:

- no unintended state drift when no new fills occur
- stable CSV and SQLite parity
- closed-position immutability
- stable idempotent behaviour
- reconciliation consistency across repeated runs

### 2. Prove fail-closed behaviour
Deliberately trigger faults and confirm the system stops cleanly for cases such as:

- malformed schemas
- invalid lifecycle transitions
- snapshot mismatches
- duplicate or conflicting fills
- parity failures
- missing critical files

### 3. Add interrupted-run recovery discipline
Introduce explicit rules for:

- interrupted run detection
- safe replay versus manual intervention
- duplicate fill prevention after crash or stop
- continuation blocking when state safety is uncertain

Current implementation now fails closed when `run_history.csv` contains a prior
`running` row. See `docs/operator_runbook.md` for the operator process before
marking an interrupted run terminal or retrying the pipeline.

### 4. Write the operator runbook
Document:

- clean-machine bootstrap
- pre-run checklist
- post-run checklist
- failure triage
- parity-failure handling
- backup and restore rules
- manual repair boundaries

### 5. Keep storage migration conservative
Use SQLite first for read-side analytics and reporting. Do not move mutation authority until parity proof is sustained over time.

### 6. Expand strategy only after operational proof
Once repeatability, fail-closed behaviour, and recovery controls are proven, then expand:

- signal quality
- portfolio construction sophistication
- data source breadth
- analytics depth
- reporting automation

## Intended use

This system is intended for advisory portfolio workflows where:

- recommendations are generated systematically
- controls are explicit and testable
- execution remains human-approved
- traceability matters as much as output quality

It is not designed as an autonomous live trading engine in its current form.

## Status

Current build status:

- advisory-only portfolio engine: **implemented**
- deterministic orchestration: **implemented**
- controlled mutation boundary: **implemented**
- lifecycle enforcement: **implemented**
- audit logging and reconciliation: **implemented**
- performance history and drawdown tracking: **implemented**
- atomic CSV write discipline: **implemented**
- SQLite shadow persistence with parity checks: **implemented**
- interrupted-run start guard: **implemented**
- operator runbook and recovery discipline: **initial pass implemented**
- pipeline failure terminalization coverage: **implemented**
- broader operational hardening and fault-injection coverage: **in progress**
- immutable execution domain and operational command ledger: **implemented, disabled by default**
- independent Demo pre-trade risk, approval, and kill switch: **implemented, writes disabled**
- eToro Demo Gate A contract adapter: **read-only implementation; connected MCP Demo smoke passed, repository adapter remains disabled by default**

## Disclaimer

This repository is for portfolio analysis, governance, and advisory workflow development.

It does not constitute financial advice. Any real-world execution should remain subject to manual review, independent judgement, and appropriate risk controls.
