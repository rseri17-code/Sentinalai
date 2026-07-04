"""Supervisor-side intelligence module registrations.

Each submodule declares a ``ModuleSpec`` + runner callable that plugs into
the Phase 19 Intelligence Runtime. ``install_default_modules(runtime)``
registers every default module in one call — the single seam that
``supervisor.agent.investigate()`` needs to know about.

Future intelligence activations register themselves here, requiring zero
changes to ``investigate()``.

Currently registered:
- resolution_memory  (Phase 20, POST_PERSIST, ENABLE_RESOLUTION_MEMORY_WRITE)
"""
from supervisor.intelligence_modules.resolution_memory import (
    RESOLUTION_MEMORY_FEATURE_FLAG,
    RESOLUTION_MEMORY_SPEC,
    resolution_memory_runner,
)


def install_default_modules(runtime) -> None:
    """Register every default intelligence module on the runtime.

    Callers should invoke this once per investigation after
    ``build_default_runtime()`` — the runtime instance is per-investigation.
    Idempotency is NOT required (the runtime rejects duplicate names, so a
    second call would raise); callers must guard.
    """
    runtime.register(RESOLUTION_MEMORY_SPEC, resolution_memory_runner)


__all__ = [
    "install_default_modules",
    "RESOLUTION_MEMORY_SPEC",
    "RESOLUTION_MEMORY_FEATURE_FLAG",
    "resolution_memory_runner",
]
