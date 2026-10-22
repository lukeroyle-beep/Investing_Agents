# Stage 1: eToro Demo connection and readiness

22 October 2026. **BLOCKED: Stage 1 has not passed.** The repository's
credentialed transport has not captured the selected Demo account, and the
required risk evidence is not usable. Broker-backed operator integration must
not treat this checkpoint as live acceptance. The objective is virtual
functionality, with no capital-return target.

## Scope and evidence

This pass follows merged [PR #6](https://github.com/lukeroyle-beep/Investing_Agents/pull/6),
merge revision `ce6b43b1ea1314322bfd0d88063f591bd292f432`. The isolated branch
`codex/stage1-demo-readiness` started from that exact default-branch revision.
One execution owner performed inspection and verification; supporting agents
performed read-only contract research and review. The unrelated dirty primary
checkout remained at `12c53484f4f048d58d563d30e2f58319a6dadeff` and was not updated.

All five requested milestone/readiness documents were read. No `AGENTS.md`
exists in the checked-out Git tree or applicable inspected ancestor directories;
the GitHub root-file lookup also returned 404. There were no such instructions
to apply. Current repository controls and the user's bounded Stage 1 request
govern this checkpoint.

The inspected operator candidate is the local macOS 26.6.2 arm64 host; its exact
hostname, paths and inspection time are retained privately. No separately
configured broker operator service was established by this inspection. The
project's inspected launchd template is for dashboard publishing and supplies
no eToro environment variables; it is not broker-runtime evidence.

The shell's Python is 3.14.4 and `uv` is 0.13.1, outside the repository's selected
toolchain. Verification therefore used a separate frozen environment with
**Python 3.12.13 and uv 0.11.6**, without modifying the primary environment.
Installed primary dependencies are pandas 3.0.5, PyYAML 6.0.3 and yfinance 1.7.0;
pytest is 9.1.1. The frozen lock and generated requirements export agree.

Only the normal workflow's configured credential mechanism was inspected:
`EtoroCredentials.from_environment` reads `ETORO_DEMO_API_KEY` and
`ETORO_DEMO_USER_KEY`. Both are absent from the executing process. No project
`.env` file was present, and the client does not implement a dotenv loader.
No independently selected expected Demo identity was supplied. No secrets were
printed, no other connection's credentials were extracted, and no authenticated
broker request was attempted. This does not establish whether another host has
an independently configured connection.

## Readiness matrix

PASS means the stated check was actually satisfied; FAIL means a known
prerequisite is absent; UNVERIFIED means supporting evidence is unavailable.
Software checks and official schemas do not count as account observations.

| Requirement | Status | Evidence and blocker |
| --- | --- | --- |
| PR #6 merged; current base identified | PASS | GitHub merge metadata and isolated Git HEAD agree on `ce6b43b1...`. |
| Preserve unrelated work; isolated execution owner | PASS | Separate clone/branch; no primary tracked edits or changes to existing operator state. Only a new ignored private evidence archive is added to the original workspace. |
| Read `AGENTS.md` | UNVERIFIED | File absent from inspected tree/ancestors and GitHub root. |
| Reproducible dependency state | PASS | Pinned toolchain, frozen sync, lock check, export comparison and package consistency passed. |
| Configured repository Demo access | FAIL | Required environment values absent; checked-in read adapter remains disabled. |
| Independently selected Demo identity | FAIL | No expected account identity available to bind the collector. |
| Offset-free aggregate timestamp convention | UNVERIFIED | Current official contract still does not assign a timezone to offset-free values. |
| Complete Demo cash movements and interval provenance | UNVERIFIED | No documented exhaustive Demo funding/reset/adjustment feed established. |
| Pending-order contract shape | PASS | Seven direct collections are documented; mirrors have nested collections. Existing collector requires every direct collection explicitly empty and rejects mirrors. |
| Actual pending-order completeness | UNVERIFIED | No current broker response; absent arrays cannot mean zero orders. |
| Complete executions, pagination, boundaries and event identity | UNVERIFIED | Closed-trade history and known-order lookup do not establish an exhaustive event ledger. |
| Actual repository transport and full account reconciliation | UNVERIFIED | No authenticated capture; no current cash, holdings, margin or total evidence. |
| Prospective baseline prerequisites | FAIL | Identity and fresh reconciled account capture unavailable. No baseline established. |
| Daily-loss and drawdown evidence usable by existing controls | FAIL | No verified boundary/flow history or qualified adapter from prospective ledger to risk evaluator. |
| Issuer/sector mapping and current exposure-limit compliance | UNVERIFIED | Complete verified mapping and fresh account values unavailable. Earlier reported breaches remain unresolved. |
| Read-only and real-capital boundaries | PASS | Source/config inspection and offline tests retain disabled execution, real routes and scheduling; zero broker operations in this pass. |
| Offline validation | PASS | 608 maintained tests; dependency, compilation and disabled-preflight checks passed. |

## Official contract findings

The public [eToro OpenAPI specification](https://api-portal.etoro.com/api-reference/openapi.json)
was retrieved on 22 October 2026: **v1.400.0**, 1,832,832 bytes, SHA-256
`549e77e8f37dc167dfb6295e4771faf1f9bb71f62d0e925109cd6aa9a5d299a0`.
The saved complete bytes are private verification evidence. The earlier
implementation's v1.398.0 comments are its historical contract pin, not a claim
that it has completed live validation against v1.400.0.

- **Identity:** `GET /api/v1/me` documents `demoCid` as the virtual account
  identifier. This is a possible read-side cross-check after normal access is
  configured, not independent selection of the intended account. The current
  collector compares all detailed position customer IDs and aggregate `cid`
  against the supplied expected identity. Do not derive that expectation from
  the account response being tested. No identity endpoint was called or added.
  [Identity contract](https://api-portal.etoro.com/api-reference/identity/get-authenticated-user-profile)
- **Timestamp:** `TradingPortfolioDemoApi_AggregatedPortfolioResponse.timestamp`
  is a date-time describing snapshot calculation time. A `Z` example does not
  define offset-free values. Retain strict rejection; neither the host timezone
  nor a real-account endpoint supplies the missing Demo convention.
  [Demo aggregate](https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot)
- **Daily cutoff:** aggregate requests document `dailyCutoffUtc` as the user's
  local day start expressed in UTC, bounded to 48 hours in the past and five
  minutes in the future. It is required only with the daily-PnL option and
  rejected otherwise. For POST the exact enum is `dailyPnl`; GET uses `DailyPnl`.
  This parameter does not fix the response timestamp ambiguity or establish
  cash-flow completeness. The existing POST uses `pnl` and no cutoff.
  [Demo POST aggregate](https://api-portal.etoro.com/api-reference/trading--demo/get-a-filtered-aggregated-portfolio-snapshot)
- **Holdings and arithmetic:** the existing POST omits `instrumentIds` and
  `mirrorFilters`, uses `mirrorLevel=details`, and requests complete account
  totals and instrument details. Filters must not hide exposures while totals
  still describe the whole account. The contract defines available cash as
  balance less frozen cash, and equity as available cash plus used margin plus
  current PnL. Used margin includes manual margins, frozen orders and active
  mirror margin. The current restricted collector rejects mirrors, bonus
  credit, any pending orders and unsupported holdings instead of dropping them.
- **Pending orders:** the direct detailed-portfolio collections are `orders`,
  `stockOrders`, `entryOrders`, `exitOrders`, `ordersForOpen`, `ordersForClose`
  and `ordersForCloseMultiple`. Even obsolete fields must be explicitly empty
  for the current parser. Mirrored holdings and nested orders are outside its
  accepted scope. No pagination is documented for these portfolio snapshots.
  [Demo portfolio](https://api-portal.etoro.com/api-reference/trading--demo/get-demo-portfolio-breakdown)
- **Funding and history:** `GET /api/v1/trading/info/trade/demo/history` returns
  closed trades from `minDate` through the present. Its `page` and `pageSize`
  parameters do not specify page origin, limits, stable ordering, termination,
  interval inclusivity or snapshot stability. It advises a lookback shorter
  than one year and successive windows but exposes no upper-date parameter.
  An empty page proves neither complete execution history nor zero funding.
  Generic balance history has no established Demo selector; Money cash-account
  transactions belong to a distinct account product and are not substitutes.
  [Demo history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history),
  [Cash-account transactions](https://api-portal.etoro.com/api-reference/cash-accounts/list-cash-account-transactions-paginated)
- **Executions:** Demo lookup addresses a known order ID or reference ID; it
  does not enumerate all account executions. Order and position IDs are
  documented, but no durable execution-event identity or partial-close
  deduplication guarantee was established. Position `orderID` must be paired
  with its `orderType`; a position ID is not a complete execution-event key.
  The collector correctly retains `executions_complete=False`.
  [Demo order lookup](https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details)

## Can a prospective baseline satisfy the controls?

**Potentially after additional evidence and reviewed integration; not with the
current evidence or ledger alone.** A prospective T0 can measure subsequent
performance without reconstructing lifetime history. It cannot erase earlier
losses, establish a lifetime peak, or make an incomplete current risk day valid.

`capture_pretrade_account_evidence` brackets aggregate and security metadata
reads with unchanged detailed reads, binds the expected identity, requires a
fresh offset-aware aggregate within 15 seconds, and reconciles complete
instrument coverage, units, invested margin, liquidation value, unrealised PnL,
cash and equity. Money tolerance is USD 0.02 and units tolerance is 0.00000001.
Bracket agreement is still not an atomic broker snapshot. Every current
holding must pass; the six-equity research pilot must never filter the account.

Only after that evidence is proven may an immutable prospective baseline be
considered. Later observations require verified external-flow coverage for the
exact previous-to-current interval, with durable identity and endpoint boundary
semantics. The ledger's local convention is `(start, end]`; this is not a claim
about eToro. Deposits/withdrawals are external flows; fees, financing and income
remain performance. Resets and adjustments need qualified handling rather than
being silently ignored or treated as a new T0.

The existing ledger pins risk days to Europe/London and requires an exact local
midnight snapshot before reporting daily PnL. No such boundary was observed.
Do not replace it with an arbitrary first observation or current unrealised PnL.
The current daily-loss limit is 1%; the drawdown stop is 5%. With verified zero
external movements, observation-time equity can support a prospective peak
under the accepted prospective scope. Any external flow currently makes
drawdown unavailable: immediate pre-flow valuations and reviewed unitisation
are missing. Even declared-complete intervals leave
`broker_history_verified=False` in the current ledger.

`risk/pretrade.py` expects trusted `daily_pnl` and `peak_equity`; a prospective
unitised index is not a raw peak-equity input. A qualified conversion and its
provenance must be reviewed before using this ledger in that evaluator.
Issuer/sector mapping must cover all holdings and any pending exposures; an
instrument-level concentration lower bound cannot establish sector compliance.

Earlier account checkpoints report exposure-limit breaches, but this pass
obtained no current measurements and does not invent ratios or mark them fixed.
Limits remain 50% gross, 50% absolute net, 5% issuer, 20% sector and a 20% cash
buffer. No holdings, risk limits, account history or strategy were changed.

## Verification and retained evidence

The frozen isolated environment passed the maintained `pytest.ini` suite:
**608 passed in 13.35 seconds**. `uv lock --check --offline`, frozen requirements
export comparison, `deptry`, source compilation and `pip check` also passed.
No production behaviour changed, so no new tests were added.

`tools/demo_account_preflight.py` with the checked-in disabled configuration
returned expected exit code 2. Its leading blockers were
`read_only_client_unavailable_or_disabled` and
`expected_demo_customer_id_required`; account completeness, submission,
real execution and qualification remained false. This is a disabled-path test,
not a network attempt or authenticated transport failure.

Private evidence is under ignored `runtime/validation/stage1-20261022/` in the
isolated checkout: runtime inspection, complete public OpenAPI bytes and receipt,
disabled-preflight JSON, test/check logs and execution-contract receipts.
No raw account payload was obtained; none is represented as captured. Evidence
files are restricted to the local owner. A durable owner-only copy is retained as
`runtime/validation/stage1-20261022/private-evidence.tar.gz` in the original
workspace, without overwriting existing state. No credentials or account
identifiers are committed. A later actual capture must retain raw response bodies privately
with request/receipt times and hashes, without storing authorization headers;
the current preflight's sanitised report alone does not retain those bodies.

## Smallest next action and bounded handoff

**Supply the existing workflow's Demo Read credential environment and an
independently verified expected Demo identity to the intended operator process.**
Do this through the normal private operator setup, not chat, Git, another
connector's credentials, or new permission grants. If that configured runtime
is on another host, identify that host instead of assuming this Mac has access.
The read client uses the credential pair documented by
[eToro authentication](https://api-portal.etoro.com/core/getting-started/authentication).

For a later bounded read, use a private copy of `config/brokers/etoro_demo.yaml`
under ignored runtime storage with only the read adapter enabled, keep Demo,
read-only mode, the pinned HTTPS host, real-route and write prohibitions, and
set `max_read_retries: 0` for a single diagnostic attempt. Bind the independently
reviewed identity to the existing preflight argument privately. Do not pass
`--baseline-db` until capture prerequisites have been proven. Do not edit the
execution, write-client or scheduler configurations. This setup was not
performed in this pass.

In parallel, obtain authoritative provider answers for (1) offset-free Demo
aggregate timestamps; (2) exhaustive Demo deposits, withdrawals, resets,
adjustments and executions; (3) pagination termination, consistent interval
boundaries/cursors and durable event IDs; and (4) valuations immediately before
external flows. No support message was sent. Retry only when setup or evidence
changes. Do not guess a timezone or use real-account data to bypass a blocker.

After prerequisites change, one owner should perform the full unfiltered
repository-backed bracketed capture, retain raw private evidence, reconcile
every holding and report actual exposure breaches. Establish T0 only if its
prerequisites pass; daily-loss/drawdown readiness remains separate. A capture
alone grants no broker-backed operator or trading permission.

This checkpoint contains documentation only and ends at one reviewable PR.
Independent read-only review found no required fixes. Local link, privacy-pattern
and Git whitespace checks passed. Reverting the two documentation files reverses
the tracked change; no runtime rollback is needed.
No order, closure, cancellation, reset, permission change, trading enablement,
scheduling, deployment, strategy retuning, universe expansion or merge occurred.
The user's resumed stop is **10% remaining usage**, replacing the earlier 20%
stop; visible usage was above that floor during verification. No later milestone
or unattended collection was started.
