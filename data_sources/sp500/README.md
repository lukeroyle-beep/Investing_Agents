# S&P 500 point-in-time membership

Retrieved 2026-11-22. Valid as-of dates are 2021-12-20 through 2026-11-22.
This is not an official S&P Dow Jones constituent product, and it is not a
CRSP survivor-bias-free database.

## Sources

| File | What it is | Retrieved |
| --- | --- | --- |
| `wikipedia_constituents_2026-11-22.csv` | Current constituents from [List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies) | 2026-11-22 |
| `wikipedia_changes_after_2026-08-18.csv` | Changes after the splice date from [Historical components of the S&P 500](https://en.wikipedia.org/wiki/Historical_components_of_the_S%26P_500) | 2026-11-22 |
| `sp500_changes_since_2019.csv` | Change file from [fja05680/sp500](https://github.com/fja05680/sp500) commit `a2430f2af0c79ddf0748e91de11bdeb1616ab5a7` (committed 2026-09-07). MIT; see `LICENSE.fja05680` | 2026-11-22 |
| `anchors/2021-12-20.txt`, `anchors/2026-08-18.txt` | Membership snapshots extracted the same day from that repo's `S&P 500 Historical Components & Changes (Updated).csv` | 2026-11-22 |
| `supplemental_changes.csv` | One ticker change the Wikipedia change table omitted | 2026-11-22 |

The supplemental row is PSKY to SKYD on 2026-10-06, effective at the open.
Sources: OCC infomemo 59894 dated 2026-10-05, and Skydance Corporation's
Form 8-K filed 2026-10-06
(`https://www.sec.gov/Archives/edgar/data/2041610/000110465926113913/tm2626659d7_8k.htm`).
The retrieved Wikipedia constituent list uses SKYD. Without this row the
2026-08-18 snapshot does not reconcile to that list.

`scripts/build_sp500_point_in_time.py` rebuilds `point_in_time_membership.csv`.
The build fails if the walk from the current list does not match both anchors.
Dots in source symbols are stored as Yahoo hyphens (`BRK.B` becomes `BRK-B`),
the same convention as `data_sources/stock_universe.csv`. That is the same
security, not a different one.

## How a date is interpreted

A change is effective before the open on its published effective date.
`effective_from` is inclusive. `effective_to` is exclusive, so a removed name
is not a member on the removal date. `delisted_at` is blank: these files do
not consistently say whether the shares kept trading. An `effective_from` of
2021-12-20 means the anchor snapshot already contained the symbol. It is not
the date the symbol joined the index.

On 2022-01-01 the table has 505 members. On 2026-11-22 it has 503. The S&P 500
often has more than 500 lines because more than one share class is included.
The built file has 611 intervals and 609 symbols.

## What is still biased

Yahoo, through `shared/market_data.py`, often has no bars for names that were
acquired or failed. A probe on 2026-11-22 returned no history for TWTR, SIVB,
FRC, ATVI, XLNX, CERN, and other removed names. Those members are counted as
missing. Dropping them removes both takeover premia and failure losses, so
the direction of the remaining bias is not known from the coverage ratio alone.

Some tickers were reused. On the same probe, FB and SBNY returned bars that
start years after the membership in this table ended. Those series do not
cover the membership sessions, so they are partial and are not traded. They
are not mapped onto the successor ticker.

The strategy does not use index weights. Split-adjusted Yahoo `Close` with
`auto_adjust` off is the same price convention as the rest of this repo, so a
later split can still rescale earlier bars. Coverage sessions are SPY's
sessions, a proxy, not a qualified exchange calendar. Membership after
2026-11-22 is not in this file; the backtest rejects an end date after that.
