"""supervisor.phases — scaffolding for future agent.py decomposition.

This package is intentionally near-empty in Phase 7. The contracts in
``supervisor.phases.contracts`` describe the envelope each phase will
exchange when the work to move investigate()'s internal stages out of
agent.py begins. Until then, the individual phase modules (fetch, classify,
collect, analyze, persist) are scaffolds only — they document the boundary
they will hold but contain no live behavior.

This separation lets downstream code start writing against
``supervisor.phases.contracts`` (PhaseInput / PhaseOutput / PhaseResult /
PhaseStatus) before the implementation migration begins.
"""
from supervisor.phases.contracts import (
    PhaseInput,
    PhaseOutput,
    PhaseResult,
    PhaseStatus,
)

__all__ = [
    "PhaseInput",
    "PhaseOutput",
    "PhaseResult",
    "PhaseStatus",
]
