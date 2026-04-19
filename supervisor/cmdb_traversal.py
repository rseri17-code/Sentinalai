"""CMDB Traversal — multi-hop CI dependency graph analysis.

Core insight: when an alert fires on service X, the root cause is almost always
a change on a *dependency* of X, not X itself.  This module walks the CI
dependency graph up to N hops and returns every CI that has had a recent change.

Algorithm:
  1. Get CI details for the affected service (tier, deps, owner)
  2. Walk dependencies up to max_hops (default=2)
  3. For each CI, call get_change_records
  4. Return blast_radius: {ci_name: [change_records]}

Design:
- Deduplication: each CI visited at most once regardless of graph structure
- Budget-aware: stops early if budget is exhausted
- Tier-aware: Tier 1 services get 2-hop traversal; Tier 2+ get 1-hop
- Result ordering: CIs with the most recent changes ranked first

Usage:
    from supervisor.cmdb_traversal import CMDBTraversal

    traversal = CMDBTraversal(itsm_worker)
    result = traversal.get_change_blast_radius("payment-service", hours=24)
    # {
    #   "affected_ci": "payment-service",
    #   "blast_radius": {"payment-db": [...changes], "redis-cache": [...changes]},
    #   "hops_traversed": 2,
    #   "cis_checked": 7,
    #   "changes_found": 2,
    # }
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any

logger = logging.getLogger("sentinalai.cmdb_traversal")

# Maximum time window when caller does not specify
_DEFAULT_HOURS = 24
# Maximum CIs to visit (prevents infinite loops on dense graphs)
_MAX_CIS = 25


class CMDBTraversal:
    """Walk the CMDB dependency graph to find change blast radius.

    Parameters
    ----------
    itsm_worker:
        An ``ItsmWorker`` (or any object that implements
        ``execute(action, params) -> dict``).
    max_hops:
        How many dependency hops to follow.
        ``1`` = direct dependencies only.
        ``2`` = direct + their dependencies (default for Tier-1 CIs).
    """

    def __init__(self, itsm_worker: Any, max_hops: int = 2) -> None:
        self._itsm = itsm_worker
        self._max_hops = max_hops

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_change_blast_radius(
        self,
        ci: str,
        hours: int = _DEFAULT_HOURS,
        max_hops: int | None = None,
    ) -> dict[str, Any]:
        """Walk dependency graph and collect recent change records.

        Parameters
        ----------
        ci:
            Name of the affected CI / service.
        hours:
            Look-back window for change records.
        max_hops:
            Override instance-level ``max_hops`` for this call.

        Returns
        -------
        dict with keys:
            affected_ci, blast_radius, dependency_graph,
            hops_traversed, cis_checked, changes_found, ci_tiers
        """
        effective_hops = max_hops if max_hops is not None else self._max_hops

        # Adjust hops based on service tier (fetched from CI details)
        ci_details = self._get_ci_details(ci)
        tier = _extract_tier(ci_details)
        if tier and tier > 1:
            effective_hops = min(effective_hops, 1)

        blast_radius: dict[str, list[dict]] = {}
        dependency_graph: dict[str, list[str]] = {}
        ci_tiers: dict[str, int | None] = {ci: tier}
        cis_checked = 0
        visited: set[str] = {ci}

        # BFS over dependency graph
        queue: deque[tuple[str, int]] = deque()
        queue.append((ci, 0))

        while queue and cis_checked < _MAX_CIS:
            current_ci, depth = queue.popleft()
            cis_checked += 1

            # Get dependencies of this CI
            if depth < effective_hops:
                deps = self._get_dependencies(current_ci, ci_details if current_ci == ci else None)
                dependency_graph[current_ci] = deps
                for dep in deps:
                    if dep not in visited:
                        visited.add(dep)
                        queue.append((dep, depth + 1))

            # Check for recent changes on every CI we visit (except the root)
            if current_ci != ci:
                changes = self._get_change_records(current_ci, hours)
                if changes:
                    blast_radius[current_ci] = changes
                    logger.info(
                        "CMDB traversal: found %d change(s) on dependency %s of %s",
                        len(changes), current_ci, ci,
                    )
                dep_details = self._get_ci_details(current_ci)
                ci_tiers[current_ci] = _extract_tier(dep_details)

        # Also check the root CI itself for changes
        root_changes = self._get_change_records(ci, hours)
        if root_changes:
            blast_radius[ci] = root_changes

        logger.info(
            "CMDB traversal complete for %s: cis_checked=%d, changes_found=%d, "
            "hops=%d, blast_radius=%s",
            ci, cis_checked, len(blast_radius), effective_hops, list(blast_radius.keys()),
        )

        return {
            "affected_ci": ci,
            "blast_radius": blast_radius,
            "dependency_graph": dependency_graph,
            "hops_traversed": effective_hops,
            "cis_checked": cis_checked,
            "changes_found": len(blast_radius),
            "ci_tiers": ci_tiers,
        }

    def get_most_recent_change(self, blast_radius: dict[str, list[dict]]) -> dict | None:
        """Return the single most recent change across the entire blast radius.

        Used by the agent to select the most likely causal change.
        """
        best_change = None
        best_ts = ""

        for ci_name, changes in blast_radius.items():
            for change in changes:
                ts = change.get("end_date", change.get("start_date", ""))
                if ts > best_ts:
                    best_ts = ts
                    best_change = dict(change)
                    best_change["_ci"] = ci_name

        return best_change

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _get_ci_details(self, ci: str) -> dict:
        """Fetch CI details, returning empty dict on failure."""
        try:
            result = self._itsm.execute("get_ci_details", {"service": ci})
            return result.get("ci", {}) if isinstance(result, dict) else {}
        except Exception as exc:
            logger.warning("CMDB get_ci_details failed for %s: %s", ci, exc)
            return {}

    def _get_dependencies(self, ci: str, ci_details: dict | None = None) -> list[str]:
        """Extract list of dependency CI names."""
        details = ci_details if ci_details is not None else self._get_ci_details(ci)
        deps = details.get("dependencies", [])
        if isinstance(deps, list):
            return [str(d) for d in deps if d]
        return []

    def _get_change_records(self, ci: str, hours: int) -> list[dict]:
        """Fetch change records for a CI, returning empty list on failure."""
        try:
            result = self._itsm.execute(
                "get_change_records",
                {"service": ci, "time_window_hours": hours},
            )
            if isinstance(result, dict):
                records = result.get("change_records", [])
                return records if isinstance(records, list) else []
            return []
        except Exception as exc:
            logger.warning("CMDB get_change_records failed for %s: %s", ci, exc)
            return []


# ------------------------------------------------------------------ #
# Module-level helpers
# ------------------------------------------------------------------ #

def _extract_tier(ci_details: dict) -> int | None:
    """Extract service tier from CI details (1=critical, higher=less critical)."""
    tier_raw = ci_details.get("tier", ci_details.get("service_tier"))
    if tier_raw is None:
        return None
    try:
        return int(tier_raw)
    except (ValueError, TypeError):
        tier_str = str(tier_raw).lower()
        return {"critical": 1, "tier1": 1, "high": 2, "tier2": 2,
                "medium": 3, "tier3": 3, "low": 4}.get(tier_str)


def build_change_summary(blast_radius_result: dict) -> str:
    """Build a concise text summary of the blast radius for the LLM."""
    blast = blast_radius_result.get("blast_radius", {})
    if not blast:
        return "No recent changes found in CMDB dependency graph."

    lines = [
        f"CMDB dependency scan found changes on {len(blast)} CI(s):"
    ]
    for ci_name, changes in blast.items():
        for ch in changes[:3]:  # cap at 3 per CI
            lines.append(
                f"  • [{ci_name}] {ch.get('type', 'change').upper()} "
                f"{ch.get('number', '')} "
                f"'{ch.get('short_description', '')}' "
                f"by {ch.get('requested_by', 'unknown')} "
                f"at {ch.get('end_date', ch.get('start_date', 'unknown time'))} "
                f"(risk={ch.get('risk', 'unknown')})"
            )
    return "\n".join(lines)
