"""Deterministic ordering helpers — the Sprint 3 canonical policy.

Every aggregation, selection, and serialization boundary in
``sentinel_core`` uses the helpers here so callers cannot leak the
insertion order of their input tuples into the output.

Determinism policy
------------------

    Primary key   → domain metric being ranked / selected (count,
                    probability, strength, occurrence_count, …) — usually
                    descending (negate).
    Secondary key → stable lexical identifier of the item (memory_id,
                    capability_id, service_id, pattern_id, root_cause
                    string, …) — ascending.

Callers pass a ``primary`` callable that returns the primary key and a
``secondary`` callable that returns the tie-breaker. The helpers apply
the composite sort ``(primary(x), secondary(x))`` — negate the primary
inside the callable to sort descending.

These helpers are **pure**: no I/O, no state, no logging. They are
stateless and thread-safe.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable, TypeVar


T = TypeVar("T")


def canonical_sort(
    items: Iterable[T],
    primary:   Callable[[T], Any],
    secondary: Callable[[T], Any] | None = None,
) -> list[T]:
    """Return ``items`` sorted by ``(primary(x), secondary(x))``.

    If ``secondary`` is None, only the primary key is applied. Sort is
    stable so callers who already provide a canonical primary don't
    need a secondary. Python's built-in sort is a Timsort — the same
    input produces the same output on every Python version.
    """
    if secondary is None:
        return sorted(items, key=primary)
    return sorted(items, key=lambda x: (primary(x), secondary(x)))


def canonical_top(
    counter: Counter,
    k: int,
    secondary: Callable[[Any], Any] | None = None,
) -> list[tuple[Any, int]]:
    """Return the top ``k`` items from ``counter`` with a stable tie-break.

    ``Counter.most_common(k)`` in CPython breaks count-ties in insertion
    order — that is exactly the RC-F leak we are closing. This helper
    replaces every call site that consumed ``most_common`` for
    externally-visible output.

    The default secondary key is the item itself (lex ascending) which
    is well-defined for the string keys used across ``sentinel_core``.
    Callers who need a non-string key (e.g. tuple-keyed transitions)
    pass an explicit ``secondary`` callable.

    Result shape matches ``Counter.most_common`` — a list of
    ``(key, count)`` tuples, highest count first. Length capped by
    ``k`` (or the counter's own size).
    """
    if secondary is None:
        secondary = lambda x: x
    # Primary: -count so highest goes first. Secondary: the key itself.
    ordered = sorted(
        counter.items(),
        key=lambda kv: (-kv[1], secondary(kv[0])),
    )
    return ordered[:int(k)] if int(k) >= 0 else ordered


def canonical_max(
    items: Iterable[T],
    primary:   Callable[[T], Any],
    secondary: Callable[[T], Any],
) -> T | None:
    """Return the ``max`` element with a stable tie-break, or None on empty.

    Equivalent to ``canonical_sort(items, ...)[0]`` — but expressed at
    the intent level so call sites read as selections rather than sorts.

    ``primary`` should return a value that sorts the DESIRED winner
    first (typically ``-metric`` for "maximize metric"). ``secondary``
    breaks ties.
    """
    items = list(items)
    if not items:
        return None
    return canonical_sort(items, primary, secondary)[0]


__all__ = ["canonical_sort", "canonical_top", "canonical_max"]
