"""CausalGraphBuilder — build a CausalGraph from MemoryRecord corpus."""
from __future__ import annotations

from typing import Iterable

from sentinel_core.causal_graph.causal_edge import CausalEdge, CausalEdgeType
from sentinel_core.causal_graph.causal_node import CausalNode, CausalNodeType
from sentinel_core.causal_graph.schemas import CausalGraph
from sentinel_core.intel_memory import MemoryRecord


class CausalGraphBuilder:
    """Deterministic builder over a MemoryRecord corpus.

    Never mutates the input. Same corpus → byte-identical graph.
    """

    def build(self, records: Iterable[MemoryRecord]) -> CausalGraph:
        nodes: dict[str, CausalNode] = {}
        edges: dict[str, CausalEdge] = {}

        def add_node(n: CausalNode) -> str:
            if n.node_id not in nodes:
                nodes[n.node_id] = n
            return n.node_id

        def add_edge(e: CausalEdge) -> None:
            if e.edge_id not in edges:
                edges[e.edge_id] = e

        for r in records or ():
            svc_id = None
            if r.service:
                svc_id = add_node(CausalNode.make(
                    CausalNodeType.SERVICE, r.service,
                    {"application": r.application},
                ))
            inc_id = None
            if r.incident_id:
                inc_id = add_node(CausalNode.make(
                    CausalNodeType.INCIDENT, r.incident_id,
                    {"memory_id": r.memory_id, "incident_type": r.incident_type},
                ))
                if svc_id:
                    add_edge(CausalEdge.make(
                        inc_id, svc_id, CausalEdgeType.AFFECTS,
                    ))
            # Failure mode / incident type
            if r.incident_type:
                fm = add_node(CausalNode.make(
                    CausalNodeType.FAILURE_MODE, r.incident_type,
                ))
                if inc_id:
                    add_edge(CausalEdge.make(inc_id, fm,
                                              CausalEdgeType.CORRELATES_WITH))
            # Root cause
            rc_id = None
            if r.detected_root_cause:
                rc_id = add_node(CausalNode.make(
                    CausalNodeType.ROOT_CAUSE,
                    r.detected_root_cause[:120].strip(),
                    {"confidence": r.confidence},
                ))
                if inc_id:
                    add_edge(CausalEdge.make(
                        inc_id, rc_id, CausalEdgeType.CAUSED_BY,
                        weight=float(r.confidence or 0) / 100.0,
                    ))
            # Remediation
            if r.resolution:
                rm_id = add_node(CausalNode.make(
                    CausalNodeType.REMEDIATION, r.resolution[:120].strip(),
                ))
                if rc_id:
                    add_edge(CausalEdge.make(rc_id, rm_id,
                                              CausalEdgeType.RESOLVED_BY))
                if inc_id:
                    add_edge(CausalEdge.make(inc_id, rm_id,
                                              CausalEdgeType.RESOLVED_BY))
            # Evidence
            for e in r.evidence_collected:
                ev_id = add_node(CausalNode.make(CausalNodeType.EVIDENCE, e))
                if inc_id:
                    add_edge(CausalEdge.make(
                        ev_id, inc_id, CausalEdgeType.OBSERVED_IN,
                    ))
                if rc_id:
                    add_edge(CausalEdge.make(
                        ev_id, rc_id, CausalEdgeType.SUPPORTS,
                    ))
            # False leads
            for fl in r.false_leads:
                sig_id = add_node(CausalNode.make(
                    CausalNodeType.SIGNAL, fl,
                ))
                if rc_id:
                    add_edge(CausalEdge.make(
                        sig_id, rc_id, CausalEdgeType.DISPROVES,
                    ))
            # Deployment change (skills_used hint)
            for skill in r.skills_used:
                if skill.startswith("git_") or skill.startswith("argocd") \
                        or skill.startswith("deploy"):
                    dc_id = add_node(CausalNode.make(
                        CausalNodeType.DEPLOYMENT_CHANGE, skill,
                    ))
                    if inc_id:
                        add_edge(CausalEdge.make(
                            dc_id, inc_id, CausalEdgeType.PRECEDES,
                        ))
                    if rc_id:
                        add_edge(CausalEdge.make(
                            dc_id, rc_id, CausalEdgeType.CAUSED_BY,
                        ))
            # Dependencies (topology-carried)
            for a, b in r.topology.dependencies:
                da = add_node(CausalNode.make(
                    CausalNodeType.DEPENDENCY, f"{a}->{b}",
                ))
                if svc_id:
                    add_edge(CausalEdge.make(
                        svc_id, da, CausalEdgeType.DEPENDS_ON,
                    ))
                if inc_id:
                    add_edge(CausalEdge.make(
                        da, inc_id, CausalEdgeType.CORRELATES_WITH,
                    ))
            # Hypotheses (from decision_trace if present)
            hypotheses = (r.decision_trace or {}).get("hypotheses", [])
            for h in hypotheses:
                if isinstance(h, dict):
                    name = str(h.get("name") or h.get("hypothesis") or "")
                else:
                    name = str(h)
                if name:
                    hy_id = add_node(CausalNode.make(
                        CausalNodeType.HYPOTHESIS, name,
                    ))
                    if inc_id:
                        add_edge(CausalEdge.make(
                            hy_id, inc_id, CausalEdgeType.OBSERVED_IN,
                        ))
                    if rc_id:
                        add_edge(CausalEdge.make(
                            hy_id, rc_id, CausalEdgeType.SUPPORTS,
                        ))

        # Cross-incident RECURS_WITH — incidents sharing the same root cause
        rc_to_incidents: dict[str, list[str]] = {}
        inc_type = CausalNodeType.INCIDENT.value
        for n in nodes.values():
            if n.node_type != inc_type:
                continue
            for e in list(edges.values()):
                if e.source_id == n.node_id and e.edge_type == CausalEdgeType.CAUSED_BY.value:
                    rc_to_incidents.setdefault(e.target_id, []).append(n.node_id)
        for rc, incs in rc_to_incidents.items():
            if len(incs) < 2:
                continue
            for i in range(len(incs)):
                for j in range(i + 1, len(incs)):
                    add_edge(CausalEdge.make(
                        incs[i], incs[j], CausalEdgeType.RECURS_WITH,
                    ))

        return CausalGraph(
            nodes=tuple(sorted(nodes.values(), key=lambda n: n.node_id)),
            edges=tuple(sorted(edges.values(), key=lambda e: e.edge_id)),
        )


__all__ = ["CausalGraphBuilder"]
