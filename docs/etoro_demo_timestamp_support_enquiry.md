# Draft eToro support enquiry — UNSENT

Prepared 26 October 2026. No message has been sent. This draft contains no
private request, account or position identifiers and no credentials.

**Subject:** Demo aggregate POST response timestamp lacks an offset — contract clarification

Hello eToro API Support,

We are integrating the read-only Demo portfolio path. A successful HTTP 200
response from **`POST /api/v1/trading/info/demo/aggregate-portfolio`** contained
an offset-free top-level **`timestamp`**. Our strict parser rejected it with
`EtoroSchemaError: timestamp must include a timezone`, and collection stopped
before further reads. We have not retried the capture.

Sanitised shape-only example, not the actual account response:

```json
{"timestamp": "2026-01-02T03:04:05.1234567"}
```

The request used `conversionMode=eToroApp`, `pnlLevel=pnl`,
`accountLevel=totals`, `instrumentLevel=details` and `mirrorLevel=details`,
with no instrument/mirror filters or `dailyCutoffUtc`.

The public OpenAPI retrieved on 26 October reports **v1.400.0**. Operation
`postAggregatedPortfolioDemo` returns
`TradingPortfolioDemoApi_AggregatedPortfolioResponse`, whose `timestamp`
description is "Time at which this portfolio snapshot was calculated."
The date-time declaration and `Z` example do not explain an offset-free value.

Could you please confirm:

1. Is an offset-free `timestamp` valid for this exact Demo POST? If so, what
   timezone is guaranteed, for which API/service versions and effective dates,
   and is that rule independent of account locale, region, server location and
   daylight saving time? Please provide a contractual reference or written
   provider confirmation specific to this route.
2. If explicit offsets are required, is the observed response a provider
   defect? Will a correction emit `Z` or a numeric offset on every response,
   and which corrected version, rollout date and release note cover it?
3. What fractional-second precision is supported, and what rounding or
   truncation behavior should clients expect? We need to retain the source
   timestamp and apply freshness/future-time checks without guessing its zone
   or precision.

We will retain the ambiguous original response and will not treat receipt time,
the request cutoff, another endpoint or the client's local timezone as its
observation time.

## Separate existing questions: Demo cash movements and execution history

- Which supported Demo read enumerates **all external funding, resets,
  adjustments and other cash movements**, with stable event IDs, timestamps,
  retention and verifiable coverage of a requested interval?
- Does `GET /api/v1/trading/info/trade/demo/history` cover only closed trades
  or all executions, including partial fills and partial closes? Which endpoint
  enumerates every execution, and what durable event key supports deduplication?
- Please specify page origin/size limits, stable ordering, completion marker,
  interval inclusivity, upper time bound, retention and snapshot consistency
  for these histories, including how to obtain contiguous, complete windows.
  A known-order lookup does not provide account-wide enumeration. We are not
  treating an empty page as proof of zero funding, or substituting Money
  cash-account transactions or generic balances for Demo trading history.

Thank you.
