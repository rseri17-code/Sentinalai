"""Online evaluation: score every investigation without ground truth labels.

Unlike ground_truth_eval.py (which requires external labels and only runs
when labeled data is available), online_evaluator scores EVERY investigation
using intrinsic quality signals computable from the result + evidence alone.

Five dimensions:
  1. evidence_volume       — Were enough data sources gathered?
  2. evidence_coherence    — Do multiple source types agree on the same anomaly?
  3. confidence_calibration — Is stated confidence proportional to evidence?
  4. root_cause_specificity — Is the RCA actionable (specific enough to act on)?
  5. hypothesis_diversity  — Were alternative explanations considered?

Overall = weighted average (weights tuned for SRE incident response).

Online scores are used by:
  - strategy_evolver.py: identifies which playbook steps produce high-quality evidence
  - learning_loop.py: supplements calibrator when ground truth is unavailable
  - experience_store.py: gates which experiences are worth storing (score >= 0.6)
  - RCA report: surfaces quality metadata for the consumer

Configuration:
  ONLINE_EVAL_ENABLED  — Enable/disable (default: true)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("sentinalai.online_evaluator")

ONLINE_EVAL_ENABLED = os.environ.get("ONLINE_EVAL_ENABLED", "true").lower() in ("1", "true", "yes")

# Evidence keys that carry meaningful signal per data category
_SOURCE_GROUPS: dict[str, tuple[str, ...]] = {
    "logs":    ("search_logs", "get_error_logs", "search_error_logs",
                "search_timeout_logs", "search_oom_logs", "search_latency_logs",
                "search_spike_logs", "search_saturation_logs", "search_network_logs",
                "search_cascading_logs", "search_missing_logs", "search_flapping_logs",
                "search_silent_logs"),
    "metrics": ("query_metrics", "query_response_time", "query_error_rate",
                "query_memory_metrics", "query_cpu_metrics", "query_saturation_metrics",
                "query_network_metrics", "query_process_metrics", "query_flapping_metrics"),
    "signals": ("get_golden_signals", "check_golden_signals",
                "get_apm_signals", "check_apm_signals",
                "get_apm_dependencies", "get_apm_traces"),
    "events":  ("get_k8s_events", "get_events", "get_network_events",
                "get_cascading_events", "get_process_events"),
    "changes": ("get_change_data", "get_recent_deployments", "get_config_changes"),
}

# Minimum number of sources for a "well-evidenced" investigation
_MIN_SOURCES_GOOD = 3


@dataclass
class OnlineScore:
    """Per-investigation online quality score."""
    overall: float                                    # 0.0–1.0
    dimensions: dict[str, float] = field(default_factory=dict)
    source_count: int = 0
    sources_found: list[str] = field(default_factory=list)


def evaluate(
    result: dict,
    evidence: dict,
    budget_calls_made: int = 0,
    hypothesis_count: int = 0,
) -> OnlineScore:
    """Compute the online quality score for a completed investigation.

    Args:
        result: The RCA result dict from _analyze_evidence()
        evidence: The raw evidence dict gathered during investigation
        budget_calls_made: Number of tool calls consumed
        hypothesis_count: How many hypotheses were generated/scored

    Returns:
        OnlineScore with overall (0.0–1.0) and per-dimension breakdown
    """
    if not ONLINE_EVAL_ENABLED:
        return OnlineScore(overall=1.0)

    root_cause  = result.get("root_cause", "")
    confidence  = result.get("confidence", 0)
    timeline    = result.get("evidence_timeline", [])

    # Categorise gathered evidence
    sources_found, source_count = _categorise_sources(evidence)

    # --- Dimension 1: Evidence volume ---
    volume = _score_volume(source_count)

    # --- Dimension 2: Evidence coherence ---
    coherence = _score_coherence(evidence, sources_found)

    # --- Dimension 3: Confidence calibration ---
    calibration = _score_calibration(confidence, source_count, timeline)

    # --- Dimension 4: Root cause specificity ---
    specificity = _score_specificity(root_cause)

    # --- Dimension 5: Hypothesis diversity ---
    diversity = _score_diversity(hypothesis_count)

    weights = {
        "volume":      0.20,
        "coherence":   0.25,
        "calibration": 0.20,
        "specificity": 0.25,
        "diversity":   0.10,
    }
    overall = (
        weights["volume"]      * volume +
        weights["coherence"]   * coherence +
        weights["calibration"] * calibration +
        weights["specificity"] * specificity +
        weights["diversity"]   * diversity
    )
    overall = round(min(1.0, max(0.0, overall)), 3)

    dims = {
        "volume":      round(volume, 3),
        "coherence":   round(coherence, 3),
        "calibration": round(calibration, 3),
        "specificity": round(specificity, 3),
        "diversity":   round(diversity, 3),
    }

    logger.info(
        "Online eval: overall=%.3f vol=%.2f coh=%.2f cal=%.2f spec=%.2f div=%.2f "
        "sources=%d hyps=%d",
        overall, volume, coherence, calibration, specificity, diversity,
        source_count, hypothesis_count,
    )
    return OnlineScore(
        overall=overall,
        dimensions=dims,
        source_count=source_count,
        sources_found=sources_found,
    )


# ---------------------------------------------------------------------------
# Dimension scorers
# ---------------------------------------------------------------------------

def _categorise_sources(evidence: dict) -> tuple[list[str], int]:
    """Map evidence keys to high-level source categories."""
    found: list[str] = []
    for ev_key in evidence:
        if ev_key.startswith("_"):
            continue
        for category, markers in _SOURCE_GROUPS.items():
            if any(m in ev_key for m in markers):
                if category not in found:
                    found.append(category)
                break
    return found, len(found)


def _score_volume(source_count: int) -> float:
    """Score how many distinct data-source categories contributed evidence."""
    if source_count == 0:
        return 0.0
    if source_count == 1:
        return 0.35
    if source_count == 2:
        return 0.60
    if source_count == 3:
        return 0.80
    return min(1.0, 0.80 + (source_count - 3) * 0.05)


def _score_coherence(evidence: dict, sources_found: list[str]) -> float:
    """Do multiple source types show anomalies consistent with the same root cause?

    Coherence proxy: evidence entries that contain non-empty / non-None values.
    Multiple populated sources that all show anomalies → high coherence.
    """
    if not sources_found:
        return 0.0

    populated = 0
    for ev_key, ev_val in evidence.items():
        if ev_key.startswith("_"):
            continue
        if ev_val is None:
            continue
        if isinstance(ev_val, dict) and not ev_val:
            continue
        if isinstance(ev_val, list) and not ev_val:
            continue
        if isinstance(ev_val, str) and not ev_val.strip():
            continue
        populated += 1

    if populated == 0:
        return 0.1
    if len(sources_found) < 2:
        return 0.4  # single source → cannot judge coherence

    # Ratio of populated to total evidence keys (excluding internals)
    total_keys = sum(1 for k in evidence if not k.startswith("_"))
    if total_keys == 0:
        return 0.1
    ratio = populated / total_keys
    # Bonus for multi-source agreement
    multi_source_bonus = min(0.2, (len(sources_found) - 1) * 0.08)
    return min(1.0, 0.5 * ratio + 0.3 + multi_source_bonus)


def _score_calibration(confidence: int, source_count: int, timeline: list) -> float:
    """Is stated confidence proportional to evidence breadth?

    Rule of thumb: each evidence source justifies ~15 confidence points.
    Timeline entries also contribute (+5 per entry, max +20).
    """
    evidence_capacity = source_count * 15 + min(4, len(timeline)) * 5
    evidence_capacity = max(10, min(95, evidence_capacity))

    gap = abs(confidence - evidence_capacity)
    if gap <= 10:
        return 1.0
    if gap <= 20:
        return 0.8
    if gap <= 35:
        return 0.6
    # Severely miscalibrated
    return max(0.1, 1.0 - gap / 100)


def _score_specificity(root_cause: str) -> float:
    """Is the root cause specific and actionable?"""
    if not root_cause:
        return 0.0
    if root_cause.startswith("INSUFFICIENT EVIDENCE"):
        return 0.05
    if root_cause.startswith("LOW CONFIDENCE"):
        return 0.30
    if root_cause == "META_QUERY_NOT_INCIDENT":
        return 1.0

    # Markers of specificity
    specific_terms = [
        "exhaustion", "timeout", "OOMKilled", "connection pool", "memory leak",
        "cpu throttl", "disk full", "network packet", "certificate expir",
        "DNS resolution", "queue depth", "deadlock", "thread starvation",
        "configuration", "deploy", "rollback", "version", "replica",
        "saturation", "rate limit",
    ]
    hits = sum(1 for t in specific_terms if t.lower() in root_cause.lower())
    length_bonus = min(0.3, len(root_cause) / 200)
    return round(min(1.0, 0.35 + hits * 0.15 + length_bonus), 3)


def _score_diversity(hypothesis_count: int) -> float:
    """Were enough competing hypotheses generated and evaluated?"""
    if hypothesis_count == 0:
        return 0.1
    if hypothesis_count == 1:
        return 0.35
    if hypothesis_count == 2:
        return 0.60
    if hypothesis_count == 3:
        return 0.80
    return min(1.0, 0.80 + (hypothesis_count - 3) * 0.05)


# ---------------------------------------------------------------------------
# Convenience: emit online score into result dict
# ---------------------------------------------------------------------------

def annotate_result(result: dict, online_score: OnlineScore) -> None:
    """Inject online score into the result dict for downstream consumers."""
    result["online_quality_score"] = online_score.overall
    result["_online_eval"] = {
        "dimensions": online_score.dimensions,
        "source_count": online_score.source_count,
        "sources_found": online_score.sources_found,
    }
