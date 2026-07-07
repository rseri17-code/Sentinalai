"""Safe coercion helpers for adversarial-input tolerance at ingest boundaries.

Every call site inside ``sentinel_core`` that reads a value out of an
untrusted dict payload (receipt, JSON, connector output) should use
these helpers instead of raw ``str(...)``, ``int(...)``, ``float(...)``,
or ``tuple(x for x in reported)``.

Design constraints
------------------
- Pure. No I/O, no state, no logging.
- Deterministic: same input → same output.
- ``None`` never surfaces as the string ``"None"``.
- Invalid numeric strings never raise — they become the caller-supplied
  default.
- A ``str`` is treated as a scalar, not a sequence — so a scenario that
  reports ``evidence_keys="abc"`` does not silently score three
  characters.
- Never masks corruption if a warning/error field is available upstream;
  these helpers just refuse to *crash* on it.
"""
from __future__ import annotations

from typing import Any


def coerce_str(value: Any, default: str = "") -> str:
    """Return ``str(value)`` unless value is ``None``.

    ``None`` returns ``default`` (empty string) rather than the literal
    text ``"None"`` — the RC-H "str(None) contamination" fix.
    """
    if value is None:
        return default
    return str(value)


def coerce_int(value: Any, default: int = 0) -> int:
    """Return ``int(value)`` if convertible, else ``default``.

    Tolerates ``None``, ``"N/A"``, ``"unknown"``, and other adversarial
    strings that would raise from raw ``int(...)``. Falls through
    ``float`` so ``"42.7"`` becomes ``42``.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def coerce_float(value: Any, default: float = 0.0) -> float:
    """Return ``float(value)`` if convertible, else ``default``.

    Tolerates ``None`` and adversarial strings like ``"high"`` that
    would raise from raw ``float(...)``.
    """
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def coerce_seq(value: Any) -> tuple:
    """Coerce ``value`` to a tuple.

    Crucial RC-H semantic: a ``str`` is treated as a single scalar and
    returned as a one-element tuple ``(value,)``, NOT iterated as a
    sequence of characters. This closes the SentinelBench
    string-iteration hole where ``reported_keys="abc"`` was being
    scored as if it reported three separate evidence keys ``"a"``,
    ``"b"``, ``"c"``.

    ``None`` returns ``()``. ``list``/``tuple``/``set`` pass through
    as ``tuple(value)``. ``dict`` returns the tuple of ``.items()``.
    Anything else that iterates is coerced via ``tuple(value)``; the
    final fallback wraps the scalar.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(value.items())
    try:
        return tuple(value)
    except TypeError:
        return (value,)


__all__ = ["coerce_str", "coerce_int", "coerce_float", "coerce_seq"]
