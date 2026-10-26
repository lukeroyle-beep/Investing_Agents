# First manual eToro Demo cycle

This change implements the manual connector lifecycle and exact Fill Agent
runtime. It does not record a live trade. Repository defaults and launcher pins
remain disabled and unchanged. An unmerged branch can be reviewed and tested;
merging is not evidence of trading readiness.

## Source and admission contracts

* [Component time](component_time_account_evidence.md) retains each actual source
  clock, exact timestamp text, structural read interval, coverage and skew.
  Capture time and position times never become an atomic account timestamp.
* [Accounting](first_demo_trade_accounting_contract.md) separates invested
  principal, margin and exposure; preserves Decimal values; defines London-day
  cash-flow-adjusted performance and persistent unitised drawdown. Missing
  transactions or valuation history remain unknown.
* [Exact Fill runtime](exact_broker_fill_runtime.md) defines source-bound
  principal, complete command-batch deduplication and journal recovery.
* [Official connector review](connector_demo_contract_review.md) records the
  supported prepare/place/lookup contracts and their limitations.

`execution.account_bridge.component_risk_account` maps every held broker
instrument and position into the registry before evaluating the account. The
component-time source requires complete pending-order collections and rejects
any nonempty pending collection it cannot value; it never treats omitted orders
as empty. Instrument sector and issuer classifications must be complete. Risk
includes existing holdings, pending commitments and the proposed order, reserves
preview costs, and keeps the existing exposure, issuer, sector, loss and drawdown
limits. Legacy fixture evidence cannot enter this connector path.

## Supported host lifecycle

`EtoroConnectorAdapter` accepts host callbacks for the connected eToro profile,
`prepare_trade`, `place_trade` and Demo order lookup. `EtoroConnectorReadOnlyClient`
accepts the supported read callback. It never obtains local credentials from a
connector session. Raw read receipts precede parsing; decimal JSON text is
preferred to binary-float objects. Read transport does not retry failed requests.

1. Verify the same connection's Demo identity. Acquire and retain a fresh,
   reconciled full account with source authority and complete performance
   history. Resolve every held instrument and the candidate. Acquire exact
   eligibility, current instrument state and realtime quote evidence.
2. Build the versioned account evidence and evaluate the minimum eligible
   long-only, real-settlement, leverage-one order through
   `ConnectorDemoOperator.prepare`. A rejected risk decision cannot bind a
   preview or reach approval.
3. Use the adapter's preparation callback and bind its exact confirmation,
   warnings, eligibility and expiring token to that command. Display all of
   those terms to the operator. Only an explicit approval for that confirmation
   authorizes `approve`; an old approval or general project instruction cannot.
4. Refresh the same account, market, quote and usage evidence immediately before
   `submit`. At least 25% of each available usage window must remain: the 20%
   floor plus five percentage points reserved for recovery. Reevaluate risk and
   all expiry deadlines without changing the approved order. The adapter checks
   current Demo identity again, then durably claims the single send before
   calling `place_trade` once. Real execution remains unsupported here.
5. Persist acknowledgement separately from execution. Recover only by lookup
   using an actual broker order or reference identity. A local UUID is not a
   recovery handle. An ambiguous response without a broker handle remains held;
   a restart never resends. A connector response saved before a coordinator
   crash can supply its verified broker reference to durable recovery.
6. Retain terminal full-opening execution aggregates using order-plus-position
   identity. Check account, instrument, direction, settlement, leverage, amount,
   fees, taxes, execution time and conversion. Preserve the broker's explicit
   `investedAmountCurrency`; do not infer principal from rounded average price.
   Partial executions remain unreconciled and unstaged in this restricted path.
7. Acquire fresh post-trade full-account evidence. Only
   `agents.fill_agent.broker_runtime.BrokerEconomicRuntime` writes broker
   economics. It requires a new empty runtime, seeds every existing holding from
   bound detailed account evidence, and retains exact portfolio, cash, fill,
   cash-ledger and processed-fill CSVs. Its journal recovers interrupted writes;
   duplicate executions are idempotent only if all identity and economics agree.
   Pre-existing holdings, new units/principal, actual costs and post-trade cash
   must reconcile before committing. The legacy float/synthetic-cash writer
   refuses actual broker staging and this runtime's manifest.

The host must supply genuine source evidence and callbacks from the same
connection. A caller-created timestamp, history-complete boolean or account ID
does not independently establish broker provenance. The repository contains no
authoritative Demo history adapter when the provider does not expose that data.

## Validation and live blockers

Run the complete offline lifecycle in a new directory:

```sh
python scripts/validate_connector_demo.py --output-dir /tmp/new-demo-cycle
python scripts/validate_connector_demo.py --output-dir /tmp/new-demo-recovery --simulate-crash-after-response
```

Every profile, source clock, complete history, approval, quote and execution in
these runs is synthetic. The report keeps live submission and fill counts at
zero. The second command tests a response retained before a coordinator crash,
lookup after restart, exact Fill application and replay deduplication.

The 26 October 2026 live investigation did not establish an eligible order:

* The connected account's observed gross exposure exceeded the existing cap.
  A smaller buy cannot repair an account that already exceeds that cap.
* The aggregate account timestamp had no timezone, and no authoritative fresh
  cash clock or complete Demo activity/peak-valuation history was available.
  Unrealised DailyPnl and closed-trade history cannot fill these gaps.
* The required Demo instrument-breakdown route needs a `CID` header. The
  connected `execute_read` surface has no header parameter. Its adapter fails
  before transport instead of moving that header into a query parameter or
  repeating an impossible request.

Private identities, account balances, raw receipts and diagnostic evidence are
kept outside Git under the ignored runtime directory. No submission or Fill
economic application occurred during that investigation. An expired exploratory
preview is not an approved order and cannot be resumed.

Resume with an existing admissible Demo account and authoritative evidence for
the missing cash, activity and valuation coverage, through a supported transport
that can meet the documented route contract (or an independently reviewed
authoritative alternative). Do not reset the account, close holdings, relax
limits, re-date replay or invent missing zero events. Reopen current usage,
identity and all source evidence, then restart preparation for one new exact
approval. The later 30-session/20-cycle qualification is not a first-cycle gate.

Rollback before any live execution is to leave the branch unmerged and retain
the private evidence. After a send, recovery means lookup and reconciliation;
neither source rollback nor process restart authorizes another placement.
