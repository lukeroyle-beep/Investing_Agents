# Virtual-functionality milestone: backtest validation

Assessment date: 21 October 2026.

## Goal and boundary

Demonstrate a working, inspectable trading simulation before connecting its
decisions to a virtual broker account. There is **no capital-return target**.
A loss-making strategy can pass functional validation; a run that never
completes a trade cannot demonstrate the trading lifecycle.

This milestone implements a deterministic baseline, not a self-modifying live
agent. Future strategy changes must be versioned, evaluated as new candidates
and reviewed independently of execution authority. Neither an experiment nor
a successful backtest enables demo or real trading.

## What changed

- Portfolio accounting debits entry notional and fees from cash, values open
  positions at each session's close, and records realised and unrealised
  profit or loss separately. Drawdown includes losses before a position closes.
- Unfunded or overlapping positions are rejected and saved as explicit
  evidence. A rejected candidate does not suppress later candidate signals.
- Entries use the next session's open after a signal. A stop crossed by an
  opening gap fills at that open, not at an unavailable stop price. Spread,
  slippage, fees and configured tax affect execution accounting.
- Walk-forward evaluation independently replays validation and test windows,
  uses matching benchmark periods, and replays stressed costs. It no longer
  copies full-history returns or invents zero drawdown for separate windows.
- Historical inputs, rejected trades and individual fold results are retained.
  Experiment identities are reused for identical experiments across runs;
  changed metrics, changed stable evidence and tampered files are rejected.
- The offline validation command saves its configuration, inputs, outputs,
  checks and fingerprints in a new directory. It never submits an order.

## Acceptance checks

| Check | Functional acceptance |
| --- | --- |
| Trading lifecycle | At least one accepted and completed simulated trade |
| Replay | Identical inputs produce identical trades, rejections, valuation and drawdown |
| Rolling evaluation | Identical splits and results, with the configured minimum number of folds |
| Cash | No negative cash; insufficient funds reject the proposed trade |
| Final positions | No remaining open positions after partition-end liquidation |
| Reconciliation | Final cash equals starting capital plus accepted trades' net profit or loss |
| Return | Recorded as an observation; no minimum return is required |

Adversarial tests additionally exercise missing prices, invalid inputs, stop
gaps, future-data isolation, transaction costs, duplicate candidates and
immutable experiment evidence. These are automated checks, not broker sessions.

Local verification on Python 3.12: **256 maintained tests passed**. Dependency
declarations, installed dependency consistency, source compilation and
`git diff --check` also passed. The full agent was tested twice against
synthetic downloads in isolated temporary directories: both runs reconciled,
preserved all eight experiment evidence files and reused one registry identity.
The maintained suite is the `tests/` directory selected by `pytest.ini`; see
the readiness document for the unrelated legacy root-test collection issue.

## Reproduce the offline evidence

From the repository root, using the pinned environment:

```bash
uv sync --frozen --all-groups
uv run --frozen python -m pytest -q
uv run --frozen python scripts/validate_virtual_backtest.py --synthetic --output-dir runtime/validation/virtual-backtest-example
```

The output directory must not already exist. Inspect `validation.json` and
the accompanying `historical_prices.csv`, `trades.csv`, `rejected_trades.csv`,
`equity.csv`, `drawdown.csv`, `walk_forward.csv` and `config.yaml`.

Replay the saved inputs with their saved configuration:

```bash
uv run --frozen python scripts/validate_virtual_backtest.py --prices runtime/validation/virtual-backtest-example/historical_prices.csv --config runtime/validation/virtual-backtest-example/config.yaml --output-dir runtime/validation/virtual-backtest-replay
```

The input and result fingerprints must match the original. The second report
labels its source as a supplied, unqualified snapshot; it does not infer the
provenance of a CSV file. Reading and writing these synthetic inputs is tested
for exact numeric round-trip reproduction.

The reference synthetic scenario contains 600 artificial weekday observations,
four rolling evaluation folds, 10 accepted trades and 327 rejected candidates.
All seven functional checks passed. The two competing strategy instruments
each request 60% of initial capital, intentionally exercising cash and
overlap rejection. These are fixture outcomes, not investment results.

Reference fingerprints with the checked-in configuration:

- Input: `404a677c5aa47c31c3963f36e03647a32f094f3c217ec1700bbffdaf932a9166`
- Result: `b6e6300a938bc5972f478f48dfd17c8c538adc18b1235ab5ac1aae36b5df23fe`

## Modelling assumptions and limitations

- Long-only, unleveraged research with fractional shares. Position size is a
  fixed percentage of initial capital, not an adaptive allocation model.
- Daily open/high/low/close prices do not reveal the intraday path. After
  checking the opening gap, an ambiguous stop-and-target session resolves to
  the stop. A favourable target gap receives only the target price.
- Same-session entries are processed before exits, so unavailable closing
  proceeds cannot fund opening purchases. Simultaneous candidates are ordered
  by ticker, which can affect selection when cash is constrained.
- Every symbol must cover the entire evaluation session set. Missing or
  truncated observations fail validation rather than silently liquidating
  early. Observed dates are not verified exchange-calendar sessions.
- The configured 252 training, 63 validation and 63 test sessions define
  rolling windows. Parameters are fixed; the training window provides past
  history, not model fitting. Test windows do not overlap. Each partition
  starts flat and liquidates at its end, so fold results are not one continuous
  trading record. This is not nested model selection or a permanently sealed
  holdout for an autonomous optimisation loop.
- Spread, slippage, fees and entry tax are explicit modelling assumptions,
  not calibrated eToro charges. Stress replay doubles configured costs.
  Financing, borrow costs, partial fills, liquidity and exchange lot/minimum
  order rules are not modelled.
- Point-in-time universe membership, delistings, survivorship, corporate
  actions, dividend treatment and official exchange calendars are not
  qualified. The current historical-data source must not be treated as
  investment-grade evidence solely because accounting checks pass.
- Existing advisory-promotion performance safeguards remain separate. Their
  presence does not introduce a return target for this functional milestone.
  Data and cost qualification flags remain false, and the validator explicitly
  reports `strategy_promotion_eligible: false` and `live_demo_qualified: false`.

## Next milestone

Complete the broker evidence and operator workflow described in
[demo execution readiness](virtual_demo_execution_readiness.md), then observe
an explicitly approved demo order through broker confirmation, fill ingestion
and reconciliation. Tests do not substitute for the required genuine demo
sessions and recovery evidence. Real-capital execution remains out of scope.
