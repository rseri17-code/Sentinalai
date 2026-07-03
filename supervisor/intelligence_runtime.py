"""Supervisor-side factory for the IntelligenceRuntime.

Phase 19 ships the scaffold only. ``build_default_runtime()`` returns an
``IntelligenceRuntime`` whose enabled state is derived from the
``ENABLE_INTELLIGENCE_RUNTIME`` env var (default off). No default module
registrations happen here — future phases opt individual modules in.

When the master flag is off, the runtime is a zero-cost no-op at every
hook site inside ``investigate()``.
"""
from __future__ import annotations

import os

from sentinel_core.intelligence import IntelligenceRuntime

RUNTIME_ENV_FLAG = "ENABLE_INTELLIGENCE_RUNTIME"


def is_runtime_enabled() -> bool:
    """Read the master runtime feature flag at call time."""
    val = os.environ.get(RUNTIME_ENV_FLAG, "").strip().lower()
    return val in ("1", "true", "yes", "on")


def build_default_runtime() -> IntelligenceRuntime:
    """Return a runtime whose enabled state mirrors the master env flag.

    No modules are registered by default in Phase 19; callers may register
    their own via ``runtime.register(spec, runner)``. Individual tests
    construct their own runtime directly to avoid env-var coupling.
    """
    return IntelligenceRuntime(enabled=is_runtime_enabled())


__all__ = ["RUNTIME_ENV_FLAG", "is_runtime_enabled", "build_default_runtime"]
