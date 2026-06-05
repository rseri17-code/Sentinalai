"""SentinelAI Intelligence Foundation — Phase 1.

Public API surface. Import from here; internal module structure may change.

    from intelligence import EvidenceGraph, EvidenceNode, EvidenceEdge
    from intelligence import ResolutionOutcome, ResolutionStatus
    from intelligence import ServiceProfile, PatternSignature, DecisionTrace
    from intelligence import ReplaySeed
    from intelligence import evidence_dict_to_graph
    from intelligence import get_store, InvestigationStore
"""

from intelligence.evidence_graph import EvidenceEdge, EvidenceGraph, EvidenceNode
from intelligence.decision_trace import DecisionTrace, DecisionTraceLog
from intelligence.investigation_store import InvestigationStore, InvestigationRecord, get_store
from intelligence.pattern_signature import PatternSignature, PatternSignatureIndex
from intelligence.replay_seed import ReplaySeed, ReplaySeedStore
from intelligence.resolution_outcome import ResolutionOutcome, OutcomeStore, get_outcome_store
from intelligence.schema import (
    EdgeRelationship,
    EntityType,
    InvestigationPhase,
    NodeType,
    ResolutionStatus,
    SCHEMA_VERSION,
    new_id,
)
from intelligence.service_profile import ServiceProfile, ServiceProfileIndex
from intelligence.bridge import evidence_dict_to_graph, graph_to_evidence_dict

__all__ = [
    # Graph
    "EvidenceGraph", "EvidenceNode", "EvidenceEdge",
    # Schema types
    "NodeType", "EdgeRelationship", "ResolutionStatus",
    "EntityType", "InvestigationPhase", "SCHEMA_VERSION", "new_id",
    # Intelligence entities
    "ResolutionOutcome", "OutcomeStore", "get_outcome_store",
    "ServiceProfile", "ServiceProfileIndex",
    "PatternSignature", "PatternSignatureIndex",
    "DecisionTrace", "DecisionTraceLog",
    "ReplaySeed", "ReplaySeedStore",
    # Store
    "InvestigationStore", "InvestigationRecord", "get_store",
    # Bridge
    "evidence_dict_to_graph", "graph_to_evidence_dict",
]
