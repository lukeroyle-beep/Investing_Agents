# Design Review Card: Agent Failure Modes

This card is a pre-flight review for new automation in the Trading Agent Pipeline.

The project goal is not just to produce trade ideas. The goal is to produce trade ideas that are grounded, auditable, advisory-only, and safe to reject.

## Failure mode 1: Hallucination cascades

### Risk

One weak or wrong assumption becomes the foundation for downstream signal, risk, portfolio, and advisory outputs.

### Trading-pipeline examples

- Treating stale or partial market data as live.
- Accepting a macro regime label without checking the proxy data behind it.
- Producing trade advice without current-run evidence from signal, risk, news, and portfolio agents.
- Assuming a ticker identity is correct across exchanges, currencies, or providers.

### Required controls

- Every advisory recommendation must trace back to current-run artifacts.
- Market data must carry source-health metadata: source, fetched_at, as_of, stale, error, retry_count.
- Agents must distinguish:
  - fact: directly observed artifact or provider result,
  - estimate: derived metric or model score,
  - hypothesis: unproven strategic interpretation.
- Trade advice should be rejected or held if evidence is missing, stale, or internally inconsistent.

### Halt conditions

- Missing evidence bundle.
- Missing or stale data-source health record.
- Advisory output not tied to current `run_id`.
- Macro/news/signal assumptions cannot be traced to current artifacts.

## Failure mode 2: Reward misalignment

### Risk

The system optimises for appearing productive, generating trades, or completing the run instead of protecting capital and preserving trust.

### Trading-pipeline examples

- Marking a run successful despite reconciliation warnings.
- Treating more trade candidates as better output.
- Promoting a strategy because it performs well in-sample but fails out-of-sample.
- Continuing after a partial provider failure because enough rows were fetched.

### Required controls

- Success is composite:
  - correctness,
  - governance compliance,
  - evidence completeness,
  - reconciliation cleanliness,
  - cost/risk discipline.
- A miss on safety or evidence is a fail, not a partial success.
- Strategy refinement must use out-of-sample and walk-forward validation before promotion.
- Live trading requires explicit human approval and governance gates.

### Halt conditions

- Reconciliation failure.
- Parity failure affecting economic state.
- Missing manual approval where required.
- Strategy promotion without out-of-sample evidence.
- Run marked successful while critical warnings remain unresolved.

## Failure mode 3: Brittle tool use

### Risk

A tool or file is called with the wrong contract, a CSV shape drifts, a partial failure is ignored, or a retry corrupts state.

### Trading-pipeline examples

- Advisory reading a stale file instead of current Portfolio output.
- Duplicate fill processing.
- Closed-position fields changing after closure.
- Direct yfinance calls returning inconsistent shapes across agents.
- Reruns after interruption without resolving prior `running` run history.

### Required controls

- Deterministic orchestration through `run_pipeline.py`.
- CSV remains authoritative until a migration is explicitly proven.
- Fill Agent remains the only economic-state mutator.
- Schema checks and invariant checks run before trusting outputs.
- Writes use atomic discipline.
- Interrupted runs fail closed.
- Tool/provider calls go through typed adapters where possible.

### Halt conditions

- Closed-position economic field mutation.
- Duplicate `fill_id`.
- Missing required schema column.
- Previous run still marked `running`.
- Tool/provider returns partial data and no stale/error flag is recorded.

## New automation pre-flight checklist

Before wiring any new agent, automation, dashboard action, strategy loop, or broker integration, answer these:

1. **Inputs grounded?**
   - Which files/provider results are facts?
   - Are timestamps and `run_id`s current?

2. **Assumptions tagged?**
   - What is fact, estimate, or hypothesis?
   - Which assumptions can block execution if unproven?

3. **Critical prove-it gates defined?**
   - What must be checked by schema, invariant, reconciliation, parity, or source-health evidence?

4. **Reward shape safe?**
   - Does success require accuracy, safety, completeness, evidence, and cost/risk discipline?

5. **Tool contracts locked?**
   - Are schemas versioned or validated?
   - Are provider results typed or normalised?

6. **Single source of truth preserved?**
   - Which file/table is authoritative?
   - Which component can mutate it?

7. **Audit bundle emitted?**
   - Can a human reconstruct why the output was produced?
   - Are before/after states logged for mutations?

8. **Kill switches active?**
   - What blocks continuation?
   - What requires manual sign-off?

## Pipeline-specific evidence bundle

A recommendation is not eligible for manual review unless it can point to:

- current `run_id`,
- market-data source-health rows,
- universe/signal evidence,
- macro regime evidence,
- news review/flags,
- risk decision and notes,
- portfolio order/proposal row,
- governance checks,
- open-position block result,
- advisory output row,
- reconciliation outcome for the run.

## Current implementation status

Already implemented or started:

- deterministic pipeline orchestration,
- advisory-only governance config,
- single Fill Agent economic mutation boundary,
- closed-position immutability invariants,
- duplicate fill checks,
- interrupted-run fail-closed guard,
- current-run Portfolio to Advisory handoff,
- shared market-data provider first slice,
- source-health artifact,
- nightly operational checklist.

Still needed:

- evidence bundle attached to each advisory recommendation,
- current-run source-health enforcement rather than warning-only reporting,
- provider migration for Macro, News, Signal, Backtesting, and Position Tracking,
- strategy promotion gates with out-of-sample evidence,
- dashboard card showing failure-mode status,
- broker sandbox and live-capital kill switches.
