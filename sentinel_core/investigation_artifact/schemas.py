"""InvestigationArtifact schema — the canonical investigation envelope.

Frozen, versioned, deterministic. Contains summaries and pointers only —
never full evidence values (those live in the replay store) and never
mutable runtime state (budgets, circuits, futures, thread-locals).

Field groups follow the approved Wave 1 contract:
IDENTITY / OUTCOME / PROCESS / AUDIT / LINKS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sentinel_core.models._immutable import freeze_dict

ARTIFACT_SCHEMA_VERSION = 1

# The five admission lifecycle states. The artifact's own ``admission_state``
# field records the state at creation time (always "candidate" — content-
# addressed identity must never change). The authoritative live state is
# the store directory the artifact file sits in, plus append-only audit
# events ("validated" is an event on an admitted artifact, not a move).
ADMISSION_STATES: tuple[str, ...] = (
    "candidate", "admitted", "validated", "quarantined", "rejected",
)

# Outcome of the investigation run this artifact snapshots.
OUTCOME_STATUSES: tuple[str, ...] = (
    "completed", "early_return", "meta_query", "blocked", "failed",
)


@dataclass(frozen=True)
class InvestigationArtifact:
    """Immutable record of one completed ``investigate()`` call.

    All container fields are frozen in ``__post_init__`` (RC-D pattern) so
    a caller holding a reference cannot mutate the snapshot. Timestamps
    are caller-supplied ISO-8601 strings — never generated here.
    """

    # -- IDENTITY -------------------------------------------------------
    artifact_id:              str
    incident_id:              str
    investigation_id:         str
    created_at:               str

    # -- OUTCOME --------------------------------------------------------
    root_cause:               str = ""
    confidence:               int = 0
    status:                   str = "completed"          # OUTCOME_STATUSES

    # -- IDENTITY/OUTCOME enrichment (Wave 3 readiness, B2) --------------
    # Additive optional fields; absent on pre-enrichment artifacts and
    # defaulted by from_dict — no schema_version bump (additive rule).
    service:                  str = ""
    incident_type:            str = ""
    severity:                 str = ""
    environment:              str = ""
    application:              str = ""
    resolution:               str = ""
    false_leads:              tuple = ()                 # tuple[str, ...]
    runtime_cost:             int = 0

    # -- PROCESS --------------------------------------------------------
    phase_receipts:           tuple = ()                 # tuple[dict, ...]
    receipt_hashes:           tuple = ()                 # tuple[str, ...]
    final_result_summary:     dict[str, Any] = field(default_factory=dict)
    decision_summary:         dict[str, Any] = field(default_factory=dict)
    evidence_key_summary:     dict[str, Any] = field(default_factory=dict)
    planner_trace_summary:    dict[str, Any] = field(default_factory=dict)
    worker_execution_summary: dict[str, Any] = field(default_factory=dict)

    # -- AUDIT ----------------------------------------------------------
    admission_state:          str = "candidate"
    provenance:               dict[str, Any] = field(default_factory=dict)

    # -- LINKS ----------------------------------------------------------
    replay_pointer:           str = ""
    benchmark_pointer:        str = ""
    memory_pointer:           str = ""

    schema_version:           int = ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # RC-D: freeze every container field against mutation-by-reference.
        object.__setattr__(self, "phase_receipts",
                           tuple(freeze_dict(r) if isinstance(r, dict) else r
                                 for r in self.phase_receipts))
        object.__setattr__(self, "receipt_hashes", tuple(self.receipt_hashes))
        object.__setattr__(self, "false_leads",
                           tuple(str(x) for x in self.false_leads))
        for name in ("final_result_summary", "decision_summary",
                     "evidence_key_summary", "planner_trace_summary",
                     "worker_execution_summary", "provenance"):
            object.__setattr__(self, name, freeze_dict(getattr(self, name)))


__all__ = [
    "ADMISSION_STATES",
    "ARTIFACT_SCHEMA_VERSION",
    "OUTCOME_STATUSES",
    "InvestigationArtifact",
]
