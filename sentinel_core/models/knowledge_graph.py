"""Enterprise Knowledge Graph — canonical runtime object model.

Sentinel's first entity-centric operational graph. It is NOT a
visualization, NOT a graph database, NOT a persistence layer. It is a
pure runtime canonical model built each investigation from the
accumulated intelligence corpus we already have.

Node types cover the enterprise entity taxonomy (services,
applications, incidents, patterns, hosts, clusters, cloud resources,
service owners, runbooks, dashboards, alerts, transactions, external
dependencies, etc.). Edge types cover the operational relationships
between them (Supports, DependsOn, Calls, HostedOn, ObservedBy,
AffectedBy, RelatedIncident, HistoricalFailure, KnownPattern,
KnownBlastRadius, etc.).

Design principles
-----------------
- **Additive only**: this file introduces nothing that touches existing
  storage, existing schemas, or existing modules. It is a new canonical
  view over data we already have.
- **Immutable models**: frozen dataclasses; safe across boundaries.
- **Deterministic serialization**: nodes sorted by ``node_id``, edges
  sorted by ``edge_id`` in ``to_dict()`` output. Same intelligence
  input → byte-identical graph output.
- **No graph database**: this is an in-memory graph. Persistence, if any,
  is left to the runtime module that constructs it (see
  ``supervisor/intelligence_modules/enterprise_knowledge_graph.py``).
- **Reuse only**: every node/edge is derived from an existing source —
  IntelligenceContext (which itself reads RM + IS + patterns + graphs +
  episodes + causal). No new collector, no new persistence.

Extension policy
----------------
Adding a new node or edge type only requires appending to the ``NodeType``
or ``EdgeType`` enum. No existing consumers break because both enums
inherit from ``str`` — unknown strings deserialise back to their raw
form.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable


SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

class NodeType(str, Enum):
    """Enterprise entity taxonomy. Str-valued for stable serialization."""
    # Runtime / compute
    APPLICATION       = "application"
    SERVICE           = "service"
    NAMESPACE         = "namespace"
    POD               = "pod"
    NODE              = "node"
    CLUSTER           = "cluster"
    HOST              = "host"
    VM                = "vm"
    # Data / connectivity
    DATABASE          = "database"
    GATEWAY           = "gateway"
    LOAD_BALANCER     = "load_balancer"
    API               = "api"
    KAFKA             = "kafka"
    QUEUE             = "queue"
    DNS               = "dns"
    CERTIFICATE       = "certificate"
    # Cloud
    CLOUD_RESOURCE    = "cloud_resource"
    AWS_ACCOUNT       = "aws_account"
    AZURE_SUBSCRIPTION = "azure_subscription"
    # People / ownership
    SERVICE_OWNER     = "service_owner"
    SUPPORT_TEAM      = "support_team"
    BUSINESS_SERVICE  = "business_service"
    # Operational
    INCIDENT          = "incident"
    CHANGE            = "change"
    DEPLOYMENT        = "deployment"
    RUNBOOK           = "runbook"
    DASHBOARD         = "dashboard"
    ALERT             = "alert"
    TRANSACTION       = "transaction"
    EXTERNAL_DEPENDENCY = "external_dependency"
    # Historical / intelligence-derived
    PATTERN           = "pattern"


class EdgeType(str, Enum):
    """Operational relationships between enterprise entities."""
    SUPPORTS          = "supports"
    DEPENDS_ON        = "depends_on"
    CALLS             = "calls"
    OWNS              = "owns"
    HOSTED_ON         = "hosted_on"
    RUNS_IN           = "runs_in"
    CONNECTED_TO      = "connected_to"
    PROTECTED_BY      = "protected_by"
    OBSERVED_BY       = "observed_by"
    AFFECTED_BY       = "affected_by"
    CHANGED_BY        = "changed_by"
    DEPLOYS_TO        = "deploys_to"
    RESOLVES          = "resolves"
    CONSUMES          = "consumes"
    PRODUCES          = "produces"
    RELATED_INCIDENT  = "related_incident"
    HISTORICAL_FAILURE = "historical_failure"
    KNOWN_PATTERN     = "known_pattern"
    KNOWN_BLAST_RADIUS = "known_blast_radius"


# ---------------------------------------------------------------------------
# Node / edge
# ---------------------------------------------------------------------------

def _node_id(node_type: NodeType | str, label: str) -> str:
    """Deterministic node id — sha256[:16] of (type, label)."""
    raw = f"{str(node_type)}:{label}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _edge_id(source_id: str, target_id: str, edge_type: EdgeType | str) -> str:
    raw = f"{source_id}:{target_id}:{str(edge_type)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class KnowledgeNode:
    """One enterprise entity in the runtime knowledge graph."""
    node_id:    str
    node_type:  str        # value of NodeType — str-typed so unknown types survive
    label:      str
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        node_type: NodeType | str,
        label: str,
        properties: dict[str, Any] | None = None,
    ) -> "KnowledgeNode":
        return cls(
            node_id=_node_id(node_type, label),
            node_type=str(node_type.value if isinstance(node_type, NodeType) else node_type),
            label=str(label),
            properties=dict(properties or {}),
        )


@dataclass(frozen=True)
class KnowledgeEdge:
    """Typed directed relationship between two knowledge nodes."""
    edge_id:    str
    source_id:  str
    target_id:  str
    edge_type:  str        # value of EdgeType — str-typed
    weight:     float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make(
        cls,
        source_id: str,
        target_id: str,
        edge_type: EdgeType | str,
        weight: float = 1.0,
        properties: dict[str, Any] | None = None,
    ) -> "KnowledgeEdge":
        return cls(
            edge_id=_edge_id(source_id, target_id, edge_type),
            source_id=source_id,
            target_id=target_id,
            edge_type=str(edge_type.value if isinstance(edge_type, EdgeType) else edge_type),
            weight=float(weight),
            properties=dict(properties or {}),
        )


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KnowledgeGraph:
    """In-memory typed graph. Immutable; construct via the builder."""
    nodes: tuple[KnowledgeNode, ...] = ()
    edges: tuple[KnowledgeEdge, ...] = ()
    schema_version: int = SCHEMA_VERSION

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def node_count(self) -> int:
        return len(self.nodes)

    def edge_count(self) -> int:
        return len(self.edges)

    def nodes_by_type(self, node_type: NodeType | str) -> tuple[KnowledgeNode, ...]:
        t = str(node_type.value if isinstance(node_type, NodeType) else node_type)
        return tuple(n for n in self.nodes if n.node_type == t)

    def edges_by_type(self, edge_type: EdgeType | str) -> tuple[KnowledgeEdge, ...]:
        t = str(edge_type.value if isinstance(edge_type, EdgeType) else edge_type)
        return tuple(e for e in self.edges if e.edge_type == t)

    def find_node(self, node_id: str) -> KnowledgeNode | None:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    # ------------------------------------------------------------------
    # Serialization — deterministic
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict. Nodes sorted by node_id; edges by edge_id.
        Same input graph → byte-identical dict."""
        nodes_sorted = sorted(self.nodes, key=lambda n: n.node_id)
        edges_sorted = sorted(self.edges, key=lambda e: e.edge_id)
        return {
            "schema_version": self.schema_version,
            "node_count":     len(nodes_sorted),
            "edge_count":     len(edges_sorted),
            "nodes":          [asdict(n) for n in nodes_sorted],
            "edges":          [asdict(e) for e in edges_sorted],
        }


# ---------------------------------------------------------------------------
# Builder — pure library factory
# ---------------------------------------------------------------------------

class KnowledgeGraphBuilder:
    """Deterministic KG construction from existing intelligence.

    All builders are static / pure. The builder holds no state. Same
    inputs → identical graph output.
    """

    @staticmethod
    def from_intelligence_context(
        ic,
        *,
        incident_id: str = "",
        service: str = "",
        incident_type: str = "",
        root_cause: str = "",
        remediation_action: str = "",
    ) -> KnowledgeGraph:
        """Construct a KnowledgeGraph from an IntelligenceContext.

        ``ic`` may be an IntelligenceContext or a dict-like duck. Every
        source is optional. Missing sources yield empty edge sets, not
        exceptions.
        """
        nodes_by_id: dict[str, KnowledgeNode] = {}
        edges_by_id: dict[str, KnowledgeEdge] = {}

        def _add_node(n: KnowledgeNode) -> str:
            existing = nodes_by_id.get(n.node_id)
            if existing is None:
                nodes_by_id[n.node_id] = n
            return n.node_id

        def _add_edge(e: KnowledgeEdge) -> None:
            if e.edge_id not in edges_by_id:
                edges_by_id[e.edge_id] = e

        service = str(service or _getattr_safe(ic, "service", "") or "")
        incident_type = str(incident_type or _getattr_safe(ic, "incident_type", "") or "")

        # -----------------------------------------------------------
        # Central service + incident
        # -----------------------------------------------------------
        service_node_id: str | None = None
        if service:
            service_node = KnowledgeNode.make(
                NodeType.SERVICE,
                service,
                properties=KnowledgeGraphBuilder._service_properties(
                    ic, service, incident_type, root_cause,
                ),
            )
            service_node_id = _add_node(service_node)

        incident_node_id: str | None = None
        if incident_id:
            incident_node = KnowledgeNode.make(
                NodeType.INCIDENT,
                incident_id,
                properties={
                    "incident_type": incident_type,
                    "root_cause":    (root_cause or "")[:200],
                    "remediation":   (remediation_action or "")[:200],
                    "service":       service,
                },
            )
            incident_node_id = _add_node(incident_node)

        if service_node_id and incident_node_id:
            _add_edge(KnowledgeEdge.make(
                source_id=incident_node_id,
                target_id=service_node_id,
                edge_type=EdgeType.AFFECTED_BY,
                properties={"role": "primary_service"},
            ))

        # -----------------------------------------------------------
        # ResolutionMemory matches → historical incidents
        # -----------------------------------------------------------
        for m in _as_tuple(_getattr_safe(ic, "resolution_memory_matches", ())):
            mem_id = str(_getattr_safe(m, "memory_id", "") or "")
            if not mem_id:
                continue
            hist_node = KnowledgeNode.make(
                NodeType.INCIDENT,
                label=f"resolution_memory:{mem_id}",
                properties={
                    "source":         "resolution_memory",
                    "memory_id":      mem_id,
                    "confidence":     int(_getattr_safe(m, "confidence", 0) or 0),
                    "recorded_at":    str(_getattr_safe(m, "recorded_at", "") or ""),
                    "root_cause_head": str(_getattr_safe(m, "root_cause_head", "") or "")[:200],
                    "service":        str(_getattr_safe(m, "service", "") or ""),
                    "incident_type":  str(_getattr_safe(m, "incident_type", "") or ""),
                },
            )
            hid = _add_node(hist_node)
            if incident_node_id:
                _add_edge(KnowledgeEdge.make(
                    source_id=incident_node_id, target_id=hid,
                    edge_type=EdgeType.HISTORICAL_FAILURE,
                    weight=float(int(_getattr_safe(m, "confidence", 0) or 0)) / 100.0,
                ))

        # -----------------------------------------------------------
        # Investigation matches → prior incidents on this service
        # -----------------------------------------------------------
        for m in _as_tuple(_getattr_safe(ic, "investigation_matches", ())):
            iid = str(_getattr_safe(m, "investigation_id", "") or "")
            if not iid:
                continue
            n = KnowledgeNode.make(
                NodeType.INCIDENT,
                label=f"investigation:{iid}",
                properties={
                    "source":           "investigation_store",
                    "investigation_id": iid,
                    "created_at":       str(_getattr_safe(m, "created_at", "") or ""),
                    "incident_type":    str(_getattr_safe(m, "incident_type", "") or ""),
                    "service":          str(_getattr_safe(m, "service", "") or ""),
                },
            )
            nid = _add_node(n)
            if incident_node_id:
                _add_edge(KnowledgeEdge.make(
                    source_id=incident_node_id, target_id=nid,
                    edge_type=EdgeType.HISTORICAL_FAILURE,
                ))

        # -----------------------------------------------------------
        # Pattern matches → KNOWN_PATTERN
        # -----------------------------------------------------------
        for p in _as_tuple(_getattr_safe(ic, "pattern_matches", ())):
            pid = str(_getattr_safe(p, "pattern_id", "") or "")
            if not pid:
                continue
            n = KnowledgeNode.make(
                NodeType.PATTERN,
                label=f"pattern:{pid}",
                properties={
                    "pattern_id":       pid,
                    "incident_type":    str(_getattr_safe(p, "incident_type", "") or ""),
                    "services":         list(_getattr_safe(p, "services", []) or []),
                    "canonical_symptoms": list(_getattr_safe(p, "canonical_symptoms", []) or []),
                    "occurrence_count": int(_getattr_safe(p, "occurrence_count", 0) or 0),
                    "success_count":    int(_getattr_safe(p, "success_count", 0) or 0),
                    "success_rate":     float(_getattr_safe(p, "success_rate", 0.0) or 0.0),
                    "last_seen":        str(_getattr_safe(p, "last_seen", "") or ""),
                },
            )
            nid = _add_node(n)
            if service_node_id:
                _add_edge(KnowledgeEdge.make(
                    source_id=service_node_id, target_id=nid,
                    edge_type=EdgeType.KNOWN_PATTERN,
                    weight=float(_getattr_safe(p, "success_rate", 0.0) or 0.0),
                ))

        # -----------------------------------------------------------
        # Related incidents (from IncidentGraph)
        # -----------------------------------------------------------
        for rid in _as_tuple(_getattr_safe(ic, "related_incident_ids", ())):
            rid_s = str(rid or "")
            if not rid_s:
                continue
            n = KnowledgeNode.make(
                NodeType.INCIDENT,
                label=f"related:{rid_s}",
                properties={"source": "incident_graph", "incident_id": rid_s},
            )
            nid = _add_node(n)
            if incident_node_id:
                _add_edge(KnowledgeEdge.make(
                    source_id=incident_node_id, target_id=nid,
                    edge_type=EdgeType.RELATED_INCIDENT,
                ))

        # -----------------------------------------------------------
        # Upstream / downstream dependencies
        # -----------------------------------------------------------
        for e in _as_tuple(_getattr_safe(ic, "upstream_dependencies", ())):
            target = str(_getattr_safe(e, "target_service", "") or "")
            if not target or not service_node_id:
                continue
            tgt = KnowledgeNode.make(NodeType.SERVICE, target,
                                        properties={"role": "upstream"})
            tid = _add_node(tgt)
            _add_edge(KnowledgeEdge.make(
                source_id=service_node_id, target_id=tid,
                edge_type=EdgeType.DEPENDS_ON,
                weight=float(_getattr_safe(e, "strength", 0.0) or 0.0),
                properties={
                    "dep_type":       str(_getattr_safe(e, "dep_type", "") or ""),
                    "observed_count": int(_getattr_safe(e, "observed_count", 0) or 0),
                },
            ))

        for e in _as_tuple(_getattr_safe(ic, "downstream_dependents", ())):
            source = str(_getattr_safe(e, "source_service", "") or "")
            if not source or not service_node_id:
                continue
            src = KnowledgeNode.make(NodeType.SERVICE, source,
                                        properties={"role": "downstream"})
            sid = _add_node(src)
            _add_edge(KnowledgeEdge.make(
                source_id=sid, target_id=service_node_id,
                edge_type=EdgeType.DEPENDS_ON,
                weight=float(_getattr_safe(e, "strength", 0.0) or 0.0),
                properties={
                    "dep_type":       str(_getattr_safe(e, "dep_type", "") or ""),
                    "observed_count": int(_getattr_safe(e, "observed_count", 0) or 0),
                },
            ))

        # -----------------------------------------------------------
        # Blast radius (causal graph)
        # -----------------------------------------------------------
        for a in _as_tuple(_getattr_safe(ic, "blast_radius_affected", ())):
            svc = str(_getattr_safe(a, "service_id", "") or "")
            if not svc or not service_node_id:
                continue
            tgt = KnowledgeNode.make(NodeType.SERVICE, svc,
                                        properties={"role": "blast_radius"})
            tid = _add_node(tgt)
            _add_edge(KnowledgeEdge.make(
                source_id=service_node_id, target_id=tid,
                edge_type=EdgeType.KNOWN_BLAST_RADIUS,
                weight=float(_getattr_safe(a, "probability", 0.0) or 0.0),
                properties={
                    "propagation_ms": int(_getattr_safe(a, "propagation_ms", 0) or 0),
                },
            ))

        # -----------------------------------------------------------
        # Episodes → historical incidents on this service
        # -----------------------------------------------------------
        for ep in _as_tuple(_getattr_safe(ic, "episode_matches", ())):
            eid = str(_getattr_safe(ep, "episode_id", "") or "")
            if not eid:
                continue
            n = KnowledgeNode.make(
                NodeType.INCIDENT,
                label=f"episode:{eid}",
                properties={
                    "source":                 "episodic_memory",
                    "episode_id":             eid,
                    "incident_id":            str(_getattr_safe(ep, "incident_id", "") or ""),
                    "service":                str(_getattr_safe(ep, "service", "") or ""),
                    "incident_type":          str(_getattr_safe(ep, "incident_type", "") or ""),
                    "root_cause_head":        str(_getattr_safe(ep, "root_cause_head", "") or "")[:200],
                    "resolution_action_head": str(_getattr_safe(ep, "resolution_action_head", "") or "")[:200],
                    "outcome":                str(_getattr_safe(ep, "outcome", "") or ""),
                    "confidence":             float(_getattr_safe(ep, "confidence", 0.0) or 0.0),
                    "recorded_at":            str(_getattr_safe(ep, "recorded_at", "") or ""),
                },
            )
            nid = _add_node(n)
            if service_node_id:
                _add_edge(KnowledgeEdge.make(
                    source_id=nid, target_id=service_node_id,
                    edge_type=EdgeType.AFFECTED_BY,
                    properties={"role": "prior_episode"},
                ))

        # -----------------------------------------------------------
        # Return deterministic, sorted graph
        # -----------------------------------------------------------
        return KnowledgeGraph(
            nodes=tuple(sorted(nodes_by_id.values(), key=lambda n: n.node_id)),
            edges=tuple(sorted(edges_by_id.values(), key=lambda e: e.edge_id)),
        )

    # ------------------------------------------------------------------
    # Central-service property derivation — the "known-X" bag
    # ------------------------------------------------------------------

    @staticmethod
    def _service_properties(
        ic,
        service: str,
        incident_type: str,
        root_cause: str,
    ) -> dict[str, Any]:
        rm = _as_tuple(_getattr_safe(ic, "resolution_memory_matches", ()))
        inv = _as_tuple(_getattr_safe(ic, "investigation_matches", ()))
        patterns = _as_tuple(_getattr_safe(ic, "pattern_matches", ()))
        related = _as_tuple(_getattr_safe(ic, "related_incident_ids", ()))
        upstream = _as_tuple(_getattr_safe(ic, "upstream_dependencies", ()))
        downstream = _as_tuple(_getattr_safe(ic, "downstream_dependents", ()))
        episodes = _as_tuple(_getattr_safe(ic, "episode_matches", ()))
        module_names = _as_tuple(_getattr_safe(ic, "module_names_seen", ()))
        blast_severity = str(_getattr_safe(ic, "blast_radius_severity", "low") or "low")
        blast_total = _as_int(_getattr_safe(ic, "blast_radius_total_affected", 0))

        historical_failures = len(rm) + len(inv) + len(episodes)

        known_incident_types = sorted({
            *(str(_getattr_safe(p, "incident_type", "") or "") for p in patterns),
            *(str(_getattr_safe(m, "incident_type", "") or "") for m in rm),
            *(str(_getattr_safe(e, "incident_type", "") or "") for e in episodes),
        } - {""})

        known_rca = ""
        top_conf = -1
        for m in rm:
            c = int(_getattr_safe(m, "confidence", 0) or 0)
            if c > top_conf:
                top_conf = c
                known_rca = str(_getattr_safe(m, "root_cause_head", "") or "")

        known_fixes = sorted({
            str(_getattr_safe(ep, "resolution_action_head", "") or "")
            for ep in episodes
            if _getattr_safe(ep, "resolution_action_head", "")
        })

        upstream_ids   = [str(_getattr_safe(e, "target_service", "") or "") for e in upstream]
        downstream_ids = [str(_getattr_safe(e, "source_service", "") or "") for e in downstream]

        # Health score: start at 100; penalise for blast + recurrence
        health = 100
        health -= {"critical": 50, "high": 30, "medium": 15, "low": 0}.get(blast_severity, 0)
        recurring = any(int(_getattr_safe(p, "occurrence_count", 0) or 0) >= 2 for p in patterns)
        if recurring:
            health -= 10
        health = max(0, min(100, health))

        top_confidence = 0
        for m in rm:
            v = int(_getattr_safe(m, "confidence", 0) or 0)
            if v > top_confidence:
                top_confidence = v

        return {
            "historical_failures":  historical_failures,
            "known_incident_types": known_incident_types,
            "known_rca":            known_rca[:200] if known_rca else "",
            "known_fixes":          [f[:200] for f in known_fixes],
            "owners":               [],   # not in intelligence corpus yet
            "runbooks":             [],
            "deployments":          [],
            "changes":              [],
            "blast_radius":         {
                "severity":       blast_severity,
                "total_affected": blast_total,
            },
            "downstream":           downstream_ids,
            "upstream":             upstream_ids,
            "transaction_paths":    [],   # future — Transaction Intelligence
            "confidence":           top_confidence,
            "health_score":         health,
            "recurrence":           bool(recurring),
            "evidence_sources":     list(module_names),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _getattr_safe(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_tuple(value: Any) -> tuple:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, (list, set)):
        return tuple(value)
    if isinstance(value, (str, bytes)):
        return ()
    try:
        return tuple(value)
    except Exception:
        return ()


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "SCHEMA_VERSION",
    "NodeType",
    "EdgeType",
    "KnowledgeNode",
    "KnowledgeEdge",
    "KnowledgeGraph",
    "KnowledgeGraphBuilder",
]
