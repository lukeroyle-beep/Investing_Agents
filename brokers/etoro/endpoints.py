from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RateBucket(StrEnum):
    ACCOUNT_READ = "account_read"
    MARKET_READ = "market_read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class Endpoint:
    operation: str
    method: str
    path: str
    rate_bucket: RateBucket


# Pinned per operation from the official reference on 2026-08-28.
SEARCH_INSTRUMENTS = Endpoint(
    operation="search_instruments",
    method="GET",
    path="/api/v1/market-data/search",
    rate_bucket=RateBucket.MARKET_READ,
)
GET_MARKET_RATES = Endpoint(
    operation="get_market_rates",
    method="GET",
    path="/api/v1/market-data/instruments/rates",
    rate_bucket=RateBucket.MARKET_READ,
)
GET_DEMO_AGGREGATE_PORTFOLIO = Endpoint(
    operation="get_demo_aggregate_portfolio",
    method="GET",
    path="/api/v1/trading/info/demo/aggregate-portfolio",
    rate_bucket=RateBucket.ACCOUNT_READ,
)

# Gate B routes are deliberately pinned per operation.  A queued/accepted
# response from any mutation is not fill evidence; the matching lookup route
# remains authoritative for lifecycle and Fill ingestion.
SUBMIT_DEMO_ORDER_V3 = Endpoint(
    operation="submit_demo_order_v3",
    method="POST",
    path="/api/v3/trading/execution/demo/orders",
    rate_bucket=RateBucket.WRITE,
)
LOOKUP_DEMO_ORDER_V2 = Endpoint(
    operation="lookup_demo_order_v2",
    method="GET",
    path="/api/v2/trading/info/demo/orders:lookup",
    rate_bucket=RateBucket.ACCOUNT_READ,
)
CLOSE_DEMO_POSITION_V1 = Endpoint(
    operation="close_demo_position_v1",
    method="POST",
    path="/api/v1/trading/execution/demo/market-close-orders/positions/{position_id}",
    rate_bucket=RateBucket.WRITE,
)
LOOKUP_DEMO_CLOSE_ORDER_V1 = Endpoint(
    operation="lookup_demo_close_order_v1",
    method="GET",
    path="/api/v1/trading/info/demo/close-orders/{order_id}",
    rate_bucket=RateBucket.ACCOUNT_READ,
)
CANCEL_DEMO_ORDER_V3 = Endpoint(
    operation="cancel_demo_order_v3",
    method="DELETE",
    path="/api/v3/trading/execution/demo/orders/{order_id}",
    rate_bucket=RateBucket.WRITE,
)

DEMO_EXECUTION_ENDPOINTS = (
    SUBMIT_DEMO_ORDER_V3,
    LOOKUP_DEMO_ORDER_V2,
    CLOSE_DEMO_POSITION_V1,
    LOOKUP_DEMO_CLOSE_ORDER_V1,
    CANCEL_DEMO_ORDER_V3,
)

if any("/real/" in endpoint.path for endpoint in DEMO_EXECUTION_ENDPOINTS):
    raise RuntimeError("Gate B endpoint table must not contain Real routes")

READ_ONLY_ENDPOINTS = (
    SEARCH_INSTRUMENTS,
    GET_MARKET_RATES,
    GET_DEMO_AGGREGATE_PORTFOLIO,
)

if any(endpoint.method != "GET" for endpoint in READ_ONLY_ENDPOINTS):
    raise RuntimeError("Gate A endpoint table must remain read-only")
