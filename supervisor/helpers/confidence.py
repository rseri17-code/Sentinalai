"""Evidence-weighted confidence calculator (W3).

Extracted verbatim from ``supervisor.agent`` in Phase 7. The function
remains re-exported from ``supervisor.agent`` for backward compatibility
with every existing import — see the ``compute_confidence`` reassignment at
the bottom of ``supervisor/agent.py``.
"""
from __future__ import annotations

from supervisor.helpers.placeholders import is_placeholder


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
      +1 if metrics have a pattern that is not a placeholder
      (the strings none / unknown / "", null, and empty lists or dicts
      are not data and do not count)
      -5 if signals absent AND the incident type is not one where absence is the symptom
      -3 if metrics absent AND the incident type is not one where absence is the symptom
    Bounded to [0, 100].

    ``incident_type`` guards the missing-source penalties: for ``silent_failure``
    and ``missing_data`` incidents the absence of golden signals or metrics is
    the defining characteristic of the incident, not a gap in investigation quality.
    Penalising those types would systematically under-score correct investigations.
    """
    # ``corroborating_sources`` is retained for backward compatibility with the
    # single caller (agent.py) but no longer contributes: R2 proved it
    # double-counted sources already credited by ``source_count`` — each
    # ``evidence_ref`` (e.g. "logs:timeout") names a category ``source_count``
    # already counts. Corroboration is now counted exactly ONCE per source.
    base_val, contributions = _score(
        base, logs, signals, metrics, events, changes, incident_type)
    final = base_val + sum(c["delta"] for c in contributions)
    return max(0, min(100, int(round(final))))


def _score(base, logs, signals, metrics, events, changes, incident_type):
    """Single source of truth for confidence math. Returns (base, contributions)
    where each contribution is {kind, source, delta} and appears exactly once."""
    contributions: list[dict] = []

    # Per-source corroboration: counted once per present category (+2 each),
    # plus source-specific detail bonuses.
    real_logs = [item for item in logs if not is_placeholder(item)] if isinstance(logs, list) else []
    if real_logs:
        contributions.append({"kind": "corroboration", "source": "logs",
                              "delta": 2})
        contributions.append({"kind": "detail", "source": "logs",
                              "delta": min(len(real_logs), 5)})   # +1/log, max +5
    golden = signals.get("golden_signals") if isinstance(signals, dict) else None
    if not is_placeholder(golden):
        contributions.append({"kind": "corroboration", "source": "golden_signals",
                              "delta": 2})
        anomaly = signals.get("anomaly_detected") if isinstance(signals, dict) else None
        if anomaly and not is_placeholder(anomaly):
            contributions.append({"kind": "detail", "source": "golden_signals",
                                  "delta": 2})
    series = metrics.get("metrics") if isinstance(metrics, dict) else None
    if not is_placeholder(series):
        contributions.append({"kind": "corroboration", "source": "metrics",
                              "delta": 2})
        pattern = metrics.get("pattern") if isinstance(metrics, dict) else None
        # "none" is a non-empty string. It is still no data.
        if not is_placeholder(pattern):
            contributions.append({"kind": "detail", "source": "metrics",
                                  "delta": 1})
    real_events = [item for item in events if not is_placeholder(item)] if isinstance(events, list) else []
    if real_events:
        contributions.append({"kind": "corroboration", "source": "events",
                              "delta": 2})
    real_changes = [item for item in changes if not is_placeholder(item)] if isinstance(changes, list) else []
    if real_changes:
        contributions.append({"kind": "corroboration", "source": "changes",
                              "delta": 2})

    # Missing-source penalties (once each) where presence is expected.
    if incident_type not in _ABSENCE_IS_SYMPTOM:
        if is_placeholder(golden):
            contributions.append({"kind": "penalty", "source": "golden_signals",
                                  "delta": -5})
        if is_placeholder(series):
            contributions.append({"kind": "penalty", "source": "metrics",
                                  "delta": -3})

    return float(base), contributions


def confidence_provenance(
    base: float,
    logs: list[dict],
    signals: dict,
    metrics: dict,
    events: list[dict],
    changes: list[dict],
    incident_type: str = "",
) -> dict:
    """Deterministic, fully-attributable breakdown of evidence-derived
    confidence: base + one line item per contribution → final. Every
    contribution appears exactly once and sums to ``compute_confidence``."""
    base_val, contributions = _score(
        base, logs, signals, metrics, events, changes, incident_type)
    final = max(0, min(100, int(round(
        base_val + sum(c["delta"] for c in contributions)))))
    return {
        "base": base_val,
        "contributions": contributions,
        "final_confidence": final,
    }


__all__ = ["compute_confidence", "confidence_provenance", "_ABSENCE_IS_SYMPTOM"]
