# Trading Agent Pipeline Roadmap 2026

Roadmap baseline date: 2026-04-24.

This project remains advisory-only until explicit governance gates are met. The system must prove correctness, resilience, auditability, and controlled behaviour before any live-capital pilot.

## Operating principle

The goal is not to rush into live trading. The goal is to build a system that can:

1. backtest repeatably,
2. trade virtually against live market data,
3. learn/refine strategy parameters under controlled evaluation,
4. survive failures without unsafe mutation,
5. produce explainable advisory decisions,
6. and only then run a small live-capital pilot under strict governance.

## Milestones

### M0 — Safety baseline and first universe expansion

Target: 2026-04-24
Status: in progress / mostly complete

Deliverables:

- Interrupted-run recovery guard.
- Fault-injection fail-closed tests.
- Curated multi-asset universe source.
- Universe Agent metadata validation and normalisation.
- Full local test suite passing.

Exit criteria:

- Tests pass locally.
- Repo remains advisory-only.
- No broker execution path exists.

### M1 — Data-provider foundation

Target: 2026-05-01

Deliverables:

- Shared market data provider abstraction.
- yfinance adapter behind provider interface.
- Local cache for OHLCV and metadata.
- Rate limiting, retry/backoff, stale-data flags.
- Data-source health artifact.
- Deterministic tests using mocked provider responses.

Exit criteria:

- Universe, Signal, Macro, News, Backtesting, and Position Tracking no longer call yfinance directly.
- Pipeline can report partial/stale data instead of silently trusting it.

### M2 — Backtesting engine v1

Target: 2026-05-08

Deliverables:

- Deterministic backtest runner using historical OHLCV fixtures/cache.
- Strategy parameter config.
- Transaction-cost/slippage model v1.
- Position sizing model v1.
- Benchmark comparison against SPY / QQQ / relevant index proxy.
- Backtest output artifacts: trades, equity curve, drawdown, win/loss, exposure, turnover.

Exit criteria:

- Backtests are reproducible from fixed inputs.
- Results include realistic costs and drawdown metrics.
- No forward-looking data leakage in test fixtures.

### M3 — Portfolio/advisory contract repair

Target: 2026-05-15

Deliverables:

- Fix Portfolio Agent to Advisory Agent handoff.
- Remove stale/static recommendation dependency.
- Schema tests for portfolio orders/recommendations/advisory trades.
- End-to-end pipeline test proving generated portfolio output feeds advisory output.

Exit criteria:

- Advisory output is generated from the current pipeline run, not stale CSV state.
- Contract is schema-validated.

### M4 — Virtual trade environment v1

Target: 2026-05-22

Deliverables:

- Paper-trading/virtual-broker ledger.
- Simulated order lifecycle: proposed → manually approved virtual order → virtual fill → portfolio state update.
- Virtual cash, positions, realised/unrealised PnL, equity history.
- Daily virtual run mode.
- No real broker integration.

Exit criteria:

- System can trade virtually across multiple runs.
- Fill Agent remains the only economic-state mutator.
- Every virtual fill is auditable and reconcilable.

### M5 — Strategy evaluation and refinement loop

Target: 2026-06-05

Deliverables:

- Strategy scoring framework.
- Parameter sweep / walk-forward evaluation.
- Out-of-sample validation split.
- Overfitting guards.
- Candidate strategy promotion rules.
- Strategy version registry.

Exit criteria:

- Strategy changes are evaluated against historical and out-of-sample windows.
- No strategy can self-promote without meeting explicit metrics.

### M6 — Dashboard integration v1

Target: 2026-06-12

Deliverables:

- Read-only dashboard summary artifact or SQLite view.
- Mission Control dashboard cards for:
  - latest run status,
  - reconciliation,
  - parity,
  - data-source health,
  - universe scan counts,
  - top leads,
  - advisory trades,
  - open positions,
  - exits/alerts,
  - virtual performance.

Exit criteria:

- Dashboard is read-only.
- No UI path can mutate economic state.

### M7 — Live-market paper trading pilot

Target: 2026-06-26

Deliverables:

- Scheduled live-market paper trading runs.
- Daily/weekly performance reports.
- Drift/staleness alerts.
- Manual review workflow for advisory decisions.
- Governance checklist before simulated fills.

Exit criteria:

- At least 2 weeks of stable daily paper runs.
- No unresolved reconciliation/parity failures.
- Human-readable trade rationale for every recommendation.

### M8 — Extended paper-trading validation

Target: 2026-07-31

Deliverables:

- 4–6 weeks of paper-trading history.
- Drawdown, Sharpe-like, hit-rate, exposure, and benchmark-relative reporting.
- Failure and recovery drills.
- Strategy freeze candidate.
- Live-trading governance policy draft.

Exit criteria:

- Strategy beats agreed benchmark/risk criteria over paper window, or live trading is deferred.
- Recovery drills pass.
- Governance policy reviewed before any broker integration.

### M9 — Broker integration sandbox only

Target: 2026-08-14

Deliverables:

- Broker API integration in sandbox/paper mode only.
- Read-only account sync first.
- Sandbox order submission guarded by explicit approvals.
- Kill switch and maximum order/exposure controls.
- Credentials isolated from repo.

Exit criteria:

- Broker sandbox works without touching real capital.
- Kill switch tested.
- Manual approval required for every sandbox order.

### M10 — Live-capital readiness review

Target: 2026-08-28

Deliverables:

- Live-readiness checklist.
- Governance controls:
  - max capital allocation,
  - max position size,
  - max daily loss,
  - max drawdown stop,
  - allowed instruments,
  - excluded instruments,
  - manual approval requirements,
  - emergency halt process.
- Evidence pack from backtests, paper trading, sandbox broker testing, and recovery drills.

Exit criteria:

- Human sign-off required.
- No unresolved P0/P1 defects.
- Live pilot may still be rejected if evidence is weak.

### M11 — Limited live-capital pilot

Earliest target: 2026-09-11

Deliverables:

- Small capital allocation only.
- Highly restricted instrument set.
- Manual approval before each live order.
- Daily reconciliation and post-trade review.
- Automatic halt on governance breach.

Exit criteria:

- 2–4 weeks of clean live pilot behaviour.
- No uncontrolled execution.
- No unexplained state mutation.

### M12 — Controlled live expansion decision

Target: 2026-10-09

Deliverables:

- Pilot review.
- Expand, hold, or roll back decision.
- If expanding, increase capital only gradually and keep governance gates.

Exit criteria:

- Expansion only if live pilot meets predefined risk-adjusted criteria.

## Live-trading gate

Live trading with real capital should not begin before 2026-09-11, and only if all earlier gates pass.

Any failure in these areas automatically delays live trading:

- unresolved reconciliation failure,
- SQLite/CSV parity failure affecting economic state,
- stale or partial market data without explicit handling,
- unexplained portfolio-state mutation,
- failed recovery drill,
- strategy underperformance versus benchmark/risk threshold,
- missing manual approval control,
- missing kill switch,
- broker sandbox instability.

## Immediate next build priorities

1. Shared data-provider layer.
2. Cache/rate-limit/source-health logging.
3. Portfolio to Advisory contract repair.
4. Backtest runner v1.
5. Virtual broker ledger.
6. Dashboard summary artifact.
