"""sentinel_core.runtime — pluggable module runtime primitives.

Zero-dependency (stdlib + typing) plumbing for pluggable pipeline modules.
Modules remain independent — this package owns orchestration only.

Public surface:
    IntelligenceStage    — pipeline hook points
    IntelligenceRuntime  — registration + execution engine
    ModuleSpec           — declarative registration record
    ModuleResult         — structured per-module output
    RuntimeContext       — per-invocation, per-stage input handle
"""
from sentinel_core.runtime.pipeline import (
    IntelligenceRuntime,
    IntelligenceStage,
    ModuleResult,
    ModuleSpec,
    RuntimeContext,
)

__all__ = [
    "IntelligenceStage",
    "IntelligenceRuntime",
    "ModuleSpec",
    "ModuleResult",
    "RuntimeContext",
]
