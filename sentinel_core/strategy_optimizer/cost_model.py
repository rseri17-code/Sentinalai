"""Deterministic cost model for investigation strategies.

Cost = evidence_cost + tool_cost + switching_overhead.
Overall value = information_gain × confidence_gain × success_rate
              - normalised_execution_cost.

Every value is a closed-form transform. No LLM, no randomness.
"""
from __future__ import annotations

from typing import Iterable, Mapping


DEFAULT_EVIDENCE_COST: Mapping[str, int] = {
    "logs":                2500,
    "metrics_red":         1000,
    "iops":                1500,
    "pvc_state":           1500,
    "oom_events":           500,
    "pod_lifecycle":       1500,
    "restart_events":       500,
    "deployments":         1500,
    "git_shas":            1000,
    "dns_records":         1000,
    "coredns_health":      1500,
    "auth_failures":       1500,
    "authz_denials":       1500,
    "trace_spans":         3000,
    "trace_hops":          2000,
    "upstream_topology":    500,
    "downstream_topology":  500,
    "historical_incidents": 500,
    "blast_radius":        1000,
    "kg_neighbors":         500,
    "historical_comparison": 500,
}


DEFAULT_TOOL_COST: Mapping[str, int] = {
    "kubectl_pods":              1000,
    "kubectl_dns":               1000,
    "kubectl_deployments":       1000,
    "kubectl_logs":              2000,
    "kubectl_svc":               1000,
    "kubectl_pvc":               1000,
    "sysdig_pods":               2000,
    "prometheus_latency":        1500,
    "prometheus_metrics":        1500,
    "prometheus_storage":        2000,
    "grafana_latency":           1000,
    "grafana_metrics":           1000,
    "git_history":                500,
    "argocd_history":             500,
    "dig_dns":                   1000,
    "otel_traces":               3000,
    "tempo_traces":              3000,
    "elastic_logs":              4000,
    "elastic_auth":              2500,
    "loki_logs":                 3000,
    "resolution_memory_read":    1000,
    "investigation_store_read":  1000,
    "pattern_intelligence_read":  500,
    "dependency_graph_read":      500,
    "causal_graph_read":          500,
    "knowledge_graph_read":       500,
    "blast_radius_read":         1000,
    "cloudtrail_auth":           2000,
}


DEFAULT_SWITCHING_OVERHEAD = 200      # per tool-switch between adjacent steps


class CostModel:
    """Deterministic cost calculator.

    Custom maps may override the built-in evidence / tool costs.
    """

    def __init__(
        self,
        evidence_cost: Mapping[str, int] | None = None,
        tool_cost: Mapping[str, int] | None = None,
        switching_overhead: int = DEFAULT_SWITCHING_OVERHEAD,
    ) -> None:
        self._evidence = dict(DEFAULT_EVIDENCE_COST)
        if evidence_cost:
            self._evidence.update({str(k): int(v) for k, v in evidence_cost.items()})
        self._tool = dict(DEFAULT_TOOL_COST)
        if tool_cost:
            self._tool.update({str(k): int(v) for k, v in tool_cost.items()})
        self._switch = int(switching_overhead)

    # ------------------------------------------------------------------
    # Per-step primitives
    # ------------------------------------------------------------------

    def evidence_cost(self, keys: Iterable[str]) -> int:
        total = 0
        for k in (keys or ()):
            total += self._evidence.get(str(k), 500)   # 500 unit default
        return total

    def tool_cost(self, skills: Iterable[str]) -> int:
        total = 0
        for s in (skills or ()):
            total += self._tool.get(str(s), 500)
        return total

    def switching_cost(self, prev_skill: str, next_skill: str) -> int:
        """Cost incurred when switching from one tool to another."""
        if not prev_skill or not next_skill:
            return 0
        return self._switch if prev_skill != next_skill else 0

    def execution_cost(
        self,
        evidence_keys: Iterable[str] = (),
        skills: Iterable[str] = (),
        prev_skill: str = "",
    ) -> int:
        """Sum of evidence + tool + switching cost for one step."""
        skills_tuple = tuple(str(s) for s in (skills or ()))
        first = skills_tuple[0] if skills_tuple else ""
        return (
            self.evidence_cost(evidence_keys)
            + self.tool_cost(skills_tuple)
            + self.switching_cost(prev_skill, first)
        )

    # ------------------------------------------------------------------
    # Value model
    # ------------------------------------------------------------------

    def overall_value(
        self,
        expected_information_gain: float,
        expected_confidence_gain: int,
        historical_success_rate: float,
        execution_cost: int,
    ) -> float:
        """Deterministic ExpectedValue score in `[0, ~1.0]`.

        Formula: (info_gain × conf_gain/100 × success_rate) − cost_penalty
        where cost_penalty = min(0.5, execution_cost / 20_000).
        """
        gain = (
            float(expected_information_gain)
            * (int(expected_confidence_gain) / 100.0)
            * float(historical_success_rate)
        )
        penalty = min(0.5, float(execution_cost) / 20_000.0)
        return round(max(0.0, gain - penalty), 4)


__all__ = [
    "DEFAULT_EVIDENCE_COST",
    "DEFAULT_TOOL_COST",
    "DEFAULT_SWITCHING_OVERHEAD",
    "CostModel",
]
