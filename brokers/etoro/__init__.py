"""eToro Demo adapter. Gate A exposes read capabilities only."""

from brokers.etoro.adapter import EtoroDemoExecutionAdapter, EtoroDemoReadOnlyAdapter
from brokers.etoro.client import EtoroReadOnlyClient
from brokers.etoro.execution_client import EtoroDemoExecutionClient

__all__ = [
    "EtoroDemoExecutionAdapter",
    "EtoroDemoExecutionClient",
    "EtoroDemoReadOnlyAdapter",
    "EtoroReadOnlyClient",
]
