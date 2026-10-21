# Demo operator workflow milestone

21 October 2026. Objective: virtual functionality, with no return target.

## Delivered software and evidence boundary

The inspectable operator service now connects preparation, independent risk
checks, per-order approval, submission, broker lookup, fill staging and restart
recovery. It is deliberately **restricted to offline fixtures**. A caller
cannot label manually supplied account/quote inputs `broker_demo` and use them
as live authority. The service rejects eToro adapters and any non-fixture
source until a reviewed live evidence path exists.

The execution ledger now binds each command to its persisted risk decision and
approval. Approval consumption and the pending transition are atomic; a
durable single-use send claim prevents stale objects or concurrent callers from
submitting the same logical request twice. After an ambiguous send or a crash,
recovery uses lookup, never a resend.

Operator evidence retains the exact account, quote, instrument mapping, intent,
risk decision, approval and lookup observations. Executions must match the
approved broker/order/reference/instrument/currency and cumulative notional.
Changed execution identities, missing prior fills and contradictory staging
fail closed. A broker-terminal command is not proof of economic reconciliation.
Fill Agent remains the only economic-state writer.

Reconciliation serializes access to the staging file with an exclusive
`broker_fills.csv.operator.lock` beside it. An existing or crash-stale lock
blocks work before lookup and is never automatically cleared. Removing such a
lock requires recovery review and verification that no owner is still active.
An interrupted approval-evidence write can be retried with the original
approver and still-valid bound approval; it does not mint another approval.
Missing preparation evidence requires manual recovery review and cannot be
reconstructed by guessing or creating a second command.

The new read-only account collector proves only a narrow account shape:
US-dollar long, unleveraged, real-asset stocks/funds, no copy portfolios and no
pending orders. It requires every documented pending collection explicitly,
brackets the aggregate read with unchanged detailed reads, verifies the
independently selected Demo customer identity, checks exact instrument metadata,
and reconciles positions and account totals. Omitted or unsupported evidence
blocks capture. It never equates current unrealised gains with daily profit or
loss; execution-history completeness and peak equity remain unproven.

## Run the isolated functional exercise

```bash
uv run --frozen python scripts/validate_demo_operator.py --output-dir runtime/validation/demo-operator-example
```

The output directory must be new. The exercise uses a fake broker, simulated
approval and a simulated clock. Its in-memory fake execution configuration
does not change checked-in configuration or the operator's runtime.

Ten checks cover one fake submission, lookup-based restart, duplicate-submit
blocking, one staged execution, one processed fill, one cash-ledger entry,
idempotent Fill Agent replay, correct fixture cash, correct fixture position
and zero live qualification credit. The actual Fill Agent runs twice in a
new isolated subprocess runtime. No broker transport or credentials are used.
Inspect `validation.json` and `fixture_runtime/` for the retained evidence.

## Read-only preflight command

```bash
uv run --frozen python tools/demo_account_preflight.py
```

The checked-in disabled configuration returns an explicit blocked report and
exit code 2 without loading credentials or initializing an execution ledger.
For a separately approved read-only setup, the command accepts a read-only
configuration path and independently reviewed `--expected-demo-customer-id`.
An optional `--output` writes a new report without overwriting old evidence.
The command has no submit capability. A successful narrow account capture
still reports live submission unavailable and no qualification credit.

## Actual read-only observations and remaining blockers

The connected eToro service's official route specifications reported version
`v1.398.0` on 21 October 2026. Read-only probes observed:

- The detailed Demo portfolio route returned eight positions and explicitly
  empty pending-order collections, with no copy portfolios.
- Three positions use contracts for difference, while five use real-asset
  settlement. Instrument metadata classified four as stocks, three as
  exchange-traded funds and one as crypto. All were long with leverage one.
  This account is outside the collector's restricted initial scope. Unsupported
  positions must not be omitted from account-wide risk calculations.
- The aggregate-portfolio request returned HTTP (Hypertext Transfer Protocol)
  status 415, Unsupported Media Type, through the connected service despite
  the published read contract. That is an unresolved acceptance failure, not
  a successful test of this repository's own transport.
- The inspected snapshots do not establish complete realised-plus-unrealised
  daily profit/loss, cash-flow-adjusted peak equity, complete execution history
  or a fresh, bound market-session decision.

No credentials, live positions, broker permissions, scheduler settings or
checked-in execution configuration were changed. No actual Demo or real
orders were submitted. No demo sessions or mutation cycles were credited.

Before an actual demo order, resolve the aggregate-read discrepancy, extend
account-wide risk evidence to cover the existing holdings without making those
instruments executable, and establish reconciled daily/peak-equity and session
evidence. Then review the live evidence collector and exact Demo order with
the operator. Do not reset the account, close positions or relax controls to
obtain a passing result. Real capital remains out of scope.

Current 30-session/20-cycle scheduled qualification and fault-drill gates are
unchanged. Their broader record-linkage limitations remain documented in
[execution readiness](virtual_demo_execution_readiness.md).

## References

- [Official eToro API reference](https://api-portal.etoro.com/api-reference)
- [Aggregate Demo portfolio contract](https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot)
- [Configured market coverage](market_coverage.md)
- `brokers/etoro/account_evidence.py`, `execution/operator.py`,
  `tests/test_demo_operator.py`, `tests/test_demo_operator_commands.py`
