# Requests transport and bounded Demo capture

26 October 2026. **Transport accepted for the two observed reads; complete
account and risk acceptance remains BLOCKED.** The single authorised bracket
attempt is consumed. It stopped at the unchanged timestamp gate. Do not repeat
it or launch another diagnostic from this checkpoint.

## Source and chronology

The base is current GitHub `master`, PR #8 merge
`ec69c316a90e0831cb986e165375ba50281a02c8`. The isolated requests integration
commit is `72564afcd782759ed110c965d3247f956c8803ef`. Delivery uses a separate
`codex/requests-demo-capture-review` worktree; the dirty primary checkout and
the original capture worktree remain preserved. No applicable `AGENTS.md` was
found in the inspected repository or ancestor directories.

The [latest PR #8 description](https://github.com/lukeroyle-beep/Investing_Agents/pull/8)
is published evidence for the successful single portfolio diagnostic on
22 October and supersedes that PR's earlier missing-setup/zero-request report.
The two preceding urllib 403/1010 failures remain retained; their cause is
unproven. The [offline implementation report](requests_transport_offline_checkpoint.md)
describes the separately reviewed requests integration, not live acceptance.

The intended Mac operator subsequently ran the reviewed collector once on
26 October at 07:52:42 UTC, using the existing workflow credential entry and
independently supplied private Demo identity. The invocation claim, source
manifest, setup provenance, terminal reports and raw responses were inspected
locally. These capture observations were not previously published in PR #8.
This delivery made no further authenticated broker requests and did not read
or extract credential values.

Independent implementation and installed-launcher reviews completed on
22 October, before these sends. The source manifest matches the reviewed
implementation commit; the installed helper matches its reviewed hash. Fresh
independent code and retained-evidence reviews were also obtained for delivery.
The original source used for capture is unchanged; delivery adds documentation
and one synthetic interruption regression.

## Integration and retained controls

The capture-specific transport now uses ordinary `requests`, declared directly
in `pyproject.toml` and locked at 2.34.2. Only root dependency metadata edges
changed in `uv.lock`; resolved external packages and the generated
`requirements.txt` are unchanged. The existing collector and launcher retain
their architecture and strict checks.

- A fresh session uses verified TLS, the ordinary library User-Agent,
  `trust_env=False`, no ambient auth/cookies/proxies and zero adapter retries.
- Redirect resolution is disabled, including requests' preparation of a next
  request, so compressed redirect bodies are retained without implicit decoding.
- Exact HTTPS host, Demo routes and fixed unfiltered read-equivalent aggregate
  POST remain allowlisted. No write route is introduced.
- The 16-request maximum, 15-second initiation deadline, reduced socket timeout
  and separate 15-second account-validity check remain. Socket timeout is not
  represented as an atomic snapshot or a hard total wall-clock limit.
- Exclusive attempt claims and request receipts are durable before send.
  Owner-only files/directories use exclusive creation and file/directory sync.
  Interrupted or failed attempts are never automatically resumed.
- Exact content-encoded raw bytes and their hashes precede decoding; separate
  decoded bytes/hash receipts precede parsing. Partial reads fail closed.
  Gzip and deflate are supported; unsupported or malformed encoding stops work.
  Request and response headers are not serialized.
- Identity, pending collections, holdings, reconciliation, freshness, timestamp
  parsing and risk checks are unchanged. Execution, writes and scheduling remain
  disabled; this path has no order or baseline creation authority.

## Observed attempt and readiness matrix

Exactly two sends are evidenced: the detailed Demo portfolio GET followed by
the fixed aggregate POST. Both returned HTTP 200 with complete gzip bodies.
Raw/decoded lengths and hashes, exact gzip decoding, receipt ordering, setup
provenance and private permissions pass independent inspection. Both sends
were within the existing initiation budget. No metadata or closing detailed
request followed the aggregate parser rejection.

The terminal report records a schema failure. Offline replay of the retained
aggregate reproduces `timestamp must include a timezone`, classified here as
`aggregate_timestamp_timezone_missing`. That descriptive classification does
not imply the launcher emitted a new failure code.

| Requirement | Result | Scope of evidence / remaining prerequisite |
| --- | --- | --- |
| Transport acceptance | PASS, bounded | Both allowed reads returned HTTP 200 through the reviewed requests path. No claim of all-route or complete collector acceptance. |
| Identity | PARTIAL PASS | Every opening detailed position matches the independently supplied identity. Retained aggregate `cid` also matches in a separate offline comparison; the collector stopped before its aggregate identity check. Closing identity is unverified. |
| Complete bracket | INCOMPLETE | Detailed and aggregate responses retained; security metadata, closing snapshot, bracket stability and final capture validity were not reached. |
| Pending-order coverage | PARTIAL PASS | All seven documented direct collections were explicitly present and empty in the opening snapshot; mirrors were absent. No closing-snapshot coverage or complete bracket acceptance. |
| Holdings/cash reconciliation | NOT PERFORMED | Opening detailed parsing and supported-shape checks passed. Complete metadata, aggregate parsing and closing snapshot are required before full holdings, units, margins, cash, liquidation and equity reconciliation. |
| Timestamps and freshness | BLOCKED | Aggregate timestamp has no explicit timezone; strict rejection retained. Local aware receipts do not establish broker observation time or freshness. |
| Cash-flow/execution history | UNVERIFIED | No history endpoints were called. Complete Demo funding/execution coverage, interval semantics and durable event identity remain unqualified; absent flows are not inferred. |
| Risk baseline | BLOCKED | No baseline created. Qualified account evidence, history, daily boundary, daily PnL, peak-equity inputs and pre-flow valuations remain unavailable. |
| Exposure compliance | UNVERIFIED | No fresh reconciled ratios or full issuer/sector mapping. Earlier reported breaches are neither remeasured nor declared resolved. |
| Order readiness | BLOCKED | No proposed order, intent, approval or submission; zero orders. Market/session/tradeability, sizing and risk evidence remain unqualified. |

All preparation, approval, submission, baseline and qualification flags remain
false. Two successful responses do not qualify the account or grant permission
to trade. The capture date is historical evidence, not a current account state.

## Timestamp and history contract questions

Official public documentation was checked again on 26 October. The complete
OpenAPI refresh was unavailable (the public-document download returned 403),
so the [Stage 1](stage1_demo_connection_readiness.md) v1.400.0 specification and
hash remain explicitly dated 22 October evidence, not a fresh specification.

The [Demo aggregate schema](https://api-portal.etoro.com/api-reference/trading--demo/get-aggregated-portfolio-snapshot)
defines `timestamp` as the calculation time with a date-time type and a `Z`
example. It does not define the timezone of offset-free values. The
[`dailyCutoffUtc` parameter](https://api-portal.etoro.com/api-reference/trading--demo/get-a-filtered-aggregated-portfolio-snapshot)
describes a local day boundary expressed in UTC for daily-PnL calculations;
it supplies neither the missing response offset nor cash-flow completeness.
The existing fixed POST body remains unchanged. Neither local timezone nor
another product's endpoint justifies appending an offset.

[Demo history](https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history)
documents closed trades from `minDate` through the present, with `page` and
`pageSize`. The inspected contract does not establish page origin, stable
ordering, end-of-history guarantees, interval inclusivity or snapshot
stability, and exposes no upper-date parameter. It does not establish complete
cash movements or all execution events. An empty result cannot prove zero
funding or complete history.

[Demo order lookup](https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details)
addresses a known order or reference; it does not enumerate account-wide
executions. [Money cash-account transactions](https://api-portal.etoro.com/api-reference/cash-accounts/list-cash-account-transactions-paginated)
have a distinct account route and no established equivalence to Demo trading
history. No substitution, guessed event identity or zero-flow assumption was
made.

## Validation, privacy and handoff

Fresh Python 3.12.13 / uv 0.11.6 validation passed **315 focused tests and 742
full-suite tests**. The focused command covers capture transport/launcher,
account evidence, fixture operator/commands, submission safety and Fill Agent.
It exercises actual requests Session/HTTPAdapter paths with synthetic low-level
I/O for success, failures, redirects, compression, timeouts and truncation;
existing tests cover identity mismatches and duplicate-attempt refusal.

The added test interrupts `send` with `KeyboardInterrupt`, checks that the
durable private setup/request receipts survive, and confirms that reusing the
attempt sends nothing and leaves all retained bytes unchanged. This closes the
independent review's validation gap without changing production behavior.

Tests ran under an explicit macOS network-denying sandbox and Python socket/DNS
guard. Frozen sync, offline lock validation, exact compatibility export,
dependency declaration/consistency, source compilation and whitespace checks
passed. CI and security results for the published final head are recorded on
the PR; local tests alone do not claim alternate-OS or security-scan success.

Sensitive raw evidence, identity provenance and launch manifests remain in
ignored owner-only runtime storage on the intended host. Independent inspection
verified historical evidence hashes and did not alter attempts. Public source
and this report contain no account identities, holding details, credential
values or authorization headers. The primary checkout's unrelated changes
remain preserved. Weekly usage was observable and remained above the requested
20% reserve during this delivery.

**Smallest next milestone:** obtain authoritative Demo aggregate timezone
semantics or a provider correction that returns an explicit offset, and
document the resulting parser contract with offline fixtures. No support
message has been sent. Any later capture needs a new bounded authorization
after that evidence changes; never reset, rename or reuse this attempt.
Complete history and risk evidence remain subsequent gates. Broker-backed
operator development is outside this milestone.

Stop at the PR review checkpoint. No automatic merge, deployment, scheduling,
policy/strategy change, permission change, broker mutation or baseline occurred.
Rollback is to close the unmerged PR or revert the integration after review;
preserve every private attempt and receipt regardless of code rollback.
