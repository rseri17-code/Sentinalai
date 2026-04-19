"""Self-critique: the agent evaluates its own RCA output before returning it.

After _analyze_evidence() produces an initial result, self_critique:
  1. Scores the result across 5 quality dimensions (heuristic, always runs)
  2. Identifies specific evidence gaps (what is still missing / unchecked)
  3. Returns gap_queries — additional {worker, action, params} steps for targeted
     follow-up evidence gathering
  4. If LLM is available, refines the critique with an LLM pass

The result is fed back into agent._apply_self_critique():
  - If critique_score < CRITIQUE_THRESHOLD and budget.remaining() >= MIN_GAP_BUDGET:
      → run gap_queries, re-analyze with enriched evidence
      → if refined confidence is higher, keep the refined result
  - Always embed the critique metadata in the result dict for transparency

Configuration:
  SELF_CRITIQUE_ENABLED   - Enable/disable (default: true)
  CRITIQUE_THRESHOLD      - Minimum acceptable quality (default: 0.62)
  MIN_GAP_BUDGET          - Minimum remaining calls required to run gap queries (default: 3)
  CRITIQUE_LLM_ENABLED    - Use LLM for critique pass (default: follows LLM_ENABLED)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("sentinalai.self_critique")

SELF_CRITIQUE_ENABLED = os.environ.get("SELF_CRITIQUE_ENABLED", "true").lower() in ("1", "true", "yes")
CRITIQUE_THRESHOLD = float(os.environ.get("CRITIQUE_THRESHOLD", "0.62"))
MIN_GAP_BUDGET = int(os.environ.get("MIN_GAP_BUDGET", "3"))
CRITIQUE_LLM_ENABLED = os.environ.get("CRITIQUE_LLM_ENABLED", "").lower() not in ("0", "false", "no")

# Evidence keys that signal a given worker produced real data
_EVIDENCE_SIGNAL_KEYS = {
    "logs":           ["search_logs", "get_error_logs", "search_error_logs",
                       "search_timeout_logs", "search_oom_logs"],
    "metrics":        ["query_metrics", "query_response_time", "query_error_rate",
                       "query_memory_metrics", "query_cpu_metrics", "query_saturation_metrics",
                       "query_network_metrics"],
    "golden_signals": ["get_golden_signals", "check_golden_signals",
                       "get_apm_signals", "check_apm_signals"],
    "events":         ["get_k8s_events", "get_events", "get_network_events",
                       "get_cascading_events", "get_process_events"],
    "changes":        ["get_change_data", "get_recent_deployments", "get_config_changes"],
}

# Workers that cover each evidence category
_CATEGORY_WORKERS: dict[str, list[dict]] = {
    "logs": [
        {"worker": "log_worker", "action": "get_error_logs",
         "params": {"query": "error OR exception OR fatal", "limit": 50}},
    ],
    "metrics": [
        {"worker": "metrics_worker", "action": "query_metrics",
         "params": {"metric_name": "error_rate", "window_minutes": 30}},
    ],
    "golden_signals": [
        {"worker": "apm_worker", "action": "get_golden_signals", "params": {}},
    ],
    "events": [
        {"worker": "apm_worker", "action": "get_k8s_events", "params": {}},
    ],
    "changes": [
        {"worker": "log_worker", "action": "get_change_data", "params": {}},
    ],
}


@dataclass
class CritiqueResult:
    """Output of the self-critique pass."""
    score: float                               # 0.0–1.0 overall quality
    dimensions: dict[str, float] = field(default_factory=dict)  # per-dimension scores
    gaps: list[str] = field(default_factory=list)               # human-readable gap descriptions
    gap_queries: list[dict] = field(default_factory=list)       # actionable follow-up steps
    llm_critique: str = ""                                       # LLM narrative (if available)
    triggered_refinement: bool = False                           # did we actually run gap queries?


def critique(
    result: dict,
    evidence: dict,
    incident_type: str,
    service: str = "unknown",
    budget_remaining: int = 0,
) -> CritiqueResult:
    """Evaluate the RCA result and identify evidence gaps.

    Args:
        result: The dict returned by _analyze_evidence()
        evidence: The raw evidence dict gathered during the investigation
        incident_type: Classified incident type (timeout, oomkill, etc.)
        service: Affected service name
        budget_remaining: How many tool calls are still available

    Returns:
        CritiqueResult with score, gaps, and optional gap_queries
    """
    if not SELF_CRITIQUE_ENABLED:
        return CritiqueResult(score=1.0)

    root_cause = result.get("root_cause", "")
    confidence = result.get("confidence", 0)
    timeline = result.get("evidence_timeline", [])
    reasoning = result.get("reasoning", "")

    # --- Dimension 1: Root cause specificity (0–1) ---
    specificity = _score_specificity(root_cause, confidence)

    # --- Dimension 2: Evidence coverage (0–1) ---
    coverage, missing_categories = _score_evidence_coverage(evidence)

    # --- Dimension 3: Reasoning coherence (0–1) ---
    coherence = _score_reasoning_coherence(reasoning, root_cause, incident_type)

    # --- Dimension 4: Confidence calibration (0–1) ---
    calibration = _score_confidence_calibration(confidence, coverage, timeline)

    # --- Dimension 5: Timeline completeness (0–1) ---
    timeline_score = _score_timeline(timeline)

    # Weighted overall
    weights = {
        "specificity":   0.25,
        "coverage":      0.25,
        "coherence":     0.20,
        "calibration":   0.15,
        "timeline":      0.15,
    }
    score = (
        weights["specificity"]  * specificity +
        weights["coverage"]     * coverage +
        weights["coherence"]    * coherence +
        weights["calibration"]  * calibration +
        weights["timeline"]     * timeline_score
    )

    dims = {
        "specificity": round(specificity, 3),
        "coverage":    round(coverage, 3),
        "coherence":   round(coherence, 3),
        "calibration": round(calibration, 3),
        "timeline":    round(timeline_score, 3),
    }

    gaps: list[str] = []
    gap_queries: list[dict] = []

    # Only produce gap queries if below threshold and budget allows
    if score < CRITIQUE_THRESHOLD and budget_remaining >= MIN_GAP_BUDGET:
        gaps, gap_queries = _build_gap_queries(
            missing_categories, root_cause, confidence, service, incident_type
        )

    if coverage < 0.5:
        for cat in missing_categories:
            gaps.append(f"No {cat} evidence gathered")

    if confidence < 40 and not gaps:
        gaps.append("Low confidence with no clear evidence gap — consider broadening log search")

    if root_cause.startswith("INSUFFICIENT") or root_cause.startswith("LOW CONFIDENCE"):
        gaps.append("Root cause determination was inconclusive")

    critique_result = CritiqueResult(
        score=round(score, 3),
        dimensions=dims,
        gaps=gaps,
        gap_queries=gap_queries,
    )

    # Optional LLM critique pass
    if CRITIQUE_LLM_ENABLED:
        try:
            llm_narrative = _llm_critique(result, evidence, incident_type, service)
            if llm_narrative:
                critique_result.llm_critique = llm_narrative
        except Exception as exc:
            logger.debug("LLM critique pass failed (non-critical): %s", exc)

    logger.info(
        "Self-critique: incident_type=%s score=%.2f coverage=%.2f specificity=%.2f "
        "gaps=%d gap_queries=%d",
        incident_type, score, coverage, specificity, len(gaps), len(gap_queries),
    )
    return critique_result


# ---------------------------------------------------------------------------
# Dimension scorers
# ---------------------------------------------------------------------------

def _score_specificity(root_cause: str, confidence: int) -> float:
    """Is the root cause statement specific enough to act on?"""
    if not root_cause:
        return 0.0
    if root_cause.startswith("INSUFFICIENT EVIDENCE"):
        return 0.1
    if root_cause.startswith("LOW CONFIDENCE"):
        return 0.35
    if root_cause == "META_QUERY_NOT_INCIDENT":
        return 1.0

    # Reward specificity markers: service names, error codes, component names
    specificity_tokens = [
        "exhaustion", "timeout", "OOMKilled", "connection pool", "memory",
        "cpu", "disk", "network", "deploy", "config", "certificate", "DNS",
        "queue", "deadlock", "leak", "saturation", "throttl",
    ]
    token_hits = sum(1 for t in specificity_tokens if t.lower() in root_cause.lower())
    length_score = min(1.0, len(root_cause) / 80)  # longer → more specific (cap at 80 chars)
    confidence_score = confidence / 100.0

    return round(min(1.0, 0.3 * confidence_score + 0.4 * length_score + 0.3 * min(1.0, token_hits / 2)), 3)


def _score_evidence_coverage(evidence: dict) -> tuple[float, list[str]]:
    """What fraction of expected evidence categories were gathered?"""
    found_categories: set[str] = set()
    for ev_key in evidence.keys():
        if ev_key.startswith("_"):  # internal keys
            continue
        for category, signal_keys in _EVIDENCE_SIGNAL_KEYS.items():
            if any(sk in ev_key for sk in signal_keys):
                found_categories.add(category)
                break

    all_cats = set(_EVIDENCE_SIGNAL_KEYS.keys())
    missing = sorted(all_cats - found_categories)
    coverage = len(found_categories) / len(all_cats) if all_cats else 0.0
    return round(coverage, 3), missing


def _score_reasoning_coherence(reasoning: str, root_cause: str, incident_type: str) -> float:
    """Does the reasoning logically connect evidence to root cause?"""
    if not reasoning or len(reasoning) < 20:
        return 0.2

    # Causal language markers
    causal_markers = [
        "because", "caused by", "due to", "resulted in", "led to",
        "indicates", "suggests", "confirms", "corroborates", "consistent with",
        "therefore", "thus", "as a result",
    ]
    causal_hits = sum(1 for m in causal_markers if m in reasoning.lower())

    # Evidence references
    evidence_markers = ["log", "metric", "signal", "event", "deploy", "change", "error"]
    evidence_hits = sum(1 for m in evidence_markers if m in reasoning.lower())

    # Does reasoning mention the incident type or root cause keywords?
    rc_overlap = 1.0 if root_cause.split()[:3] and any(
        w.lower() in reasoning.lower()
        for w in root_cause.split()[:5] if len(w) > 4
    ) else 0.5

    causal_score = min(1.0, causal_hits / 3)
    evidence_score = min(1.0, evidence_hits / 3)
    length_score = min(1.0, len(reasoning) / 200)

    return round((causal_score * 0.35 + evidence_score * 0.35 + rc_overlap * 0.15 + length_score * 0.15), 3)


def _score_confidence_calibration(confidence: int, evidence_coverage: float, timeline: list) -> float:
    """Is the stated confidence proportional to gathered evidence?"""
    # Expected confidence range given evidence coverage
    expected_low  = int(evidence_coverage * 45)
    expected_high = int(evidence_coverage * 90) + 15

    if expected_low <= confidence <= expected_high:
        return 1.0
    # Overconfident (high confidence with little evidence)
    if confidence > expected_high:
        overshot = (confidence - expected_high) / 100
        return max(0.0, 1.0 - overshot * 1.5)
    # Underconfident (low confidence despite good evidence) — less penalized
    if confidence < expected_low:
        undershot = (expected_low - confidence) / 100
        return max(0.3, 1.0 - undershot)
    return 0.5


def _score_timeline(timeline: list) -> float:
    """Is the evidence timeline populated and meaningful?"""
    if not timeline:
        return 0.1
    if len(timeline) < 2:
        return 0.4
    if len(timeline) < 4:
        return 0.7
    return min(1.0, 0.7 + len(timeline) * 0.03)


# ---------------------------------------------------------------------------
# Gap query builder
# ---------------------------------------------------------------------------

def _build_gap_queries(
    missing_categories: list[str],
    root_cause: str,
    confidence: int,
    service: str,
    incident_type: str,
) -> tuple[list[str], list[dict]]:
    """Build targeted follow-up queries for missing evidence categories."""
    gaps: list[str] = []
    gap_queries: list[dict] = []

    for cat in missing_categories[:3]:  # max 3 gap fills per critique
        if cat in _CATEGORY_WORKERS:
            step = dict(_CATEGORY_WORKERS[cat][0])  # copy base step
            # Inject service context into params
            if "params" in step:
                step["params"] = dict(step["params"])
                step["params"]["service"] = service
            gaps.append(f"Missing {cat} evidence — querying {step['worker']}.{step['action']}")
            gap_queries.append(step)

    # If confidence is very low without coverage issues, suggest broadening logs
    if confidence < 35 and not gap_queries:
        gap_queries.append({
            "worker": "log_worker",
            "action": "search_logs",
            "params": {"query": f"{incident_type} {service} error", "limit": 100},
        })
        gaps.append(f"Very low confidence — broadening log search for {incident_type}")

    return gaps, gap_queries


# ---------------------------------------------------------------------------
# Optional LLM critique
# ---------------------------------------------------------------------------

def _llm_critique(result: dict, evidence: dict, incident_type: str, service: str) -> str:
    """Use LLM to produce a narrative critique of the RCA quality."""
    try:
        from supervisor.llm import converse, is_enabled as _llm_enabled
        if not _llm_enabled():
            return ""

        evidence_summary = ", ".join(
            k for k in evidence.keys() if not k.startswith("_")
        )
        root_cause = result.get("root_cause", "")
        confidence = result.get("confidence", 0)
        reasoning = result.get("reasoning", "")[:500]

        system = (
            "You are an expert SRE evaluating an automated incident RCA output. "
            "Be concise. Identify gaps, logical flaws, or missing evidence. "
            "Reply with 2-3 sentences max. Focus on actionable gaps only."
        )
        user = (
            f"Incident type: {incident_type}, Service: {service}\n"
            f"Root cause: {root_cause}\n"
            f"Confidence: {confidence}%\n"
            f"Evidence gathered: {evidence_summary}\n"
            f"Reasoning (excerpt): {reasoning}\n\n"
            "What is the most important gap or weakness in this RCA?"
        )

        response = converse(system_prompt=system, user_message=user, max_tokens=150)
        return response.get("content", "") if isinstance(response, dict) else str(response)

    except Exception as exc:
        logger.debug("LLM critique call failed: %s", exc)
        return ""
