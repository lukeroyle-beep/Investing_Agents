# Requests capture transport: offline checkpoint

Historical offline checkpoint. The later [26 October capture and delivery
report](requests_demo_capture_checkpoint.md) records the consumed bounded attempt
and its timestamp rejection; do not treat the no-live-capture statements below
as current operational status.

22 October 2026. **Stage 2 remains BLOCKED. Full live collector acceptance is
unverified.** This checkpoint implements and tests transport behavior only.
No broker/market-data request, credential probe, runtime setup change, order,
intent, approval, baseline, schedule, push, PR publication, merge or deployment
was performed for this integration.

## Starting state and chronology

GitHub `master` was verified at PR #8's merge,
`ec69c316a90e0831cb986e165375ba50281a02c8`, with no subsequent commits. The dirty
primary checkout remained at `12c53484f4f048d58d563d30e2f58319a6dadeff`; its
working files were preserved. Only its Git fetch metadata was refreshed.
Implementation used an isolated `codex/requests-capture-transport` worktree.
No applicable `AGENTS.md` was found in the repository or inspected ancestors.

The [PR #8 dated post-capture update](https://github.com/lukeroyle-beep/Investing_Agents/pull/8)
reports a successful private ordinary requests 2.34.2 diagnostic on
22 October at 15:46:10–11 UTC. That was one detailed Demo GET, separate from the
merged urllib collector. It did not obtain aggregate, metadata, closing bracket
or full reconciliation. The earlier merged Stage 2 document remains historical;
its missing-setup/zero-request statements are not current operational claims.

The local reviewed diagnostic source and redirect regression tests were
available and inspected. No private diagnostic main was executed, and no
credentials, account responses, archives or operational claims were changed.
The historical live result was not reproduced or independently revalidated
in this task. New tests use synthetic credentials/responses and temporary files.

## Narrow implementation

`CaptureHttpTransport` uses a fresh ordinary requests session for each request,
with default requests User-Agent, verified TLS, no environment trust, no auth
handler, no cookies/proxies, zero adapter retries, streaming and no redirects.
Its redirect resolver is deliberately empty: requests otherwise consumes
`Response.content` while preparing `Response._next`, even when
`allow_redirects=False`. This includes same-host redirects and failure bodies.
Only gzip and deflate are advertised; unsupported encodings fail closed.

The existing recorder and bracketed collector remain in place. The shared
`HttpResponse`, general client/write transport, schemas, account collector,
strategy, universe, risk limits, execution and scheduler configuration are
unchanged. A capture-specific `RawCaptureResponse` separates content-encoded
bytes from the decoded parser view. Existing synthetic `HttpResponse`
transports remain compatible as identity-encoded responses, explicitly tested.

The receipt sequence is:

1. Exclusively create the private attempt and sync its parent. Persist the
   setup receipt. Existing claims are never removed, renamed or resumed.
2. Exclusively persist and sync the request receipt before send. Recheck the
   initiation deadline after storage sync and reduce the timeout accordingly.
3. Read with `raw.read1(..., decode_content=False)`, retaining available prefixes
   on errors; close response/session on success and failure.
4. Persist `NNN-response.bin` and `NNN-response.json` before decoding. Their
   length/hash always describe raw content-encoded payload bytes (HTTP transfer
   framing is removed by the HTTP engine). `read_outcome` is `complete`,
   `partial` or `failed`; incomplete reads never reach decoding or parsing.
5. Decode only after both raw artifacts are durable. Persist separate
   `NNN-decoded.bin` and `NNN-decoded.json` with decoded length/hash and the raw
   hash link before returning the parser-facing response. Invalid encoding
   retains raw evidence and a fixed failure code.

Every file uses exclusive 0600 creation, file fsync and containing-directory
fsync, within the existing owner-only/non-symlink 0700 attempt path. No headers
are serialized; the internal encoding classification is not retained as header
logging. `requested_at` means local pre-send receipt creation; `received_at`
means local body-read completion/failure; `decoded_at` means local decode
completion. All are timezone-aware; none supplies broker observation time.
Persistence failure blocks further work. A recorder failure is terminal, and
non-200 responses cannot cause a follow-up send through the same recorder.

## Gate-to-test coverage

| Preserved gate | Offline verification |
| --- | --- |
| Pinned HTTPS host; exact endpoints/methods; fixed unfiltered aggregate POST | Launcher allowlist and changed/filtered operation rejection tests; actual requests full bracket asserts GET/POST/GET/GET and the fixed body. |
| Independent Demo identity/provenance, exact workflow credential names, Demo/read-only config, disabled execution/scheduler | Existing launcher setup rejection suite; both detailed identity brackets and aggregate identity tests in account-evidence contracts. |
| TLS and ordinary client identity; no .netrc, environment proxies/CA overrides or credential fallback | Real Session/HTTPAdapter test with hostile synthetic environment; asserts default UA, verified TLS, no ambient auth/cookies/proxy, no netrc lookup. |
| Zero retries, redirects, auth resend or fallback | Actual Session/HTTPAdapter to fake pool I/O; 301/302/303/307/308 with same/other-host bodies, 401/403/429/500/503, timeouts and exceptions; one send and resource closure. |
| At most 16 requests; 15-second initiation deadline; remaining-budget timeout | Request 17 rejection, already expired deadline, receipt sync crossing deadline, timeout recalculated after receipt. |
| Separate 15-second collector validity gate | Full four-response fixture with completion clock beyond 15 seconds is rejected even when all requests fit the initiation budget. |
| Exclusive durable claims; no retry/resume after interruption | Existing duplicate claim and interrupted-attempt checks; directory sync failure retains claim; stopped recorder rejects another send. |
| Owner-only/non-symlink storage, 0700/0600, exclusive writes, file/directory fsync | Launcher private-path tests, file mode checks, sync ordering and injected file/directory failures for both raw artifacts and decoded receipt. |
| Raw before decoding, decoded before parsing, distinct hashes | gzip/identity/deflate success and HTTP failures; redirect content access forbidden; raw durability event ordering; full-bracket parser asserts decoded receipt already exists. |
| Partial/failed bodies, malformed encoding/JSON, sanitization | Actual urllib3 Content-Length and chunked truncation; stream timeouts; invalid/truncated compression; malformed JSON; fixed error receipts and no header/credential/exception sentinel leakage. |
| Strict schemas, seven explicitly empty pending collections, holdings/mirrors, metadata, stability and reconciliation | Existing account-evidence contract suite, unchanged, including omitted collections, unsupported holdings, identity/coverage mismatch and conflicting totals. |
| Submission deduplication, lookup-only ambiguous recovery, expiring approvals, sole Fill Agent economic writer | Existing fixture operator, command, submission-safety and Fill Agent regression suites; full suite. |
| No preparation, approval, submission, baseline or qualification from capture | Synthetic actual requests full bracket succeeds only at restricted capture; all downstream flags remain false and launcher main still exits 2. |

## Dependency and validation evidence

Python **3.12.13**, uv **0.11.6**, requests **2.34.2**, urllib3 **2.8.0**.
Requests is now a direct runtime dependency. Existing resolved package nodes,
versions, markers, artifact URLs and hashes remain unchanged. A fresh offline
resolution initially failed because compatible pandas index metadata was absent
from the available cache. No packages were downloaded. Only the two root
requests edges were added to the existing lock; pinned uv's offline lock check,
frozen cache sync and export validation then passed. `requirements.txt` was
regenerated and is byte-identical; the dependency-check configuration is unchanged.

All runtime checks used a task-local wrapper at
`/private/tmp/investing-requests-review/offline/run`: macOS `sandbox-exec` with
`(version 1)(allow default)(deny network*)`, plus inherited `sitecustomize`
socket/DNS rejection. `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never` and the existing
`/private/tmp/investing-stage1-uv-cache` were set. No operational capture command
was run against private setup. The exact commands, from the isolated checkout:

```sh
OFFLINE_RUN=/private/tmp/investing-requests-review/offline/run
PINNED_UV=/private/tmp/investing-stage1-uv-cache/archive-v0/MuED7d7QzcoH6LTW/uv-0.11.6.data/scripts/uv

"$OFFLINE_RUN" "$PINNED_UV" lock --offline --check
"$OFFLINE_RUN" "$PINNED_UV" sync --offline --frozen --all-groups
"$OFFLINE_RUN" "$PINNED_UV" export --offline --quiet --frozen --no-dev --format requirements.txt --no-annotate --no-header --output-file /private/tmp/investing-requests-review/requirements.txt
diff -u requirements.txt /private/tmp/investing-requests-review/requirements.txt
"$OFFLINE_RUN" .venv/bin/deptry .
"$OFFLINE_RUN" .venv/bin/python -m pip check
"$OFFLINE_RUN" .venv/bin/python -m pytest -q tests/test_demo_operator_capture.py tests/test_requests_capture_transport.py tests/contracts/etoro/test_etoro_account_evidence.py tests/test_demo_operator.py tests/test_demo_operator_commands.py tests/test_execution_submission_safety.py tests/test_fill_agent.py
"$OFFLINE_RUN" .venv/bin/python -m pytest -q
"$OFFLINE_RUN" .venv/bin/python -m compileall -q agents brokers execution risk scheduler shared strategy tools scripts run_pipeline.py
git diff --check
```

Results: **314 focused tests passed; 741 full-suite tests passed**. The
requests transport file contributes 62 tests. All listed dependency,
compilation and whitespace checks passed. Before implementation, all 244
requested existing regression tests passed. The final suite replaces three
urllib-specific tests with actual requests coverage. No live validation,
network-based vulnerability audit, remote CI or Python 3.11/3.13 platform run
was performed; those results are not claimed.

## Delivery and remaining limits

Independent read-only review must identify the exact final commit/diff and
dispose of any actionable findings before handoff. The local handoff receipt
records that identity, review, exact commands/results and PR-ready title/body;
this document does not certify a different future revision. No substantive
edit may follow final review without renewing it.

Rollback is to discard the isolated branch or revert its implementation commit
before any separately authorized operational use. Never remove or rewrite an
attempt claim, receipt, archive, setup record or provenance to enable another
attempt. No operational rollback is necessary for this offline work.

Timestamp convention, complete cash-flow/execution history, risk baseline,
daily PnL, peak equity, exposure compliance and intent-bound market prerequisites
remain unresolved. Offline success grants no broker acceptance or qualification.
A further bounded bracketed capture requires separate owner authorization.
It has not been run or scheduled.
