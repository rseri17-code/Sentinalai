"""Metadata filter for institutional knowledge retrieval.

Enforces hard-filter BEFORE any similarity retrieval. If the filter
returns empty, retrieval must not proceed (no global search allowed).
"""

from __future__ import annotations

import time
from typing import Any


def filter_by_metadata(
    candidates: list[dict[str, Any]],
    service: str | None = None,
    environment: str | None = None,
    time_window_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Apply hard metadata filters to candidate nodes.

    Args:
        candidates: List of node dicts with 'metadata' and optional 'timestamp'
        service: Filter by metadata.service (exact match)
        environment: Filter by metadata.environment (exact match)
        time_window_seconds: Only include nodes within this many seconds of now

    Returns:
        Filtered list. Empty list means retrieval should be skipped entirely.
    """
    results = candidates

    if service is not None:
        results = [
            c for c in results
            if c.get("metadata", {}).get("service") == service
        ]

    if environment is not None:
        results = [
            c for c in results
            if c.get("metadata", {}).get("environment") == environment
        ]

    if time_window_seconds is not None:
        cutoff = time.time() - time_window_seconds
        results = [
            c for c in results
            if c.get("timestamp", 0) >= cutoff
        ]

    return results
