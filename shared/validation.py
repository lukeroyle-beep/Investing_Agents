from __future__ import annotations

"""
Backward-compatible validation shim.

This module previously carried an older, diverged implementation of
portfolio-state validation. The canonical schema and normalization logic now
live in shared.schemas and should be imported from there directly in new code.
"""

from shared.schemas import normalise_to_schema, validate_portfolio_state

__all__ = [
    "normalise_to_schema",
    "validate_portfolio_state",
]
