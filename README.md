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

## Virtual-functionality milestone

The current objective is correct operation in a virtual trading environment,
with **no capital-return target**. An offline accounting and replay check is
available without credentials or broker access:

```bash
uv run --frozen python scripts/validate_virtual_backtest.py --synthetic --output-dir runtime/validation/virtual-backtest-example
```

Use a new output directory for each run. A pass proves the stated functional
checks on synthetic data, not profitability or live demo qualification. See
[backtest validation](docs/virtual_backtest_validation.md) for assumptions,
saved-input replay and acceptance checks, and
[demo execution readiness](docs/virtual_demo_execution_readiness.md) for the
remaining broker-integration work. Demo and real submission stay disabled.

The next [operator workflow milestone](docs/demo_operator_milestone.md) adds
an isolated approval-to-fill exercise and a fail-closed read-only account
preflight. The [first manual Demo cycle](docs/first_manual_demo_cycle.md)
implements versioned risk/accounting, a supported connector bridge, exact
approval, durable single send, lookup recovery and Fill-owned Decimal economics.
Its normal and crash-recovery exercises are synthetic; live submission remains
blocked by account risk and missing authoritative source coverage. See
[market coverage](docs/market_coverage.md) for the curated 61-instrument scanner
and separate 12-stock backtest; neither is a whole-market scan. The 12-stock
replay is survivor-biased. An optional S&P 500 point-in-time file is described
in `data_sources/sp500/README.md`.

The [historical pilot checkpoint](docs/historical_pilot_milestone.md) validates
saved eToro daily prices for the optional six-stock pilot and both benchmarks.
Its 599-observation replay passes functional checks, with source, calendar and
corporate-action limits recorded explicitly. It grants no trading permission.

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

## Paper signal log

Advisory and exit views that are already a buy, sell, or hold are appended to
`runtime/logs/signal_log.jsonl`. The log does not place or cancel orders. A
blocked advisory row, an exit review, a raise-stop, and an already-closed stub
are not recorded. Re-running the same view for the same run keeps the first
line.

Print the log, each horizon, and the summary:

```bash
uv run --frozen python scripts/paper_signal_log.py
```

Write the same report as Markdown:

```bash
uv run --frozen python scripts/paper_signal_log.py --markdown runtime/logs/paper_signal_report.md
```

`--as-of 2026-11-22T00:00:00+00:00` fixes the evaluation clock. `--log` and
`--config` override `config/paper_signal_log.yaml`. The cost assumption is
`cost_bps` in that file (default 10 basis points, a round-trip spread plus
fee, not a measured broker charge). The summary prints the value it applied.
Buy and sell rules subtract it from the side the signal chooses. Hold rules
subtract nothing.

Horizons are 1, 3, 6, and 12 months. A horizon that has not reached a known
close is pending and is not estimated. The benchmark is Yahoo `VWRL.L`, the
London listing of the Vanguard FTSE All-World UCITS ETF. The score is in GBP.
USD listings use the Yahoo close `GBPUSD=X` (USD per GBP). EUR listings use
`EURGBP=X` (GBP per EUR). JPY listings use `GBPJPY=X` (JPY per GBP). `GBp`
and `GBX` are pence and are divided by 100. A pound price against a pence
feed is left unscored. The signal FX close is the last close strictly before
the signal's UTC date. The outcome FX close is the first close on or after
the horizon and before the evaluation date. A missing FX close is
`fx_missing` and is not replaced with the local-currency return.
`ticker_return_gbp` and `benchmark_return_gbp` are the GBP returns used for
the excess. `ticker_return_local` and `benchmark_return_local` keep each
listing's own quote. The hit rules are printed at the bottom of the command
output and next to the code in `shared/paper_signal_log.py`.

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

The separately selected [bounded Demo integration pilot](docs/demo_pilot_v1.md)
uses an immutable attribution ledger, supported MCP evidence and exact human
order approval. The existing strict policy and default write settings remain
unchanged. A pilot implementation or preview does not establish a confirmed fill.

## Mac mini market-hours schedule

A LaunchAgent can run `run_pipeline.py` on the owner's Mac mini during the NYSE
regular session. It is shipped **disabled**. Merging this repository does not
install the agent, load it, or run the pipeline. Broker, governance, and
scheduled-Demo preparation flags stay off; the schedule does not change them.
Order placement remains gated by those existing flags.

Two switches have to be on before a pipeline run starts:

- `host_schedule.enabled` in `config/scheduler.yaml` (default `false`)
- the LaunchAgent loaded with `launchctl bootstrap` (the installer does not do this)

`launchd` only fires on a fixed interval (`host_schedule.interval_seconds`,
default 900). The wrapper then checks `America/New_York` and the repository's
XNYS calendar (weekends, holidays, early closes) and exits when the session is
closed. London wall time is not used, because the US and UK change clocks on
different dates. A lock at `runtime/control/schedule.lock` skips an overlapping
run. The same lock is used by scheduled Demo preparation.

Logs go to `runtime/logs/host_schedule/` (rotated) and, once installed, to
`runtime/logs/launchd.stdout.log` and `runtime/logs/launchd.stderr.log`. Those
paths are gitignored. Secret values are never written to them.

This agent runs in the GUI session (`gui/$UID`). The owner must be logged in
(the screen may be locked). It does not start at the login window. After a
reboot, automatic login is what keeps it running unattended. A locked login
keychain fails closed: the pipeline does not start.

### Install

From the repository root, after `uv sync --frozen`:

```bash
bash scripts/install_launch_agent.sh
```

The script renders `scheduler/launchd/com.investing-agents.pipeline.plist.template`
into `~/Library/LaunchAgents/com.investing-agents.pipeline.plist` using the
current interval and the virtualenv Python. It boots out a previous job with
the same label if one was loaded. It does not bootstrap the new job.
`RunAtLoad` is false. Re-run the installer after changing
`interval_seconds` or moving the repository.

### Add credential names

`host_schedule.secrets` lists environment variable names and Keychain
coordinates. It ships empty. When the broker connector's variable names are
known, add them. Do not put secret values in the yaml file.

```yaml
secrets:
  - env: REPLACE_WITH_BROKER_ENV_VAR
    service: investing-agents-demo
    account: REPLACE_WITH_KEYCHAIN_ACCOUNT
```

Create the Keychain item in the login keychain. The command prompts for the
secret so it is not stored in shell history:

```bash
security add-generic-password \
  -s "investing-agents-demo" \
  -a "REPLACE_WITH_KEYCHAIN_ACCOUNT" \
  -U
```

The wrapper reads it with `security find-generic-password -s <service> -a <account> -w`
and exports it only into the `run_pipeline.py` child process.
`security ... -w` prints the secret; do not redirect that output into the repository.

### File alternative

Set `secrets_source: file` to read `runtime/secrets/launchd.env` instead of the
Keychain. That path is gitignored. The wrapper refuses the file unless it is a
regular file and no group or world permission bit is set (`chmod 600`).

```bash
mkdir -p runtime/secrets
install -m 600 /dev/null runtime/secrets/launchd.env
```

Edit the file locally so each configured name has one `KEY=value` line, then
confirm the mode:

```bash
chmod 600 runtime/secrets/launchd.env
stat -f '%Lp' runtime/secrets/launchd.env
```

`stat` should print `600`. A group-readable or world-readable file is not loaded.

### Enable

1. Set `host_schedule.enabled: true` in `config/scheduler.yaml`.
2. Load the agent:

```bash
launchctl bootstrap gui/$(id -u) "$HOME/Library/LaunchAgents/com.investing-agents.pipeline.plist"
```

The first fire is on the interval, not at load. To exercise the gate once by hand:

```bash
.venv/bin/python scripts/run_host_schedule.py
```

That command still exits immediately while the switch is off or the NYSE session
is closed. It does not turn broker writes on.

### Disable

Stop launchd without deleting the plist:

```bash
launchctl bootout gui/$(id -u)/com.investing-agents.pipeline
```

Setting `host_schedule.enabled: false` also makes a loaded agent exit without
running the pipeline. Use both when you want it fully stopped.

### Status

```bash
.venv/bin/python scripts/scheduler_status.py
```

This prints whether `launchctl print` sees the agent, the enable switch,
scheduled-Demo and broker-submission flags, the last run time and exit code,
the last error, the next expected fire (estimated from the interval, in UTC and
New York time), and whether the lock is held. Equivalent direct check:

```bash
launchctl print "gui/$(id -u)/com.investing-agents.pipeline"
```

### Uninstall

```bash
bash scripts/uninstall_launch_agent.sh
```

This boots the job out and deletes the plist. It does not edit
`config/scheduler.yaml`. Set `host_schedule.enabled: false` yourself if you
had turned it on. A leftover `runtime/control/schedule.lock` means a run was
still holding the lock or was killed. Confirm no pipeline process is running
before removing that file; do not delete it while a run is active.

### Recover a stuck lock

The lock fails closed. Status shows the pid. If that pid is not running, remove
`runtime/control/schedule.lock` only after you have confirmed the pipeline is
not still running.

## Disclaimer

This repository is for portfolio analysis, governance, and advisory workflow development.

It does not constitute financial advice. Any real-world execution should remain subject to manual review, independent judgement, and appropriate risk controls.
