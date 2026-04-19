"""Blast Radius — pre-fix risk assessment for SentinalAI.

Before any remediation action is applied, this module computes the full blast
radius: which services could be disrupted, what percentage of users could be
affected, and what precautions should be taken first.

This is the "look before you leap" safety gate in the fix lifecycle:

    ProposedFix → [blast_radius.compute_blast_radius] → BlastRadiusReport
                          ↓
              safe_to_auto_apply / requires_human_approval

No competitor offers pre-fix topology-aware risk scoring.  Where other tools
blindly apply fixes, SentinalAI maps every downstream service, aggregates the
user-facing impact estimate, and generates concrete precautions before a single
kubectl command runs.

Algorithm:
  1. BFS from target_service through the cmdb_topology (callers + dependencies)
  2. Classify each reachable service as direct_downstream, indirect, or
     shared_resource
  3. Aggregate impact percentages — P1 services carry high base impact weights
  4. Determine risk_tier from total impact % and presence of P1 dependencies
  5. Generate actionable precautions (drain traffic, notify team, etc.)
  6. Set safe_to_auto_apply = True only when risk_tier == LOW and no P1 deps
  7. Set requires_human_approval = True for MEDIUM / HIGH / CRITICAL

Topology dict schema (cmdb_topology):
    {
        "service-name": {
            "tier": "P1" | "P2" | "P3",
            "dependencies": ["svc-a", "svc-b"],   # what this service calls
            "callers": ["svc-x", "svc-y"],         # what calls this service
            "has_circuit_breaker": True | False,
            "team": "payments-sre",                # optional, for notify precautions
            "traffic_pct": 15.0,                   # % of total user traffic (0-100)
        },
        ...
    }

KG edges list schema (kg_edges):
    [{"src": "svc-a", "dst": "svc-b", "rel": "DEPENDS_ON"}, ...]

Usage:
    from supervisor.blast_radius import compute_blast_radius, RiskTier

    report = compute_blast_radius(
        target_service="payment-service",
        fix_type="restart",
        cmdb_topology=topology,
        kg_edges=edges,
    )
    if report.safe_to_auto_apply:
        engine.approve_and_apply(...)
    else:
        agui.request_human_approval(report)
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("sentinalai.blast_radius")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Base user-impact weight by service tier (percentage points contributed)
_TIER_BASE_IMPACT: dict[str, float] = {
    "P1": 25.0,
    "P2": 10.0,
    "P3": 3.0,
}

# Indirect-hop impact discount factor (each additional hop reduces estimate)
_HOP_DISCOUNT = 0.5

# Thresholds that define risk tier boundaries
_LOW_MAX_IMPACT = 5.0       # < 5%   → LOW
_MEDIUM_MAX_IMPACT = 20.0   # 5-20%  → MEDIUM
_HIGH_MAX_IMPACT = 50.0     # 20-50% → HIGH
                            # > 50%  → CRITICAL

# Fix types that are temporarily disruptive (service goes offline during action)
_DISRUPTIVE_FIX_TYPES = {"restart", "rollback", "traffic_shift"}


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------

class RiskTier(str, Enum):
    LOW = "low"          # < 5% user impact, no P1 dependencies
    MEDIUM = "medium"    # 5-20% impact or 1 P1 dependency
    HIGH = "high"        # 20-50% impact or 2+ P1 dependencies
    CRITICAL = "critical"  # > 50% impact or cascading risk


@dataclass
class AffectedService:
    """Describes a single service that could be affected by the proposed fix."""

    name: str
    dependency_type: str    # "direct_downstream" | "indirect" | "shared_resource"
    dependency_path: list[str]  # traversal path from target to this service
    tier: str               # "P1" | "P2" | "P3"
    estimated_impact_pct: float
    can_degrade_gracefully: bool  # True if service has circuit breaker / fallback


@dataclass
class BlastRadiusReport:
    """Full pre-fix risk assessment returned by compute_blast_radius()."""

    target_service: str
    fix_type: str           # "restart" | "rollback" | "scale_up" | "config_change" | "traffic_shift"
    affected_services: list[AffectedService]
    total_estimated_user_impact_pct: float
    risk_tier: RiskTier
    recommended_precautions: list[str]   # ordered list of actionable steps
    safe_to_auto_apply: bool             # True only if LOW risk and no P1 deps
    requires_human_approval: bool        # True for MEDIUM / HIGH / CRITICAL
    reasoning: str


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def compute_blast_radius(
    target_service: str,
    fix_type: str,
    cmdb_topology: dict[str, Any],   # {service: {tier, dependencies, callers, has_circuit_breaker, ...}}
    kg_edges: list[dict] | None = None,  # optional extra edges from knowledge graph
) -> BlastRadiusReport:
    """Compute the blast radius of a proposed fix before applying it.

    Traverses the CMDB topology via BFS to discover every service that could be
    disrupted when the target_service is restarted/rolled-back/scaled/changed.
    Aggregates user-facing impact estimates and determines the risk tier.

    Parameters
    ----------
    target_service:
        The service the fix will be applied to.
    fix_type:
        One of "restart", "rollback", "scale_up", "config_change",
        "traffic_shift".  Disruptive types (restart, rollback, traffic_shift)
        carry higher baseline risk.
    cmdb_topology:
        CMDB service map.  Keys are service names; values are dicts with at
        least ``callers``, ``dependencies``, ``tier``, and
        ``has_circuit_breaker`` fields.  Missing fields default gracefully.
    kg_edges:
        Optional list of knowledge-graph edges that can supplement the topology
        with dependency relationships not yet captured in the CMDB.

    Returns
    -------
    BlastRadiusReport
        Complete pre-fix risk assessment with affected services, risk tier,
        precautions, and auto-apply / approval flags.
    """
    kg_edges = kg_edges or []

    # ------------------------------------------------------------------
    # Step 1: Augment topology with KG edges
    # ------------------------------------------------------------------
    topology = _augment_topology_from_kg(cmdb_topology, kg_edges)

    # ------------------------------------------------------------------
    # Step 2: BFS to find all reachable affected services
    # ------------------------------------------------------------------
    affected_services = _bfs_affected_services(target_service, fix_type, topology)

    # ------------------------------------------------------------------
    # Step 3: Compute aggregate impact
    # ------------------------------------------------------------------
    total_impact = _aggregate_impact(target_service, affected_services, topology)
    # Cap at 100%
    total_impact = min(total_impact, 100.0)

    # ------------------------------------------------------------------
    # Step 4: Determine risk tier
    # ------------------------------------------------------------------
    p1_deps = [s for s in affected_services if s.tier == "P1"]
    risk_tier = _determine_risk_tier(total_impact, p1_deps)

    # ------------------------------------------------------------------
    # Step 5: Generate precautions
    # ------------------------------------------------------------------
    precautions = _generate_precautions(
        target_service, fix_type, affected_services, risk_tier, topology
    )

    # ------------------------------------------------------------------
    # Step 6: Compute gate flags
    # ------------------------------------------------------------------
    safe_to_auto_apply = risk_tier == RiskTier.LOW and len(p1_deps) == 0
    requires_human_approval = risk_tier in (RiskTier.MEDIUM, RiskTier.HIGH, RiskTier.CRITICAL)

    # ------------------------------------------------------------------
    # Step 7: Build reasoning string
    # ------------------------------------------------------------------
    reasoning = _build_reasoning(
        target_service, fix_type, affected_services, total_impact, risk_tier, p1_deps
    )

    report = BlastRadiusReport(
        target_service=target_service,
        fix_type=fix_type,
        affected_services=affected_services,
        total_estimated_user_impact_pct=round(total_impact, 2),
        risk_tier=risk_tier,
        recommended_precautions=precautions,
        safe_to_auto_apply=safe_to_auto_apply,
        requires_human_approval=requires_human_approval,
        reasoning=reasoning,
    )

    logger.info(
        "Blast radius computed: target=%s fix_type=%s risk=%s impact=%.1f%% "
        "affected=%d safe_to_auto=%s",
        target_service, fix_type, risk_tier.value,
        total_impact, len(affected_services), safe_to_auto_apply,
    )
    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _augment_topology_from_kg(
    topology: dict[str, Any],
    kg_edges: list[dict],
) -> dict[str, Any]:
    """Return a shallow copy of topology augmented with KG dependency edges."""
    import copy
    aug = copy.deepcopy(topology)

    for edge in kg_edges:
        src = edge.get("src", "")
        dst = edge.get("dst", "")
        rel = edge.get("rel", "")
        if not src or not dst:
            continue

        # DEPENDS_ON means src depends on dst → src is a caller of dst
        if rel in ("DEPENDS_ON", "CALLS"):
            if dst in aug:
                callers = aug[dst].setdefault("callers", [])
                if src not in callers:
                    callers.append(src)
            if src in aug:
                deps = aug[src].setdefault("dependencies", [])
                if dst not in deps:
                    deps.append(dst)

        # AFFECTED means src was affected by dst → dst is a dependency of src
        elif rel == "AFFECTED":
            if src in aug:
                deps = aug[src].setdefault("dependencies", [])
                if dst not in deps:
                    deps.append(dst)

    return aug


def _bfs_affected_services(
    target_service: str,
    fix_type: str,
    topology: dict[str, Any],
) -> list[AffectedService]:
    """BFS from target through callers (services that depend on target).

    For disruptive fix types we also follow shared_resource relationships —
    anything that shares a resource with the target service may be disrupted.

    Returns a list of AffectedService, ordered by discovery (BFS level = hop
    distance from target, i.e. impact decreasing with distance).
    """
    is_disruptive = fix_type in _DISRUPTIVE_FIX_TYPES
    target_info = topology.get(target_service, {})

    affected: list[AffectedService] = []
    visited: set[str] = {target_service}

    # Queue entries: (service_name, hop_count, dependency_path, dependency_type)
    queue: deque[tuple[str, int, list[str], str]] = deque()

    # Seed the BFS with direct callers of the target service
    for caller in target_info.get("callers", []):
        if caller not in visited:
            visited.add(caller)
            queue.append((caller, 1, [target_service, caller], "direct_downstream"))

    # Also treat services that share a dependency as potential indirect victims
    # (relevant for config_change / scale_up that affect a shared resource)
    if not is_disruptive:
        # For non-disruptive fixes, limit to direct callers only
        pass
    else:
        # For disruptive fixes, also check other services that depend on the same
        # resources as target (shared_resource pattern)
        for dep in target_info.get("dependencies", []):
            dep_info = topology.get(dep, {})
            for sibling_caller in dep_info.get("callers", []):
                if sibling_caller not in visited and sibling_caller != target_service:
                    visited.add(sibling_caller)
                    queue.append((
                        sibling_caller,
                        2,
                        [target_service, dep, sibling_caller],
                        "shared_resource",
                    ))

    while queue:
        svc_name, hop, path, dep_type = queue.popleft()
        svc_info = topology.get(svc_name, {})
        tier = svc_info.get("tier", "P3")
        has_cb = bool(svc_info.get("has_circuit_breaker", False))

        # Base impact decreases with hop distance and circuit-breaker presence
        base_impact = _TIER_BASE_IMPACT.get(tier, 3.0)
        hop_factor = _HOP_DISCOUNT ** (hop - 1)
        cb_factor = 0.3 if has_cb else 1.0  # circuit breaker dramatically reduces impact
        estimated_impact = base_impact * hop_factor * cb_factor

        affected.append(AffectedService(
            name=svc_name,
            dependency_type=dep_type,
            dependency_path=list(path),
            tier=tier,
            estimated_impact_pct=round(estimated_impact, 2),
            can_degrade_gracefully=has_cb,
        ))

        # Continue BFS into callers of this service (next hop)
        for next_caller in svc_info.get("callers", []):
            if next_caller not in visited:
                visited.add(next_caller)
                new_dep_type = "indirect" if dep_type == "direct_downstream" else dep_type
                queue.append((
                    next_caller,
                    hop + 1,
                    path + [next_caller],
                    new_dep_type,
                ))

    return affected


def _aggregate_impact(
    target_service: str,
    affected_services: list[AffectedService],
    topology: dict[str, Any],
) -> float:
    """Compute total estimated user-facing impact percentage.

    Strategy:
    - If topology has explicit ``traffic_pct`` for the target service, use that
      as the floor impact (the target itself is disrupted).
    - Add the estimated_impact_pct of each affected service, avoiding double
      counting by tracking the maximum impact seen from any single path.
    - Result is capped by the caller at 100%.
    """
    target_info = topology.get(target_service, {})
    # Start from the target's own traffic share if known
    target_traffic = float(target_info.get("traffic_pct", 0.0))

    # Sum additional downstream impact (de-duplicated by service name —
    # we already do that via the BFS visited set)
    downstream_impact = sum(s.estimated_impact_pct for s in affected_services)

    # Blend: use the larger of (target's own traffic %) and (downstream sum)
    # because if the target handles 30% of traffic, restarting it disrupts 30%
    # regardless of how many downstream callers we found.
    return max(target_traffic, downstream_impact)


def _determine_risk_tier(
    total_impact: float,
    p1_deps: list[AffectedService],
) -> RiskTier:
    """Map impact percentage and P1 dependency count to a RiskTier."""
    p1_count = len(p1_deps)

    if total_impact > _HIGH_MAX_IMPACT or p1_count >= 3:
        return RiskTier.CRITICAL
    if total_impact > _MEDIUM_MAX_IMPACT or p1_count >= 2:
        return RiskTier.HIGH
    if total_impact > _LOW_MAX_IMPACT or p1_count >= 1:
        return RiskTier.MEDIUM
    return RiskTier.LOW


def _generate_precautions(
    target_service: str,
    fix_type: str,
    affected_services: list[AffectedService],
    risk_tier: RiskTier,
    topology: dict[str, Any],
) -> list[str]:
    """Generate ordered, actionable precautions for the operator.

    Precautions are returned from highest to lowest priority.
    """
    precautions: list[str] = []
    target_info = topology.get(target_service, {})

    # --- Traffic management ---
    if fix_type in ("restart", "rollback"):
        precautions.append(
            f"Drain traffic from '{target_service}' before applying fix "
            f"(use load balancer or Kubernetes graceful termination)."
        )

    if fix_type == "traffic_shift":
        precautions.append(
            f"Enable maintenance mode on '{target_service}' before shifting traffic."
        )

    # --- P1 dependency notifications ---
    p1_services = [s for s in affected_services if s.tier == "P1"]
    if p1_services:
        p1_names = ", ".join(s.name for s in p1_services)
        precautions.append(
            f"Notify owning SRE teams for P1 services before proceeding: {p1_names}."
        )

    # --- Team-specific notifications from topology ---
    notified_teams: set[str] = set()
    for svc in affected_services:
        svc_info = topology.get(svc.name, {})
        team = svc_info.get("team", "")
        if team and team not in notified_teams:
            notified_teams.add(team)
            precautions.append(f"Notify team '{team}' that '{svc.name}' may be disrupted.")

    # Also notify the target service's own team
    own_team = target_info.get("team", "")
    if own_team and own_team not in notified_teams:
        notified_teams.add(own_team)
        precautions.append(
            f"Inform team '{own_team}' (owner of '{target_service}') about the fix window."
        )

    # --- High/Critical tier advice ---
    if risk_tier in (RiskTier.HIGH, RiskTier.CRITICAL):
        precautions.append(
            "Open a change-management ticket and get secondary approval before applying."
        )
        precautions.append(
            f"Prepare a rollback plan for '{target_service}' in case the fix worsens symptoms."
        )

    if risk_tier == RiskTier.CRITICAL:
        precautions.append(
            "Consider a full maintenance window with user-facing communication."
        )

    # --- Circuit-breaker advice for services without one ---
    no_cb = [s for s in affected_services if not s.can_degrade_gracefully and s.tier in ("P1", "P2")]
    if no_cb:
        no_cb_names = ", ".join(s.name for s in no_cb[:3])
        precautions.append(
            f"Services without circuit breakers may hard-fail: {no_cb_names}. "
            f"Verify fallback behaviour before proceeding."
        )

    # --- Scale-up specific ---
    if fix_type == "scale_up":
        precautions.append(
            f"Verify cluster has sufficient node capacity before scaling '{target_service}'."
        )

    # --- Config-change specific ---
    if fix_type == "config_change":
        precautions.append(
            f"Apply config change to a canary instance of '{target_service}' first."
        )

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for p in precautions:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    return unique


def _build_reasoning(
    target_service: str,
    fix_type: str,
    affected_services: list[AffectedService],
    total_impact: float,
    risk_tier: RiskTier,
    p1_deps: list[AffectedService],
) -> str:
    """Construct a human-readable reasoning string for the report."""
    lines: list[str] = [
        f"Fix type '{fix_type}' on '{target_service}' assessed as {risk_tier.value.upper()} risk.",
    ]

    if not affected_services:
        lines.append("No downstream services found in topology — isolated service.")
    else:
        direct = [s for s in affected_services if s.dependency_type == "direct_downstream"]
        indirect = [s for s in affected_services if s.dependency_type == "indirect"]
        shared = [s for s in affected_services if s.dependency_type == "shared_resource"]
        parts: list[str] = []
        if direct:
            parts.append(f"{len(direct)} direct downstream service(s)")
        if indirect:
            parts.append(f"{len(indirect)} indirect service(s)")
        if shared:
            parts.append(f"{len(shared)} shared-resource service(s)")
        lines.append(f"Affected: {', '.join(parts)}.")

    lines.append(f"Estimated total user impact: {total_impact:.1f}%.")

    if p1_deps:
        p1_names = ", ".join(s.name for s in p1_deps)
        lines.append(f"P1 dependencies affected: {p1_names}.")

    graceful = [s for s in affected_services if s.can_degrade_gracefully]
    if graceful:
        lines.append(
            f"{len(graceful)} of {len(affected_services)} affected service(s) "
            f"have circuit breakers and can degrade gracefully."
        )

    return " ".join(lines)
