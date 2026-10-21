# Demo account and risk evidence checkpoint

21 October 2026. Objective: prove virtual functionality; no capital-return target.
This is the next bounded development pass after merged pull request 3. It is a
review checkpoint, not completion of broker-backed trading qualification.

The following bounded pass is documented in
[Demo market evidence](demo_market_evidence_milestone.md). It adds instrument
eligibility and scheduled-session diagnostics while retaining the unresolved
account-history and timestamp blocks described here.

## Scope and acceptance contract

- AC-1: use the documented, unfiltered, read-only POST aggregate operation with
  the account-read rate limit. Reject other POST operations, real routes and
  account filters. Preserve detailed/aggregate/metadata/detailed capture.
- AC-2: account for the observed long, leverage-one stocks, funds and crypto,
  real-asset and contract-for-difference settlement, with US-dollar account
  values and US-dollar or sterling-pence asset quotes. Keep market exposure
  separate from liquidation, invested margin, fees and taxes. Reconcile full
  position coverage, units, margins, profit/loss and cash.
- AC-3: provide a persistent prospective risk ledger with account identity,
  replay protection, integrity checks and an immutable inception baseline.
  Missing interval cash-flow evidence must prevent additional records. A
  caller's completeness declaration must never become verified broker history.
- AC-4: integrate optional baseline recording and existing-exposure checks into
  the read-only preflight. An unavailable account or disabled configuration must
  not load credentials or create the ledger. Every result remains blocked from
  execution and receives no qualification credit.
- AC-5: provide one explicit versioned research pilot shared by scanner and
  backtester, with an inspectable coverage report and strict invalid-selection
  handling. Membership agreement is not historical-data qualification.
- NG-1: no broker orders, position closures, account resets, new credentials,
  enabled trading, scheduler changes or changes to approved risk limits.
- NG-2: no claim of whole-market coverage, validated historical performance,
  completed market-session integration, lifetime peak equity or real-money
  readiness. The existing fixture-only operator remains fixture-only.

## What the software now does

The aggregate operation is an exact allowlisted read-semantics POST. Its fixed
body requests account totals, instrument details and mirror details without
instrument or mirror filters. The connected eToro service successfully returned
this operation during the preceding read-only investigation. This does not
prove the repository's credentialed transport has passed an actual capture.

The account collector accepts the existing account's instrument shapes while
preserving execution restrictions. Unsupported holdings are never dropped from
risk. Copies, pending orders, shorts, leverage greater than one, other currencies
and unknown settlement/instrument types remain outside this qualified parser
scope. Sector and issuer aggregation still require verified instrument mapping.

The local ledger uses transactional, append-only SQLite observations linked by
hashes. It pins Demo, account identity, US dollars and Europe/London risk days.
Fresh new observations must be no more than 15 seconds old. Exact historical
replay keeps the original observation time; it cannot make stale evidence fresh.
Hashes detect inconsistent records, not a malicious owner rewriting the whole
database and all its hashes.

Daily profit/loss is equity change less external cash movements, only when an
exact local-midnight snapshot exists. Otherwise only inception profit/loss is
reported. Deposits and withdrawals are external movements; fees, financing and
investment income remain performance. Drawdown is measured only at observation
times since inception. Any external movement makes drawdown unavailable because
the ledger does not yet have the immediate pre-flow valuations needed to unitise
equity correctly. There is no automatic restart or rebase after a loss or flow.

All ledger outputs explicitly say that broker transaction history is unverified
and that submission and qualification credit are unavailable. This arithmetic
layer is not an adapter to the live risk evaluator's raw peak-equity input.

## Commands and privacy

Default disabled preflight, with no credentials or orders:

```bash
uv run --frozen python tools/demo_account_preflight.py
```

Exit code 2 and a blocked report are expected. For a separately configured
read-only Demo connection and independently checked account identity, optional
`--baseline-db runtime/demo-risk/risk.sqlite3` starts a private baseline after a
successful capture. It does not invent zero cash movements on later captures;
without interval history, another snapshot is blocked and the original baseline
is preserved. Store real account evidence only under ignored private runtime
paths, never in source control. This pass did not establish an actual baseline.

Inspect the six-equity pilot without downloading market data:

```bash
INVESTING_RESEARCH_UNIVERSE=config/demo_research_universe.yaml uv run --frozen python tools/research_coverage.py
```

Use that same environment selection for the scanner and separately invoked
backtester. Both select AAPL, MSFT, NVDA, AMZN, GOOGL and META. Backtesting retains
SPY and QQQ as separate benchmarks. The manifest checksum includes the selected
source metadata and is retained in pilot experiment evidence. Unset selection
preserves the legacy 61-instrument scanner and 12-stock backtest. A malformed or
missing selected manifest fails instead of silently falling back. Current
membership is not a point-in-time historical universe and grants no execution
permission. No new historical-data study was run in this pass.

## Remaining work in order

1. Resolve aggregate timestamp semantics. The successful connected-service
   response omitted its timezone; the inspected specification described a
   date-time but did not establish the meaning of offset-free values. The parser
   rejects these values. Obtain authoritative timezone evidence or a compliant
   broker response, then validate the actual repository transport and full
   bracketed capture. Do not append a guessed timezone.
2. Establish complete Demo cash-movement/execution history and durable cursors.
   Bind its provenance to the ledger and add pre-flow valuations before claiming
   cash-flow-adjusted drawdown. Generic balance-history routes do not establish
   Demo support and must not be substituted for verified Demo history.
3. Capture an actual baseline and verified risk-day boundary; collect forward
   evidence. Finish exact issuer/sector mapping, instrument-specific market
   sessions and broker tradeability checks.
4. Resolve the existing account's risk-limit breaches through an explicit policy
   or clean-baseline decision. The preflight now reports exposure breaches;
   account data availability does not override them. No policy was changed.
5. Bind the reviewed evidence to a separate broker-backed operator, then prepare
   one exact Demo order for human approval. Verify one submission, execution,
   economic reconciliation and lookup-based restart recovery.
6. Run actual historical validation on the shared pilot and account for missing
   data, corporate actions, trading calendars, realistic costs and selection
   bias. Expand asset classes only after each model is qualified.

The later 30-session/20-cycle scheduled-operation gate is unchanged. It is not
a prerequisite for the first explicitly approved supervised Demo order.

## Safe pause and resumption

Local verification: full suite 465 passed; 26 affected tests passed after the
final exposure guard and pilot evidence adjustment. Dependency declarations,
Python compilation, installed-package consistency and whitespace checks passed.
The independent review found a division-by-zero case for an empty account; the
guard and regression were added and the reviewer confirmed it resolved.
These are software checks using synthetic fixtures, not live trading evidence.

The user requested a pause at 20% remaining weekly usage. The agent cannot read
that allowance, so this pass ends at one reviewed, tested pull-request checkpoint
without claiming the threshold was measured. No unattended process is launched.
Resume from the pull-request head and this outstanding-work list. Human merge
and any eventual account reset, policy change or exact order approval remain
separate decisions; none is needed merely to inspect this checkpoint.

## References

- [Official eToro API reference](https://api-portal.etoro.com/api-reference),
  inspected operation `postAggregatedPortfolioDemo`, version `v1.398.0`.
- [Earlier operator milestone](demo_operator_milestone.md).
- [Configured market coverage](market_coverage.md).
