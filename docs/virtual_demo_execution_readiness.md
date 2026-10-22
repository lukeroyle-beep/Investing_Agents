# Virtual trading: execution readiness

Assessment date: 21 October 2026.

Latest connection checkpoint: [Stage 1 readiness, 22 October 2026](stage1_demo_connection_readiness.md)
records the merged PR #6 base, current official contract review, missing local
Demo access/identity, and passing offline checks. Stage 1 remains blocked;
actual repository-backed account capture and usable risk evidence are unverified.

Follow-up: [the operator workflow milestone](demo_operator_milestone.md) adds
restricted read-only account capture, durable single-send controls and an
isolated end-to-end fixture exercise. The live broker/account blockers below
are not thereby cleared; the follow-up records current acceptance failures.

## Objective and evidence boundary

The current objective is to demonstrate correct functionality and successful
operation in a virtual trading environment. There is no capital-return target
for this workstream. Return, drawdown and benchmark results remain useful
measurements; they are not a promise of profitability or a requirement to
manufacture a passing result.

This assessment combines source inspection and the synthetic, offline tests
listed below. It does **not** establish live broker acceptance, completed
qualification sessions, or permission to trade real capital. No broker orders,
credentials, execution configuration or operator runtime were changed during
this assessment. Existing safety and promotion gates remain in force.

The earlier read-only eToro check observed eight existing demo positions. New
experiments must have separate performance attribution, while all existing
positions and pending orders remain included in account-wide exposure, cash,
risk and reconciliation. An experiment ledger is not a separate broker account.

## Verified offline tests

From the repository root:

```bash
.venv/bin/python -m pytest -q tests/contracts/etoro tests/test_execution_domain.py tests/test_pretrade_risk.py tests/test_broker_capability_boundaries.py tests/test_scheduler_qualification.py tests/test_fill_agent.py
```

Result: **63 passed in 1.44 seconds**, exit status 0.

These tests cover the disabled defaults, exact instrument mapping, broker
response contracts, credentials redaction, asynchronous order outcomes,
ambiguous submission recovery, cancellation races, fill deduplication, risk
checks, expiring single-use approvals, stop controls, scheduler locking and
qualification-evaluator behaviour. They use fixtures and simulated transports;
they do not count as real demo trading cycles or fault-drill evidence.

An initial expanded invocation also explicitly included the legacy root-level
`test_fill_agent.py` and `test_portfolio_schema.py`. Collection stopped because
the former imports the removed `append_processed_fill_ids` symbol. Those files
are outside the configured `pytest.ini` test directory (`tests`). They were not
modified, and this failure is separate from the passing maintained suite above.

## Blockers before broker demo qualification

| Blocker | Inspected implementation | Required evidence or change |
| --- | --- | --- |
| Incomplete account evidence | `brokers/etoro/adapter.py`: the execution adapter inherits an account snapshot with `pending_orders_complete=False` and `executions_complete=False`; pending-order and execution collection still raise `BrokerDataUnavailable`. | Obtain and validate complete broker data, including current pending orders, before constructing risk evidence. Never change the completeness flags without proving the data contract. |
| Missing operator workflow | `agents/execution_agent/execution_agent.py` only imports advisory intents. | Connect fresh account and quote evidence, risk evaluation, explicit approval, submission, outcome lookup, broker-confirmed fills, reconciliation and qualification recording. |
| Partial scheduler integration | `scripts/run_scheduled_demo.py` does not return approval waits, ignores the execution-agent subprocess result, and reports a hardcoded zero duplicate count. | Propagate preparation failures, derive approval waits and duplicate counts from durable evidence, and retain the scheduler's absence of submission capability. |
| Qualification records are insufficiently cross-checked | `scheduler/qualification.py` counts distinct date strings and manually entered cycle flags. `scheduler/store.py` does not require cycle records to reference actual execution and approval records. | Validate exchange sessions and link each counted cycle to the broker command, approval, executions and reconciliation evidence. Exclude synthetic records and unverified assertions. |
| Actual runtime evidence is unavailable in the checkout | Operator state under `runtime/` is intentionally excluded from Git. | Inspect the intended operator runtime or an approved evidence export. Do not infer completed sessions from source files or unit tests. |
| Qualification status command creates stores | `tools/demo_qualification_status.py` constructs `ExecutionStore` and `ScheduleStore`, which initialize database files if absent. | Use a genuinely read-only inspection path for existing runtime evidence. An empty database created during inspection is not evidence about another host. |

`record_mutation_cycle()` currently has callers in the scheduler tests, but no
production caller that accumulates the required broker cycles. Therefore,
enabling configuration alone would not complete qualification.

The eToro contracts were pinned in the repository on 28 August 2026. Recheck
the current official, operation-specific request and response contracts before
broker acceptance testing. A successful connection through an external
connector is not proof that this repository's own transport and parsing work.

## Genuine qualification sequence

1. **Validate the backtest.** Prove accounting, current valuation of open
   positions, reproducibility, costs and evaluation-window handling. Report
   remaining data limitations explicitly. Judge this milestone on correctness,
   with no capital-return target.
2. **Complete broker evidence and operator wiring.** Resolve the blockers above,
   establish reviewed instrument identities and capture the complete demo
   account baseline, including the existing eight positions.
3. **Prepare an inspectable demo order.** Bind its environment, instrument,
   amount and risk decision to an explicit, unexpired human approval. Keep real
   execution unavailable. Confirm the exact demo account and the supported
   unleveraged, long-only scope.
4. **Observe an actual broker lifecycle.** Persist the logical request before
   submission; treat acceptance as pending rather than filled; query its
   outcome using the same reference after ambiguity. Stage only
   broker-confirmed executions and let Fill Agent update economic state once.
5. **Reconcile and recover.** Match quantities, prices, fees, taxes, cash and
   positions. Demonstrate restart and ambiguous-response recovery without
   duplicate orders or fills. A simulated test alone does not prove the
   operator host or live broker path.
6. **Accumulate operating evidence.** The current configuration requires at
   least 30 distinct clean trading sessions and 20 manually approved demo
   mutation cycles, with no duplicate orders, unresolved reconciliation or
   unverified recovery. Record validated sessions and evidence-linked cycles,
   not arbitrary dates or manually asserted success flags.
7. **Complete the required drills.** Record evidence for process termination at
   lifecycle boundaries, host restart, clock skew, service outage, sustained
   rate limiting, streaming gaps, corrupted cache, partial fills,
   cancellation/fill races and unavailable operators. For an unused capability,
   record the review decision rather than inventing an executed drill.
8. **Review the result.** Report operational qualification separately from
   strategy performance. Preserve every failed and incomplete check. Real
   capital and autonomous submission require separate scope and approval.

## Current conclusion

The maintained offline demo/execution/risk/scheduler/Fill test suite passes.
Broker demo qualification remains **not established**. The immediate useful
deliverable is a trustworthy backtest and an accurate readiness record; actual
demo qualification requires completed integration and genuinely accumulated
broker and operating evidence.
