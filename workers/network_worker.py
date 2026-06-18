"""ThousandEyes network evidence worker.

Gated by ENABLE_THOUSANDEYES_RCA env var (default: false).  When disabled,
all actions return {} so existing RCA flows are completely unaffected.

When enabled, fetches ThousandEyes alerts and test results, normalizes them
into NetworkEvidence instances, runs deterministic correlation rules, and
returns additive evidence keys that the supervisor merges into the evidence dict.

Added keys (all additive — never mutate existing keys):
  network_evidence    list[dict]  — normalized per-agent evidence
  network_correlation list[dict]  — matched correlation rules
  network_summary     str         — human-readable summary for LLM synthesis
"""

from __future__ import annotations

import logging
import os
from typing import Any

from workers.base_worker import BaseWorker

logger = logging.getLogger(__name__)

_ENABLED_VALUES = ("1", "true", "yes")


def _te_enabled() -> bool:
    return os.environ.get("ENABLE_THOUSANDEYES_RCA", "false").lower() in _ENABLED_VALUES


class ThousandEyesWorker(BaseWorker):
    """Worker that calls the ThousandEyes MCP server for network RCA evidence."""

    worker_name = "network_worker"

    def __init__(self) -> None:
        super().__init__()
        self.register("get_network_evidence", self._get_network_evidence)
        self.register("get_network_alerts", self._get_network_alerts)
        self.register("check_network_health", self._check_network_health)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _get_network_evidence(self, params: dict) -> dict:
        """Fetch alerts + test results, normalize, and run correlation rules.

        Returns {} when ENABLE_THOUSANDEYES_RCA is false (feature flag gate).
        """
        if not _te_enabled():
            return {}

        from integrations.thousandeyes import adapter, normalizer, correlation

        window_start = params.get("window_start")
        window_end = params.get("window_end")
        existing_evidence = params.get("existing_evidence", {})

        evidence_list: list[normalizer.NetworkEvidence] = []

        # P0: alerts (fast, pre-aggregated by ThousandEyes)
        raw_alerts = adapter.list_alerts(window_start, window_end)
        for alert in raw_alerts.get("alerts", []):
            try:
                ev = normalizer.normalize_alert(alert)
                evidence_list.append(ev)
            except Exception as exc:
                logger.debug("Alert normalization skipped: %s", exc)

        # P0: test results for tests that have active alerts
        active_test_ids = {
            str(a.get("testId"))
            for a in raw_alerts.get("alerts", [])
            if a.get("active") and a.get("testId")
        }
        for test_id in active_test_ids:
            raw_results = adapter.get_test_results(test_id, window_start)
            test_type = raw_results.get("type", "")
            for result in raw_results.get("results", []):
                try:
                    ev = normalizer.normalize_test_result(result, test_type)
                    evidence_list.append(ev)
                except Exception as exc:
                    logger.debug("Test result normalization skipped: %s", exc)

        # Aggregate scope across all results
        for ev in evidence_list:
            ev.affected_scope = normalizer.aggregate_scope(evidence_list)

        # Run correlation rules
        corr_results = correlation.run_all_rules(evidence_list, existing_evidence)

        network_summary = _build_summary(evidence_list, corr_results)

        return {
            "network_evidence": [ev.to_dict() for ev in evidence_list],
            "network_correlation": [
                {
                    "rule_id": r.rule_id,
                    "rule_name": r.rule_name,
                    "confidence_delta": r.confidence_delta,
                    "owner": r.owner,
                    "rca_summary": r.rca_summary,
                }
                for r in corr_results
            ],
            "network_summary": network_summary,
        }

    def _get_network_alerts(self, params: dict) -> dict:
        """Fetch only ThousandEyes alerts (faster; no test-result enrichment)."""
        if not _te_enabled():
            return {}

        from integrations.thousandeyes import adapter, normalizer

        raw_alerts = adapter.list_alerts(
            params.get("window_start"),
            params.get("window_end"),
        )
        alerts = raw_alerts.get("alerts", [])
        evidence = [normalizer.normalize_alert(a) for a in alerts]

        return {
            "network_evidence": [ev.to_dict() for ev in evidence],
            "network_summary": (
                f"ThousandEyes: {len(alerts)} active alerts" if alerts
                else "ThousandEyes: no active alerts in window"
            ),
        }

    def _check_network_health(self, params: dict) -> dict:
        """Quick health check — returns whether ThousandEyes is reachable + enabled."""
        enabled = _te_enabled()
        if not enabled:
            return {"enabled": False, "reason": "ENABLE_THOUSANDEYES_RCA=false"}

        from integrations.thousandeyes import adapter

        raw = adapter.list_tests()
        if raw.get("error"):
            return {"enabled": True, "healthy": False, "error": raw["error"]}

        test_count = len(raw.get("tests", []))
        return {"enabled": True, "healthy": True, "test_count": test_count}


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------

def _build_summary(
    evidence_list: list[Any],
    corr_results: list[Any],
) -> str:
    if not evidence_list:
        return "ThousandEyes: no evidence collected (no active alerts or tests)"

    owners = [r.owner for r in corr_results if r.matched]
    primary_owner = owners[0] if owners else "unknown"

    degraded_count = sum(
        1 for ev in evidence_list
        if ev.availability is not None and ev.availability < 80
    )
    total = len(evidence_list)

    summary_parts = [
        f"ThousandEyes: {degraded_count}/{total} agent measurements show degradation."
    ]

    for r in corr_results[:2]:   # top 2 matched rules
        summary_parts.append(r.rca_summary)

    if primary_owner != "unknown":
        summary_parts.append(f"Likely responsible party: {primary_owner.upper()}.")

    return " ".join(summary_parts)
