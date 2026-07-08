"""Investigation Artifact — Wave 1 of the Runtime Convergence Program.

The canonical, immutable, versioned record of a completed investigation.
Produce-only in Wave 1: the runtime writes candidate artifacts; nothing
at runtime reads them back. Every downstream subsystem derives its own
view from this artifact (Wave 2+).

Deterministic. Offline. Append-only. Zero runtime reads.
"""
from sentinel_core.investigation_artifact.admission import (
    AdmissionDecision,
    AdmissionController,
)
from sentinel_core.investigation_artifact.builder import build_artifact
from sentinel_core.investigation_artifact.schemas import (
    ADMISSION_STATES,
    ARTIFACT_SCHEMA_VERSION,
    OUTCOME_STATUSES,
    InvestigationArtifact,
)
from sentinel_core.investigation_artifact.serialization import (
    artifact_from_dict,
    artifact_to_dict,
    canonical_json,
    make_artifact_id,
)
from sentinel_core.investigation_artifact.store import (
    ArtifactStore,
    ArtifactStoreError,
)

__all__ = [
    "ADMISSION_STATES",
    "ARTIFACT_SCHEMA_VERSION",
    "OUTCOME_STATUSES",
    "AdmissionController",
    "AdmissionDecision",
    "ArtifactStore",
    "ArtifactStoreError",
    "InvestigationArtifact",
    "artifact_from_dict",
    "artifact_to_dict",
    "build_artifact",
    "canonical_json",
    "make_artifact_id",
]
