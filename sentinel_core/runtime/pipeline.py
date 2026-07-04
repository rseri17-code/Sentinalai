"""IntelligenceRuntime — orchestration layer for pluggable intelligence modules.

Additive activation scaffold designed under strict architectural constraints:
- No business-logic duplication. Modules register callable runners; the
  runtime never inspects module contents.
- Failure isolation. A runner that raises is contained — the exception is
  captured on the ModuleResult and swallowed; other modules still run.
- Deterministic execution. Modules are ordered by their declared
  dependencies (topological sort), with ``priority`` as the tie-breaker
  (lower runs first; stable sort by module name for ties within priority).
- Zero-cost when disabled. When ``is_enabled()`` is ``False`` (the default
  when constructed without a master flag), ``run_stage()`` returns an empty
  list without touching any module state.
- Additive-only. No pipeline behavior changes unless a module is registered
  AND both the master flag and (if declared) the module's per-flag are on.

Dependency rule: stdlib + typing only. Belongs in ``sentinel_core`` next to
the other zero-dependency contracts.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("sentinalai.intelligence.runtime")


# ---------------------------------------------------------------------------
# Stages — one per pipeline hook point
# ---------------------------------------------------------------------------

class IntelligenceStage(str, Enum):
    POST_FETCH    = "post_fetch"
    POST_CLASSIFY = "post_classify"
    POST_COLLECT  = "post_collect"
    POST_ANALYZE  = "post_analyze"
    POST_PERSIST  = "post_persist"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModuleSpec:
    """Declarative registration record for an intelligence module."""
    name: str
    stage: IntelligenceStage
    feature_flag: str = ""                    # env-var name; empty = always eligible
    priority: int = 100                       # lower runs first
    dependencies: tuple[str, ...] = ()        # module names that must complete first


# ---------------------------------------------------------------------------
# Per-invocation input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeContext:
    """Handle passed to each module runner.

    All non-``investigation_id`` fields are optional so early-stage runners
    (e.g. ``POST_FETCH``) can be constructed with only the data available at
    that stage. Runners read what they need; the runtime never validates
    field presence.
    """
    investigation_id: str
    stage: IntelligenceStage
    fetch_out: Optional[dict[str, Any]] = None
    cres: Any = None                          # ClassificationResult
    cout: Any = None                          # CollectResult
    aout: Any = None                          # AnalyzeResult
    result: Optional[dict[str, Any]] = None   # persisted result (POST_PERSIST only)
    # Snapshot of already-finalized phase receipts (JSON-safe dicts) at the
    # moment this context was constructed. Empty for stages that run before
    # any receipt exists. Populated by the ``_intel_hook`` helper at
    # POST_PERSIST so downstream modules can build a canonical
    # cross-stage summary (see sentinel_core.models.intel_context) without
    # a runtime-primitive change per iteration.
    phase_receipts: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Per-invocation output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModuleResult:
    """Structured output envelope. JSON-safe via ``to_dict()``."""
    name: str
    status: str                               # success | skipped | failed
    elapsed_ms: float
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name":       self.name,
            "status":     self.status,
            "elapsed_ms": self.elapsed_ms,
            "warnings":   list(self.warnings),
            "metadata":   dict(self.metadata),
            "error_type": self.error_type,
        }


# ---------------------------------------------------------------------------
# Runner type
# ---------------------------------------------------------------------------

# A runner takes a RuntimeContext and returns a dict of metadata that will
# ride on the ModuleResult. May raise — the runtime will capture the
# exception's class name and continue with remaining modules.
RunnerFn = Callable[[RuntimeContext], dict[str, Any]]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------

class IntelligenceRuntime:
    """Registry + executor.

    Register modules via ``register(spec, runner)`` and invoke each stage via
    ``run_stage(stage, ctx)``. The runtime is single-threaded per invocation
    but instance-level thread-safe for registration.
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = bool(enabled)
        self._modules: dict[str, tuple[ModuleSpec, RunnerFn]] = {}

    # ------------------------------------------------------------------
    # Master state
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, spec: ModuleSpec, runner: RunnerFn) -> None:
        """Register a module. Raises ValueError on duplicate name or empty name."""
        if not spec.name:
            raise ValueError("ModuleSpec.name must be non-empty")
        if spec.name in self._modules:
            raise ValueError(f"module already registered: {spec.name!r}")
        if not callable(runner):
            raise ValueError(f"runner for {spec.name!r} is not callable")
        self._modules[spec.name] = (spec, runner)

    def unregister(self, name: str) -> bool:
        """Remove a module by name. Returns True if it existed."""
        return self._modules.pop(name, None) is not None

    def has(self, name: str) -> bool:
        return name in self._modules

    def all_specs(self) -> list[ModuleSpec]:
        return [s for s, _ in self._modules.values()]

    def modules_for(self, stage: IntelligenceStage) -> list[ModuleSpec]:
        """Return the ordered execution plan for this stage."""
        specs = [s for s, _ in self._modules.values() if s.stage == stage]
        return _topological_sort(specs)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_stage(
        self,
        stage: IntelligenceStage,
        ctx: RuntimeContext,
    ) -> list[ModuleResult]:
        """Run all eligible modules at ``stage`` in dependency + priority order.

        Never raises. Any runner exception is captured on its ModuleResult
        (status="failed", error_type=<class name>) and subsequent modules
        still run.
        """
        if not self._enabled:
            return []
        results: list[ModuleResult] = []
        for spec in self.modules_for(stage):
            results.append(self._run_one(spec, ctx))
        return results

    def _run_one(self, spec: ModuleSpec, ctx: RuntimeContext) -> ModuleResult:
        # Per-module feature flag
        if spec.feature_flag:
            v = os.environ.get(spec.feature_flag, "").strip().lower()
            if v not in ("1", "true", "yes", "on"):
                return ModuleResult(name=spec.name, status="skipped", elapsed_ms=0.0)

        _, runner = self._modules[spec.name]
        started = time.monotonic()
        try:
            payload = runner(ctx) or {}
            if not isinstance(payload, dict):
                payload = {}
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return ModuleResult(
                name=spec.name, status="success", elapsed_ms=elapsed_ms,
                metadata=dict(payload),
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            logger.debug(
                "intelligence module %s failed (%s): %s",
                spec.name, type(exc).__name__, exc,
            )
            return ModuleResult(
                name=spec.name, status="failed", elapsed_ms=elapsed_ms,
                error_type=type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# Topological sort — deps first, ties broken by (priority, name)
# ---------------------------------------------------------------------------

def _topological_sort(specs: list[ModuleSpec]) -> list[ModuleSpec]:
    """Stable topological sort by declared dependencies.

    Ties broken by ``priority`` ascending, then ``name`` ascending. Missing
    dependencies (dependency name not present at this stage) are silently
    ignored — modules should only declare deps on modules they know will be
    present. Cyclic deps are broken deterministically (later-visited edges
    are dropped) so ``run_stage`` remains total.
    """
    by_name = {s.name: s for s in specs}
    order: list[ModuleSpec] = []
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _visit(name: str) -> None:
        if name in visited or name not in by_name:
            return
        if name in in_stack:
            # Cycle detected — drop this edge.
            return
        in_stack.add(name)
        s = by_name[name]
        for dep in s.dependencies:
            _visit(dep)
        in_stack.discard(name)
        visited.add(name)
        order.append(s)

    for s in sorted(specs, key=lambda x: (x.priority, x.name)):
        _visit(s.name)
    return order


__all__ = [
    "IntelligenceStage",
    "IntelligenceRuntime",
    "ModuleSpec",
    "ModuleResult",
    "RuntimeContext",
    "RunnerFn",
]
