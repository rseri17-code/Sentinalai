"""SkillRegistry — capability → skill-name mapping.

The planner never chooses skills; it only chooses capabilities. This
registry is a **data-only** module: it maps a capability id to the
possible concrete skills that could implement it. Nothing here
executes anything. Execution is a future milestone.

Design principles
-----------------
- **Data only**: no execution, no side effects.
- **Immutable-by-convention**: the default mapping is a module-level
  frozen dict; instances hold a defensive copy so callers cannot mutate
  the default.
- **Extensible**: :meth:`register` and :meth:`extend` return NEW
  registry instances (registry is treated as immutable by callers).
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Iterable, Mapping


# ---------------------------------------------------------------------------
# Default capability → skill-name mapping
#
# Skill names are string labels only — nothing here imports, invokes, or
# knows the concrete tool. They exist so a future execution layer can
# resolve "cap:collect_pod_lifecycle" to a real MCP tool call.
# ---------------------------------------------------------------------------

DEFAULT_SKILL_REGISTRY: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "cap:collect_pod_lifecycle":        ("kubectl_pods",       "sysdig_pods"),
    "cap:collect_deployment_history":   ("git_history",        "argocd_history", "kubectl_deployments"),
    "cap:collect_dns_state":            ("dig_dns",            "kubectl_dns"),
    "cap:collect_latency":              ("prometheus_latency", "grafana_latency"),
    "cap:collect_topology":             ("dependency_graph_read", "kubectl_svc"),
    "cap:collect_historical_incidents": ("investigation_store_read", "resolution_memory_read"),
    "cap:collect_transaction_path":     ("otel_traces",        "tempo_traces"),
    "cap:collect_logs":                 ("elastic_logs",       "loki_logs",       "kubectl_logs"),
    "cap:collect_metrics":              ("prometheus_metrics", "grafana_metrics"),
    "cap:collect_storage_metrics":      ("prometheus_storage", "kubectl_pvc"),
    "cap:collect_auth_events":          ("elastic_auth",       "cloudtrail_auth"),
    "cap:compare_historical_failures":  ("resolution_memory_read", "pattern_intelligence_read"),
    "cap:query_knowledge_graph":        ("knowledge_graph_read",),
    "cap:assess_dependency_health":     ("dependency_graph_read", "causal_graph_read"),
    "cap:assess_blast_radius":          ("causal_graph_read",  "blast_radius_read"),
})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Immutable-by-convention capability → skills mapping.

    The default constructor holds a defensive copy of
    :data:`DEFAULT_SKILL_REGISTRY`. To extend the registry, callers
    should use :meth:`extend` which returns a new instance.
    """

    def __init__(self, mapping: Mapping[str, tuple[str, ...]] | None = None) -> None:
        raw = mapping if mapping is not None else DEFAULT_SKILL_REGISTRY
        self._mapping: dict[str, tuple[str, ...]] = {
            str(k): tuple(str(x) for x in v)
            for k, v in raw.items()
        }

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def has(self, capability_id: str) -> bool:
        return capability_id in self._mapping

    def skills_for(self, capability_id: str) -> tuple[str, ...]:
        """Return the tuple of possible skill names for a capability.

        Returns an empty tuple if the capability is unknown — the
        planner should still emit the step (it's a capability-level
        intent), but a future execution layer may report the gap.
        """
        return self._mapping.get(capability_id, ())

    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted(self._mapping.keys()))

    def to_dict(self) -> dict[str, list[str]]:
        """JSON-safe rendering. Sorted by capability id."""
        return {k: list(self._mapping[k]) for k in sorted(self._mapping.keys())}

    def __len__(self) -> int:
        return len(self._mapping)

    def __contains__(self, capability_id: str) -> bool:
        return self.has(capability_id)

    # ------------------------------------------------------------------
    # Extend (immutable — returns a new instance)
    # ------------------------------------------------------------------

    def extend(self, extra: Mapping[str, Iterable[str]]) -> "SkillRegistry":
        """Return a new SkillRegistry that also contains ``extra``.

        Existing entries take precedence over ``extra`` on collision.
        Never mutates ``self``.
        """
        merged: dict[str, tuple[str, ...]] = dict(self._mapping)
        for k, v in extra.items():
            if k not in merged:
                merged[k] = tuple(str(x) for x in v)
        return SkillRegistry(merged)


__all__ = [
    "SkillRegistry",
    "DEFAULT_SKILL_REGISTRY",
]
