# Stage 2: gated eToro Demo operator integration

Historical merged checkpoint below. The later single-read diagnostic and
22 October requests integration are distinguished in the
[offline transport checkpoint](requests_transport_offline_checkpoint.md).
Neither establishes full live capture acceptance; Stage 2 remains **BLOCKED**.

22 October 2026. **BLOCKED for broker-backed order preparation.** Offline
capture tooling is implemented; actual repository acceptance remains unproven.
There is no proposed order, approved intent, risk baseline or trading enablement.
The objective is virtual functionality, with no return target.

## Authority and bounded scope

The base is current `master` at `2b78b7e455e60ae186cc2386106a573a4d66ab28`,
the merge of [PR #7](https://github.com/lukeroyle-beep/Investing_Agents/pull/7).
The dirty primary checkout remains untouched. This change uses the isolated
`codex/stage2-demo-operator` branch. No applicable `AGENTS.md` was found in the
repository or inspected ancestors. The four requested milestone documents and
their controls govern this work. Merging PR #7 did not pass Stage 1.

The local macOS operator candidate and normal environment-based credential
mechanism are unchanged from [Stage 1](stage1_demo_connection_readiness.md).
No separately configured broker service or remote operator host was established.
The inspected dashboard launchd configuration is not a broker setup. A fresh
presence-only check found neither `ETORO_DEMO_API_KEY` nor
`ETORO_DEMO_USER_KEY` in the executing process, no project `.env`, and no supplied
independent expected Demo identity. No broker probe was justified or attempted.
Other connections and credential stores were not searched or extracted.

The latest owner instruction permits using the remaining usage budget for this
checkpoint, superseding the earlier 10% reserve. It does not authorize a reset,
another milestone, unattended execution or any order.

## Delivered path and preserved gates

`tools/demo_operator_capture.py` is a separate one-shot readiness launcher using
the existing repository `EtoroReadOnlyClient` and bracketed account collector.
It reads the credential pair only from the exact normal environment names and
an independently supplied identity/provenance file. It requires owner-only,
non-symlink files below ignored `runtime/`, an enabled private Demo read-only
configuration, the exact pinned HTTPS base, zero retries, no websocket, and
disabled repository execution and scheduling settings. Setup errors make zero
broker requests and create no capture directory.

The attempt directory is created exclusively before sending; reusing its name
cannot issue another read. The recorder admits only detailed Demo portfolio,
unfiltered aggregate and security-metadata operations, with at most 16 requests
and a 15-second request-initiation budget. Each socket timeout is reduced to
the remaining budget. The collector separately rejects a completed capture
outside its 15-second validity window; socket timeout is not a claim of atomic
or precisely timed broker snapshots. Redirects are not followed. The existing
client fixes the aggregate body and never applies an instrument universe filter.

Private request receipts precede send. Raw response bytes and SHA-256 receipts
with local request/receipt times precede parsing, including schema/HTTP failures.
No request or response headers are retained. Local receipt times never replace
broker observation times. Files are exclusively created with mode 0600 inside
a mode-0700 attempt directory. File contents and their containing directories
are synced before proceeding; pre-send sync failure blocks network access.
An interrupted/incomplete attempt remains visible
and is never resumed automatically. Do not retry under a new name unless setup
or authoritative evidence has changed.

The collector reconciles all supported holdings, cash, margins, units, PnL,
liquidation and equity, checking both detailed endpoints against the independent
identity. It requires all seven pending-order arrays explicitly empty and rejects
unsupported holdings/mirrors or nonempty orders instead of omitting exposure.
Early failures may stop before a complete bracket; retained partial responses
do not establish full account coverage. The report preserves that distinction.

Even successful capture leaves preparation, approval, submission and
qualification false. It reports measured exposure breaches without changing
holdings or limits. It passes no baseline argument, creates no economic or
execution stores and cannot issue an order. The fixture `DemoOperator`, durable
single-submit claim, lookup-only ambiguous recovery, execution deduplication,
expiring approval bindings and Fill Agent economic-writer boundary are unchanged.

A preparation success path is deliberately deferred: current contracts and
collectors cannot supply trusted account-wide daily PnL, peak equity and complete
history. Accepting caller booleans or relabelling fixture inputs would manufacture
authority. When those prerequisites pass, a separate reviewed implementation
must bind the persisted intent and expiring approval to all evidence below;
this launcher is not that authorization path.

## Sanitised readiness matrix

PASS applies only to the stated check. FAIL means a known prerequisite is absent;
UNVERIFIED means the required evidence has not been obtained.

| Requirement | Status | Evidence / next dependency |
| --- | --- | --- |
| Current merged base and isolation | PASS | PR #7 merge matches isolated HEAD; original checkout preserved. |
| Applicable repository instructions | PASS | No `AGENTS.md` in inspected tree/ancestors; requested documents read. |
| Frozen toolchain | PASS | Python 3.12.13, uv 0.11.6; lock and project manifest match Stage 1 byte-for-byte. |
| Normal private Demo access | FAIL | Both required credential environment values absent in this process. |
| Independently verified expected Demo identity | FAIL | No private identity/provenance supplied. A response under test cannot select itself. |
| One-shot private capture launcher | PASS | Focused fake-transport checks; no account acceptance implied. |
| Actual transport, full bracket and reconciliation | UNVERIFIED | Zero authenticated repository requests in this stage. |
| Broker timestamp interpretation | UNVERIFIED | Offset-free Demo timestamp convention unresolved; strict rejection retained. |
| Complete cash movements and executions | UNVERIFIED | No exhaustive Demo feed, interval boundaries, pagination or durable execution-event identity established. |
| Risk baseline and daily/drawdown inputs | FAIL | No qualified capture, flow coverage, risk-day boundary or reviewed risk adapter. No baseline created. |
| Existing exposure compliance | UNVERIFIED | Prior reported breaches remain unresolved; no fresh ratios measured. |
| Complete issuer/sector mapping | UNVERIFIED | Current evaluator groups issuer exposure by internal instrument ID; it cannot prove cross-line issuer aggregation. |
| Exact executable instrument identity | UNVERIFIED | No order selected and no current broker identity captured. |
| Fresh executable quotes | UNVERIFIED | V2 documents realtime/delayed quote type; existing V1 path does not bind it to an intent. |
| Session and current tradeability | UNVERIFIED | Calendar diagnostic exists; current flags and receipt freshness are not integrated into a broker preparation. |
| Broker sizing limits | UNVERIFIED | Available real/long/1x configuration, amount/exposure minimums, quantity type, maximum units and funds buffer need exact-intent checks. |
| Persisted broker intent and expiring approval | FAIL | Required evidence absent; no success path or approval created. |
| Fixture lifecycle and sole economic writer | PASS | Existing fake-transport lifecycle rehearsed; no live acceptance or qualification credit. |

## Contract evidence reused, not reprobed

Stage 1 retained complete official OpenAPI **v1.400.0**, retrieved on
22 October 2026, SHA-256
`549e77e8f37dc167dfb6295e4771faf1f9bb71f62d0e925109cd6aa9a5d299a0`.
The same bytes were reused. This is dated contract evidence, not a new broker
response. See the [Stage 1 findings](stage1_demo_connection_readiness.md) for
the timestamp, seven pending collections, history and risk-baseline details.

- The [Demo aggregate contract](https://api-portal.etoro.com/api-reference/trading--demo/get-a-filtered-aggregated-portfolio-snapshot)
  does not qualify offset-free timestamps. Neither host timezone nor a different
  real-account route provides authority to append an offset.
- [Demo closed-trade history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history)
  is not complete funding or execution history. Known-order lookup is recovery
  for an identified command, not account-wide enumeration. No zero flows are
  inferred from missing history. Risk-day and pre-flow valuations remain required.
- The retained [official OpenAPI](https://api-portal.etoro.com/api-reference/openapi.json)
  documents V2 rates `quoteType` as realtime/delayed and `rate.date` as UTC.
  It also documents search `Instrument` flags `isOpen`, `isCurrentlyTradable`,
  `isExchangeOpen`, `isActiveInPlatform`, `isBuyEnabled` and `isDelisted`.
  These are available contract candidates, not qualified current observations.
  Eligibility permission alone does not establish current tradeability.

No risk-limit, strategy or universe change is justified by those findings.

## Exact private setup for a later bounded read

Use the intended Mac operator terminal and this reviewed checkout. Use only
existing credentials designated for this workflow with Demo Read scope. Do not
create or broaden broker permissions, borrow connector credentials or put keys
in chat, shell command arguments, source, screenshots or Git. If the intended
runtime is elsewhere, configure that host instead; this Mac's absence says
nothing about another host. The normal client has no dotenv loader.

From the checkout root, use `uvx --from uv==0.11.6 uv` to select the pinned
toolchain even when the host's default `uv` differs. Run setup
and capture in the same terminal session. Environment values entered in a
separate terminal are not inherited by an already-running agent process:

```zsh
set +x
read -r -s 'ETORO_DEMO_API_KEY?Existing Demo Read API key: '
print
read -r -s 'ETORO_DEMO_USER_KEY?Existing Demo Read user key: '
print
export ETORO_DEMO_API_KEY ETORO_DEMO_USER_KEY
uvx --from uv==0.11.6 uv run --frozen python - <<'PY'
import getpass, json, os
from datetime import datetime, timezone
from pathlib import Path
import yaml

private = Path('runtime/demo-operator-private')
private.mkdir(parents=True, mode=0o700)  # Stop if this setup already exists.
config = yaml.safe_load(Path('config/brokers/etoro_demo.yaml').read_text())
config.update(enabled=True, max_read_retries=0)
identity = {
    'expected_demo_customer_id': getpass.getpass('Independently verified Demo customer ID: '),
    'verification_source': getpass.getpass('Independent record used to verify it: '),
    'verified_at': datetime.now(timezone.utc).isoformat(),
}
for name, body in [('read.yaml', yaml.safe_dump(config)),
                   ('identity.json', json.dumps(identity, indent=2) + '\n')]:
    fd = os.open(private / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(body)
(private / 'captures').mkdir(mode=0o700)
PY
```

Verify the expected Demo ID against an independent trusted virtual-account
record before entering it. The file records the operator's supplied provenance;
the launcher cannot independently attest the truth of that record. Never obtain
the expected value by copying the response of the capture being tested. If the
identity is unknown, stop setup and obtain that evidence privately.

Only after setup or authoritative evidence changes, one owner can run:

```zsh
uvx --from uv==0.11.6 uv run --frozen python tools/demo_operator_capture.py \
  --config runtime/demo-operator-private/read.yaml \
  --identity-file runtime/demo-operator-private/identity.json \
  --evidence-parent runtime/demo-operator-private/captures \
  --attempt first-configured-read
unset ETORO_DEMO_API_KEY ETORO_DEMO_USER_KEY
```

Exit 2 is expected even after valid capture because order preparation remains
blocked. Inspect `readiness.json` privately. Keep each attempt and its raw files;
do not delete an attempted directory to trigger a repeat. The standard execution,
write-client and scheduler configurations remain unchanged. Do not run the
optional baseline command until its prerequisites are separately established.

## Evidence, validation and next action

Private Stage 2 validation evidence is retained at ignored
`runtime/validation/stage2-20261022/` in the isolated checkout, with a durable
owner-only archive at the same relative path in the primary workspace.
It contains setup presence evidence, reused-contract hashes, focused test/check
receipts and independent review. It contains **no actual account capture**.
The prior Stage 1 archive remains at `runtime/validation/stage1-20261022/`.
Future raw account reads go only to the private attempt location shown above.

Validation uses the unchanged frozen Stage 1 environment: **63 launcher tests
and 181 existing account, fixture operator, submission-safety and Fill Agent
tests passed**. Dependency declaration checks and compilation passed. Independent
review and exact revision are recorded in the pull request. These checks use
fake transports, not broker acceptance. No broad history download or unchanged
broker probe was run.

**Next action:** privately supply the workflow's existing Demo Read credential
pair and independently verified expected Demo identity on the intended operator
host, then perform one bounded repository capture and retain its raw evidence.
In parallel obtain authoritative answers to the unresolved history/timestamp
questions already listed in Stage 1. No provider support message was sent.

After qualified account/risk evidence, review complete issuer/sector mapping,
exact instrument, realtime quote, session/current tradeability and sizing
integration, then bind one exact proposed order to expiring approval. No proposed
order can be produced at this checkpoint. No orders, cancellations, closures,
resets, permission changes, policy relaxation, deployment, scheduling or merge
occurred. Reverting this branch removes the optional launcher; existing runtime
and fixture workflows require no rollback. Stop at this one PR checkpoint.
