"""Lightweight service registry for SentinalAI.

A controlled place to construct and retrieve core services. Designed to live
alongside the existing direct-import / module-level-singleton patterns rather
than replace them — callers are free to keep using the old paths.

Design rules:
- Zero external dependencies (stdlib + typing only).
- No reflection, no auto-import scanning.
- Explicit registration. Lazy construction. Thread-safe singletons.
- Test overrides supported via ``register(..., override=True)``.

Typical wiring:

    from sentinel_core.service_registry import get_registry, ServiceLifecycle

    registry = get_registry()
    registry.register("config", lambda: get_config())
    cfg = registry.get("config")

Lifecycle:
- ``SINGLETON`` (default): factory runs once on first ``get``; result is cached.
- ``TRANSIENT``: factory runs on every ``get`` (use sparingly — only for
  intentionally per-call services).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class ServiceLifecycle(str, Enum):
    SINGLETON = "singleton"
    TRANSIENT = "transient"


class ServiceNotFoundError(KeyError):
    """Raised when ``get(name)`` is called for an unregistered service."""


@dataclass
class ServiceHealth:
    """Snapshot of one service's runtime state."""
    name: str
    lifecycle: ServiceLifecycle
    constructed: bool
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name":        self.name,
            "lifecycle":   self.lifecycle.value,
            "constructed": self.constructed,
            "last_error":  self.last_error,
        }


@dataclass
class ServiceDescriptor:
    """Internal record of a registered service.

    Holds the factory, the cached singleton (if applicable), and any last
    construction error for health reporting.
    """
    name: str
    factory: Callable[[], Any]
    lifecycle: ServiceLifecycle = ServiceLifecycle.SINGLETON
    instance: Any = None
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def constructed(self) -> bool:
        return self.instance is not None

    def health(self) -> ServiceHealth:
        return ServiceHealth(
            name=self.name,
            lifecycle=self.lifecycle,
            constructed=self.constructed,
            last_error=self.last_error,
        )


class ServiceRegistry:
    """Process-level registry. Keep one per process (use ``get_registry()``)."""

    def __init__(self) -> None:
        self._services: dict[str, ServiceDescriptor] = {}
        self._registry_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        factory: Callable[[], Any],
        lifecycle: ServiceLifecycle = ServiceLifecycle.SINGLETON,
        override: bool = False,
    ) -> None:
        """Register a service factory.

        - ``override=False`` (default): raise if the name is already registered.
        - ``override=True``: replace the factory and clear any cached instance
          (use this from tests to inject fakes).
        """
        if not name:
            raise ValueError("service name must be non-empty")
        with self._registry_lock:
            if name in self._services and not override:
                raise ValueError(f"service already registered: {name!r}")
            self._services[name] = ServiceDescriptor(
                name=name,
                factory=factory,
                lifecycle=lifecycle,
            )

    def has(self, name: str) -> bool:
        return name in self._services

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def get(self, name: str) -> Any:
        """Resolve a service by name.

        Singletons construct on first call and cache. Transients construct
        every call. Construction errors are stored on the descriptor's
        ``last_error`` for health reporting.
        """
        desc = self._services.get(name)
        if desc is None:
            raise ServiceNotFoundError(name)

        if desc.lifecycle == ServiceLifecycle.TRANSIENT:
            return self._build(desc)

        if desc.instance is not None:
            return desc.instance
        with desc._lock:
            if desc.instance is None:
                desc.instance = self._build(desc)
            return desc.instance

    @staticmethod
    def _build(desc: ServiceDescriptor) -> Any:
        try:
            instance = desc.factory()
            desc.last_error = ""
            return instance
        except Exception as exc:
            desc.last_error = f"{type(exc).__name__}: {exc}"
            raise

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def health(self, name: str) -> ServiceHealth:
        desc = self._services.get(name)
        if desc is None:
            raise ServiceNotFoundError(name)
        return desc.health()

    def list_services(self) -> list[ServiceHealth]:
        return [d.health() for d in self._services.values()]

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def reset_for_tests(self) -> None:
        """Clear all registrations and cached instances.

        Use only from tests — never from production code paths.
        """
        with self._registry_lock:
            self._services.clear()

    def clear_instances(self) -> None:
        """Drop all cached singleton instances but keep registrations.

        Useful when tests need a fresh build of services but the same factories.
        """
        with self._registry_lock:
            for desc in self._services.values():
                desc.instance = None
                desc.last_error = ""


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_registry: Optional[ServiceRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> ServiceRegistry:
    """Return the process-level ServiceRegistry, creating it on first call."""
    global _registry
    if _registry is not None:
        return _registry
    with _registry_lock:
        if _registry is None:
            _registry = ServiceRegistry()
        return _registry


def reset_registry_for_tests() -> None:
    """Drop the process-level registry entirely (tests only)."""
    global _registry
    with _registry_lock:
        _registry = None


__all__ = [
    "ServiceLifecycle",
    "ServiceNotFoundError",
    "ServiceHealth",
    "ServiceDescriptor",
    "ServiceRegistry",
    "get_registry",
    "reset_registry_for_tests",
]
