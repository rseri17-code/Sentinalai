"""Evidence-weighted confidence calculator (W3).

Extracted verbatim from ``supervisor.agent`` in Phase 7. The function
remains re-exported from ``supervisor.agent`` for backward compatibility
with every existing import — see the ``compute_confidence`` reassignment at
the bottom of ``supervisor/agent.py``.
"""
from __future__ import annotations


# Incident types where absence of signals/metrics is the expected finding,
# not a gap in investigation quality.
_ABSENCE_IS_SYMPTOM = frozenset({"silent_failure", "missing_data"})


def compute_confidence(
    base: float,
    logs: list[dict],
    signals: dict,
    metrics: dict,
    events: list[dict],
    changes: list[dict],
    corroborating_sources: int = 0,
    incident_type: str = "",
) -> int:
    """Compute evidence-weighted confidence.

    base:  the analyzer's starting score (e.g. 80 for a strong match)
    Then:
      +2 per corroborating evidence source (logs, signals, metrics, events, changes)
      +1 per log entry (max +5)
      +2 if golden signals present with anomaly detected
      +1 if metrics have pattern field
      -5 if signals absent AND the incident type is not one where absence is the symptom
      -3 if metrics absent AND the incident type is not one where absence is the symptom
    Bounded to [0, 100].

    ``incident_type`` guards the missing-source penalties: for ``silent_failure``
    and ``missing_data`` incidents the absence of golden signals or metrics is
    the defining characteristic of the incident, not a gap in investigation quality.
    Penalising those types would systematically under-score correct investigations.
    """
    score = base

    # Corroboration bonus: count how many sources have data
    source_count = 0
    if logs:
        source_count += 1
        score += min(len(logs), 5)  # +1 per log, max +5
    if signals and signals.get("golden_signals"):
        source_count += 1
        if signals.get("anomaly_detected"):
            score += 2
    if metrics and metrics.get("metrics"):
        source_count += 1
        if metrics.get("pattern"):
            score += 1
    if events:
        source_count += 1
    if changes:
        source_count += 1

    # Cross-signal bonus
    score += source_count * 2

    # Missing-source penalty (only for incident types where presence is expected)
    if incident_type not in _ABSENCE_IS_SYMPTOM:
        if not signals or not signals.get("golden_signals"):
            score -= 5
        if not metrics or not metrics.get("metrics"):
            score -= 3

    # Explicit corroboration from caller
    score += corroborating_sources * 2

    return max(0, min(100, int(round(score))))


__all__ = ["compute_confidence", "_ABSENCE_IS_SYMPTOM"]
