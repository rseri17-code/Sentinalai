"""Source confidence tiers and staleness decay.

Every evidence source has two quality dimensions:
  1. Base confidence — how reliable is this source type in principle?
  2. Freshness — how much does age degrade reliability?

Tier definitions reflect operational reality:
  LIVE_METRICS  — golden signals, APM traces: sampled seconds ago, ground truth
  LOGS          — error logs: high signal, bounded by search window age
  EVENTS        — k8s events, change events: time-indexed, drift with age
  CHANGES       — deployment records: highly time-sensitive (rollback window)
  INSTITUTIONAL — experience store, knowledge graph: depends on similarity quality
  RUNBOOK       — wiki notes, runbooks: useful but stale by definition

Staleness model: exponential decay
  confidence *= max(MIN_CONFIDENCE, exp(-age_hours / half_life_hours * ln(2)))
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

_MIN_CONFIDENCE = float(os.getenv("SOURCE_MIN_CONFIDENCE", "0.10"))

# (base_confidence, half_life_hours)
_TIER: dict[str, tuple[float, float]] = {
    # Live signals — near-zero staleness window
    "golden_signals":           (1.00, 24.0),
    "check_golden_signals":     (1.00, 24.0),
    "get_golden_signals":       (1.00, 24.0),
    "query_metrics":            (0.95, 24.0),
    "query_response_time":      (0.95, 24.0),
    "query_error_rate":         (0.95, 24.0),
    "get_apm_signals":          (0.90, 48.0),
    "get_apm_traces":           (0.90, 48.0),
    "check_apm_signals":        (0.90, 48.0),
    # Logs — reliable but window-bounded
    "search_logs":              (0.85, 72.0),
    "search_error_logs":        (0.85, 72.0),
    "get_error_logs":           (0.85, 72.0),
    "search_latency_logs":      (0.82, 72.0),
    "search_timeout_logs":      (0.82, 72.0),
    "search_oom_logs":          (0.82, 72.0),
    "search_spike_logs":        (0.80, 72.0),
    # Events
    "get_k8s_events":           (0.80, 48.0),
    "get_events":               (0.80, 48.0),
    "get_cascading_events":     (0.78, 48.0),
    # Changes — very time-sensitive
    "get_change_data":          (0.78, 24.0),
    "get_recent_deployments":   (0.78, 24.0),
    "get_config_changes":       (0.75, 24.0),
    "check_itsm_changes":       (0.75, 36.0),
    # CMDB / topology
    "cmdb_blast_radius":        (0.72, 168.0),
    "get_apm_dependencies":     (0.72, 168.0),
    # Institutional memory
    "experience_store":         (0.70, 720.0),
    "pattern_match":            (0.68, 720.0),
    # Wiki / runbooks
    "wiki_note":                (0.60, 2160.0),   # 90d half-life
    "runbook":                  (0.58, 2160.0),
}

# Fallback for unknown source types
_DEFAULT_TIER: tuple[float, float] = (0.65, 168.0)


@dataclass
class SourceScore:
    source_type: str
    base_confidence: float       # 0–1, from tier table
    age_hours: float             # document age in hours
    freshness_factor: float      # staleness decay (0–1)
    final_confidence: float      # base * freshness, floor at MIN_CONFIDENCE

    def is_stale(self, stale_threshold: float = 0.50) -> bool:
        """True if freshness_factor has decayed below stale_threshold."""
        return self.freshness_factor < stale_threshold


def score_source(
    source_type: str,
    collected_at: str | None = None,
    age_hours: float | None = None,
) -> SourceScore:
    """Score a single evidence source by type and age.

    Args:
        source_type:   Key from the evidence dict or tier table.
        collected_at:  ISO-8601 timestamp when evidence was collected.
        age_hours:     Explicit override for document age.

    Returns:
        SourceScore with all intermediate values.
    """
    base, half_life = _TIER.get(source_type, _DEFAULT_TIER)

    if age_hours is None:
        age_hours = _compute_age_hours(collected_at)

    freshness = _decay(age_hours, half_life)
    final = max(_MIN_CONFIDENCE, base * freshness)

    return SourceScore(
        source_type=source_type,
        base_confidence=base,
        age_hours=age_hours,
        freshness_factor=freshness,
        final_confidence=round(final, 4),
    )


def score_evidence_dict(
    evidence: dict[str, Any],
    collected_at: str | None = None,
) -> dict[str, SourceScore]:
    """Score all keys in an evidence dict.

    Returns {source_key: SourceScore} for every non-internal key.
    """
    return {
        key: score_source(key, collected_at=collected_at)
        for key in evidence
        if not key.startswith("_") and evidence[key]
    }


def mean_evidence_confidence(
    evidence: dict[str, Any],
    collected_at: str | None = None,
) -> float:
    """Mean final_confidence across all non-empty evidence sources."""
    scores = score_evidence_dict(evidence, collected_at)
    if not scores:
        return 0.0
    return sum(s.final_confidence for s in scores.values()) / len(scores)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decay(age_hours: float, half_life_hours: float) -> float:
    """Exponential decay: f(t) = 2^(-t/half_life)."""
    if age_hours <= 0:
        return 1.0
    exponent = -age_hours / max(0.1, half_life_hours)
    return max(0.0, math.pow(2, exponent))


def _compute_age_hours(collected_at: str | None) -> float:
    """Parse ISO-8601 timestamp and return age in hours. 0 if unparseable."""
    if not collected_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        return max(0.0, delta.total_seconds() / 3600)
    except (ValueError, TypeError):
        return 0.0
