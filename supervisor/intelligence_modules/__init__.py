"""Supervisor-side intelligence module registrations.

Each submodule declares a ``ModuleSpec`` + runner callable that plugs into
the Phase 19 Intelligence Runtime. ``install_default_modules(runtime)``
registers every default module in one call — the single seam that
``supervisor.agent.investigate()`` needs to know about.

Future intelligence activations register themselves here, requiring zero
changes to ``investigate()``.

Currently registered:

POST_CLASSIFY (read-path):
- historical_lookup       (ENABLE_HISTORICAL_LOOKUP) — first read consumer
                           of the persisted intelligence corpus (RM + IS).
- pattern_recognition     (ENABLE_PATTERN_RECOGNITION) — surfaces recurring
                           operational patterns from PatternIntelligenceStore.
- incident_graph_lookup   (ENABLE_INCIDENT_GRAPH_LOOKUP) — related incidents
                           on the current service from IncidentGraphStore.
- dependency_graph_lookup (ENABLE_DEPENDENCY_GRAPH_LOOKUP) — upstream +
                           downstream service topology and blast radius from
                           DependencyGraphStore.

POST_PERSIST (write-path):
- resolution_memory       (Phase 20, ENABLE_RESOLUTION_MEMORY_WRITE)
- investigation_store     (Phase 21, ENABLE_INVESTIGATION_STORE_WRITE)
                           — depends on resolution_memory so it can reference
                             the RM record_id in its envelope
"""
from supervisor.intelligence_modules.dependency_graph_lookup import (
    DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG,
    DEPENDENCY_GRAPH_LOOKUP_SPEC,
    dependency_graph_lookup_runner,
)
from supervisor.intelligence_modules.historical_lookup import (
    HISTORICAL_LOOKUP_FEATURE_FLAG,
    HISTORICAL_LOOKUP_SPEC,
    historical_lookup_runner,
)
from supervisor.intelligence_modules.incident_graph_lookup import (
    INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG,
    INCIDENT_GRAPH_LOOKUP_SPEC,
    incident_graph_lookup_runner,
)
from supervisor.intelligence_modules.investigation_store import (
    INVESTIGATION_STORE_FEATURE_FLAG,
    INVESTIGATION_STORE_SPEC,
    investigation_store_runner,
)
from supervisor.intelligence_modules.pattern_recognition import (
    PATTERN_RECOGNITION_FEATURE_FLAG,
    PATTERN_RECOGNITION_SPEC,
    pattern_recognition_runner,
)
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
    runtime.register(HISTORICAL_LOOKUP_SPEC, historical_lookup_runner)
    runtime.register(PATTERN_RECOGNITION_SPEC, pattern_recognition_runner)
    runtime.register(INCIDENT_GRAPH_LOOKUP_SPEC, incident_graph_lookup_runner)
    runtime.register(DEPENDENCY_GRAPH_LOOKUP_SPEC, dependency_graph_lookup_runner)
    runtime.register(RESOLUTION_MEMORY_SPEC, resolution_memory_runner)
    runtime.register(INVESTIGATION_STORE_SPEC, investigation_store_runner)


__all__ = [
    "install_default_modules",
    "HISTORICAL_LOOKUP_SPEC",
    "HISTORICAL_LOOKUP_FEATURE_FLAG",
    "historical_lookup_runner",
    "PATTERN_RECOGNITION_SPEC",
    "PATTERN_RECOGNITION_FEATURE_FLAG",
    "pattern_recognition_runner",
    "INCIDENT_GRAPH_LOOKUP_SPEC",
    "INCIDENT_GRAPH_LOOKUP_FEATURE_FLAG",
    "incident_graph_lookup_runner",
    "DEPENDENCY_GRAPH_LOOKUP_SPEC",
    "DEPENDENCY_GRAPH_LOOKUP_FEATURE_FLAG",
    "dependency_graph_lookup_runner",
    "RESOLUTION_MEMORY_SPEC",
    "RESOLUTION_MEMORY_FEATURE_FLAG",
    "resolution_memory_runner",
    "INVESTIGATION_STORE_SPEC",
    "INVESTIGATION_STORE_FEATURE_FLAG",
    "investigation_store_runner",
]
