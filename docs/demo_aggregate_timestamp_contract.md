# Demo aggregate timestamp contract investigation

26 October 2026. **BLOCKED: provider timezone semantics remain unresolved.**
Production parsing is unchanged. The saved offset-free response remains
rejected; no authenticated broker request or new capture was made.

## Starting evidence and scope

[PR9](https://github.com/lukeroyle-beep/Investing_Agents/pull/9) was verified
merged at 09:11:26 UTC. This investigation starts from current `master`, merge
`6d44115a38150df0c9c5f70e6a59b92bd6a8be1c`, in an isolated checkout. The dirty
primary checkout and original capture worktree are preserved. No applicable
`AGENTS.md` was found in the repository or inspected ancestors.

The [capture checkpoint](requests_demo_capture_checkpoint.md), PR9 changes and
discussion, aggregate parser, client, account-evidence gates and relevant tests
were inspected. The checkpoint remains authoritative for the original attempt:
two successful responses followed by rejection, without metadata, closing
snapshot or reconciliation. There is no baseline or order.

No provider clarification was present in the inspected repository, PR9
discussion or verified selected Institutional Memory project note. That note
was last reviewed on 29 August and supplies no current timestamp authority.
Private support correspondence outside those sources was not available to this
investigation; its contents are unknown.

## Exact provider contract

New official-source research ran **09:23:24–09:25:48 UTC on 26 October**,
2 minutes 24 seconds, within the 15-minute limit. Each successfully downloaded
source was retained and not repeatedly fetched. Public documentation downloads
used no broker credentials. Reader access failures were resolved using a
credential-free public-document download; no authenticated API request occurred.

| Source and date | Version / exact scope | Supporting wording and result |
| --- | --- | --- |
| [OpenAPI](https://api-portal.etoro.com/api-reference/openapi.json), retrieved 26 October 09:24:16 UTC; server Last-Modified 22 October 07:12:09 GMT | OpenAPI 3.0.1; API v1.400.0; `postAggregatedPortfolioDemo`, `POST /api/v1/trading/info/demo/aggregate-portfolio`; HTTP 200 schema `TradingPortfolioDemoApi_AggregatedPortfolioResponse` | `timestamp`: `type: string`, `format: date-time`; description: "Time at which this portfolio snapshot was calculated." No offset-free timezone guarantee or correction commitment. |
| [Exact POST reference](https://api-portal.etoro.com/api-reference/trading--demo/get-a-filtered-aggregated-portfolio-snapshot), retrieved 26 October 09:24:20 UTC; publication date not supplied | Same Demo POST; no separate page version supplied | Same field description; generated example `2023-11-07T05:31:56Z`. Neither establishes the meaning of the saved offset-free value. |
| [Core changelog](https://api-portal.etoro.com/core/changelog.md), retrieved 26 October 09:25:20 UTC | 25 August 2026 entry, v1.354.0–v1.361.0, filtered aggregate POST including Demo | "You can now request a filtered aggregated portfolio snapshot." No timestamp semantics. Newest visible entry is 12 October, v1.393.0, concerning price-alert filters. No timestamp correction was identified. |

The fresh OpenAPI is **1,832,832 bytes**, SHA-256
`549e77e8f37dc167dfb6295e4771faf1f9bb71f62d0e925109cd6aa9a5d299a0`,
identical to the [22 October specification](stage1_demo_connection_readiness.md).
This successful refresh supersedes only PR9's earlier unavailable refresh;
it does not establish a runtime deployment version or resolve the ambiguity.
The inspected implementation retains its historical v1.398.0 contract pin.

The exact POST page SHA-256 is
`29a054c557bcc8db7b9e295784031c46327be812886a3fa8fd5d5c51652a255c`;
the complete changelog SHA-256 is
`adc2cece113220bd0fdf66f2c6b08909aea8b06eb830f306add00e373d5e3de0`.
Targeted official-domain searches found no applicable provider clarification.
Absence from the inspected public sources does not prove no private clarification
or future fix exists. **Guaranteed timezone, applicable deployed version,
explicit-offset correction and rollout date are all unknown.**

A date-time declaration, a `Z` example, the request's `dailyCutoffUtc`, another
endpoint, the host timezone and local receipt times are not authority to append
an offset. No endpoint-specific normalization is justified, and the shared
timestamp parser is unchanged.

## Historical offline replay

Original private raw and decoded bodies, receipts, timestamp text and identity
provenance were accessible. They were read in place, not rewritten or copied
into this PR. A local replay record retains the original timestamp and receipt
times, with interpretation explicitly unset.

Eight replay checks passed: both responses' raw/decoded hashes and lengths,
gzip equivalence, ordered aware receipt times and initiation budget, absence
of a response offset, offline aggregate identity comparison, exact schema
rejection, collector rejection after the two saved responses, absence of a
third request receipt, and unchanged retained runtime bytes are covered by
these grouped checks.

The direct schema parser and collector both raise
`EtoroSchemaError: timestamp must include a timezone`. Collector replay uses
the original recorded capture start time and the two original decoded bodies
through an in-memory transport with synthetic credentials. It cannot supply a
third response. Recorded request/receive/decode times were checked separately;
they were never substituted for the broker's timestamp. The collector fails
before its completion-time check, metadata or closing snapshot, exactly as in
the original attempt. No closing time or response was invented.

This replay establishes the historical failure only. Broker observation time
and freshness remain unknown. It does not establish current freshness, complete
the missing bracket, perform reconciliation or create account/risk evidence.

## Offline regression coverage

The new synthetic timestamp suite adds 42 cases and passes with the existing
account-evidence contracts: **149 tests passed**. The full suite passed
**784 tests in 13.30 seconds** on Python 3.12.13. It covers:

- Explicit `Z`, positive and negative offsets, half-hour offsets and date rollover.
- Fractional seconds; malformed dates/offsets and unsupported timezone labels;
  strict rejection of offset-free values including seven fractional digits.
- Original timestamp text remaining unchanged and bound into snapshot identity.
- Stopping before metadata and the closing snapshot after timestamp rejection.
- Freshness and capture duration at zero, exactly 15 seconds and one microsecond
  either side; freshness measured at completion; naive local clocks rejected.

The unchanged parser uses Python `datetime.fromisoformat`, which truncates
fractions beyond six digits. Tests characterize that existing behavior; they
do not authorize a provider precision rule. Boundaries are verified to
microsecond resolution. A sub-microsecond future instant can normalize to the
same datetime; no stronger precision guarantee is claimed. The original text
remains preserved in raw evidence and bound into the snapshot hash. Any future
contract implementation must assess provider precision as well as timezone.

Tests and retained replay ran with Python socket/DNS guards and explicit macOS
network denial. Only synthetic tests write temporary fixtures. No launcher was
run, credential values read, or original claim, receipt or response modified.
Existing identity, pending-order, holdings, future-time, freshness and capture
checks remain unchanged. Independent review and remote CI results are recorded
on the PR; local results alone do not claim alternate-platform validation.

The full-suite command ran from the isolated repository with an empty
environment, `PYTHONPATH` set to the repository plus a task-only network guard,
`PYTHONDONTWRITEBYTECODE=1`, and the verified existing Python environment:
`python -m pytest -q -p no:cacheprovider --basetemp=<temporary-directory>`.
The macOS sandbox profile explicitly denied `network*`; the Python guard also
denied socket connections and DNS. No dependency or runtime code changed.

## Handoff

The [support enquiry](etoro_demo_timestamp_support_enquiry.md) is **unsent**.
It identifies the exact route, response field and sanitised failure, and keeps
the existing cash-movement/execution-history questions in a separate section.

**Exact next action:** the owner may send that draft to eToro support and obtain
either an explicit timezone guarantee for offset-free values on this exact
Demo POST and its applicable version, or confirmation that offsets are required
plus the corrected service version and rollout. Review that evidence offline
before making any parser decision. A correction requiring explicit offsets
would still leave this saved ambiguous response rejected.

Any later capture requires a separate bounded instruction after the contract
issue is resolved. No retry, new capture, order, approval, baseline, reset,
permission/risk-policy change, scheduling, deployment or automatic merge is
part of this work. Weekly allowance was observable at admission (79% remaining)
and after full offline validation (78% remaining). Stop at this review checkpoint. Rollback
is closing the unmerged tests/documentation PR; preserve all private evidence.
