"""Immutable-dict helper for frozen dataclasses.

Frozen dataclasses (``@dataclass(frozen=True)``) prevent attribute
reassignment but do NOT prevent mutation of container-valued fields
like ``dict`` / ``list``. Callers that hold a reference to
``record.decision_trace`` can silently mutate what is supposed to be
an immutable snapshot, breaking append-only and byte-identical
determinism guarantees.

This module provides a small ``dict``-subclass whose mutation methods
raise ``TypeError``. It preserves every reader operation (iteration,
``__getitem__``, ``.get``, ``.items``, ``json.dumps``, ``dict(x)``,
``isinstance(x, dict)``) so no downstream consumer needs to change.

Design constraints:
- Must be a ``dict`` subclass so ``json.dumps``, ``isinstance(_, dict)``,
  and ``copy.deepcopy`` behave normally.
- Must raise on every mutation method, including the less-common ones
  (``setdefault``, ``pop``, ``popitem``, ``clear``, ``update``).
- Must not shadow attribute assignment on the dataclass itself; the
  dataclass's own ``frozen=True`` still guards that.
- Public escape hatch: ``dict(x)`` or ``x.copy()`` return a plain
  mutable dict (Python's built-in ``dict.copy`` on a subclass returns
  ``dict`` by design).
"""
from __future__ import annotations

from typing import Any


class _FrozenDict(dict):
    """dict subclass with all mutation operations disabled.

    Behaves identically to ``dict`` for readers. Any attempt to
    ``__setitem__``, ``__delitem__``, ``clear``, ``update``, ``pop``,
    ``popitem`` or ``setdefault`` raises ``TypeError``.

    Not intended for external instantiation; the ``freeze_dict``
    factory below is the sanctioned constructor.
    """
    __slots__ = ()

    def _mutation_blocked(self, *args: Any, **kwargs: Any) -> Any:
        raise TypeError(
            "cannot mutate immutable field on a frozen dataclass; "
            "call dict(x) or x.copy() to obtain a mutable copy"
        )

    __setitem__ = _mutation_blocked
    __delitem__ = _mutation_blocked
    clear       = _mutation_blocked
    pop         = _mutation_blocked
    popitem     = _mutation_blocked
    setdefault  = _mutation_blocked
    update      = _mutation_blocked

    def __repr__(self) -> str:                             # pragma: no cover
        return f"_FrozenDict({dict.__repr__(self)})"


def freeze_dict(d: Any) -> _FrozenDict:
    """Return a shallow-copied ``_FrozenDict`` view.

    Accepts anything convertible to a dict (dict-like, ``None``, empty
    tuple, existing ``_FrozenDict``). Idempotent on ``_FrozenDict``
    inputs so ``__post_init__`` can be called safely from
    ``dataclasses.replace``.
    """
    if isinstance(d, _FrozenDict):
        return d
    if d is None:
        return _FrozenDict()
    return _FrozenDict(d)


__all__ = ["_FrozenDict", "freeze_dict"]
