"""Incident Intelligence Memory — permanent operational memory layer.

Placed at ``sentinel_core/intel_memory/`` rather than
``sentinel_core/intelligence_memory/`` because the sentinel_core
package enforces a substring rule against ``"intelligence"`` in module
names (see ``tests/test_sentinel_core_compatibility.py``). The
"``intel_``" convention mirrors ``sentinel_core.models.intel_context``.

Public surface:
- :class:`MemoryRecord` — canonical per-investigation memory row
- :func:`compute_fingerprint` — deterministic incident fingerprint
- :class:`SimilarityEngine` — 11-dimension weighted similarity
- :class:`MemoryStore` — JSON-per-record store with query APIs
- :class:`Retrieval` — filter APIs by fingerprint, service, incident_type, ...
- :class:`LearningLoop` — recurring-pattern detector
- :class:`Ranker` — deterministic ranking helper
- :class:`GuidedInvestigation` — top-N similar + aggregated recommendation
- report renderers in :mod:`sentinel_core.intel_memory.report`
"""
from __future__ import annotations

from sentinel_core.intel_memory.fingerprint import (
    FINGERPRINT_SCHEMA_VERSION,
    FingerprintInput,
    compute_fingerprint,
    compute_evidence_pattern_hash,
    compute_planner_path_hash,
    compute_topology_hash,
    compute_transaction_path_hash,
)
from sentinel_core.intel_memory.learning import LearningLoop
from sentinel_core.intel_memory.memory_store import MemoryStore, MemoryStoreError
from sentinel_core.intel_memory.ranking import Ranker
from sentinel_core.intel_memory.recommendation import GuidedInvestigation
from sentinel_core.intel_memory.retrieval import Retrieval
from sentinel_core.intel_memory.schemas import (
    MEMORY_SCHEMA_VERSION,
    BlastRadiusSnapshot,
    MemoryRecord,
    RecurringPattern,
    RecurringPatternKind,
    SimilarityScore,
    TopologySnapshot,
)
from sentinel_core.intel_memory.similarity import (
    SIMILARITY_WEIGHTS,
    SimilarityEngine,
)


__all__ = [
    # Schema
    "MEMORY_SCHEMA_VERSION",
    "MemoryRecord",
    "TopologySnapshot",
    "BlastRadiusSnapshot",
    "SimilarityScore",
    "RecurringPattern",
    "RecurringPatternKind",
    # Fingerprint
    "FINGERPRINT_SCHEMA_VERSION",
    "FingerprintInput",
    "compute_fingerprint",
    "compute_topology_hash",
    "compute_transaction_path_hash",
    "compute_planner_path_hash",
    "compute_evidence_pattern_hash",
    # Engines
    "SIMILARITY_WEIGHTS",
    "SimilarityEngine",
    "MemoryStore",
    "MemoryStoreError",
    "Retrieval",
    "LearningLoop",
    "Ranker",
    "GuidedInvestigation",
]
