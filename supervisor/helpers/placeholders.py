"""Placeholder values are no data.

Compared without case and with whitespace trimmed, the strings ``none``,
``unknown``, and ``""`` are placeholders. So are null and empty lists or
dicts. A container whose every value is a placeholder is a placeholder too.
"""
from __future__ import annotations

_PLACEHOLDER_STRINGS = frozenset({"", "none", "unknown"})


def is_placeholder(value: object) -> bool:
    """Return True when *value* carries no data."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _PLACEHOLDER_STRINGS
    if isinstance(value, (list, tuple)):
        return len(value) == 0 or all(is_placeholder(item) for item in value)
    if isinstance(value, dict):
        return len(value) == 0 or all(is_placeholder(item) for item in value.values())
    return False
