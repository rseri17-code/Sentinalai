"""Tranche 3 — Enterprise causal reasoning & topology-aware localization.

Turns the hypothesis-driven investigator (Tranche 1) into a causal one,
in shadow: it builds an investigation-scoped causal graph from the
topology evidence already collected, anchors every hypothesis to graph
nodes, ranks competing CAUSAL CHAINS (not just hypotheses), eliminates
chains that violate enterprise topology or temporal causality, and
localizes the failure into root / immediate / trigger / symptom /
collateral instead of one flat explanation.

Not topology visualization. Not Wave 3. Not Transaction Intelligence.
A pure investigation-engine enhancement — additive, shadow, flag-gated.

Reuses (Phase 0 audit — nothing new invented for graph machinery):
  sentinel_core.causal_graph  CausalNodeType / CausalEdgeType /
                              make_node_id / make_edge_id / CausalNode /
                              CausalEdge / CausalGraph / CausalChain
  evidence['cmdb_blast_radius']  dependency_graph + change blast radius
  evidence['trace_correlation']  call_chain (transaction path)
  Tranche 2 temporal-causality primitives (extended here)

SHADOW CONTRACT: writes only ``result['_causal_investigation']``. Never
touches root_cause / confidence / evidence routing. Flag OFF (default)
⇒ result untouched. Deterministic: sorted iteration, no clock, no
randomness. Never raises past its own boundary.

Flag: CAUSAL_INVESTIGATION_ENABLED (default OFF).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping

from supervisor.adaptive_investigation import _extract_timestamps
from supervisor.hypothesis_engine import _tokens

logger = logging.getLogger("sentinalai.causal_investigation")

# Blast-radius mismatch beyond this fraction lowers chain confidence.
BLAST_MISMATCH_THRESHOLD = 0.5
BLAST_MISMATCH_PENALTY = 15
MAX_CHAIN_LEN = 8


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


# ---------------------------------------------------------------------------
# Evidence extraction (defensive — shapes vary by connector)
# ---------------------------------------------------------------------------

def _dependency_graph(evidence: Mapping[str, Any]) -> dict[str, list[str]]:
    """Adjacency ``dep_of -> [dependencies]`` from CMDB blast radius."""
    cmdb = evidence.get("cmdb_blast_radius")
    if not isinstance(cmdb, dict):
        return {}
    dg = cmdb.get("dependency_graph")
    if not isinstance(dg, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k in sorted(dg):
        deps = dg[k]
        if isinstance(deps, (list, tuple)):
            out[_norm(k)] = sorted({_norm(d) for d in deps if _norm(d)})
    return out


def _call_chain(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    tc = evidence.get("trace_correlation")
    if not isinstance(tc, dict):
        return []
    chain = tc.get("call_chain")
    return chain if isinstance(chain, list) else []


def _blast_affected(evidence: Mapping[str, Any]) -> set[str]:
    """Services observed to be affected (blast keys + cross-service)."""
    out: set[str] = set()
    cmdb = evidence.get("cmdb_blast_radius")
    if isinstance(cmdb, dict):
        br = cmdb.get("blast_radius")
        if isinstance(br, dict):
            out |= {_norm(k) for k in br}
    tc = evidence.get("trace_correlation")
    if isinstance(tc, dict):
        for s in tc.get("cross_service_impact") or ():
            out.add(_norm(s))
    return {s for s in out if s}


# ---------------------------------------------------------------------------
# Phase 1 — Investigation-scoped causal graph
# ---------------------------------------------------------------------------

def build_causal_graph(
    symptom_service: str,
    evidence: Mapping[str, Any],
) -> Any:
    """Construct a CausalGraph from this investigation's topology evidence.

    Nodes: SYMPTOM (the incident), SERVICE (every service seen), and
    DEPLOYMENT_CHANGE (changes in the blast radius). Edges: DEPENDS_ON
    (from the dependency graph), PRECEDES (call-chain ordering), AFFECTS
    (change -> service), OBSERVED_IN (symptom -> service).
    """
    from sentinel_core.causal_graph import (
        CausalEdge, CausalEdgeType, CausalGraph, CausalNode,
        CausalNodeType, make_edge_id, make_node_id,
    )

    sym = _norm(symptom_service)
    services: set[str] = {sym} if sym else set()
    dg = _dependency_graph(evidence)
    for k, deps in dg.items():
        services.add(k)
        services.update(deps)
    chain = _call_chain(evidence)
    chain_services = [_norm(s.get("service")) for s in chain
                      if _norm(s.get("service"))]
    services.update(chain_services)
    services.update(_blast_affected(evidence))
    services = {s for s in services if s}

    nodes: dict[str, CausalNode] = {}
    node_id: dict[str, str] = {}
    for svc in sorted(services):
        nid = make_node_id(CausalNodeType.SERVICE, svc)
        node_id[svc] = nid
        nodes[nid] = CausalNode(
            node_id=nid, node_type=CausalNodeType.SERVICE.value,
            label=svc, properties={"is_symptom": svc == sym})

    edges: dict[str, CausalEdge] = {}

    def _edge(src_id, tgt_id, etype, **props):
        eid = make_edge_id(src_id, tgt_id, etype)
        edges[eid] = CausalEdge(
            edge_id=eid, source_id=src_id, target_id=tgt_id,
            edge_type=etype.value, weight=1.0, properties=props)

    # DEPENDS_ON: dependent -> dependency
    for dep_of in sorted(dg):
        for dep in dg[dep_of]:
            if dep_of in node_id and dep in node_id:
                _edge(node_id[dep_of], node_id[dep],
                      CausalEdgeType.DEPENDS_ON)

    # PRECEDES: call-chain ordering (root -> leaf as collected)
    for a, b in zip(chain_services, chain_services[1:]):
        if a != b and a in node_id and b in node_id:
            _edge(node_id[a], node_id[b], CausalEdgeType.PRECEDES)

    # Symptom node + OBSERVED_IN edge to its service
    incident_label = f"symptom:{sym}" if sym else "symptom:unknown"
    sym_nid = make_node_id(CausalNodeType.SYMPTOM, incident_label)
    nodes[sym_nid] = CausalNode(
        node_id=sym_nid, node_type=CausalNodeType.SYMPTOM.value,
        label=incident_label, properties={"service": sym})
    if sym in node_id:
        _edge(sym_nid, node_id[sym], CausalEdgeType.OBSERVED_IN)

    # DEPLOYMENT_CHANGE nodes + AFFECTS edges (deterministic labels)
    cmdb = evidence.get("cmdb_blast_radius")
    if isinstance(cmdb, dict) and isinstance(cmdb.get("blast_radius"), dict):
        for ci in sorted(cmdb["blast_radius"]):
            ci_n = _norm(ci)
            recs = cmdb["blast_radius"][ci]
            if not isinstance(recs, list) or not recs:
                continue
            change_label = f"change:{ci_n}:{len(recs)}"
            cid = make_node_id(CausalNodeType.DEPLOYMENT_CHANGE,
                               change_label)
            nodes[cid] = CausalNode(
                node_id=cid,
                node_type=CausalNodeType.DEPLOYMENT_CHANGE.value,
                label=change_label,
                properties={"ci": ci_n, "change_count": len(recs)})
            if ci_n in node_id:
                _edge(cid, node_id[ci_n], CausalEdgeType.AFFECTS)

    return CausalGraph(
        nodes=tuple(nodes[k] for k in sorted(nodes)),
        edges=tuple(edges[k] for k in sorted(edges)),
    )


# ---------------------------------------------------------------------------
# Phase 2 — Propagation reasoning (root vs victim vs collateral)
# ---------------------------------------------------------------------------

def _reachable_down(dg: Mapping[str, list[str]], start: str) -> set[str]:
    """Everything ``start`` transitively depends on (downstream)."""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        for dep in dg.get(cur, ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def _dependents_of(dg: Mapping[str, list[str]], target: str) -> set[str]:
    """Everything that transitively depends ON ``target`` (upstream
    callers) — the victims when ``target`` fails."""
    reverse: dict[str, set[str]] = {}
    for k in dg:
        for d in dg[k]:
            reverse.setdefault(d, set()).add(k)
    seen, stack = set(), [target]
    while stack:
        cur = stack.pop()
        for up in reverse.get(cur, ()):
            if up not in seen:
                seen.add(up)
                stack.append(up)
    return seen


def classify_roles(
    symptom_service: str, evidence: Mapping[str, Any],
) -> dict[str, str]:
    """Label each service ROOT (origin candidate) / VICTIM / COLLATERAL.

    Failure propagates from a dependency upward to its dependents. The
    origin is the deepest dependency of the symptom; the symptom's
    dependents are victims; unrelated affected services are collateral.
    """
    sym = _norm(symptom_service)
    dg = _dependency_graph(evidence)
    downstream = _reachable_down(dg, sym)      # candidates upstream of sym
    victims = _dependents_of(dg, sym)          # callers of the symptom
    affected = _blast_affected(evidence)

    roles: dict[str, str] = {}
    for svc in sorted({sym, *downstream, *victims, *affected} - {""}):
        if svc in downstream:
            roles[svc] = "root_candidate"
        elif svc == sym:
            roles[svc] = "symptom"
        elif svc in victims:
            roles[svc] = "victim"
        else:
            roles[svc] = "collateral"
    return roles


# ---------------------------------------------------------------------------
# Phase 3 — Topology-constrained hypothesis anchoring
# ---------------------------------------------------------------------------

def _anchor_service(hyp: Mapping[str, Any], services: set[str]) -> str:
    """Best service match for a hypothesis.

    Priority 1: the service name appears as a whole word in the
    hypothesis text (handles short names like ``db`` that the >=3-char
    token filter would drop). Priority 2: token overlap on the
    dash-split service name. Deterministic on ties (service_id asc).
    """
    text = f" {_norm(hyp.get('name', ''))} {_norm(hyp.get('root_cause', ''))} "
    words = set(text.replace("-", " ").replace(".", " ").split())
    for svc in sorted(services):
        if svc and svc in words:
            return svc
    h_tokens = _tokens(hyp.get("name", "")) | _tokens(
        hyp.get("root_cause", ""))
    best, best_score = "", 0
    for svc in sorted(services):
        score = len(_tokens(svc.replace("-", " ")) & h_tokens)
        if score > best_score:
            best, best_score = svc, score
    return best


def anchor_hypotheses(
    hypotheses_meta: list[dict[str, Any]],
    symptom_service: str,
    evidence: Mapping[str, Any],
    roles: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Anchor each hypothesis to a graph node and flag topology
    violations (an origin that is a downstream VICTIM of the symptom
    cannot itself be the origin)."""
    sym = _norm(symptom_service)
    services = set(roles.keys())
    out = []
    for h in sorted(hypotheses_meta or [],
                    key=lambda x: (-float(x.get("score", 0)),
                                   str(x.get("name", "")))):
        anchor = _anchor_service(h, services) or sym
        role = roles.get(anchor, "symptom" if anchor == sym else "unknown")
        # Topology impossibility: the hypothesis claims causation at a
        # node that is strictly a VICTIM (depends on the symptom) — the
        # "downstream API cannot cause upstream database outage" rule.
        impossible = role == "victim"
        out.append({
            "hypothesis": str(h.get("name", "")),
            "anchor_service": anchor,
            "anchor_role": role,
            "topology_possible": not impossible,
            "rejection_reason": (
                f"rejected because topology impossible: {anchor!r} is a "
                f"downstream victim of {sym!r}, cannot originate its failure"
            ) if impossible else "",
        })
    return out


# ---------------------------------------------------------------------------
# Phase 6/7 — Competing causal chains + elimination
# ---------------------------------------------------------------------------

def _dep_path(dg: Mapping[str, list[str]], src: str, dst: str) -> list[str]:
    """Deterministic shortest dependency path src -> ... -> dst (BFS over
    DEPENDS_ON edges), or [] if none."""
    if src == dst:
        return [src]
    from collections import deque
    q = deque([[src]])
    seen = {src}
    while q:
        path = q.popleft()
        for dep in dg.get(path[-1], ()):
            if dep == dst:
                return path + [dep]
            if dep not in seen and len(path) < MAX_CHAIN_LEN:
                seen.add(dep)
                q.append(path + [dep])
    return []


def build_causal_chains(
    anchored: list[dict[str, Any]],
    symptom_service: str,
    evidence: Mapping[str, Any],
    roles: Mapping[str, str],
    symptom_time: str,
) -> list[dict[str, Any]]:
    """One causal chain per hypothesis: origin -> (deps) -> symptom, with
    support / refutation / confidence and explicit elimination reasons."""
    sym = _norm(symptom_service)
    dg = _dependency_graph(evidence)
    affected = _blast_affected(evidence)
    expected_blast = _dependents_of(dg, sym)
    # The service where the trace error span actually occurred is strong,
    # grounded evidence of the failure origin.
    tc = evidence.get("trace_correlation")
    error_span_service = ""
    if isinstance(tc, dict):
        es = tc.get("error_span")
        error_span_service = _norm(
            (es or {}).get("service") if isinstance(es, dict) else "") \
            or _norm(tc.get("root_span_service"))
    chains: list[dict[str, Any]] = []

    for a in anchored:
        origin = a["anchor_service"]
        support: list[str] = []
        refutation: list[str] = []
        confidence = 50

        if not a["topology_possible"]:
            refutation.append(a["rejection_reason"])
            confidence = 0

        # dependency path from symptom down to the origin (symptom
        # depends on origin, so failure flows origin -> symptom)
        path = _dep_path(dg, sym, origin) if origin and origin != sym else \
            [sym]
        if origin and origin != sym and not path:
            if dg:                       # graph exists but no path
                refutation.append(
                    f"rejected because dependency absent: no path from "
                    f"{sym!r} to claimed origin {origin!r}")
                confidence = min(confidence, 10)
            path = [origin, sym]
        else:
            support.append(
                f"dependency path present: {' -> '.join(reversed(path))}")
            confidence += 10

        # error-span grounding: the origin is where the trace error was
        if error_span_service and origin == error_span_service:
            support.append(
                f"trace error span located at origin {origin!r}")
            confidence += 15

        # blast-radius reasoning (Phase 5)
        if expected_blast or affected:
            exp, obs = expected_blast, affected
            overlap = len(exp & obs)
            union = len(exp | obs) or 1
            mismatch = 1.0 - overlap / union
            if mismatch > BLAST_MISMATCH_THRESHOLD and exp and obs:
                refutation.append(
                    f"blast radius mismatch {mismatch:.2f}: expected "
                    f"{sorted(exp)} vs observed {sorted(obs)}")
                confidence -= BLAST_MISMATCH_PENALTY
            else:
                support.append(
                    f"blast radius consistent (mismatch {mismatch:.2f})")

        # temporal causality on change-shaped origins (Phase 4 extension)
        if origin in roles and _tokens(a["hypothesis"]) & {
                "deploy", "deployment", "change", "rollback", "release"}:
            change_times = sorted(
                ts for k, v in evidence.items()
                if any(t in _norm(k) for t in ("deploy", "change", "diff"))
                for ts in _extract_timestamps(v))
            if change_times and symptom_time and \
                    change_times[0] > str(symptom_time)[:16]:
                refutation.append(
                    "rejected because temporal ordering impossible: "
                    f"earliest change {change_times[0]} postdates symptom "
                    f"{str(symptom_time)[:16]}")
                confidence = 0

        confidence = max(0, min(100, confidence))
        chains.append({
            "origin": origin,
            "hypothesis": a["hypothesis"],
            "path": list(reversed(path)),
            "support": sorted(support),
            "refutation": sorted(refutation),
            "confidence": confidence,
            "eliminated": bool(refutation) and confidence == 0,
        })

    chains.sort(key=lambda c: (-c["confidence"], c["origin"],
                               c["hypothesis"]))
    return chains


# ---------------------------------------------------------------------------
# Phase 8 — Localization
# ---------------------------------------------------------------------------

def localize(
    chains: list[dict[str, Any]],
    symptom_service: str,
    evidence: Mapping[str, Any],
    roles: Mapping[str, str],
) -> dict[str, Any]:
    surviving = [c for c in chains if not c["eliminated"]]
    winner = surviving[0] if surviving else (chains[0] if chains else None)
    sym = _norm(symptom_service)

    if winner is None:
        return {"root_cause_service": sym, "immediate_cause_service": sym,
                "trigger": "", "symptom_service": sym,
                "collateral": [], "recovery_point": ""}

    path = winner["path"]
    root_service = path[0] if path else winner["origin"]
    immediate = path[-2] if len(path) >= 2 else root_service

    trigger = ""
    cmdb = evidence.get("cmdb_blast_radius")
    if isinstance(cmdb, dict) and isinstance(cmdb.get("blast_radius"), dict):
        for ci in sorted(cmdb["blast_radius"]):
            if _norm(ci) == root_service and cmdb["blast_radius"][ci]:
                trigger = f"change on {root_service}"
                break

    collateral = sorted(s for s, r in roles.items()
                        if r in ("victim", "collateral") and s != sym)
    return {
        "root_cause_service": root_service,
        "immediate_cause_service": immediate,
        "trigger": trigger,
        "symptom_service": sym,
        "collateral": collateral,
        "recovery_point": root_service,
    }


# ---------------------------------------------------------------------------
# Phase 9 — Deterministic enterprise narrative
# ---------------------------------------------------------------------------

def build_narrative(
    chains: list[dict[str, Any]],
    localization: Mapping[str, Any],
) -> dict[str, str]:
    surviving = [c for c in chains if not c["eliminated"]]
    winner = surviving[0] if surviving else (chains[0] if chains else None)
    eliminated = [c for c in chains if c["eliminated"]]

    if winner is None:
        return {"what": "No causal chain could be constructed from the "
                        "available topology evidence.",
                "where": "", "how": "", "why_survived": "",
                "why_others_failed": ""}

    return {
        "what": (f"Failure localized to {localization['root_cause_service']}; "
                 f"symptom surfaced at {localization['symptom_service']}."),
        "where": (f"Originated at {localization['root_cause_service']}"
                  + (f", triggered by {localization['trigger']}"
                     if localization["trigger"] else "")),
        "how": ("Propagated along "
                + " -> ".join(winner["path"])
                if winner["path"] else "No propagation path resolved"),
        "why_survived": ("; ".join(winner["support"])
                         or "highest confidence among competing chains"),
        "why_others_failed": ("; ".join(
            f"{c['origin']}: {'; '.join(c['refutation'])}"
            for c in eliminated) or "no competing chains eliminated"),
    }


# ---------------------------------------------------------------------------
# Entry point (shadow)
# ---------------------------------------------------------------------------

def run_causal_investigation(
    result: dict[str, Any],
    evidence: dict[str, Any],
    symptom_service: str,
    hypotheses_meta: list[dict[str, Any]] | None = None,
    symptom_time: str = "",
) -> None:
    """Attach ``_causal_investigation`` shadow metadata. No-op unless
    CAUSAL_INVESTIGATION_ENABLED. Never raises. Never changes
    root_cause / confidence."""
    if not _flag("CAUSAL_INVESTIGATION_ENABLED"):
        return
    try:
        meta = hypotheses_meta or []
        roles = classify_roles(symptom_service, evidence)
        graph = build_causal_graph(symptom_service, evidence)
        anchored = anchor_hypotheses(meta, symptom_service, evidence, roles)
        chains = build_causal_chains(anchored, symptom_service, evidence,
                                     roles, symptom_time)
        localization = localize(chains, symptom_service, evidence, roles)
        narrative = build_narrative(chains, localization)

        result["_causal_investigation"] = {
            "causal_graph": graph.to_dict(),
            "roles": roles,
            "anchored_hypotheses": anchored,
            "causal_chains": chains,
            "winning_chain": next(
                (c for c in chains if not c["eliminated"]),
                chains[0] if chains else None),
            "eliminated_chains": [c for c in chains if c["eliminated"]],
            "localization": localization,
            "blast_radius": {
                "expected": sorted(_dependents_of(
                    _dependency_graph(evidence), _norm(symptom_service))),
                "observed": sorted(_blast_affected(evidence)),
            },
            "narrative": narrative,
        }
    except Exception as exc:
        logger.warning(
            "causal_investigation.failed error_type=%s error=%s",
            type(exc).__name__, exc)


__all__ = [
    "anchor_hypotheses", "build_causal_chains", "build_causal_graph",
    "build_narrative", "classify_roles", "localize",
    "run_causal_investigation",
]
