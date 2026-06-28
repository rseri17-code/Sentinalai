"""Concrete service registrations for SentinalAI.

This module bridges the dependency-free ``sentinel_core.service_registry``
with the existing per-module singleton accessors. It is purely additive:
old direct-import paths (``get_config()``, ``get_engine()``, etc.) still
work exactly as before.

Wire services explicitly by calling ``register_default_services()`` at
process startup, or lazily by calling ``get_service(name)`` which auto-wires
on first miss.

Service names (canonical):
    "config"               → SentinelConfig
    "workflow_engine"      → WorkflowEngine
    "confidence_calibrator"→ ConfidenceCalibrator
    "fix_engine"           → FixEngine

Adding a new service:
1. Add a factory entry to ``_DEFAULT_FACTORIES`` below.
2. Add the matching test in ``tests/test_service_registry.py``.
3. Do NOT remove the existing ``get_*()`` accessor — keep both paths.
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from sentinel_core.service_registry import (
    ServiceLifecycle,
    ServiceRegistry,
    get_registry,
)


# ---------------------------------------------------------------------------
# Factory functions — each defers its heavy imports until called.
# ---------------------------------------------------------------------------

def _config_factory() -> Any:
    from supervisor.sentinel_config import get_config
    return get_config()


def _workflow_engine_factory() -> Any:
    from supervisor.workflow_engine import get_engine
    return get_engine()


def _calibrator_factory() -> Any:
    from supervisor.confidence_calibrator import get_calibrator
    return get_calibrator()


def _fix_engine_factory() -> Any:
    from supervisor.fix_engine import get_fix_engine
    return get_fix_engine()


_DEFAULT_FACTORIES: dict[str, Callable[[], Any]] = {
    "config":                _config_factory,
    "workflow_engine":       _workflow_engine_factory,
    "confidence_calibrator": _calibrator_factory,
    "fix_engine":            _fix_engine_factory,
}


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

_wired = False
_wire_lock = threading.Lock()


def register_default_services(
    registry: ServiceRegistry | None = None,
    override: bool = False,
) -> ServiceRegistry:
    """Register the default SentinalAI services.

    Safe to call multiple times — re-registration is a no-op unless
    ``override=True``. Returns the registry that was modified so callers can
    inspect it.
    """
    global _wired
    reg = registry or get_registry()
    with _wire_lock:
        for name, factory in _DEFAULT_FACTORIES.items():
            if reg.has(name) and not override:
                continue
            reg.register(name, factory, ServiceLifecycle.SINGLETON, override=override)
        _wired = True
    return reg


def get_service(name: str) -> Any:
    """Convenience accessor: resolve a service, auto-wiring defaults if needed."""
    if not _wired:
        register_default_services()
    return get_registry().get(name)


__all__ = [
    "register_default_services",
    "get_service",
]
