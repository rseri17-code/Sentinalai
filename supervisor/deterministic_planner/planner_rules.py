"""Deterministic planner rules — goal derivation + capability selection.

Every function here is a **closed-form transform** on its inputs. No
randomness, no timestamps, no external state, no LLM. Same input →
identical output.

Public surface:
- :func:`derive_goals` — PlanContext → tuple of InvestigationGoal
- :func:`catalog` — hardcoded capability catalog (Capability objects
  keyed by CapabilityType.value)
- :func:`select_capabilities_for_goal` — GoalType → tuple of Capability
- :func:`compute_dependencies` — tuple[PlanStep] → dict[step_id, deps]
"""
from __future__ import annotations

import re
from typing import Iterable

from sentinel_core.models.capability import (
    Capability,
    CapabilityType,
)
from sentinel_core.models.goal import (
    GoalType,
    InvestigationGoal,
)
from sentinel_core.models.plan import PlanStep


# ---------------------------------------------------------------------------
# Capability catalog — one Capability per CapabilityType
# ---------------------------------------------------------------------------

def _capability_catalog() -> dict[str, Capability]:
    """Build the canonical Capability catalog. Called once at import."""
    cat: dict[str, Capability] = {}

    def add(
        ct: CapabilityType,
        description: str,
        satisfies: tuple[GoalType | str, ...],
        evidence: tuple[str, ...],
        confidence_gain: int,
        runtime_ms: int,
    ) -> None:
        sat = tuple(
            (g.value if isinstance(g, GoalType) else str(g))
            for g in satisfies
        )
        cat[ct.value] = Capability.make(
            ct, description,
            satisfies_goal_types=sat,
            typical_evidence_yield=evidence,
            typical_confidence_gain=confidence_gain,
            typical_runtime_ms=runtime_ms,
        )

    add(CapabilityType.COLLECT_POD_LIFECYCLE,
        "Pod restart/OOM/eviction events for the primary service",
        satisfies=(GoalType.VALIDATE_KUBERNETES_HEALTH, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("pod_lifecycle", "restart_events", "oom_events"),
        confidence_gain=15, runtime_ms=3_000)

    add(CapabilityType.COLLECT_DEPLOYMENT_HISTORY,
        "Recent deployments touching the primary service",
        satisfies=(GoalType.VALIDATE_DEPLOYMENT_HYPOTHESIS, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("deployments", "git_shas"),
        confidence_gain=20, runtime_ms=2_500)

    add(CapabilityType.COLLECT_DNS_STATE,
        "Cluster DNS resolution + kube-dns/CoreDNS health",
        satisfies=(GoalType.DETERMINE_NETWORK_FAILURE, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("dns_records", "coredns_health"),
        confidence_gain=10, runtime_ms=2_000)

    add(CapabilityType.COLLECT_LATENCY,
        "P50/P95/P99 latency for the primary service",
        satisfies=(GoalType.DETERMINE_NETWORK_FAILURE, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("latency_p50", "latency_p95", "latency_p99"),
        confidence_gain=10, runtime_ms=2_500)

    add(CapabilityType.COLLECT_TOPOLOGY,
        "Upstream and downstream dependencies of the primary service",
        satisfies=(GoalType.VALIDATE_DEPENDENCY_HEALTH, GoalType.ASSESS_BLAST_RADIUS),
        evidence=("upstream_topology", "downstream_topology"),
        confidence_gain=8, runtime_ms=1_500)

    add(CapabilityType.COLLECT_HISTORICAL_INCIDENTS,
        "Prior investigations on this service or incident type",
        satisfies=(GoalType.COMPARE_HISTORICAL_FAILURES, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("historical_incidents",),
        confidence_gain=8, runtime_ms=1_000)

    add(CapabilityType.COLLECT_TRANSACTION_PATH,
        "Distributed trace for a representative failing transaction",
        satisfies=(GoalType.VALIDATE_DEPENDENCY_HEALTH, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("trace_spans", "trace_hops"),
        confidence_gain=15, runtime_ms=4_000)

    add(CapabilityType.COLLECT_LOGS,
        "Application logs for the primary service in the incident window",
        satisfies=(GoalType.COLLECT_ROOT_CAUSE_EVIDENCE,),
        evidence=("logs",),
        confidence_gain=12, runtime_ms=6_000)

    add(CapabilityType.COLLECT_METRICS,
        "Standard RED metrics for the primary service",
        satisfies=(GoalType.COLLECT_ROOT_CAUSE_EVIDENCE,),
        evidence=("metrics_red",),
        confidence_gain=10, runtime_ms=2_500)

    add(CapabilityType.COLLECT_STORAGE_METRICS,
        "IOPS / disk pressure / PVC state for the primary service",
        satisfies=(GoalType.DETERMINE_STORAGE_BOTTLENECK, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("iops", "disk_pressure", "pvc_state"),
        confidence_gain=18, runtime_ms=3_500)

    add(CapabilityType.COLLECT_AUTH_EVENTS,
        "Authentication / authorization failure events",
        satisfies=(GoalType.DETERMINE_AUTHENTICATION_FAILURE, GoalType.COLLECT_ROOT_CAUSE_EVIDENCE),
        evidence=("auth_failures", "authz_denials"),
        confidence_gain=18, runtime_ms=2_500)

    add(CapabilityType.COMPARE_HISTORICAL_FAILURES,
        "Compare current incident to prior root causes for this service",
        satisfies=(GoalType.COMPARE_HISTORICAL_FAILURES,),
        evidence=("historical_comparison",),
        confidence_gain=15, runtime_ms=1_500)

    add(CapabilityType.QUERY_KNOWLEDGE_GRAPH,
        "Query the runtime knowledge graph for related entities",
        satisfies=(GoalType.COLLECT_ROOT_CAUSE_EVIDENCE, GoalType.VALIDATE_DEPENDENCY_HEALTH),
        evidence=("kg_neighbors",),
        confidence_gain=5, runtime_ms=500)

    add(CapabilityType.ASSESS_DEPENDENCY_HEALTH,
        "Health of upstream + downstream dependencies",
        satisfies=(GoalType.VALIDATE_DEPENDENCY_HEALTH,),
        evidence=("dependency_health",),
        confidence_gain=12, runtime_ms=3_000)

    add(CapabilityType.ASSESS_BLAST_RADIUS,
        "Weighted blast radius from the causal graph",
        satisfies=(GoalType.ASSESS_BLAST_RADIUS,),
        evidence=("blast_radius",),
        confidence_gain=10, runtime_ms=1_000)

    return cat


_CATALOG: dict[str, Capability] = _capability_catalog()


def catalog() -> dict[str, Capability]:
    """Return the immutable capability catalog (copy)."""
    return dict(_CATALOG)


def select_capabilities_for_goal(goal_type: str) -> tuple[Capability, ...]:
    """Return capabilities whose ``satisfies_goal_types`` includes
    ``goal_type``. Order is deterministic (sorted by capability_id)."""
    matches = tuple(
        c for c in _CATALOG.values() if goal_type in c.satisfies_goal_types
    )
    return tuple(sorted(matches, key=lambda c: c.capability_id))


# ---------------------------------------------------------------------------
# Goal derivation — pure function of PlanContext
# ---------------------------------------------------------------------------

# Deterministic (case-folded), token-based keyword sets.
#
# RC-K: previously used substring ``k in h``, which incorrectly matched
# ``"auth"`` inside ``"authoritative_dns_failure"`` and triggered auth
# capabilities on a DNS incident. Now every haystack is tokenized on
# non-alphanumeric boundaries and each keyword must equal a whole
# token — so ``authoritative`` no longer matches ``auth``, but
# ``authentication_failure`` matches ``authentication``, and
# ``token_validation`` matches ``token``.
#
# Keyword sets are enumerated as concrete word forms (verb + noun +
# plural + abbreviation) so real incidents still match without the
# substring false-positive risk.
_STORAGE_KEYWORDS = frozenset({
    "storage", "disk", "disks", "iops", "pvc", "volume", "volumes",
    "pool", "pools", "saturation", "database", "db",
})
_NETWORK_KEYWORDS = frozenset({
    "network", "networking", "timeout", "timeouts", "latency",
    "connection", "connections", "dns", "tcp", "packet", "packets",
})
_AUTH_KEYWORDS = frozenset({
    "auth",                                            # bare token (auth_events)
    "authn", "authz",
    "authentication", "authenticate", "authenticated", "authenticating",
    "authorization", "authorize", "authorized", "authorizing",
    "unauthorized", "forbidden",
    "certificate", "certificates", "cert", "certs",
    "credential", "credentials",
    "token", "tokens",
})
_K8S_KEYWORDS = frozenset({
    "kubernetes", "k8s", "pod", "pods", "container", "containers",
    "oom", "restart", "restarts", "eviction", "evictions",
    "crashloop", "crashlooping",
})
_DEPLOY_KEYWORDS = frozenset({
    "deployment", "deployments", "deploy", "release", "releases",
    "rollout", "rollouts", "change", "changes", "commit", "commits",
})


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> tuple[str, ...]:
    """Return case-folded alphanumeric tokens from ``text``.

    Splits on any run of non-alphanumeric characters. Empty tokens
    (adjacent delimiters, leading/trailing delimiter) are dropped.
    Deterministic — same string → same tuple.
    """
    return tuple(t for t in _TOKEN_SPLIT.split(str(text or "").lower()) if t)


def _matches_any(haystack: str, keywords: Iterable[str]) -> bool:
    """Return True iff any keyword equals a whole token in ``haystack``.

    RC-K token-boundary match: replaces the old substring check that
    incorrectly triggered on words like ``"authoritative"`` matching
    ``"auth"``. Callers pass a frozen set / tuple of allowed tokens.
    """
    tokens = set(_tokenize(haystack))
    for k in keywords:
        if k in tokens:
            return True
    return False


def derive_goals(pc) -> tuple[InvestigationGoal, ...]:
    """Given a :class:`PlanContext`, deterministically derive the
    investigation goals it implies.

    Duck-typed: any object with matching attribute names works.
    """
    incident_type = str(getattr(pc, "incident_type", "") or "").lower()
    service       = str(getattr(pc, "service",       "") or "")
    dc            = getattr(pc, "decision_context", None)
    completed     = set(str(x) for x in (getattr(pc, "completed_goals", ()) or ()))
    outstanding   = set(str(x) for x in (getattr(pc, "outstanding_goals", ()) or ()))

    goals: list[InvestigationGoal] = []

    def _add(gt: GoalType, description: str, priority: int, gain: int,
              completion: tuple[str, ...], failure: tuple[str, ...] = ()) -> None:
        goal = InvestigationGoal.make(
            gt, description,
            priority=priority,
            completion_criteria=completion,
            failure_criteria=failure,
            expected_confidence_gain=gain,
        )
        if goal.goal_id in completed:
            return
        if any(g.goal_id == goal.goal_id for g in goals):
            return
        goals.append(goal)

    # Always: root-cause evidence gathering
    _add(GoalType.COLLECT_ROOT_CAUSE_EVIDENCE,
          f"Gather baseline evidence for {service or 'the affected service'}",
          priority=500, gain=10,
          completion=("evidence_present",))

    # Incident-type based rules
    if _matches_any(incident_type, _STORAGE_KEYWORDS):
        _add(GoalType.DETERMINE_STORAGE_BOTTLENECK,
              "Determine whether a storage/IO bottleneck is the root cause",
              priority=700, gain=25,
              completion=("iops_analyzed", "pvc_analyzed"))

    if _matches_any(incident_type, _NETWORK_KEYWORDS):
        _add(GoalType.DETERMINE_NETWORK_FAILURE,
              "Determine whether a network/DNS failure is the root cause",
              priority=700, gain=25,
              completion=("dns_verified", "latency_analyzed"))

    if _matches_any(incident_type, _AUTH_KEYWORDS):
        _add(GoalType.DETERMINE_AUTHENTICATION_FAILURE,
              "Determine whether an auth failure is the root cause",
              priority=750, gain=25,
              completion=("auth_events_reviewed",))

    if _matches_any(incident_type, _K8S_KEYWORDS):
        _add(GoalType.VALIDATE_KUBERNETES_HEALTH,
              "Validate Kubernetes control-plane and pod health",
              priority=650, gain=20,
              completion=("pod_lifecycle_reviewed",))

    if _matches_any(incident_type, _DEPLOY_KEYWORDS):
        _add(GoalType.VALIDATE_DEPLOYMENT_HYPOTHESIS,
              "Validate whether a recent deployment introduced the incident",
              priority=800, gain=25,
              completion=("deployment_diffed",))

    # DecisionContext-driven rules
    if dc is not None:
        recurring = bool(getattr(dc, "recurring_incident", False))
        if recurring:
            _add(GoalType.COMPARE_HISTORICAL_FAILURES,
                  "Compare current incident to historical patterns",
                  priority=600, gain=15,
                  completion=("historical_compared",))

        blast = getattr(dc, "likely_blast_radius", None)
        blast_severity = ""
        if blast is not None:
            blast_severity = str(getattr(blast, "severity", "") or "")
        if blast_severity in ("high", "critical"):
            _add(GoalType.ASSESS_BLAST_RADIUS,
                  "Assess blast radius and downstream impact",
                  priority=650, gain=12,
                  completion=("blast_radius_assessed",))

        upstream = getattr(dc, "recommended_next_service", "")
        has_deps = bool(upstream) or bool(getattr(dc, "recommended_queries", ())) \
            and "upstream_service_health" in (getattr(dc, "recommended_queries", ()) or ())
        if has_deps:
            _add(GoalType.VALIDATE_DEPENDENCY_HEALTH,
                  "Validate health of upstream / downstream dependencies",
                  priority=600, gain=15,
                  completion=("dependency_health_verified",))

    # Explicit outstanding_goals augmentation
    for out_id in sorted(outstanding):
        if not any(g.goal_id == out_id for g in goals):
            goals.append(InvestigationGoal.make(
                GoalType.COLLECT_ROOT_CAUSE_EVIDENCE,
                f"Caller-requested outstanding goal {out_id}",
                priority=500, expected_confidence_gain=5,
                completion_criteria=("caller_confirms",),
            ))

    # Deterministic order: priority DESC, then goal_id ASC
    goals.sort(key=lambda g: (-g.priority, g.goal_id))
    return tuple(goals)


# ---------------------------------------------------------------------------
# Dependency computation between plan steps
# ---------------------------------------------------------------------------

# Pairs of (dependent_capability, prerequisite_capability).
_CAPABILITY_DEPS: tuple[tuple[str, str], ...] = (
    (CapabilityType.COMPARE_HISTORICAL_FAILURES.value,
     CapabilityType.COLLECT_HISTORICAL_INCIDENTS.value),
    (CapabilityType.ASSESS_BLAST_RADIUS.value,
     CapabilityType.COLLECT_TOPOLOGY.value),
    (CapabilityType.ASSESS_DEPENDENCY_HEALTH.value,
     CapabilityType.COLLECT_TOPOLOGY.value),
)


def compute_dependencies(steps: tuple[PlanStep, ...]) -> dict[str, tuple[str, ...]]:
    """Return a step-id → prerequisite-step-ids mapping.

    Only edges where BOTH endpoints are in ``steps`` are recorded.
    Deterministic: keys are sorted, values are sorted.
    """
    by_cap: dict[str, PlanStep] = {}
    for s in steps:
        # Capability id is prefixed with "cap:" — dependency rules key on the
        # bare capability_type value.
        cap_type = s.capability_id[len("cap:"):] if s.capability_id.startswith("cap:") \
            else s.capability_id
        by_cap[cap_type] = s
    edges: dict[str, list[str]] = {}
    for dep_cap, prereq_cap in _CAPABILITY_DEPS:
        if dep_cap in by_cap and prereq_cap in by_cap:
            edges.setdefault(by_cap[dep_cap].step_id, []).append(
                by_cap[prereq_cap].step_id
            )
    return {k: tuple(sorted(v)) for k, v in sorted(edges.items())}


__all__ = [
    "catalog",
    "select_capabilities_for_goal",
    "derive_goals",
    "compute_dependencies",
]
