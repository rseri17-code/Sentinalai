"""Canonical MemoryRecord + supporting types.

Every completed investigation is represented as an immutable
:class:`MemoryRecord`. Every field is frozen; every tuple is
sort-friendly; every ``to_dict()`` is JSON-safe with deterministic
key ordering.

Design principles
-----------------
- **Immutable** frozen dataclasses.
- **Deterministic serialization**: tuples become lists in ``to_dict``.
- **Missing-tolerant**: every field has a sensible default; no
  constructor argument is required except ``memory_id``.
- **No LLM. No timestamps injected.** Timestamps are caller-supplied.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from sentinel_core.models._immutable import freeze_dict


MEMORY_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TopologySnapshot:
    """Compact topology snapshot at the time of the incident."""
    services:      tuple[str, ...] = ()
    namespaces:    tuple[str, ...] = ()
    pods:          tuple[str, ...] = ()
    nodes:         tuple[str, ...] = ()
    clusters:      tuple[str, ...] = ()
    regions:       tuple[str, ...] = ()
    cloud:         str             = ""
    gateway:       str             = ""
    idp:           str             = ""
    dns:           str             = ""
    databases:     tuple[str, ...] = ()
    dependencies:  tuple[tuple[str, str], ...] = ()   # (source, target) pairs


@dataclass(frozen=True)
class BlastRadiusSnapshot:
    severity:       str = "low"
    total_affected: int = 0
    affected:       tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Similarity score returned by SimilarityEngine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SimilarityScore:
    memory_id:       str
    overall:         float = 0.0
    breakdown:       dict[str, float] = field(default_factory=dict)
    exact_match:     bool = False
    # B1 (RC-L semantics): dimensions skipped because neither record had
    # data. Additive — existing readers ignore it; ``breakdown`` holds
    # measured dimensions only. Explains measured / not-measured /
    # ignored (ignored = absent from both fields, zero weight).
    not_measured:    tuple[str, ...] = ()
    schema_version:  int = MEMORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # RC-D: prevent mutation of the dict field through the attribute.
        # Uses object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "breakdown", freeze_dict(self.breakdown))
        object.__setattr__(self, "not_measured",
                           tuple(str(x) for x in self.not_measured))

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id":      self.memory_id,
            "overall":        round(float(self.overall), 4),
            "exact_match":    bool(self.exact_match),
            "breakdown":      {k: round(float(v), 4) for k, v in sorted(self.breakdown.items())},
            "not_measured":   list(self.not_measured),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Recurring patterns emitted by LearningLoop
# ---------------------------------------------------------------------------

class RecurringPatternKind(str, Enum):
    ROOT_CAUSE            = "root_cause"
    EVIDENCE              = "evidence"
    PLANNER_PATH          = "planner_path"
    FAILED_INVESTIGATION  = "failed_investigation"
    FALSE_LEAD            = "false_lead"
    MISSING_EVIDENCE      = "missing_evidence"
    TOPOLOGY_FAILURE      = "topology_failure"
    TRANSACTION_FAILURE   = "transaction_failure"
    DEPLOYMENT_FAILURE    = "deployment_failure"
    DEPENDENCY_FAILURE    = "dependency_failure"
    BLAST_RADIUS          = "blast_radius"
    MTTI_BOTTLENECK       = "mtti_bottleneck"
    CONFIDENCE_DROP       = "confidence_drop"


@dataclass(frozen=True)
class RecurringPattern:
    kind:            str
    signature:       str
    count:           int
    memory_ids:      tuple[str, ...] = ()
    average_mtti_ms: int = 0
    average_confidence: int = 0
    schema_version:  int = MEMORY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind":               self.kind,
            "signature":          self.signature,
            "count":              int(self.count),
            "memory_ids":         sorted(self.memory_ids),
            "average_mtti_ms":    int(self.average_mtti_ms),
            "average_confidence": int(self.average_confidence),
            "schema_version":     self.schema_version,
        }


# ---------------------------------------------------------------------------
# MemoryRecord — the canonical per-investigation memory row
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MemoryRecord:
    # --- identity ------------------------------------------------------
    memory_id:     str
    incident_id:   str = ""
    fingerprint:   str = ""
    timestamp:     str = ""             # ISO 8601 — caller-supplied
    # --- classification -----------------------------------------------
    service:       str = ""
    environment:   str = ""
    application:   str = ""
    incident_type: str = ""
    severity:      str = ""
    # --- context ------------------------------------------------------
    topology:            TopologySnapshot     = field(default_factory=TopologySnapshot)
    transaction_path:    tuple[str, ...]      = ()
    blast_radius:        BlastRadiusSnapshot  = field(default_factory=BlastRadiusSnapshot)
    # --- evidence -----------------------------------------------------
    evidence_collected:  tuple[str, ...]      = ()
    evidence_ordering:   tuple[str, ...]      = ()
    # --- planner + decision + KG (snapshots, not the full graph) ------
    planner_decisions:   tuple[str, ...]      = ()   # capability ids in plan order
    decision_trace:      dict[str, Any]       = field(default_factory=dict)
    knowledge_graph_snapshot: dict[str, Any]  = field(default_factory=dict)
    # --- outcome ------------------------------------------------------
    detected_root_cause: str                  = ""
    verified_root_cause: str                  = ""
    resolution:          str                  = ""
    false_leads:         tuple[str, ...]      = ()
    # --- metrics ------------------------------------------------------
    confidence:          int                  = 0
    mtti_ms:             int                  = 0
    mttr_ms:             int                  = 0
    runtime_cost:        int                  = 0
    # --- skills + refs -----------------------------------------------
    skills_used:         tuple[str, ...]      = ()
    receipt_references:  tuple[str, ...]      = ()
    # --- scoring ------------------------------------------------------
    investigation_score: float                = 0.0
    sentinelbench_score: float                = 0.0
    replay_history:      tuple[str, ...]      = ()
    # --- versioning ---------------------------------------------------
    schema_version:      int                  = MEMORY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        # RC-D: prevent mutation of dict fields via attribute access.
        # Uses object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "decision_trace",
                             freeze_dict(self.decision_trace))
        object.__setattr__(self, "knowledge_graph_snapshot",
                             freeze_dict(self.knowledge_graph_snapshot))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return _tuples_to_lists(d)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MemoryRecord":
        """Best-effort constructor from a raw dict."""
        topo = d.get("topology") or {}
        blast = d.get("blast_radius") or {}
        return cls(
            memory_id=str(d.get("memory_id", "") or ""),
            incident_id=str(d.get("incident_id", "") or ""),
            fingerprint=str(d.get("fingerprint", "") or ""),
            timestamp=str(d.get("timestamp", "") or ""),
            service=str(d.get("service", "") or ""),
            environment=str(d.get("environment", "") or ""),
            application=str(d.get("application", "") or ""),
            incident_type=str(d.get("incident_type", "") or ""),
            severity=str(d.get("severity", "") or ""),
            topology=TopologySnapshot(
                services=tuple(str(x) for x in (topo.get("services", []) or [])),
                namespaces=tuple(str(x) for x in (topo.get("namespaces", []) or [])),
                pods=tuple(str(x) for x in (topo.get("pods", []) or [])),
                nodes=tuple(str(x) for x in (topo.get("nodes", []) or [])),
                clusters=tuple(str(x) for x in (topo.get("clusters", []) or [])),
                regions=tuple(str(x) for x in (topo.get("regions", []) or [])),
                cloud=str(topo.get("cloud", "") or ""),
                gateway=str(topo.get("gateway", "") or ""),
                idp=str(topo.get("idp", "") or ""),
                dns=str(topo.get("dns", "") or ""),
                databases=tuple(str(x) for x in (topo.get("databases", []) or [])),
                dependencies=tuple(
                    (str(a), str(b)) for a, b in (topo.get("dependencies", []) or [])
                ),
            ),
            transaction_path=tuple(str(x) for x in (d.get("transaction_path", []) or [])),
            blast_radius=BlastRadiusSnapshot(
                severity=str(blast.get("severity", "low") or "low"),
                total_affected=int(blast.get("total_affected", 0) or 0),
                affected=tuple(str(x) for x in (blast.get("affected", []) or [])),
            ),
            evidence_collected=tuple(str(x) for x in (d.get("evidence_collected", []) or [])),
            evidence_ordering=tuple(str(x) for x in (d.get("evidence_ordering", []) or [])),
            planner_decisions=tuple(str(x) for x in (d.get("planner_decisions", []) or [])),
            decision_trace=dict(d.get("decision_trace", {}) or {}),
            knowledge_graph_snapshot=dict(d.get("knowledge_graph_snapshot", {}) or {}),
            detected_root_cause=str(d.get("detected_root_cause", "") or ""),
            verified_root_cause=str(d.get("verified_root_cause", "") or ""),
            resolution=str(d.get("resolution", "") or ""),
            false_leads=tuple(str(x) for x in (d.get("false_leads", []) or [])),
            confidence=int(d.get("confidence", 0) or 0),
            mtti_ms=int(d.get("mtti_ms", 0) or 0),
            mttr_ms=int(d.get("mttr_ms", 0) or 0),
            runtime_cost=int(d.get("runtime_cost", 0) or 0),
            skills_used=tuple(str(x) for x in (d.get("skills_used", []) or [])),
            receipt_references=tuple(str(x) for x in (d.get("receipt_references", []) or [])),
            investigation_score=float(d.get("investigation_score", 0.0) or 0.0),
            sentinelbench_score=float(d.get("sentinelbench_score", 0.0) or 0.0),
            replay_history=tuple(str(x) for x in (d.get("replay_history", []) or [])),
            # RC-I: preserve the caller-supplied schema_version so any
            # v>1 payload does not silently downgrade to v1 on round-trip.
            schema_version=int(d.get("schema_version", MEMORY_SCHEMA_VERSION)
                                 or MEMORY_SCHEMA_VERSION),
        )

    @classmethod
    def from_artifact(cls, artifact: Any) -> "MemoryRecord":
        """Wave 2: pure projection from an InvestigationArtifact.

        Duck-typed on the artifact's attributes — no import of the
        artifact package, so ``intel_memory`` gains no new dependency.
        Deterministic: identical artifacts project to byte-identical
        records. ``memory_id`` is the artifact's content-addressed id,
        so the projection is idempotent by construction.

        Summaries and pointers only — the artifact itself never carries
        evidence values, so neither can the record.
        """
        from sentinel_core.intel_memory.fingerprint import (
            FingerprintInput, compute_fingerprint,
        )

        def _d(name: str) -> dict:
            v = getattr(artifact, name, None)
            return dict(v) if isinstance(v, dict) else {}

        decision = _d("decision_summary")
        evidence = _d("evidence_key_summary")
        planner  = _d("planner_trace_summary")
        workers  = _d("worker_execution_summary")
        summary  = _d("final_result_summary")

        # Identity: prefer the artifact's B2 enrichment fields; fall back
        # to the as-was DecisionContext snapshot (pre-enrichment artifacts).
        service = str(getattr(artifact, "service", "") or "") \
            or str(decision.get("service", "") or "")
        incident_type = str(getattr(artifact, "incident_type", "") or "") \
            or str(decision.get("incident_type", "") or "")
        ev_keys = tuple(str(k) for k in (evidence.get("keys") or ()))
        mtti_ms = sum(
            int(p.get("elapsed_ms", 0) or 0)
            for p in workers.values() if isinstance(p, dict)
        )
        score = summary.get("online_quality_score", 0.0)
        return cls(
            memory_id=str(getattr(artifact, "artifact_id", "") or ""),
            incident_id=str(getattr(artifact, "incident_id", "") or ""),
            fingerprint=compute_fingerprint(FingerprintInput(
                service=service,
                incident_type=incident_type,
                evidence_pattern=ev_keys,
            )),
            timestamp=str(getattr(artifact, "created_at", "") or ""),
            service=service,
            incident_type=incident_type,
            environment=str(getattr(artifact, "environment", "") or ""),
            application=str(getattr(artifact, "application", "") or ""),
            severity=str(getattr(artifact, "severity", "") or ""),
            evidence_collected=ev_keys,
            # B3 projection honesty: the artifact's evidence snapshot is
            # UNORDERED (sorted keys). Never fabricate collection order —
            # truthful empty beats misleading alphabetical sequence.
            evidence_ordering=(),
            planner_decisions=tuple(
                str(s) for s in (planner.get("steps") or ())
            ),
            # Provenance preservation: the artifact's identity, origin
            # and admission snapshot travel on the decision_trace bag.
            decision_trace={
                "artifact_id": str(getattr(artifact, "artifact_id", "")),
                "artifact_status": str(getattr(artifact, "status", "")),
                "provenance": dict(getattr(artifact, "provenance", {}) or {}),
                "decision_summary": decision,
                "receipt_hashes": [
                    str(h) for h in getattr(artifact, "receipt_hashes", ())
                ],
            },
            detected_root_cause=str(getattr(artifact, "root_cause", "") or ""),
            resolution=str(getattr(artifact, "resolution", "") or ""),
            false_leads=tuple(
                str(x) for x in getattr(artifact, "false_leads", ()) or ()
            ),
            confidence=max(0, min(100,
                                   int(getattr(artifact, "confidence", 0) or 0))),
            mtti_ms=mtti_ms,
            runtime_cost=int(getattr(artifact, "runtime_cost", 0) or 0),
            receipt_references=tuple(
                str(h) for h in getattr(artifact, "receipt_hashes", ())
            ),
            investigation_score=float(score if isinstance(
                score, (int, float)) else 0.0),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tuples_to_lists(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj


__all__ = [
    "MEMORY_SCHEMA_VERSION",
    "MemoryRecord",
    "TopologySnapshot",
    "BlastRadiusSnapshot",
    "SimilarityScore",
    "RecurringPattern",
    "RecurringPatternKind",
]
