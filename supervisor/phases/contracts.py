"""Phase contracts — lightweight typed envelopes for future phase modules.

These contracts describe the inputs and outputs that each future phase will
exchange. They are intentionally minimal: this phase ships the scaffolding
but moves NO behaviour from ``supervisor.agent`` yet.

PhaseStatus already lives in ``sentinel_core.models.workflow`` and is
re-exported here for one canonical import location.

Dependency rule: imports only stdlib + sentinel_core (no agent.py / workers /
LLM / receipts), so any future phase module can adopt these without pulling
the supervisor god-module into its own import graph.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from sentinel_core.context import InvestigationContext
from sentinel_core.models.workflow import PhaseStatus


@dataclass(frozen=True)
class PhaseInput:
    """Envelope passed INTO a phase handler.

    ``ctx`` carries identifiers and per-investigation handles. ``evidence``
    is kept separate (it is mutated heavily across phases and not safe to
    pin to a frozen object) — handlers receive it by reference and may
    return mutated evidence in ``PhaseOutput.evidence``.

    ``extras`` is for phase-specific inputs that don't fit on the context
    (e.g. an LLM response payload for the analyze phase).
    """
    ctx: InvestigationContext
    evidence: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhaseOutput:
    """Envelope returned FROM a phase handler.

    ``evidence`` is the post-phase evidence dict (may be the same reference
    that was passed in, with new keys added). ``result`` is the result-shaped
    delta produced by this phase (e.g. classify produces ``incident_type``;
    analyze produces ``root_cause`` / ``confidence``).
    """
    evidence: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseResult:
    """Outcome record for one phase execution.

    Distinct from ``sentinel_core.models.workflow.PhaseResult`` which is the
    persisted lifecycle record. This one is what a phase handler RETURNS to
    the supervisor at runtime — output + status + optional error string.
    """
    phase: str
    status: PhaseStatus
    output: Optional[PhaseOutput] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PhaseStatus.COMPLETED


__all__ = [
    "PhaseInput",
    "PhaseOutput",
    "PhaseResult",
    "PhaseStatus",
]
